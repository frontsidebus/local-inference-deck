#!/usr/bin/env python3
"""judge/collector/extras.py: extra evidence sources for the collector (stdlib only).

    c3_results(session, since, until, root) -> list[dict]
        Lines of $JUDGE_REVIEW_DIR/snapshots/<session>/c3-results.jsonl (written by hooks/verify.py, one per
        verifier run) whose `t` is in [since, until], oldest first. Each gains `final`: true on the latest
        line of its (path, check) pair within the window (that is the state the turn ended with), false on
        the others. Malformed lines are skipped.

    host_state_probes(req, cfg, runner=None, root=None, gate_lines=None) -> dict[str, str]
        Read-only probe outputs for host-state claims ("nginx is active", "reload completed", "Grafana is up
        on 127.0.0.1:3001"), chosen from the request's claims/plan/detail.excerpt and the excerpts of this
        session's gate.log decisions in the request window. {artifact_name: text}; at most
        MAX_HOST_PROBES (4) probe runs per request. Every probe goes through probes/probe.py (allowlist +
        argument validation + redaction); a probe the validator refuses is dropped. Never raises.

        Selection:
          units   `systemctl <verb> <unit>` / `service <unit> <verb>` anywhere in that text (`--user` units
                  skipped), and well-known units (KNOWN_UNITS) or `<name>.service` named in a claims sentence
                  with a state word (active, running, reload, restarted, up, failed, ...). Host: the one host
                  named in the same line/sentence (walter|covenant, their site.env IPs, `edge`, or a
                  JUDGE_SSH_ALIASES alias -> covenant); else KNOWN_UNITS; else the only host named anywhere
                  in the text; else the unit is skipped. Each unit costs two probes:
                  unit_state <host> <unit> (point in time) and unit_journal <host> <unit> <since> <until>
                  (windowed). Units seen in gate excerpts come first.
          ports   loopback 127.0.0.1:<port> / localhost:<port> / [::1]:<port> in that text ->
                  port_listening <host> <port> (point in time); host as above, default walter.
        Artifact names: unit_state-<host>-<unit>, unit_journal-<host>-<unit>, port_listening-<host>-<port>
        (the collector stores them as probes/host-<name>.txt). Disabled with JUDGE_HOST_PROBES=0.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

JUDGE_DIR = Path(__file__).resolve().parent.parent
if str(JUDGE_DIR) not in sys.path:
    sys.path.insert(0, str(JUDGE_DIR))

from lib import config  # noqa: E402
from lib import queue as q  # noqa: E402

if str(JUDGE_DIR / "probes") not in sys.path:
    sys.path.insert(0, str(JUDGE_DIR / "probes"))
import probe  # noqa: E402

C3_RESULTS = "c3-results.jsonl"
MAX_HOST_PROBES = 4

# Units with a fixed home in this repo (walter/, covenant/). Others need a host named next to them.
KNOWN_UNITS: Dict[str, str] = {
    "nginx": "covenant", "oauth2-proxy": "covenant", "fail2ban": "covenant",
    "llama-swap": "walter", "docker": "walter", "docker-user-rules": "walter", "spark-backup": "walter",
    "spark-update-check": "walter", "nvidia-persistenced": "walter",
}

_UNIT_CHARS = r"[A-Za-z0-9][A-Za-z0-9@._-]*"
_SYSTEMCTL_RE = re.compile(
    r"\bsystemctl\s+((?:-{1,2}[\w-]+(?:=\S+)?\s+)*)"
    r"(start|stop|restart|reload|reload-or-restart|try-restart|try-reload-or-restart|status|is-active|"
    r"is-failed|enable|disable|show)\s+((?:-{1,2}[\w-]+\s+)*)(" + _UNIT_CHARS + r")")
_SERVICE_RE = re.compile(r"\bservice\s+(" + _UNIT_CHARS + r")\s+(start|stop|restart|reload|force-reload|status)\b")
_DOT_SERVICE_RE = re.compile(r"\b(" + _UNIT_CHARS + r"\.(?:service|timer|socket))\b")
_STATE_WORDS_RE = re.compile(r"(?i)\b(active|inactive|running|reload(?:ed|s)?|restart(?:ed|s)?|start(?:ed|s)?|"
                             r"stopp?(?:ed|s)?|up|down|failed|enabled|disabled|healthy|completed?)\b")
_LOOPBACK_RE = re.compile(r"(?<![\w.])(?:127\.0\.0\.1|localhost|\[::1\])\s*:\s*([0-9]{1,5})(?![0-9])")
_SPLIT_RE = re.compile(r"\n+|(?<=[.!?])\s+")


# ---------------------------------------------------------------- C3 results
def _parse_t(text) -> Optional[datetime]:
    try:
        return q.parse_utc(str(text))
    except (ValueError, TypeError):
        return None


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def c3_results(session: str, since: datetime, until: datetime, root: Path) -> List[Dict]:
    """C3 verifier results of *session* with `t` in [since, until]; latest per (path, check) has final=True."""
    try:
        path = q.snapshot_dir(session, root) / C3_RESULTS
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, ValueError):
        return []
    since, until = _aware(since), _aware(until)
    rows: List[Tuple[datetime, int, Dict]] = []
    for n, line in enumerate(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or not isinstance(rec.get("path"), str) or not isinstance(rec.get("check"), str):
            continue
        t = _parse_t(rec.get("t"))
        if t is None or not since <= t <= until:
            continue
        rows.append((t, n, rec))
    rows.sort(key=lambda r: (r[0], r[1]))
    latest: Dict[Tuple[str, str], int] = {}
    for i, (_, _, rec) in enumerate(rows):
        latest[(rec["path"], rec["check"])] = i  # later t (then later line) wins
    final_idx = set(latest.values())
    out = []
    for i, (_, _, rec) in enumerate(rows):
        r = dict(rec)
        r["final"] = i in final_idx
        out.append(r)
    return out


# ---------------------------------------------------------------- host-state probes
def _window(req: Mapping, cfg: Mapping[str, str]) -> Tuple[datetime, datetime]:
    since = q.parse_utc(str(req["since"]))
    until = q.parse_utc(str(req["created"])) + timedelta(seconds=config.window_grace(cfg))
    return since, max(since, until)


def _gate_excerpts(req: Mapping, root: Path, since: datetime, until: datetime) -> List[str]:
    """Excerpts of this session's gate.log decisions in [since, until] (plus a gate request's own)."""
    own = f"{req.get('id')}.json" if req.get("kind") == "gate" else None
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
        if not isinstance(rec, dict) or not isinstance(rec.get("excerpt"), str):
            continue
        mine = own is not None and os.path.basename(str(rec.get("request") or "")) == own
        if not mine:
            if str(rec.get("session") or "") != str(req.get("session") or ""):
                continue
            t = _parse_t(rec.get("ts"))
            if t is None or not since <= t <= until:
                continue
        out.append(rec["excerpt"])
    return out


def _host_tokens(cfg: Mapping[str, str]) -> Dict[str, List[str]]:
    walter = ["walter"] + [cfg.get(k) or "" for k in ("BACKEND_LAN_IP", "BACKEND_WG_IP")]
    covenant = (["covenant", "edge"] + [cfg.get(k) or "" for k in ("EDGE_PUBLIC_IP", "EDGE_WG_IP")]
                + (cfg.get("JUDGE_SSH_ALIASES") or "").split())
    return {"walter": [t for t in walter if t], "covenant": [t for t in covenant if t]}


def _hosts_in(text: str, tokens: Mapping[str, List[str]]) -> List[str]:
    found = []
    for host, toks in tokens.items():
        if any(re.search(r"(?<![\w.-])" + re.escape(t) + r"(?![\w-]|\.\w)", text, re.I) for t in toks):
            found.append(host)
    return found


def _unit_base(unit: str) -> str:
    return re.sub(r"\.(service|timer|socket)$", "", unit)


def _units_in(segment: str, claims_sentence: bool) -> List[Tuple[str, str]]:
    """[(unit, reason)] named in one line/sentence."""
    out = []
    for m in _SYSTEMCTL_RE.finditer(segment):
        if "--user" in (m.group(1) + m.group(3)):
            continue
        unit = m.group(4).rstrip(".")
        out.append((unit, f"systemctl {m.group(2)} {unit}"))
    for m in _SERVICE_RE.finditer(segment):
        unit = m.group(1).rstrip(".")
        out.append((unit, f"service {unit} {m.group(2)}"))
    if claims_sentence and _STATE_WORDS_RE.search(segment):
        for m in _DOT_SERVICE_RE.finditer(segment):
            out.append((m.group(1), f"{m.group(1)} named with a state word in the claims"))
        for name in KNOWN_UNITS:
            if re.search(r"(?<![\w.@-])" + re.escape(name) + r"(?![\w@-]|\.\w)", segment):
                out.append((name, f"{name} named with a state word in the claims"))
    return out


def _segments(text: str) -> List[str]:
    return [s for s in _SPLIT_RE.split(text or "") if s.strip()]


def plan_host_probes(req: Mapping, cfg: Mapping[str, str],
                     gate_lines: Iterable[str] = ()) -> List[Tuple[str, List[str], str]]:
    """[(probe name, args, reason)] chosen for *req*; at most MAX_HOST_PROBES. Pure (no I/O)."""
    since, until = _window(req, cfg)
    since_s, until_s = q.utc_now_iso(since), q.utc_now_iso(until)
    tokens = _host_tokens(cfg)
    detail = req.get("detail") if isinstance(req.get("detail"), dict) else {}
    gate_text = [str(x) for x in gate_lines if x]
    if isinstance(detail.get("excerpt"), str):
        gate_text.append(detail["excerpt"])
    claims_text = "\n".join(str(x) for x in (req.get("claims"), req.get("plan")) if isinstance(x, str))
    everything = "\n".join(gate_text + [claims_text])
    global_hosts = _hosts_in(everything, tokens)

    # (segment, is_claims) in priority order: gate excerpts (actions that were attempted) first
    segs = [(s, False) for g in gate_text for s in g.splitlines() if s.strip()]
    segs += [(s, True) for s in _segments(claims_text)]

    def pick_host(seg: str, unit: Optional[str], default: Optional[str]) -> Optional[str]:
        here = _hosts_in(seg, tokens)
        if len(here) == 1:
            return here[0]
        if unit and _unit_base(unit) in KNOWN_UNITS:
            return KNOWN_UNITS[_unit_base(unit)]
        if len(global_hosts) == 1:
            return global_hosts[0]
        return default

    units: List[Tuple[str, str, str]] = []
    seen_units = set()
    ports: List[Tuple[str, str, str]] = []
    seen_ports = set()
    for seg, is_claims in segs:
        for unit, reason in _units_in(seg, is_claims):
            host = pick_host(seg, unit, None)
            key = (host, _unit_base(unit))
            if host is None or key in seen_units:
                continue
            seen_units.add(key)
            units.append((host, unit, reason))
        for m in _LOOPBACK_RE.finditer(seg):
            port = m.group(1)
            host = pick_host(seg, None, "walter")
            if host is None or (host, port) in seen_ports or not 0 < int(port) < 65536:
                continue
            seen_ports.add((host, port))
            where = "claims" if is_claims else "gate excerpt"
            ports.append((host, port, f"loopback port {port} named in the {where}"))

    planned: List[Tuple[str, List[str], str]] = []
    for host, unit, reason in units:
        planned.append(("unit_state", [host, unit], reason))
        planned.append(("unit_journal", [host, unit, since_s, until_s], reason))
    for host, port, reason in ports:
        planned.append(("port_listening", [host, port], reason))
    return planned[:MAX_HOST_PROBES]


_NOTES = {
    "unit_state": "POINT IN TIME: unit state when the evidence was collected, NOT during the session",
    "unit_journal": "WINDOWED: the unit's journal lines within the request window (UTC)",
    "port_listening": "POINT IN TIME: listening sockets when the evidence was collected, NOT during the session",
}


def _artifact_name(name: str, args: List[str]) -> str:
    return "-".join([name] + [re.sub(r"[^A-Za-z0-9@._-]", "_", a) for a in args[:2]])


def host_state_probes(req: Mapping, cfg: Mapping[str, str], runner=None, root=None,
                      gate_lines: Optional[Iterable[str]] = None) -> Dict[str, str]:
    """Run the probes chosen by plan_host_probes; {artifact_name: text}. Never raises."""
    out: Dict[str, str] = {}
    try:
        flag = os.environ.get("JUDGE_HOST_PROBES") or cfg.get("JUDGE_HOST_PROBES") or "1"
        if flag.strip() in ("0", "false", "no", "off"):
            return out
        if gate_lines is None:
            since, until = _window(req, cfg)
            gate_lines = _gate_excerpts(req, Path(root) if root else config.review_dir(cfg), since, until)
        planned = plan_host_probes(req, cfg, gate_lines)
    except Exception as e:  # malformed request: no host probes, but say why
        return {"host_state_probes-error": f"# host-state probe selection failed: {type(e).__name__}\n"}
    for name, args, reason in planned:
        try:
            rc, text = probe.run_probe(name, args, cfg, runner)
        except Exception as e:
            rc, text = 1, f"probe {name} failed internally: {type(e).__name__}\n"
        if rc == probe.EX_USAGE:  # the allowlist refused the arguments: nothing ran, nothing to show
            continue
        out[_artifact_name(name, args)] = (f"# {_NOTES[name]}\n# selected because: {reason}\n" + text)
    return out
