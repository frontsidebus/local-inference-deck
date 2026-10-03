#!/usr/bin/env python3
"""judge/collector/collect.py: build the evidence bundle for a review request (deterministic, read-only).

Usage:
    collect.py <request-id>                         build evidence/<request-id>/
    collect.py <request-id> --probe <name> [args]   run one allowlisted probe, save it as
                                                    evidence/<request-id>/probes/<name>-<n>.txt, update manifest

Bundle (judge/CONTRACT.md):
    manifest.json      request copy, artifact list, collector version, data_class, window
    hermes-log.txt     agent.log + errors.log lines for the session window (this session + untagged), redacted
    local-diff.patch   watched paths vs the session-start snapshot (+ repo changes since the start HEAD)
    host-<name>.txt    walter/covenant: UTC `find -newermt` over /etc /srv /usr/local + `systemctl --failed`
    slots.json         llama-server slot summary (probe `slots`)
    probes/            probe outputs
data_class=sensitive (from the request, or from re-classifying changed_paths: the stricter wins): local diff
is a stat summary only (no file contents).  Hosts that cannot be reached are recorded, never fatal.

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

COLLECTOR_VERSION = "1"
HOST_TIMEOUT = 45
LOG_MAX_LINES = 3000
FIND_MAX = 500
ISO_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _write(path: Path, text: str) -> Path:
    return q.atomic_write(path, text)


# ---------------------------------------------------------------- pieces
def hermes_log(req: Dict, cfg: Mapping[str, str], until: datetime) -> str:
    since = q.parse_utc(req["since"])
    tz = hermeslog.log_tz(cfg)
    out = [f"# Hermes log lines for session {req['session']} (plus untagged lines), "
           f"window {req['since']} .. {q.utc_now_iso(until)} UTC; log tz {tz}; secrets redacted"]
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


def local_diff(req: Dict, cfg: Mapping[str, str], sensitive: bool, root: Path) -> str:
    d = q.snapshot_dir(req["session"], root)
    text = snapshot.diff_text(d, cfg, sensitive=sensitive)
    return text if sensitive else redact(text)


def host_command(since: str) -> str:
    """Remote, read-only. *since* must be a strict `YYYY-MM-DDTHH:MM:SSZ` (validated; no injection)."""
    if not ISO_Z_RE.match(since):
        raise ValueError(f"bad since: {since!r}")
    stamp = since.replace("T", " ").replace("Z", " UTC")
    return (
        f"echo '# files changed since {since} (UTC; owner mode mtime-UTC path; sorted by mtime)'; "
        f"L=$(TZ=UTC find /etc /srv /usr/local -xdev -newermt {shlex.quote(stamp)} "
        "\\( -type f -o -type l \\) -printf '%u %m %TY-%Tm-%TdT%TT %p\\n' 2>/dev/null); "
        "echo \"# $(printf '%s\\n' \"$L\" | grep -c .) path(s)\"; "
        f"printf '%s\\n' \"$L\" | sort -k3 | head -n {FIND_MAX}; "
        "echo; echo '# systemctl --failed'; systemctl --failed --no-legend --plain --no-pager 2>&1; "
        "echo; echo '# host clock (UTC)'; date -u +%Y-%m-%dT%H:%M:%SZ"
    )


def host_diff(name: str, req: Dict, cfg: Mapping[str, str], runner) -> str:
    hdr = f"# host {name}: changes since {req['since']} (UTC); read-only; find over /etc /srv /usr/local is " \
          f"limited to what the ssh user can read; first {FIND_MAX} paths\n"
    try:
        argv = config.host_ssh(name, cfg) + [host_command(req["since"])]
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


def effective_class(req: Dict, cfg: Mapping[str, str]) -> str:
    cwd = (req.get("detail") or {}).get("cwd") or None
    mine = config.classify(req.get("changed_paths") or [], cfg, cwd)
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
    until = max(now, q.parse_utc(req["created"]) + timedelta(minutes=2))
    data_class = effective_class(req, cfg)
    sensitive = data_class == "sensitive"
    ev = q.evidence_dir(request_id, root, create=True)
    q.ensure_dir(ev / "probes")

    _write(ev / "hermes-log.txt", hermes_log(req, cfg, until))
    _write(ev / "local-diff.patch", local_diff(req, cfg, sensitive, root))
    for host in config.HOSTS:
        _write(ev / f"host-{host}.txt", host_diff(host, req, cfg, runner))
    try:
        slots = probe.slots_summary(cfg, runner=runner)
    except ValueError as e:
        slots = {"error": str(e)}
    _write(ev / "slots.json", redact(json.dumps(slots, indent=2)) + "\n")
    _write_manifest(ev, req, data_class, {
        "collected": q.utc_now_iso(now),
        "window": {"since": req["since"], "until": q.utc_now_iso(until)},
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
    _write_manifest(ev, req, effective_class(req, cfg))
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
