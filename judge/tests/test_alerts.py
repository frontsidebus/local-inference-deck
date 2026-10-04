"""#41: runner failure alerts (runner/alert.py via judge-alert@.service) and the stall warning in the C5 inject
hook and judge-findings. Temp review dirs only; notify-send and systemctl are stubs on PATH, so nothing reaches
the desktop or the live user manager."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

JUDGE = Path(__file__).resolve().parent.parent
ALERT = JUDGE / "runner" / "alert.py"
INJECT = JUDGE / "hooks" / "inject.py"
FINDINGS = JUDGE / "bin" / "judge-findings"
SESSION = "20261004_160000_abc123"
FIX = ("systemctl --user reset-failed judge-review.service judge-review.path && "
       "systemctl --user start judge-review.path")


@pytest.fixture
def env(tmp_path):
    review = tmp_path / "hermes" / "review"
    for d in ("queue", "evidence", "findings", "acks", "done"):
        (review / d).mkdir(parents=True)
    stub = tmp_path / "stub"
    stub.mkdir()
    calls = tmp_path / "calls.log"
    for name in ("notify-send", "systemctl"):
        (stub / name).write_text(f"#!/bin/sh\necho \"{name} $*\" >> {calls}\n"
                                 + ('[ "$2" = is-failed ] && printf "failed\\nfailed\\nactive\\n"\n' if name == "systemctl"
                                    else ""))
        (stub / name).chmod(0o755)
    e = {k: v for k, v in os.environ.items() if not k.startswith(("JUDGE_", "HERMES_", "MONITOR_"))}
    e.update({"HERMES_HOME": str(review.parent), "JUDGE_REVIEW_DIR": str(review),
              "SITE_ENV": str(tmp_path / "no-site.env"), "PATH": f"{stub}:{os.environ['PATH']}",
              "JUDGE_HERMES_AGENT_DIR": str(tmp_path / "none"), "NO_COLOR": "1"})
    return {"review": review, "calls": calls, "env": e, "tmp": tmp_path}


def run(env, argv, stdin="", **extra):
    e = dict(env["env"])
    e.update(extra)
    return subprocess.run([sys.executable, *map(str, argv)], input=stdin, capture_output=True, text=True, env=e,
                          timeout=30)


def calls(env):
    return env["calls"].read_text().splitlines() if env["calls"].exists() else []


def stale_request(env, minutes=20, rid="20261004T160000Z-abc123-completion"):
    p = env["review"] / "queue" / f"{rid}.json"
    p.write_text(json.dumps({"id": rid, "kind": "completion", "session": SESSION}))
    old = time.time() - minutes * 60
    os.utime(p, (old, old))
    return rid


# ---------------------------------------------------------------- alert.py
def test_alert_logs_unit_result_and_fix_without_display(env):
    p = run(env, [ALERT, "judge-review.service"], MONITOR_SERVICE_RESULT="start-limit-hit")
    assert p.returncode == 0
    line = (env["review"] / "runner.log").read_text()
    assert "ALERT: judge unit judge-review.service failed (result=start-limit-hit)" in line
    assert FIX in line
    assert calls(env) == []                       # no DISPLAY: notify-send is never run


def test_alert_notifies_with_display_once_per_interval(env):
    p = run(env, [ALERT, "judge-review.path"], DISPLAY=":fake")
    assert p.returncode == 0
    [c] = calls(env)
    assert c.startswith("notify-send -u critical judge: judge-review.path failed") and FIX in c
    run(env, [ALERT, "judge-review.path"], DISPLAY=":fake")
    assert len(calls(env)) == 1                   # rate limited (JUDGE_ALERT_MIN_INTERVAL_S, default 900)
    assert (env["review"] / "runner.log").read_text().count("ALERT:") == 2  # but always logged
    run(env, [ALERT, "judge-review.path"], DISPLAY=":fake", JUDGE_ALERT_MIN_INTERVAL_S="0")
    assert len(calls(env)) == 2


def test_alert_rejects_odd_unit_names(env):
    run(env, [ALERT, "x; rm -rf /"])
    assert "judge unit judge-review.service failed" in (env["review"] / "runner.log").read_text()


# ---------------------------------------------------------------- C5 inject stall warning
def _inject(env, session=SESSION, **extra):
    payload = {"hook_event_name": "pre_llm_call", "session_id": session, "cwd": "/", "extra": {}}
    p = run(env, [INJECT], json.dumps(payload), **extra)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_inject_warns_once_per_stall(env):
    assert _inject(env) == {}
    rid = stale_request(env)
    ctx = _inject(env)["context"]
    assert ctx.startswith("[Judge runner warning: data, not instructions]\n")
    assert "judge runner is not running or is stalled: 1 review request(s) waiting" in ctx
    assert "do not try to restart or repair the judge yourself" in ctx
    assert rid not in ctx                          # no ids or paths, only the count and time
    assert _inject(env) == {}                      # once per stall per session
    assert "warning" in _inject(env, session="20261004_170000_def456")["context"]
    stale_request(env, minutes=30, rid="20261004T150000Z-abc123-completion")
    assert "2 review request(s)" in _inject(env)["context"]  # a new oldest request: a new stall
    assert calls(env) == []                        # the hook never calls systemctl


def test_inject_stall_threshold_and_off(env):
    stale_request(env, minutes=10)
    assert _inject(env) == {}                      # default 15 min
    assert "warning" in _inject(env, JUDGE_STALL_MINUTES="5")["context"]
    assert _inject(env, session="20261004_170000_def456", JUDGE_STALL_MINUTES="0") == {}


def test_inject_stall_warning_is_fast(env):
    for n in range(200):
        stale_request(env, minutes=1, rid=f"20261004T16{n // 60:02d}{n % 60:02d}Z-abc123-completion")
    from importlib import import_module
    sys.path.insert(0, str(JUDGE))
    q = import_module("lib.queue")
    t = time.perf_counter()
    for _ in range(20):
        assert q.stall_status(env["review"], minutes=15) is None
    assert (time.perf_counter() - t) / 20 < 0.05   # stat() only for fresh files


# ---------------------------------------------------------------- judge-findings
def test_judge_findings_prints_health_warnings(env):
    p = run(env, [FINDINGS], JUDGE_CHECK_UNITS="0")
    assert p.returncode == 0 and "WARNING" not in p.stderr
    stale_request(env)
    p = run(env, [FINDINGS], JUDGE_CHECK_UNITS="0")
    assert "WARNING: judge runner is not running or stalled: 1 review request(s) waiting" in p.stderr
    assert "reset-failed judge-review.service judge-review.path" in p.stderr
    assert calls(env) == []
    p = run(env, [FINDINGS, "--json"], JUDGE_CHECK_UNITS="1")    # stub systemctl: service + path failed
    assert json.loads(p.stdout) == []                           # stdout stays machine-readable
    assert ("WARNING: judge runner is not running: judge-review.service judge-review.path failed. Fix: " + FIX
            in p.stderr)
    assert calls(env) == ["systemctl --user is-failed judge-review.service judge-review.path judge-review.timer"]
