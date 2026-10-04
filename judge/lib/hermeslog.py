"""judge/lib/hermeslog.py: read Hermes logs and session state (read-only).

Hermes log lines look like
    2026-10-02 17:00:39,224 INFO [<session>] agent.conversation_loop: API call #1: model=... latency=17.8s
    2026-10-02 22:51:32,770 WARNING agent.message_sanitization: Unrepairable tool_call arguments ...
Parallel tool calls (several tool calls from one model response, run in worker threads) are logged WITHOUT
the session tag (`... INFO agent.tool_executor: tool skill_view completed (0.06s, 15429 chars)`): the tag lives
in thread-local state the workers do not inherit, and the lines carry no thread, task or tool_call id. See
parallel_attribution() for how they are attributed (turn adjacency, only when no other session was active).
A parallel call that FAILS is logged twice: untagged `tool X failed (...)` by the worker, then tagged
`Tool X returned error (...)` by the main thread; counters that need exact call counts pair them.
Timestamps are in the Hermes process' local time (no zone). JUDGE_LOG_TZ overrides the zone used to map
them to UTC ("" = this machine's local zone, "UTC", "+02:00", or an IANA name).  Lines without a timestamp
are continuations of the previous line.

    log_tz(cfg) -> tzinfo
    session_lines(path, session, since_utc, until_utc, tz, include_untagged=True, max_bytes=...,
                  attribute_parallel=True) -> list[str]
    split_lines(path, session, since_utc, until_utc, tz, noise=...) -> (tagged, context, dropped_counts)
                                                                        session lines / untagged context /
                                                                        untagged noise dropped per logger
    parallel_attribution(lines) -> {line index: session}                 untagged parallel tool lines that
                                                                        belong to exactly one session
    PARALLEL_MARK                                                        suffix of such a line in the output
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


# ---------------------------------------------------------------- parallel tool calls (bug #28)
TOOL_LOGGER = "agent.tool_executor"
PARALLEL_MARK = "  # judge: untagged parallel tool call, attributed to this session by turn adjacency"
# The concurrent path's lines (agent/tool_executor.py): completed / failed / cancelled / abandoned at the gate.
_PARALLEL_TOOL_RE = re.compile(r"^tool [A-Za-z0-9_.-]+ (?:completed|failed|cancelled|abandoned)\b")
_API_CALL_RE = re.compile(r"^API call #\d+")
TURN_START = ("agent.turn_context", "conversation turn:")
TURN_END = ("agent.conversation_loop", "Turn ended:")
OPEN_TURN_IDLE = timedelta(minutes=30)  # a turn with no `Turn ended` line is busy this long after its last line


def _naive_ts(ts: str) -> Optional[datetime]:
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def parallel_attribution(lines: List[str]) -> dict:
    """{index into *lines*: session} for UNTAGGED agent.tool_executor lines of parallel tool calls that can be
    attributed to exactly one session. Line k (untagged, `tool X completed|failed|cancelled|...`) goes to
    session S only when ALL of these hold:
      * k lies in S's bracket: going back from k, the session-tagged lines are S's up to S's `API call #n` (the
        model response that asked for the tools); going forward, they are S's up to S's next `API call` or
        `Turn ended` (the tool results went back to S). No line of any other session lies in the bracket
        (S's own tagged lines may: in a mixed batch some calls are logged tagged);
      * no OTHER session was busy anywhere in that bracket: its turn (from `conversation turn:` to `Turn ended`,
        or to OPEN_TURN_IDLE after its last line when the end is missing; from the start of the scanned log
        when the start is missing) does not overlap the bracket.
    Otherwise the line stays untagged context: another session's lines are never attributed. Residual risk:
    a Hermes process that logs no session-tagged lines at all is invisible to this rule."""
    recs = []  # (line index, session or None, logger, msg, naive ts)
    for i, line in enumerate(lines):
        m = LINE_RE.match(line)
        if m:
            recs.append((i, m.group("session"), m.group("logger"), m.group("msg"), _naive_ts(m.group("ts"))))
    if not recs:
        return {}
    # busy spans per session, in record positions
    spans: List[tuple] = []  # (start pos, end pos, session)
    open_at: dict = {}
    last_pos: dict = {}
    for k, (_, tag, logger, msg, _ts) in enumerate(recs):
        if not tag:
            continue
        if tag not in last_pos and not (logger == TURN_START[0] and msg.startswith(TURN_START[1])):
            open_at[tag] = 0  # first line seen mid-turn: busy from the start of the scanned log
        if logger == TURN_START[0] and msg.startswith(TURN_START[1]):
            open_at.setdefault(tag, k)
        last_pos[tag] = k
        spans.append((k, k, tag))
        if logger == TURN_END[0] and msg.startswith(TURN_END[1]) and tag in open_at:
            spans.append((open_at.pop(tag), k, tag))
    for tag, start in open_at.items():  # turn without an end: busy until OPEN_TURN_IDLE after its last line
        end = last_pos[tag]
        t_last = recs[end][4]
        while end + 1 < len(recs) and (t_last is None or recs[end + 1][4] is None
                                       or recs[end + 1][4] <= t_last + OPEN_TURN_IDLE):
            end += 1
        spans.append((start, end, tag))
    tagged_pos = [k for k, r in enumerate(recs) if r[1]]
    out: dict = {}
    j = 0  # index into tagged_pos of the first tagged record after k
    for k, (i, tag, logger, msg, _ts) in enumerate(recs):
        while j < len(tagged_pos) and tagged_pos[j] <= k:
            j += 1
        if tag or logger != TOOL_LOGGER or not _PARALLEL_TOOL_RE.match(msg):
            continue
        if j == 0 or j >= len(tagged_pos):
            continue
        sess = recs[tagged_pos[j - 1]][1]
        p = n = None
        for q in range(j - 1, -1, -1):  # back to the session's `API call #n`, over its own lines only
            r = recs[tagged_pos[q]]
            if r[1] != sess:
                break
            if r[2] == "agent.conversation_loop" and _API_CALL_RE.match(r[3]):
                p = tagged_pos[q]
                break
        for q in range(j, len(tagged_pos)):  # forward to its next `API call` / `Turn ended`, own lines only
            r = recs[tagged_pos[q]]
            if r[1] != sess:
                break
            if r[2] == "agent.conversation_loop" and (_API_CALL_RE.match(r[3]) or r[3].startswith(TURN_END[1])):
                n = tagged_pos[q]
                break
        if p is None or n is None:
            continue
        if any(t != sess and a <= n and b >= p for a, b, t in spans):
            continue
        out[i] = sess
    return out


def _marked(line: str) -> str:
    return line + PARALLEL_MARK


def session_lines(path, session: str, since: datetime, until: datetime, tz: tzinfo,
                  include_untagged: bool = True, max_bytes: int = DEFAULT_MAX_BYTES,
                  attribute_parallel: bool = True) -> List[str]:
    """Lines (with continuations) in [since, until] that are tagged with *session*, plus untagged lines
    (e.g. agent.message_sanitization warnings) when include_untagged. Lines tagged with another session
    are excluded. With attribute_parallel, untagged parallel tool-call lines that parallel_attribution()
    gives to *session* count as session lines (they end with PARALLEL_MARK)."""
    out: List[str] = []
    keep = False
    lines = _read_tail(Path(path), max_bytes)
    owner = parallel_attribution(lines) if attribute_parallel else {}
    for idx, line in enumerate(lines):
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
        mine = owner.get(idx) == session
        keep = since <= ts <= until and ((tag == session) if tag else (include_untagged or mine))
        if keep:
            out.append(_marked(line) if mine else line)
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
    logger names (or `prefix.*`) to NOISE_LOGGERS. Untagged parallel tool-call lines that parallel_attribution()
    gives to *session* go to `tagged`, with PARALLEL_MARK appended (bug #28)."""
    tagged: List[str] = []
    context: List[str] = []
    dropped: dict = {}
    dest: Optional[List[str]] = None
    lines = _read_tail(Path(path), max_bytes)
    owner = parallel_attribution(lines)
    for idx, line in enumerate(lines):
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
        elif owner.get(idx) == session:
            tagged.append(_marked(line))
            dest = tagged
            continue
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


_TOOL_NAME_RE = re.compile(r"\b[Tt]ool (?P<name>[A-Za-z0-9_.-]+) (?:completed|returned|failed|cancelled)")


def tools_used(path, session: str, since: datetime, until: datetime, tz: tzinfo) -> set:
    """Names of the tools the session ran in [since, until] (agent.tool_executor lines, e.g.
    `tool memory completed (...)` / `Tool patch returned error ...`), including attributed parallel calls."""
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
