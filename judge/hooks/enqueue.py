#!/usr/bin/env python3
"""judge/hooks/enqueue.py: Hermes shell hook (C1 plan review, C4 completion audit, session snapshots).

stdin: Hermes shell-hook JSON {hook_event_name, tool_name, tool_input, session_id, cwd, profile, extra}.
stdout: always `{}`. Never raises, never exits non-zero; errors go to $JUDGE_REVIEW_DIR/hook-errors.log.

Events (configure each in $HERMES_HOME/config.yaml `hooks:`):
  on_session_start   snapshot watched paths into snapshots/<session>/ (idempotent)
  post_tool_call     matcher "write_file|patch|terminal": records touched paths in snapshots/<session>/
                     events.jsonl (write_file/patch targets; for terminal, the path-like tokens of the
                     command, used only to attribute snapshot changes to the agent); a successful write_file/patch of */.hermes/plans/*.md enqueues a `plan`
                     request (a still-pending plan request for the same session+plan is refreshed instead
                     of duplicated)
  on_session_end     fires once per turn: enqueues a `completion` request for the window since the previous
                     on_session_end of this session (or session start). Turns with no tool activity and no
                     changed watched paths are skipped (JUDGE_ENQUEUE_ALWAYS=1 to enqueue anyway).
                     changed_paths = only what the agent touched: snapshot changes named by the session's
                     tool events (lib/snapshot.agent_touched) plus this turn's write_file/patch targets.
                     Every other snapshot change goes to detail.changed_by_others (paths only) and does not
                     count for data_class.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

JUDGE_DIR = Path(__file__).resolve().parent.parent
if str(JUDGE_DIR) not in sys.path:
    sys.path.insert(0, str(JUDGE_DIR))

PLAN_GLOB = "*/.hermes/plans/*.md"
WRITE_TOOLS = ("write_file", "patch")
_V4A_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+?)\s*$", re.M)
_V4A_MOVE_RE = re.compile(r"^\*\*\* Move to: (.+?)\s*$", re.M)


def _abs(path: str, cwd: str) -> str:
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(cwd or os.getcwd(), p)
    return os.path.normpath(p)


def tool_paths(tool_name: str, tool_input, cwd: str):
    """Absolute paths a write_file/patch call touches (V4A multi-file patches included)."""
    if not isinstance(tool_input, dict):
        return []
    paths = []
    if isinstance(tool_input.get("path"), str) and tool_input["path"].strip():
        paths.append(tool_input["path"].strip())
    if tool_name == "patch" and isinstance(tool_input.get("patch"), str):
        paths += _V4A_FILE_RE.findall(tool_input["patch"]) + _V4A_MOVE_RE.findall(tool_input["patch"])
    return list(dict.fromkeys(_abs(p, cwd) for p in paths))


_REDIR_RE = re.compile(r"^(?:\d*>>?|<|&>>?)")
MAX_TERMINAL_PATHS = 64
MAX_OTHERS = 200


def terminal_paths(command, cwd: str):
    """Path-like tokens of a terminal command, made absolute against *cwd* (best effort; used only to
    attribute snapshot changes to the agent, never shown to the judge as such). A token counts when it
    contains `/`, starts with `~`, or looks like a file name (`name.ext`); URLs are ignored."""
    if not isinstance(command, str) or not command.strip():
        return []
    try:
        toks = shlex.split(command, comments=False)
    except ValueError:
        toks = command.split()
    out = []
    for tok in toks:
        for part in re.split(r"[;|&()]+", tok):
            part = _REDIR_RE.sub("", part.strip())
            if "=" in part and part.startswith("-"):
                part = part.split("=", 1)[1]
            if not part or "://" in part or len(part) > 1024 or part.startswith("-"):
                continue
            if "/" in part or part.startswith("~") or re.fullmatch(r"[\w.+-]+\.[A-Za-z0-9]{1,8}", part):
                out.append(part)
    return list(dict.fromkeys(_abs(p, cwd) for p in out))[:MAX_TERMINAL_PATHS]


def is_plan(path: str) -> bool:
    return fnmatch.fnmatchcase(path, PLAN_GLOB)


def _plan_excerpt(path: str, tool_input) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(4000)
    except OSError:
        if isinstance(tool_input, dict) and isinstance(tool_input.get("content"), str):
            return tool_input["content"][:4000]
    return ""


def _status(extra) -> str:
    return str((extra or {}).get("status") or "") if isinstance(extra, dict) else ""


def _snapdir(cfg, session, cwd, root):
    from lib import snapshot
    from lib import queue as q
    d = q.snapshot_dir(session, root)
    if not (d / "meta.json").is_file():
        snapshot.take(session, cwd, cfg, root=root, late=True)
    return d


# ---------------------------------------------------------------- handlers
def on_session_start(payload, cfg, root):
    from lib import snapshot
    session = payload.get("session_id") or ""
    if session:
        snapshot.take(session, payload.get("cwd") or None, cfg, root=root)
    return None


def post_tool_call(payload, cfg, root):
    from lib import config, queue as q, redact, snapshot
    session = payload.get("session_id") or ""
    tool = payload.get("tool_name") or ""
    cwd = payload.get("cwd") or ""
    extra = payload.get("extra") or {}
    tinput = payload.get("tool_input")
    if tool in WRITE_TOOLS:
        paths = tool_paths(tool, tinput, cwd)
    elif tool == "terminal" and isinstance(tinput, dict):
        paths = terminal_paths(tinput.get("command"), str(tinput.get("workdir") or cwd or ""))
    else:
        paths = []
    if not session:
        return None
    d = _snapdir(cfg, session, cwd or None, root)
    cid = extra.get("tool_call_id") if isinstance(extra, dict) else None
    snapshot.record_event(d, tool, paths, _status(extra),
                          call_id=str(cid) if isinstance(cid, (str, int)) and str(cid).strip() else None,
                          call_hash=redact.call_hash(tool, tinput))
    if tool not in WRITE_TOOLS or _status(extra) == "error":
        return None
    for plan in (p for p in paths if is_plan(p)):
        meta = snapshot.load_meta(d)
        since = meta.get("started") or q.utc_now_iso()
        detail = {"tool": tool, "tool_call_id": extra.get("tool_call_id"), "turn_id": extra.get("turn_id"),
                  "cwd": cwd}
        existing = _pending_plan_request(q, root, session, plan)
        req = q.make_request(
            "plan", session, since, source_event="post_tool_call", changed_paths=[plan],
            claims=redact.redact(_plan_excerpt(plan, tinput)), plan=plan,
            data_class=config.classify([plan], cfg, cwd or None), detail=detail,
            request_id=existing["id"] if existing else None,
            created=existing["created"] if existing else None, root=root)
        if existing:
            req["detail"]["updated"] = q.utc_now_iso()
        q.write_request(req, root)
    return None


def _pending_plan_request(q, root, session, plan):
    for req in q.list_pending(root):
        if req.get("kind") == "plan" and req.get("session") == session and req.get("plan") == plan:
            return req
    return None


def on_session_end(payload, cfg, root):
    from lib import config, hermeslog, queue as q, redact, snapshot
    session = payload.get("session_id") or ""
    if not session:
        return None
    extra = payload.get("extra") or {}
    cwd = payload.get("cwd") or ""
    now = datetime.now(timezone.utc)
    d = _snapdir(cfg, session, cwd or None, root)
    meta = snapshot.load_meta(d)
    since_s = meta.get("last_end") or meta.get("started")
    if meta.get("late") and not meta.get("last_end"):
        started = hermeslog.session_started_at(cfg["HERMES_HOME"], session)
        if started:
            since_s = q.utc_now_iso(started)
    since = q.parse_utc(since_s) if since_s else now - timedelta(hours=1)

    touched = []  # this turn's write_file/patch targets (terminal tokens are only used for attribution)
    for ev in snapshot.events(d):
        try:
            if (ev.get("tool") in WRITE_TOOLS
                    and q.parse_utc(ev.get("t", "")) >= since - timedelta(seconds=1)):
                touched += ev.get("paths") or []
        except ValueError:
            continue
    changed = [p for _, p in snapshot.changed_files(d, cfg)]

    tz = hermeslog.log_tz(cfg)
    log = os.path.join(cfg["HERMES_HOME"], "logs", "agent.log")
    activity = hermeslog.tool_activity(log, session, since, now + timedelta(seconds=5), tz) if os.path.isfile(log) else -1
    try:
        started = q.parse_utc(meta.get("started") or "")
    except ValueError:
        started = since
    log_tools = (hermeslog.tools_used(log, session, min(started, since), now + timedelta(seconds=5), tz)
                 if os.path.isfile(log) else set())
    agent_keys, prefixes = snapshot.agent_touched(d, cfg, tools=log_tools)
    mine, others = snapshot.attribute(changed, agent_keys, prefixes)
    changed_paths = sorted(set(touched) | set(mine))
    others = sorted(set(others) - set(changed_paths))
    recent_events = [ev for ev in snapshot.events(d) if ev.get("t", "") >= q.utc_now_iso(since)]
    if (activity == 0 and not recent_events and not touched
            and os.environ.get("JUDGE_ENQUEUE_ALWAYS") != "1" and not _new_changes(meta, changed)):
        meta["last_end"] = q.utc_now_iso(now)
        snapshot.save_meta(d, meta)
        return None

    claims = hermeslog.last_assistant_message(cfg["HERMES_HOME"], session) or ""
    detail = {k: extra.get(k) for k in ("task_id", "turn_id", "completed", "failed", "interrupted",
                                         "turn_exit_reason", "model", "platform", "reason") if k in extra}
    detail.update({"cwd": cwd, "tool_activity": activity, "snapshot_late": bool(meta.get("late")),
                   "changed_by_others": others[:MAX_OTHERS]})
    if len(others) > MAX_OTHERS:
        detail["changed_by_others_total"] = len(others)
    plans = [p for p in changed_paths if is_plan(p)]
    req = q.make_request(
        "completion", session, q.utc_now_iso(since), source_event="on_session_end",
        changed_paths=changed_paths, claims=_trunc(redact.redact(claims)), plan=plans[-1] if plans else None,
        data_class=config.classify(changed_paths, cfg, cwd or None), detail=detail, root=root)
    dup = q.is_duplicate(req, root=root)
    if dup:
        meta["last_end"] = req["created"]
        snapshot.save_meta(d, meta)
        return None
    q.write_request(req, root)
    meta["last_end"] = req["created"]
    meta["last_changed"] = changed
    snapshot.save_meta(d, meta)
    return None


def _trunc(text: str, limit: int = 4000) -> str:
    """Keep the head and the tail (final responses usually end with the summary of what was done)."""
    if len(text) <= limit:
        return text
    marker = "\n[... truncated ...]\n"
    head = (limit - len(marker)) // 3
    return text[:head] + marker + text[-(limit - len(marker) - head):]


def _new_changes(meta, changed) -> bool:
    return sorted(changed) != sorted(meta.get("last_changed") or [])


HANDLERS = {"on_session_start": on_session_start, "post_tool_call": post_tool_call,
            "on_session_end": on_session_end}


def _log_error(root, msg: str) -> None:
    try:
        root = Path(root) if root else Path(os.path.expanduser(os.environ.get("JUDGE_REVIEW_DIR")
                                                               or os.path.join(os.environ.get("HERMES_HOME") or "~/.hermes", "review")))
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(root / "hook-errors.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            os.write(fd, f"{ts} enqueue: {msg}\n".encode("utf-8", errors="replace"))
        finally:
            os.close(fd)
    except Exception:
        pass


def main(stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    root = None
    try:
        from lib import config
        cfg = config.load_config()
        root = config.review_dir(cfg, create=True)
        payload = json.loads(stdin.read() or "{}")
        event = payload.get("hook_event_name") if isinstance(payload, dict) else None
        if len(sys.argv) > 1 and sys.argv[1] in HANDLERS:  # explicit mode overrides the payload's event name
            event = sys.argv[1]
        handler = HANDLERS.get(event)
        if handler:
            handler(payload, cfg, root)
    except Exception:
        try:
            from lib.redact import redact as _r
            _log_error(root, _r(traceback.format_exc()).replace("\n", " | "))
        except Exception:
            _log_error(root, "unloggable error")
    try:
        stdout.write("{}\n")
        stdout.flush()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        try:
            sys.stdout.write("{}\n")
        except Exception:
            pass
    sys.exit(0)
