"""judge/lib/hermeslog.py: read Hermes logs and session state (read-only).

Hermes log lines look like
    2026-10-02 17:00:39,224 INFO [<session>] agent.conversation_loop: API call #1: model=... latency=17.8s
    2026-10-02 22:51:32,770 WARNING agent.message_sanitization: Unrepairable tool_call arguments ...
Timestamps are in the Hermes process' local time (no zone). JUDGE_LOG_TZ overrides the zone used to map
them to UTC ("" = this machine's local zone, "UTC", "+02:00", or an IANA name).  Lines without a timestamp
are continuations of the previous line.

    log_tz(cfg) -> tzinfo
    session_lines(path, session, since_utc, until_utc, tz, include_untagged=True, max_bytes=...) -> list[str]
    split_lines(path, session, since_utc, until_utc, tz, noise=...) -> (tagged, context, dropped_counts)
                                                                        session lines / untagged context /
                                                                        untagged noise dropped per logger
    is_noise(logger, msg, extra_loggers=()) -> bool                       untagged startup/housekeeping noise
    tool_activity(path, session, since_utc, until_utc, tz) -> int        tool_executor lines for the session
    tools_used(path, session, since_utc, until_utc, tz) -> set[str]     tool names in those lines
    last_assistant_message(hermes_home, session) -> str | None            from state.db (opened read-only)
    session_started_at(hermes_home, session) -> datetime | None
"""
from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import List, Mapping, Optional

LINE_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:,\d{1,6})? (?P<level>[A-Z]+) "
                     r"(?:\[(?P<session>[^\]\s]+)\] )?(?P<logger>[^:\s]+): ?(?P<msg>.*)$")
DEFAULT_MAX_BYTES = 32 * 1024 * 1024  # only the tail of very large logs is scanned


def log_tz(cfg: Optional[Mapping[str, str]] = None) -> tzinfo:
    name = (cfg or {}).get("JUDGE_LOG_TZ") or ""
    if not name:
        return datetime.now().astimezone().tzinfo or timezone.utc
    if name.upper() in ("UTC", "Z"):
        return timezone.utc
    m = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", name)
    if m:
        delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
        return timezone(-delta if m.group(1) == "-" else delta)
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return datetime.now().astimezone().tzinfo or timezone.utc


def _parse_ts(ts: str, tz: tzinfo) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz).astimezone(timezone.utc)


def _read_tail(path: Path, max_bytes: int) -> List[str]:
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()  # drop the partial first line
            data = fh.read()
    except OSError:
        return []
    return data.decode("utf-8", errors="replace").splitlines()


def session_lines(path, session: str, since: datetime, until: datetime, tz: tzinfo,
                  include_untagged: bool = True, max_bytes: int = DEFAULT_MAX_BYTES) -> List[str]:
    """Lines (with continuations) in [since, until] that are tagged with *session*, plus untagged lines
    (e.g. agent.message_sanitization warnings) when include_untagged. Lines tagged with another session
    are excluded."""
    out: List[str] = []
    keep = False
    for line in _read_tail(Path(path), max_bytes):
        m = LINE_RE.match(line)
        if not m:
            if keep:
                out.append(line)
            continue
        try:
            ts = _parse_ts(m.group("ts"), tz)
        except ValueError:
            keep = False
            continue
        tag = m.group("session")
        keep = since <= ts <= until and ((tag == session) if tag else include_untagged)
        if keep:
            out.append(line)
    return out


# Untagged lines that are startup / housekeeping chatter of *any* Hermes process (the messaging gateway,
# other one-shot sessions, this process before its session id exists). They say nothing about what a session
# did, and in run 2 they pushed every session line out of the runner's per-file budget (bug #19). Only
# untagged lines are ever classified: a session-tagged line is always kept.
NOISE_LOGGERS = (
    "hermes_cli.plugins", "hermes_cli.plugin_capabilities", "hermes_cli.mem_trim",
    "hermes_cli.gateway_multiplex_mode", "hermes_cli.main", "tools.registry", "tools.tool_search",
    "tools.skills_sync", "agent.shell_hooks", "agent.auxiliary_client", "agent.credential_pool",
    "cron.*", "gateway.*", "botocore.*", "plugins.*",
)
NOISE_MESSAGES = (
    re.compile(r"^state\.db: linked SQLite .* vulnerable"),            # hermes_state, every process start
    re.compile(r"^Background MCP discovery previously exited"),        # cli, every process start
    re.compile(r"^Loaded environment variables from "),                # run_agent, every process start
    re.compile(r"^OpenAI client created \((?:agent_init|chat_completion_stream_request)"),  # run_agent, per client
)


def _logger_matches(logger: str, pattern: str) -> bool:
    if pattern.endswith(".*"):
        return logger == pattern[:-2] or logger.startswith(pattern[:-1])
    return logger == pattern


def is_noise(logger: str, msg: str, extra_loggers=()) -> bool:
    """True for an UNTAGGED line that is process startup/housekeeping noise (NOISE_LOGGERS, NOISE_MESSAGES,
    plus *extra_loggers*, e.g. from JUDGE_LOG_NOISE_LOGGERS). Everything else untagged (warnings such as
    agent.message_sanitization "Unrepairable ...", untagged agent.tool_executor / tools.* / run_agent lines)
    is context and is kept."""
    if any(_logger_matches(logger, p) for p in tuple(NOISE_LOGGERS) + tuple(extra_loggers)):
        return True
    return any(rx.search(msg) for rx in NOISE_MESSAGES)


def split_lines(path, session: str, since: datetime, until: datetime, tz: tzinfo, noise=(),
                max_bytes: int = DEFAULT_MAX_BYTES):
    """Lines (with continuations) in [since, until], split into
    (tagged: lines tagged with *session*, context: untagged non-noise lines, dropped: {logger: n} of the
    untagged noise lines left out). Lines tagged with another session are excluded entirely. *noise* adds
    logger names (or `prefix.*`) to NOISE_LOGGERS."""
    tagged: List[str] = []
    context: List[str] = []
    dropped: dict = {}
    dest: Optional[List[str]] = None
    for line in _read_tail(Path(path), max_bytes):
        m = LINE_RE.match(line)
        if not m:
            if dest is not None:
                dest.append(line)
            continue
        try:
            ts = _parse_ts(m.group("ts"), tz)
        except ValueError:
            dest = None
            continue
        dest = None
        if not since <= ts <= until:
            continue
        tag = m.group("session")
        if tag:
            dest = tagged if tag == session else None
        elif is_noise(m.group("logger"), m.group("msg"), noise):
            dropped[m.group("logger")] = dropped.get(m.group("logger"), 0) + 1
        else:
            dest = context
        if dest is not None:
            dest.append(line)
    return tagged, context, dropped


def tool_activity(path, session: str, since: datetime, until: datetime, tz: tzinfo) -> int:
    n = 0
    for line in session_lines(path, session, since, until, tz, include_untagged=False):
        m = LINE_RE.match(line)
        if m and m.group("logger") == "agent.tool_executor":
            n += 1
    return n


_TOOL_NAME_RE = re.compile(r"\b[Tt]ool (?P<name>[A-Za-z0-9_.-]+) (?:completed|returned|failed)")


def tools_used(path, session: str, since: datetime, until: datetime, tz: tzinfo) -> set:
    """Names of the tools the session ran in [since, until] (agent.tool_executor lines, e.g.
    `tool memory completed (...)` / `Tool patch returned error ...`)."""
    out = set()
    for line in session_lines(path, session, since, until, tz, include_untagged=False):
        m = LINE_RE.match(line)
        if m and m.group("logger") == "agent.tool_executor":
            t = _TOOL_NAME_RE.search(m.group("msg"))
            if t:
                out.add(t.group("name"))
    return out


def _db(hermes_home) -> Optional[sqlite3.Connection]:
    p = Path(hermes_home) / "state.db"
    if not p.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=2)
        con.execute("PRAGMA query_only = ON")
        return con
    except sqlite3.Error:
        return None


def last_assistant_message(hermes_home, session: str) -> Optional[str]:
    con = _db(hermes_home)
    if con is None:
        return None
    try:
        row = con.execute(
            "SELECT content FROM messages WHERE session_id = ? AND role = 'assistant' "
            "AND content IS NOT NULL AND trim(content) != '' ORDER BY id DESC LIMIT 1", (session,)).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        con.close()


def session_started_at(hermes_home, session: str) -> Optional[datetime]:
    con = _db(hermes_home)
    if con is None:
        return None
    try:
        row = con.execute("SELECT started_at FROM sessions WHERE id = ?", (session,)).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        con.close()
    if not row or row[0] is None:
        return None
    v = row[0]
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(float(v), timezone.utc)
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.astimezone()).astimezone(timezone.utc)
    except (ValueError, OSError):
        return None
