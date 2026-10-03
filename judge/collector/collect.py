#!/usr/bin/env python3
"""judge/collector/collect.py: build the evidence bundle for a review request (deterministic, read-only).

Usage:
    collect.py <request-id>                         build evidence/<request-id>/
    collect.py <request-id> --probe <name> [args]   run one allowlisted probe, save it as
                                                    evidence/<request-id>/probes/<name>-<n>.txt, update manifest

Bundle (judge/CONTRACT.md):
    manifest.json        request copy, artifact list, collector version, data_class, window, attribution,
                         point_in_time (what was observed at collection time), notes
    hermes-log.txt       agent.log + errors.log lines in the window (this session + untagged), redacted
    gate-decisions.jsonl gate.log lines of this session in the window (+ a gate request's own decision), redacted,
                         each with `outcome` executed | not_executed | unknown (matched against events.jsonl)
    agent-diff.patch     watched paths vs the session-start snapshot, only paths the agent touched
                         (+ repo changes since the start HEAD for those paths)
    others-changed.txt   snapshot changes NOT attributed to the agent: paths + diffstat, never content
    host-<name>.txt      walter/covenant: UTC `find -newermt <since> ! -newermt <until>` over /etc /srv /usr/local
                         + `systemctl --failed` (at collection time)
    slots.json           llama-server slot summary (probe `slots`; at collection time)
    probes/              probe outputs (at the time they ran)
Window: [request.since, request.created + JUDGE_WINDOW_GRACE_SECONDS] for the log, the host find and the gate
decisions, no matter when the bundle is collected.
data_class=sensitive (from the request, or from re-classifying the agent-attributed paths: the stricter wins):
agent diff is a stat summary only (no file contents).  Hosts that cannot be reached are recorded, never fatal.

Exit: 0 bundle written, 2 request not found / bad id, 64 bad probe (nothing executed).
"""
from __future__ import annotations

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
def window(req: Dict, cfg: Mapping[str, str]) -> Tuple[datetime, datetime]:
    """[request.since, request.created + JUDGE_WINDOW_GRACE_SECONDS] (UTC). Independent of when the bundle is
    collected, so a later turn's evidence cannot leak into an earlier request's bundle."""
    since = q.parse_utc(req["since"])
    until = q.parse_utc(req["created"]) + timedelta(seconds=config.window_grace(cfg))
    return since, max(until, since)


# ---------------------------------------------------------------- pieces
def hermes_log(req: Dict, cfg: Mapping[str, str], since: datetime, until: datetime) -> str:
    tz = hermeslog.log_tz(cfg)
    out = [f"# Hermes log lines for session {req['session']} (plus untagged lines), "
           f"window {q.utc_now_iso(since)} .. {q.utc_now_iso(until)} UTC; log tz {tz}; secrets redacted"]
    for name in ("agent.log", "errors.log"):
        p = Path(cfg["HERMES_HOME"]) / "logs" / name
        out.append(f"\n===== {name} =====")
        if not p.is_file():
            out.append("(missing)")
            continue
        lines = hermeslog.session_lines(p, req["session"], since, until, tz)
        if len(lines) > LOG_MAX_LINES:
            out.append(f"# {len(lines) - LOG_MAX_LINES} earlier lines omitted (showing the last {LOG_MAX_LINES})")
            lines = lines[-LOG_MAX_LINES:]
        out.append("\n".join(redact(ln[:4000]) for ln in lines) if lines else "(no lines in window)")
    return "\n".join(out) + "\n"


GATE_MATCH_SECONDS = 600       # an escalated call that has not run this long after its decision never ran
NOT_RUN_STATUSES = {"blocked", "denied", "rejected", "cancelled", "canceled", "not_approved"}


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
    for ev in snapshot.events(d):
        try:
            ev["_t"] = q.parse_utc(str(ev.get("t") or ""))
        except ValueError:
            continue
        if ev["_t"] <= ev_end and str(ev.get("status") or "").lower() not in NOT_RUN_STATUSES:
            evs.append(ev)
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

    agent:  changed paths named by the session's tool events up to the window end, plus the request's own
            changed_paths (already attributed by the hook that wrote it)
    others: changed paths that no agent tool event of the session names, last modified by the window end
    later:  changed paths the agent touched only after the window, or others' changes after it (omitted)"""
    res: Dict[str, List[str]] = {"agent": [], "others": [], "later": []}
    d = q.snapshot_dir(req["session"], root)
    meta = snapshot.load_meta(d)
    if not meta:
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
    keys |= {snapshot._key(p) for p in req.get("changed_paths") or [] if isinstance(p, str) and p}
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
               until: datetime) -> str:
    d = q.snapshot_dir(req["session"], root)
    text = snapshot.diff_text(d, cfg, sensitive=sensitive, include=paths, until=until)
    if snapshot.load_meta(d) and not paths:
        text += "# no changed path is attributed to the agent (other changes, if any: others-changed.txt)\n"
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
        out.append(f"# {withheld} non-infra path(s) withheld (data_class=infra bundle)")
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
          f"limited to what the ssh user can read; first {FIND_MAX} paths\n"
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


def effective_class(req: Dict, cfg: Mapping[str, str], agent_paths=()) -> str:
    """The stricter of the request's own data_class and the collector's independent classification of the
    agent-attributed paths (request changed_paths + *agent_paths*).

    With no paths, only a host-rule gate request keeps the hook's answer (its evidence is the redacted
    command). Any other path-less request is classified from the session cwd (classify([], cfg, cwd)), so
    an `infra` label alone never lets a request reach the frontier judge."""
    cwd = (req.get("detail") or {}).get("cwd") or None
    paths = sorted(set(req.get("changed_paths") or []) | set(agent_paths or []))
    if paths:
        mine = config.classify(paths, cfg, cwd)
    elif _host_rule_gate(req):
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
    since, until = window(req, cfg)
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
    agent_paths = sorted(set(att["agent"]) | set(req.get("changed_paths") or []))
    _write(ev / "agent-diff.patch", agent_diff(req, cfg, sensitive, root, agent_paths, until))
    _write(ev / "others-changed.txt", others_changed(req, cfg, root, att, sensitive, until))
    for host in config.HOSTS:
        _write(ev / f"host-{host}.txt", host_diff(host, req, cfg, runner, since_s, until_s))
    try:
        slots = probe.slots_summary(cfg, runner=runner)
    except ValueError as e:
        slots = {"error": str(e)}
    _write(ev / "slots.json", redact(json.dumps(slots, indent=2)) + "\n")
    pit = {"observed_at": now_s, "note": POINT_IN_TIME}
    _write_manifest(ev, req, data_class, {
        "collected": now_s,
        "window": {"since": since_s, "until": until_s, "grace_seconds": config.window_grace(cfg)},
        "windowed": ["hermes-log.txt", "gate-decisions.jsonl"] + [f"host-{h}.txt (find)" for h in config.HOSTS],
        "point_in_time": {
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
                               if config.is_infra_path(p, cfg, (req.get("detail") or {}).get("cwd") or None)),
                        "omitted_after_window": len(att["later"])},
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
