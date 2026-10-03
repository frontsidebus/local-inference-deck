#!/usr/bin/env python3
"""judge/collector/collect.py: build the evidence bundle for a review request (deterministic, read-only).

Usage:
    collect.py <request-id>                         build evidence/<request-id>/
    collect.py <request-id> --probe <name> [args]   run one allowlisted probe, save it as
                                                    evidence/<request-id>/probes/<name>-<n>.txt, update manifest

Bundle (judge/CONTRACT.md):
    manifest.json        request copy, artifact list, collector version, data_class, window, attribution,
                         point_in_time (what was observed at collection time), notes
    hermes-log.txt       agent.log + errors.log lines in the window, redacted: this session's lines FIRST, then
                         untagged context lines (startup/housekeeping noise of all Hermes processes dropped)
    gate-decisions.jsonl gate.log lines of this session in the window (+ a gate request's own decision), redacted,
                         each with `outcome` executed | not_executed | unknown (matched against events.jsonl)
    agent-diff.patch     watched paths vs the session-start snapshot, only paths the agent touched
                         (+ repo changes since the start HEAD for those paths)
    others-changed.txt   snapshot changes NOT attributed to the agent: paths + diffstat, never content
    host-<name>.txt      walter/covenant: UTC `find -newermt <since> ! -newermt <until>` over /etc /srv /usr/local
                         + `systemctl --failed` (at collection time)
    slots.json           llama-server slot summary (probe `slots`; at collection time)
    c3-results.jsonl     C3 (pre_verify) verifier results in the window, latest per (path, check) `final: true`
                         (from collector/extras.py when present)
    probes/              probe outputs (at the time they ran); probes/host-<name>.txt = host-state probes
                         chosen by extras.host_state_probes for the request's host claims
Window: [request.since, min(request.created + JUDGE_WINDOW_GRACE_SECONDS, next turn start - 1 s)] for the log,
the host find, the gate decisions and the C3 results, no matter when the bundle is collected. The next turn's
start comes from the session's `agent.turn_context: conversation turn:` log lines; only when the log has none,
from the `since` of the session's next request (see window_info).
Attribution: a request's changed_paths count as the agent's only when a session tool event (events.jsonl) names
them; the others are listed in manifest attribution.rejected_request_paths. Hermes bookkeeping files
(lib/snapshot.NOISE_GLOBS) never count.
data_class=sensitive (from the request, or from re-classifying the agent-attributed paths: the stricter wins):
agent diff carries one `# content withheld (data_class=sensitive): <path> — N lines changed (+a/-b)` line per
changed path (no file contents).  Hosts that cannot be reached are recorded, never fatal.

Exit: 0 bundle written, 2 request not found / bad id, 64 bad probe (nothing executed).
"""
from __future__ import annotations

import importlib
import json
import os
import re
import shlex
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

JUDGE_DIR = Path(__file__).resolve().parent.parent
if str(JUDGE_DIR) not in sys.path:
    sys.path.insert(0, str(JUDGE_DIR))

from lib import config, hermeslog, snapshot  # noqa: E402
from lib import queue as q  # noqa: E402
from lib.redact import redact  # noqa: E402

sys.path.insert(0, str(JUDGE_DIR / "probes"))
import probe  # noqa: E402

COLLECTOR_VERSION = "2"
HOST_TIMEOUT = 45
LOG_MAX_LINES = 3000
FIND_MAX = 500
GATE_MAX_LINES = 500
ISO_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
POINT_IN_TIME = "state at collection time, NOT during the session; never evidence of what the agent saw or did"


def _write(path: Path, text: str) -> Path:
    return q.atomic_write(path, text)


# ---------------------------------------------------------------- window
TURN_START_LOGGER = "agent.turn_context"
TURN_START_MSG = "conversation turn:"


def _turn_starts(req: Dict, cfg: Mapping[str, str], since: datetime, end: datetime) -> Optional[List[datetime]]:
    """Times of the session's `agent.turn_context: conversation turn:` lines in [since, end]; None when the
    Hermes log is missing."""
    log = Path(cfg["HERMES_HOME"]) / "logs" / "agent.log"
    session = str(req.get("session") or "")
    if not session or not log.is_file():
        return None
    tz = hermeslog.log_tz(cfg)
    out = []
    for line in hermeslog.session_lines(log, session, since, end, tz, include_untagged=False):
        m = hermeslog.LINE_RE.match(line)
        if m and m.group("logger") == TURN_START_LOGGER and m.group("msg").startswith(TURN_START_MSG):
            try:
                out.append(hermeslog._parse_ts(m.group("ts"), tz))
            except ValueError:
                continue
    return out


def _next_turn_from_requests(req: Dict, root: Optional[Path], created: datetime) -> Optional[datetime]:
    """Earliest `since` >= this request's `created` among the session's other requests (queue/ + done/): a
    request whose window starts after this one was created belongs to a later turn."""
    session = str(req.get("session") or "")
    if not session or root is None:
        return None
    best = None
    for other in q._iter_all_requests(root):
        if other.get("id") == req.get("id") or str(other.get("session") or "") != session:
            continue
        try:
            s = q.parse_utc(str(other.get("since") or ""))
        except ValueError:
            continue
        if s >= created and (best is None or s < best):
            best = s
    return best


def window_info(req: Dict, cfg: Mapping[str, str], root: Optional[Path] = None) -> Dict:
    """{"since", "until", "basis", "next_turn_start"?} (datetimes, UTC).

    until = min(created + JUDGE_WINDOW_GRACE_SECONDS, next turn start - 1 s), never before `created`.
    The next turn's start is the first `agent.turn_context: conversation turn:` line of the session at or after
    `created` (basis "next_turn_log"). Only when the log has no such line anywhere in [since, created + grace]
    (missing log, a Hermes without these lines) is the earliest `since` >= created of the session's other
    requests used instead (basis "next_request"). Otherwise basis "grace". Log times have 1 s resolution.
    Independent of when the bundle is collected, so a later turn's evidence cannot leak into this bundle."""
    since = q.parse_utc(req["since"])
    created = q.parse_utc(req["created"])
    until = created + timedelta(seconds=config.window_grace(cfg))
    info: Dict = {"basis": "grace"}
    nxt, basis = None, ""
    try:
        starts = _turn_starts(req, cfg, min(since, created), until)
    except (OSError, KeyError):
        starts = None
    if starts:
        later = [t for t in starts if t >= created]
        nxt, basis = (min(later) if later else None), "next_turn_log"
    else:
        nxt, basis = _next_turn_from_requests(req, root, created), "next_request"
    if nxt is not None and nxt - timedelta(seconds=1) < until:
        until = max(created, nxt - timedelta(seconds=1))
        info.update(basis=basis, next_turn_start=nxt)
    info.update(since=since, until=max(until, since))
    return info


def turn_end(req: Dict, cfg: Mapping[str, str], root: Optional[Path], until: datetime, now: datetime) -> datetime:
    """End of the request's turn, for evidence that can legitimately trail `created + grace` (C3 re-runs on
    pre_verify attempt > 0): next turn start - 1 s (same sources and precedence as window_info, but searched
    up to *now*); with no next turn known, *now*. Never before *until*."""
    since, created = q.parse_utc(req["since"]), q.parse_utc(req["created"])
    try:
        starts = _turn_starts(req, cfg, min(since, created), max(now, until))
    except (OSError, KeyError):
        starts = None
    if starts:
        later = [t for t in starts if t >= created]
        nxt = min(later) if later else None
    else:
        nxt = _next_turn_from_requests(req, root, created)
    end = (nxt - timedelta(seconds=1)) if nxt is not None else now
    return max(end, until)


def window(req: Dict, cfg: Mapping[str, str], root: Optional[Path] = None) -> Tuple[datetime, datetime]:
    w = window_info(req, cfg, root)
    return w["since"], w["until"]


# ---------------------------------------------------------------- pieces
SESSION_SECTION = "SESSION LINES"
CONTEXT_SECTION = "UNTAGGED CONTEXT"


def hermes_log(req: Dict, cfg: Mapping[str, str], since: datetime, until: datetime) -> str:
    """hermes-log.txt (CONTRACT.md "hermes-log.txt layout"): every line of the window that is tagged with the
    session comes FIRST (agent.log, then errors.log), then the untagged context lines. Untagged lines are
    classified by lib/hermeslog.is_noise: startup/housekeeping chatter of any Hermes process (plugin and
    tool registration, memory trim, gateway, ...) is dropped and only counted; every other untagged line
    (e.g. agent.message_sanitization "Unrepairable ..." warnings, untagged agent.tool_executor lines) is kept
    as context. errors.log lines that also appear in agent.log are not repeated."""
    tz = hermeslog.log_tz(cfg)
    # a C6 watcher request belongs to no Hermes session: untagged lines would only add unrelated (possibly
    # agent-content) context to a bundle that may go to the frontier judge
    untagged = not _watch_runaway(req)
    extra_noise = tuple((cfg.get("JUDGE_LOG_NOISE_LOGGERS") or "").split())
    out = [f"# Hermes log lines for session {req['session']}"
           + (" (plus untagged context lines, after the session lines)" if untagged
              else " (untagged lines omitted: C6 watcher request)")
           + f", window {q.utc_now_iso(since)} .. {q.utc_now_iso(until)} UTC; log tz {tz}; secrets redacted",
           f"# Layout: '{SESSION_SECTION}' sections hold every line tagged [{req['session']}] (agent.log, then "
           "errors.log)" + (f"; '{CONTEXT_SECTION}' sections follow: lines without a session tag, which may come "
                            "from this or any other Hermes process; startup/housekeeping noise of all Hermes "
                            "processes is dropped and only counted" if untagged else "")]
    parts: Dict[str, Tuple] = {}
    for name in ("agent.log", "errors.log"):
        p = Path(cfg["HERMES_HOME"]) / "logs" / name
        parts[name] = hermeslog.split_lines(p, req["session"], since, until, tz, extra_noise) if p.is_file() else None
    seen = set()
    if parts.get("agent.log"):
        seen = set(parts["agent.log"][0]) | set(parts["agent.log"][1])

    def section(name: str, idx: int, title: str) -> None:
        got = parts[name]
        if got is None:
            out.append(f"\n===== {name}: {title} =====")
            out.append("(missing)")
            return
        lines = got[idx]
        dup = 0
        if name == "errors.log" and seen:
            kept = [ln for ln in lines if ln not in seen]
            dup, lines = len(lines) - len(kept), kept
        extra = []
        if dup:
            extra.append(f"{dup} line(s) also in agent.log not repeated")
        if idx == 1 and got[2]:
            extra.append(f"{sum(got[2].values())} noise line(s) dropped: "
                         + ", ".join(f"{k} x{v}" for k, v in sorted(got[2].items(), key=lambda kv: (-kv[1], kv[0]))))
        out.append(f"\n===== {name}: {title} ({len(lines)} line(s){'; ' if extra else ''}{'; '.join(extra)}) =====")
        if len(lines) > LOG_MAX_LINES:
            out.append(f"# {len(lines) - LOG_MAX_LINES} earlier lines omitted (showing the last {LOG_MAX_LINES})")
            lines = lines[-LOG_MAX_LINES:]
        out.append("\n".join(redact(ln[:4000]) for ln in lines) if lines else "(no lines in window)")

    for name in ("agent.log", "errors.log"):
        section(name, 0, SESSION_SECTION)
    if untagged:
        for name in ("agent.log", "errors.log"):
            section(name, 1, CONTEXT_SECTION)
    return "\n".join(out) + "\n"


GATE_MATCH_SECONDS = 600       # an escalated call that has not run this long after its decision never ran
NOT_RUN_STATUSES = snapshot.NOT_RUN_STATUSES  # shared with attribution (lib/snapshot.py)


def _read_gate_log(root: Path) -> List[Dict]:
    out = []
    try:
        lines = (Path(root) / "gate.log").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            try:
                rec["_ts"] = q.parse_utc(str(rec.get("ts") or ""))
            except ValueError:
                rec["_ts"] = None
            out.append(rec)
    return out


def _match_executions(recs: List[Dict], evs: List[Dict]) -> Dict[int, str]:
    """{index in recs: basis} for gate decisions whose tool call was later executed (seen by post_tool_call).

    1. tool_call_id equal to an executed event's call_id.
    2. otherwise tool + call_hash: each executed event is paired with the LATEST earlier decision of the same
       call (so a retry after a declined escalation does not mark the declined one as executed)."""
    hit: Dict[int, str] = {}
    by_id = {str(r["tool_call_id"]): i for i, r in enumerate(recs) if r.get("tool_call_id")}
    rest = []
    for ev in evs:
        i = by_id.get(str(ev.get("call_id") or "")) if ev.get("call_id") else None
        if i is not None:
            hit[i] = "tool_call_id"
        else:
            rest.append(ev)
    for ev in rest:
        if not ev.get("call_hash"):
            continue
        best = None
        for i, r in enumerate(recs):
            if (r.get("call_hash") == ev["call_hash"] and r.get("tool") == ev.get("tool") and r["_ts"] is not None
                    and r["_ts"] <= ev["_t"] + timedelta(seconds=1)
                    and ev["_t"] - r["_ts"] <= timedelta(seconds=GATE_MATCH_SECONDS)):
                if best is None or r["_ts"] >= recs[best]["_ts"]:
                    best = i
        if best is not None and best not in hit:
            hit[best] = "call_hash"
    return hit


def gate_decisions(req: Dict, root: Path, since: datetime, until: datetime,
                   now: Optional[datetime] = None) -> List[str]:
    """gate.log lines (JSON, re-redacted) of this session with ts in [since, until], plus, for a `gate`
    request, the decision that created it (matched by request id, whatever its ts). Each line gains
    `outcome` (executed | not_executed | unknown) and `outcome_basis`."""
    now = now or datetime.now(timezone.utc)
    own = f"{req['id']}.json" if req.get("kind") == "gate" else None
    session = str(req.get("session") or "")
    recs = [r for r in _read_gate_log(root) if str(r.get("session") or "") == session
            or (own is not None and os.path.basename(str(r.get("request") or "")) == own)]
    d = q.snapshot_dir(req["session"], root)
    meta = snapshot.load_meta(d)
    # executions the hook saw; for a completion/plan request only those up to its window end
    ev_end = now if req.get("kind") == "gate" else until
    evs = []
    refused: Dict[str, str] = {}  # call_id -> status of a post_tool_call event saying the call never ran
    for ev in snapshot.events(d):
        try:
            ev["_t"] = q.parse_utc(str(ev.get("t") or ""))
        except ValueError:
            continue
        if ev["_t"] > ev_end:
            continue
        status = str(ev.get("status") or "").lower()
        if status not in NOT_RUN_STATUSES:
            evs.append(ev)
        elif ev.get("call_id"):
            refused[str(ev["call_id"])] = status
    hit = _match_executions(recs, evs)
    markers = any(ev.get("call_hash") or ev.get("call_id") for ev in snapshot.events(d))
    try:
        turn_end = q.parse_utc(meta.get("last_end") or "") if meta else None
    except ValueError:
        turn_end = None
    created = q.parse_utc(req["created"])
    out: List[str] = []
    for i, rec in enumerate(recs):
        ts = rec.pop("_ts")
        mine = own is not None and os.path.basename(str(rec.get("request") or "")) == own
        if not mine and (ts is None or not since <= ts <= until):
            continue
        dec = rec.get("decision")
        if i in hit:
            rec["outcome"], rec["outcome_basis"] = "executed", f"post_tool_call event matched by {hit[i]}"
        elif rec.get("tool_call_id") and str(rec["tool_call_id"]) in refused:
            rec["outcome"], rec["outcome_basis"] = (
                "not_executed", f"post_tool_call reported status={refused[str(rec['tool_call_id'])]} for this "
                                "call (matched by tool_call_id): it never ran")
        elif not meta:
            rec["outcome"], rec["outcome_basis"] = "unknown", "no session snapshot/events (hook not installed?)"
        elif not (rec.get("call_hash") or rec.get("tool_call_id")) or not markers:
            rec["outcome"], rec["outcome_basis"] = "unknown", "decision or events predate call markers"
        elif (dec == "block" or now - ts >= timedelta(seconds=GATE_MATCH_SECONDS)
              or (turn_end is not None and turn_end >= ts)
              or (req.get("kind") != "gate" and created >= ts)):
            rec["outcome"], rec["outcome_basis"] = "not_executed", "no post_tool_call event for this call"
        else:
            rec["outcome"], rec["outcome_basis"] = "unknown", "decision too recent to tell"
        if dec == "approve":
            rec["decision_meaning"] = "escalated to the human for approval; `outcome` says whether the call ran"
        elif dec == "block":
            rec["decision_meaning"] = "refused by the gate"
        out.append(redact(json.dumps(rec, ensure_ascii=False, sort_keys=True)))
        if len(out) >= GATE_MAX_LINES:
            break
    return out


def _mtime(path: str) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
    except OSError:
        return None


def attribution(req: Dict, cfg: Mapping[str, str], root: Path, until: datetime) -> Dict[str, List[str]]:
    """Split the session's snapshot changes (as of now) into the agent's and others'.

    agent:    changed paths named by the session's tool events up to the window end (or under a HERMES_HOME
              dir of a self-write tool the session ran)
    accepted: request changed_paths backed by such an event (or self-write prefix); they join the agent set
    rejected: request changed_paths NO tool event of the session backs: never attributed to the agent (a
              legacy or forged request, or an operator's change the hook misread)
    noise:    request changed_paths that are Hermes bookkeeping (snapshot.NOISE_GLOBS): ignored
    others:   changed paths that no agent tool event of the session names, last modified by the window end
    later:    changed paths the agent touched only after the window, or others' changes after it (omitted)"""
    res: Dict[str, List[str]] = {"agent": [], "others": [], "later": [], "accepted": [], "rejected": [],
                                 "noise": []}
    d = q.snapshot_dir(req["session"], root)
    meta = snapshot.load_meta(d)
    noise = snapshot.noise_globs(cfg, meta)
    req_paths = sorted({p for p in req.get("changed_paths") or [] if isinstance(p, str) and p.strip()})
    res["noise"] = [p for p in req_paths if snapshot.is_noise(p, noise)]
    req_paths = [p for p in req_paths if p not in res["noise"]]
    if not meta:
        res["rejected"] = req_paths
        return res
    changed = [p for _, p in snapshot.changed_files(d, cfg)]
    tz = hermeslog.log_tz(cfg)
    log = Path(cfg["HERMES_HOME"]) / "logs" / "agent.log"
    since = q.parse_utc(req["since"])
    try:
        started = min(q.parse_utc(meta.get("started") or req["since"]), since)
    except ValueError:
        started = since

    def tools(end: datetime):
        return hermeslog.tools_used(log, req["session"], started, end, tz) if log.is_file() else set()

    keys, prefixes = snapshot.agent_touched(d, cfg, until=until, tools=tools(until))
    res["accepted"], res["rejected"] = snapshot.attribute(req_paths, keys, prefixes)
    agent, rest = snapshot.attribute(changed, keys, prefixes)
    keys_any, prefixes_any = snapshot.agent_touched(
        d, cfg, tools=tools(datetime.now(timezone.utc) + timedelta(days=1)))
    later_agent, others = snapshot.attribute(rest, keys_any, prefixes_any)
    late = [p for p in others if (_mtime(p) or until) > until]
    res["agent"] = agent
    res["others"] = [p for p in others if p not in late]
    res["later"] = sorted(set(later_agent) | set(late))
    return res


def agent_diff(req: Dict, cfg: Mapping[str, str], sensitive: bool, root: Path, paths: List[str],
               until: datetime, rejected=()) -> str:
    d = q.snapshot_dir(req["session"], root)
    text = snapshot.diff_text(d, cfg, sensitive=sensitive, include=paths, until=until)
    if snapshot.load_meta(d) and not paths:
        text += "# no changed path is attributed to the agent (other changes, if any: others-changed.txt)\n"
    if rejected:
        text += (f"# {len(rejected)} path(s) listed by the request are NOT attributed to the agent: no tool event "
                 "of this session names them (see manifest attribution.rejected_request_paths)\n")
    return text if sensitive else redact(text)


def others_changed(req: Dict, cfg: Mapping[str, str], root: Path, att: Dict[str, List[str]], sensitive: bool,
                   until: datetime) -> str:
    hdr = ["# Watched-path changes NOT made by the agent: no tool call of this session (write_file/patch",
           "# target, path in a terminal command, memory/skill tool) touched them. The human, another program",
           "# or a `git pull` made them. Context only: never attribute them to the agent.",
           "# Paths and diffstat only, never content; the diffstat is as observed at collection time."]
    d = q.snapshot_dir(req["session"], root)
    if not snapshot.load_meta(d):
        return "\n".join(hdr + ["# no snapshot for this session: attribution unavailable"]) + "\n"
    others = att["others"]
    withheld = 0
    if not sensitive:  # an infra bundle may reach the frontier judge: name only infra-class paths
        cwd = (req.get("detail") or {}).get("cwd") or None
        shown = [p for p in others if config.is_infra_path(p, cfg, cwd)]
        withheld = len(others) - len(shown)
        others = shown
    body = snapshot.stat_lines(d, cfg, others)
    out = hdr + [f"# {len(body)} path(s)"] + (body or ["(none)"])
    if withheld:
        out.append(f"# {withheld} non-infra path(s) withheld (data_class=infra bundle): changed by others, "
                   "paths not shown")
    if att["later"]:
        out.append(f"# {len(att['later'])} path(s) changed after the window end ({q.utc_now_iso(until)}) omitted")
    return "\n".join(out) + "\n"


def host_command(since: str, until: str) -> str:
    """Remote, read-only. *since*/*until* must be strict `YYYY-MM-DDTHH:MM:SSZ` (validated; no injection)."""
    for v in (since, until):
        if not isinstance(v, str) or not ISO_Z_RE.match(v):
            raise ValueError(f"bad window bound: {v!r}")
    start = since.replace("T", " ").replace("Z", " UTC")
    end = until.replace("T", " ").replace("Z", " UTC")
    return (
        f"echo '# files changed in [{since}, {until}] (UTC; owner mode mtime-UTC path; sorted by mtime)'; "
        f"L=$(TZ=UTC find /etc /srv /usr/local -xdev -newermt {shlex.quote(start)} ! -newermt {shlex.quote(end)} "
        "\\( -type f -o -type l \\) -printf '%u %m %TY-%Tm-%TdT%TT %p\\n' 2>/dev/null); "
        "echo \"# $(printf '%s\\n' \"$L\" | grep -c .) path(s)\"; "
        f"printf '%s\\n' \"$L\" | sort -k3 | head -n {FIND_MAX}; "
        "echo; echo '# systemctl --failed (at collection time, not during the window)'; "
        "systemctl --failed --no-legend --plain --no-pager 2>&1; "
        "echo; echo '# host clock (UTC)'; date -u +%Y-%m-%dT%H:%M:%SZ"
    )


def host_diff(name: str, req: Dict, cfg: Mapping[str, str], runner, since: str, until: str) -> str:
    hdr = f"# host {name}: changes in [{since}, {until}] (UTC); read-only; find over /etc /srv /usr/local is " \
          f"limited to what the ssh user can read; first {FIND_MAX} paths\n" + probe.host_header(name) + "\n"
    try:
        argv = config.host_ssh(name, cfg) + [host_command(since, until)]
    except ValueError as e:
        return hdr + f"UNREACHABLE: not configured ({e})\n"
    rc, out, err = runner(argv, HOST_TIMEOUT)
    if rc == 255 or (rc != 0 and not out.strip()):
        why = "timeout" if rc == 124 else f"ssh exit {rc}"
        return hdr + f"UNREACHABLE: {why}: {redact(err.strip())[:500]}\n"
    tail = f"\n# exit {rc}" + (f"; stderr: {redact(err.strip())[:500]}" if err.strip() else "") + "\n"
    return hdr + redact(out) + tail


# ---------------------------------------------------------------- extras (collector/extras.py, optional)
EXTRAS_MODULE = "extras"
C3_ARTIFACT = "c3-results.jsonl"
_PROBE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _load_extras():
    """collector/extras.py (C3 results, host-state probes) or None when it is not installed."""
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        return importlib.import_module(EXTRAS_MODULE)
    except ImportError:
        return None


def c3_lines(results) -> List[str]:
    """JSON lines for c3-results.jsonl: every record re-redacted, `final` always present (bool). When the
    source did not flag finals, the latest record per (path, check) is final."""
    recs = [dict(r) for r in (results or []) if isinstance(r, dict)]
    if not any("final" in r for r in recs):
        last: Dict[Tuple[str, str], int] = {}
        for i, r in enumerate(recs):
            last[(str(r.get("path")), str(r.get("check")))] = i
        for i, r in enumerate(recs):
            r["final"] = last[(str(r.get("path")), str(r.get("check")))] == i
    out = []
    for r in recs:
        r["final"] = bool(r.get("final"))
        if isinstance(r.get("detail"), str):
            r["detail"] = r["detail"][:300]
        out.append(redact(json.dumps(r, ensure_ascii=False, sort_keys=True, default=str)))
    return out


def probe_artifact_name(name: str) -> str:
    """`probes/host-<name>.txt` for a host-state probe called *name* (sanitised, no path separators)."""
    base = str(name or "probe").strip()
    if base.endswith(".txt"):
        base = base[:-4]
    if base.startswith("host-"):
        base = base[5:]
    base = _PROBE_NAME_RE.sub("_", base).strip("._-")[:80] or "probe"
    return f"probes/host-{base}.txt"


def collect_extras(req: Dict, cfg: Mapping[str, str], root: Path, ev: Path, since: datetime, until: datetime,
                   now_s: str, notes: Dict[str, str], windowed: List[str], pit: Dict[str, Dict],
                   c3_until: Optional[datetime] = None, runner=None, gate_lines: Optional[List[str]] = None) -> Dict:
    """Write c3-results.jsonl and probes/host-*.txt from collector/extras.py. Never fatal.

    C3 results cover [since, c3_until] (the turn end, see turn_end): verify.py re-runs the verifiers on
    pre_verify attempt > 0 and those final-state lines can land after created + grace."""
    info: Dict = {"available": False}
    ex = _load_extras()
    if ex is None:
        notes[C3_ARTIFACT] = "not collected: collector/extras.py is not installed"
        notes["probes/host-*.txt"] = "not collected: collector/extras.py is not installed"
        return info
    info["available"] = True
    fn = getattr(ex, "c3_results", None)
    if callable(fn):
        try:
            c3_until = c3_until or until
            lines = c3_lines(fn(str(req.get("session") or ""), since, c3_until, Path(root)))
            info["c3_window"] = {"since": q.utc_now_iso(since), "until": q.utc_now_iso(c3_until)}
            _write(ev / C3_ARTIFACT, "".join(ln + "\n" for ln in lines))
            windowed.append(C3_ARTIFACT)
            info["c3_results"] = len(lines)
            if not lines:
                notes[C3_ARTIFACT] = "no C3 verifier results in window (empty file): no verifier ran"
            else:
                notes[C3_ARTIFACT] = ("one line per C3 verifier run in the window; `final: true` marks the latest "
                                      "result per (path, check), i.e. the state the turn ended with")
        except Exception as e:  # extras must never break the bundle
            notes[C3_ARTIFACT] = f"not collected: extras.c3_results failed ({e.__class__.__name__})"
    fn = getattr(ex, "host_state_probes", None)
    if callable(fn):
        try:
            probes = fn(req, cfg, runner=runner, root=root, gate_lines=gate_lines) or {}
            written = []
            for name, text in sorted(probes.items()):
                art = probe_artifact_name(name)
                _write(ev / art, redact(str(text)))
                written.append(art)
                head = str(text).lstrip().splitlines()[0] if str(text).strip() else ""
                if head.startswith("# WINDOWED"):
                    windowed.append(art)
                else:
                    pit[art] = {"observed_at": now_s, "note": "host-state probe run at collection time, NOT the "
                                "state during the session"}
            info["host_probes"] = written
            if not written:
                notes["probes/host-*.txt"] = "no host-state claims to probe"
        except Exception as e:
            notes["probes/host-*.txt"] = f"not collected: extras.host_state_probes failed ({e.__class__.__name__})"
    return info


# ---------------------------------------------------------------- bundle
def _artifacts(ev: Path) -> List[str]:
    return sorted(str(p.relative_to(ev)) for p in ev.rglob("*") if p.is_file() and p.name != "manifest.json"
                  and not p.name.startswith("."))


def _write_manifest(ev: Path, req: Dict, data_class: str, extra: Optional[Dict] = None) -> None:
    mp = ev / "manifest.json"
    try:
        old = json.loads(mp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    man = {**old, "request": req, "collector_version": COLLECTOR_VERSION, "data_class": data_class,
           "artifacts": _artifacts(ev), **(extra or {})}
    q.atomic_write_json(mp, man)


def _host_rule_gate(req: Dict) -> bool:
    """A C2 gate request whose fired rules are all host rules (a command aimed at Walter/Covenant)."""
    rules = (req.get("detail") or {}).get("rules")
    return (req.get("kind") == "gate" and isinstance(rules, list) and bool(rules)
            and all(isinstance(r, str) for r in rules) and set(rules) <= config.HOST_RULES)


WATCH_SESSION_RE = re.compile(r"^watch-task-?\d+$")  # session name the C6 watcher gives its requests


def _watch_runaway(req: Dict) -> bool:
    """A C6 runaway request as watch/runaway.py writes it: slot telemetry only (no agent content, no paths).
    Shape-checked (kind, source_event, session name, no changed_paths, no cwd) so a request merely labelled
    `runaway` does not qualify."""
    detail = req.get("detail") if isinstance(req.get("detail"), dict) else {}
    return (req.get("kind") == "runaway" and req.get("source_event") == "watch"
            and isinstance(req.get("session"), str) and bool(WATCH_SESSION_RE.match(req["session"]))
            and not req.get("changed_paths") and not detail.get("cwd"))


def effective_class(req: Dict, cfg: Mapping[str, str], agent_paths=()) -> str:
    """The stricter of the request's own data_class and the collector's independent classification of the
    agent-attributed paths (request changed_paths + *agent_paths*, Hermes bookkeeping noise excluded).
    Request paths rejected by attribution still count here: they can only make the bundle stricter.

    With no paths, only a host-rule gate request keeps the hook's answer (its evidence is the redacted
    command), and a C6 watcher runaway request keeps its own (slot telemetry, no agent content: it should
    reach the frontier judge, not `coder-fast`, which may be the very model that is running away). Any
    other path-less request is classified from the session cwd (classify([], cfg, cwd)), so
    an `infra` label alone never lets a request reach the frontier judge."""
    cwd = (req.get("detail") or {}).get("cwd") or None
    noise = snapshot.noise_globs(cfg)
    paths = sorted(p for p in set(req.get("changed_paths") or []) | set(agent_paths or [])
                   if isinstance(p, str) and p.strip() and not snapshot.is_noise(p, noise))
    if paths:
        mine = config.classify(paths, cfg, cwd)
    elif _host_rule_gate(req) or _watch_runaway(req):
        mine = req.get("data_class")
    else:
        mine = config.classify([], cfg, cwd)
    return "infra" if req.get("data_class") == "infra" and mine == "infra" else "sensitive"


def collect(request_id: str, cfg: Optional[Mapping[str, str]] = None, runner=None, root=None,
            now: Optional[datetime] = None) -> Path:
    cfg = cfg or config.load_config()
    runner = runner or probe.subprocess_runner
    root = Path(root) if root else config.review_dir(cfg, create=True)
    req = q.read_request(request_id, root)
    errs = q.validate_request(req)
    if errs:
        raise q.ValidationError(errs)
    now = now or datetime.now(timezone.utc)
    win = window_info(req, cfg, root)
    since, until = win["since"], win["until"]
    since_s, until_s, now_s = q.utc_now_iso(since), q.utc_now_iso(until), q.utc_now_iso(now)
    att = attribution(req, cfg, root, until)
    data_class = effective_class(req, cfg, att["agent"])
    sensitive = data_class == "sensitive"
    ev = q.evidence_dir(request_id, root, create=True)
    q.ensure_dir(ev / "probes")
    try:
        (ev / "local-diff.patch").unlink()  # collector v1 layout (mixed agent + others' changes)
    except OSError:
        pass

    notes: Dict[str, str] = {}
    _write(ev / "hermes-log.txt", hermes_log(req, cfg, since, until))
    gates = gate_decisions(req, root, since, until, now)
    _write(ev / "gate-decisions.jsonl", "".join(ln + "\n" for ln in gates))
    if not gates:
        notes["gate-decisions.jsonl"] = "no gate decisions in window (empty file)"
    agent_paths = sorted(set(att["agent"]) | set(att["accepted"]))
    _write(ev / "agent-diff.patch", agent_diff(req, cfg, sensitive, root, agent_paths, until, att["rejected"]))
    _write(ev / "others-changed.txt", others_changed(req, cfg, root, att, sensitive, until))
    cwd = (req.get("detail") or {}).get("cwd") or None
    withheld: Dict[str, str] = {}
    if sensitive:
        withheld["agent-diff.patch"] = (
            "data_class=sensitive: file contents withheld; every changed agent path has a `# content withheld` "
            "line with a line-count stat. Such a line means the file changed; it is not 'no change'.")
    if not sensitive and any(not config.is_infra_path(p, cfg, cwd) for p in att["others"]):
        withheld["others-changed.txt"] = "data_class=infra bundle: non-infra paths changed by others withheld"
    if att["rejected"]:
        notes["attribution"] = (
            f"{len(att['rejected'])} request changed_path(s) rejected: no tool event of this session names them "
            + ("(the gated call never ran, or ran after the window)" if req.get("kind") == "gate" else
               "(legacy/forged request or another actor's change)") + "; they are NOT the agent's changes")
    if att["noise"]:
        notes["noise"] = f"{len(att['noise'])} request changed_path(s) are Hermes bookkeeping files; ignored"
    for host in config.HOSTS:
        _write(ev / f"host-{host}.txt", host_diff(host, req, cfg, runner, since_s, until_s))
    try:
        slots = probe.slots_summary(cfg, runner=runner)
    except ValueError as e:
        slots = {"error": str(e)}
    _write(ev / "slots.json", redact(json.dumps(slots, indent=2)) + "\n")
    pit = {"observed_at": now_s, "note": POINT_IN_TIME}
    windowed = ["hermes-log.txt", "gate-decisions.jsonl"] + [f"host-{h}.txt (find)" for h in config.HOSTS]
    extra_pit: Dict[str, Dict] = {}
    gate_excerpts = []
    for ln in gates:
        try:
            x = json.loads(ln).get("excerpt")
        except (ValueError, AttributeError):
            x = None
        if isinstance(x, str) and x:
            gate_excerpts.append(x)
    ex_info = collect_extras(req, cfg, root, ev, since, until, now_s, notes, windowed, extra_pit,
                             c3_until=turn_end(req, cfg, root, until, now), runner=runner,
                             gate_lines=gate_excerpts)
    window_rec = {"since": since_s, "until": until_s, "grace_seconds": config.window_grace(cfg),
                  "until_basis": win["basis"]}
    if win.get("next_turn_start"):
        window_rec["next_turn_start"] = q.utc_now_iso(win["next_turn_start"])
    meta = snapshot.load_meta(q.snapshot_dir(req["session"], root))
    snap_rec = {"dir_roots": [x.get("root") for x in meta.get("dir_roots") or [] if isinstance(x, dict)],
                "truncated_roots": [{"root": x.get("root"), "files": x.get("files")}
                                    for x in meta.get("dir_roots") or [] if isinstance(x, dict) and x.get("truncated")],
                "skipped_roots": [x for x in meta.get("skipped_roots") or [] if isinstance(x, dict)],
                "caps": meta.get("snapshot_caps") or snapshot.snapshot_caps(cfg),
                "noise_globs": snapshot.noise_globs(cfg, meta)} if meta else {"available": False}
    if snap_rec.get("truncated_roots") or snap_rec.get("skipped_roots"):
        notes["snapshot"] = ("some opted-in dirs were truncated or not snapshotted at session start (see "
                             "manifest snapshot); changes there may be missing from agent-diff.patch")
    _write_manifest(ev, req, data_class, {
        "collected": now_s,
        "window": window_rec,
        "windowed": windowed,
        "snapshot": snap_rec,
        "extras": ex_info,
        "withheld": withheld,
        "point_in_time": {
            **extra_pit,
            "slots.json": dict(pit),
            **{f"host-{h}.txt (systemctl --failed, host clock)": dict(pit) for h in config.HOSTS},
            "agent-diff.patch": {"observed_at": now_s,
                                 "note": "the agent's paths vs the session-start snapshot, read at collection "
                                         "time; a file modified after the window carries a NOTE line"},
            "others-changed.txt": dict(pit),
        },
        "attribution": {"agent_paths": sorted(att["agent"]),
                        "changed_by_others": sorted(att["others"]) if sensitive else
                        sorted(p for p in att["others"]
                               if config.is_infra_path(p, cfg, cwd)),
                        "omitted_after_window": len(att["later"]),
                        "rejected_request_paths": sorted(att["rejected"]) if sensitive else
                        sorted(p for p in att["rejected"] if config.is_infra_path(p, cfg, cwd)),
                        "rejected_request_paths_total": len(att["rejected"]),
                        "ignored_noise_paths": len(att["noise"])},
        "notes": notes,
        "request_data_class": req.get("data_class"),
        "content_policy": "stat summaries only, no file contents" if sensitive else "redacted content diffs",
    })
    return ev


def add_probe(request_id: str, name: str, args: List[str], cfg: Optional[Mapping[str, str]] = None, runner=None,
              root=None) -> Tuple[int, Optional[Path]]:
    cfg = cfg or config.load_config()
    root = Path(root) if root else config.review_dir(cfg, create=True)
    req = q.read_request(request_id, root)
    rc, text = probe.run_probe(name, args, cfg, runner)
    if rc == probe.EX_USAGE:
        sys.stderr.write(text)
        return rc, None
    ev = q.evidence_dir(request_id, root, create=True)
    pdir = q.ensure_dir(ev / "probes")
    n = 1
    while (pdir / f"{name}-{n}.txt").exists():
        n += 1
    path = _write(pdir / f"{name}-{n}.txt", text)
    try:
        man = json.loads((ev / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        man = {}
    data_class = effective_class(req, cfg, (man.get("attribution") or {}).get("agent_paths") or ())
    if man.get("data_class") == "sensitive":  # never relax what collect() decided
        data_class = "sensitive"
    pit = dict(man.get("point_in_time") or {})
    pit[f"probes/{path.name}"] = {"observed_at": q.utc_now_iso(),
                                  "note": "probe output when it ran, NOT the state during the session"}
    _write_manifest(ev, req, data_class, {"point_in_time": pit})
    return rc, path


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    rid = argv[0]
    if not q.REQUEST_ID_RE.match(rid):
        print(f"collect: bad request id {rid!r}", file=sys.stderr)
        return 2
    try:
        if len(argv) >= 3 and argv[1] == "--probe":
            rc, path = add_probe(rid, argv[2], argv[3:])
            if path:
                print(path)
            return rc
        if len(argv) != 1:
            print("usage: collect.py <request-id> [--probe <name> [args...]]", file=sys.stderr)
            return 2
        print(collect(rid))
        return 0
    except FileNotFoundError as e:
        print(f"collect: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
