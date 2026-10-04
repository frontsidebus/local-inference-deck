#!/usr/bin/env python3
"""judge/runner/alert.py <failed-unit>: alert that a judge systemd unit failed (#41). Stdlib only.

Run by judge-alert@.service, which judge-review.service and judge-review.path start through OnFailure=.
  - Always appends one line to $JUDGE_REVIEW_DIR/runner.log: the failed unit, systemd's result
    ($MONITOR_SERVICE_RESULT / $MONITOR_EXIT_STATUS, set by systemd for OnFailure= units) and the fix command.
  - Runs `notify-send` when DISPLAY or WAYLAND_DISPLAY is set and notify-send exists, at most once per unit
    per JUDGE_ALERT_MIN_INTERVAL_S (default 900; state in $JUDGE_REVIEW_DIR/.alert-state.json), so a judge
    backend that keeps failing does not flood the desktop.
It never restarts, resets or stops anything: recovering the units is the human's decision.
Exit 0 always (an alert unit that fails would only add noise).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

JUDGE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(JUDGE_DIR / "runner"))

UNITS = "judge-review.service judge-review.path"
FIX = f"systemctl --user reset-failed {UNITS} && systemctl --user start judge-review.path"
STATE = ".alert-state.json"
_UNIT_RE = re.compile(r"^[A-Za-z0-9@_.:-]{1,128}$")


def fix_command() -> str:
    return FIX


def message(unit: str, env=None) -> str:
    env = os.environ if env is None else env
    bits = [f"{k}={env[v]}" for k, v in (("result", "MONITOR_SERVICE_RESULT"), ("exit", "MONITOR_EXIT_CODE"),
                                          ("status", "MONITOR_EXIT_STATUS")) if env.get(v)]
    detail = f" ({', '.join(bits)})" if bits else ""
    return (f"ALERT: judge unit {unit} failed{detail}. Reviews may not run (the gate still works, so sessions "
            f"look judged). Check: systemctl --user status {UNITS}; journalctl --user -u judge-review -n 50. "
            f"Fix: {FIX}")


def _due(review: Path, unit: str, now: float, min_interval: float) -> bool:
    """True when no desktop alert for *unit* went out in the last *min_interval* seconds (records this one)."""
    p = review / STATE
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    last = state.get(unit)
    if isinstance(last, (int, float)) and now - last < min_interval:
        return False
    state[unit] = now
    try:
        review.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, p)
    except OSError:
        pass
    return True


def notify(unit: str, text: str, review: Path, now: float = None) -> bool:
    """notify-send when a display is set (and not rate limited). True when it was run."""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) or not shutil.which("notify-send"):
        return False
    try:
        interval = float(os.environ.get("JUDGE_ALERT_MIN_INTERVAL_S") or 900)
    except ValueError:
        interval = 900.0
    if not _due(review, unit, time.time() if now is None else now, interval):
        return False
    try:
        subprocess.run(["notify-send", "-u", "critical", f"judge: {unit} failed", text], timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def main(argv) -> int:
    unit = argv[0] if argv else "judge-review.service"
    if not _UNIT_RE.match(unit):
        unit = "judge-review.service"
    try:
        import common as C
        text = message(unit)
        C.log_error("runner.log", text)
        print(text, file=sys.stderr)
        notify(unit, text, C.review_dir())
    except Exception as exc:  # never fail the alert unit
        print(f"judge alert: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
