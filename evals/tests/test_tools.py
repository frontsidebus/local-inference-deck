"""Tests for evals/tools (GPU guard, stop, status) and evals/plans/run-b-day1.sh, with a stub repo, a stub
evals/run.py and a fake ssh: no GPUs, gateway or network needed."""
import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

EVALS = Path(__file__).resolve().parent.parent
TOOLS = EVALS / "tools"
PLAN = EVALS / "plans" / "run-b-day1.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("pgrep") is None,
                                reason="needs bash and procps")

STUB_RUN = textwrap.dedent("""\
    import os, signal, sys, time
    log = os.environ["STUB_LOG"]
    with open(log, "a") as fh:
        fh.write(" ".join(sys.argv[1:]) + "\\n")
    def on_int(*_):
        open(log + ".sigint", "a").write(" ".join(sys.argv[1:]) + "\\n")
        sys.exit(130)
    signal.signal(signal.SIGINT, on_int)
    time.sleep(float(os.environ.get("STUB_SLEEP", "0.05")))
    sys.exit(130 if os.environ.get("STUB_FAILON", "\\0") in " ".join(sys.argv) else 0)
""")


@pytest.fixture
def stub_repo(tmp_path):
    """A fake repo: the real tools and plan, a stub run.py/report.py, and empty data files."""
    repo = tmp_path / "repo"
    (repo / "evals" / "data").mkdir(parents=True)
    shutil.copytree(TOOLS, repo / "evals" / "tools")
    shutil.copytree(PLAN.parent, repo / "evals" / "plans")
    (repo / "evals" / "results").mkdir()
    for name in ("run.py", "report.py"):
        (repo / "evals" / name).write_text(STUB_RUN)
    for stem in ["ctibench-mcq", "ctibench-rcm", "ctibench-vsp", "nvd-cwe", "nvd-cvss", "cse-frr", "nvd-cwe-nvdlab",
                 "nvd-cvss-nvdlab"]:
        for suffix in (".sample200", ".sample200in100"):
            (repo / "evals" / "data" / f"{stem}{suffix}.jsonl").write_text("")
    for stem in ["cybermetric-500", "cybermetric-500.sample500in100", "ctibench-ate", "sevenllm-mcq.sample100",
                 "sevenllm-qa.sample100"]:
        (repo / "evals" / "data" / f"{stem}.jsonl").write_text("")
    return repo


def env_for(repo, tmp_path, **extra):
    e = dict(os.environ, REPO=str(repo), EVAL_RUN="evaltest", EVAL_STATE_DIR=str(tmp_path / "state"),
             STUB_LOG=str(tmp_path / "calls.log"), NO_GUARD="1", SITE_ENV=str(tmp_path / "none.env"),
             EVAL_GATEWAY="edge")
    # Never reach the real desktop: unsetting DBUS_SESSION_BUS_ADDRESS is not enough (notify-send falls back to
    # $XDG_RUNTIME_DIR/bus), so a stub notify-send that records its arguments goes first on PATH.
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "notify-send"
    stub.write_text(f'#!/bin/sh\necho "$@" >> {tmp_path / "notify.log"}\n')
    stub.chmod(0o755)
    e["PATH"] = f"{bindir}:{e['PATH']}"
    e.update({k: str(v) for k, v in extra.items()})
    return e


def calls(tmp_path):
    p = tmp_path / "calls.log"
    return p.read_text().splitlines() if p.exists() else []


def test_plan_runs_phases_in_order(stub_repo, tmp_path):
    r = subprocess.run(["bash", str(stub_repo / "evals/plans/run-b-day1.sh")], env=env_for(stub_repo, tmp_path),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    c = calls(tmp_path)
    assert len(c) == 12
    assert "--model big" in c[0] and "--suite ctibench-mcq.sample200in100" in c[0] and "--run-name evaltest " in c[0] + " "
    assert "--model big" in c[1] and "--run-name evaltest-qa" in c[1] and "--no-grade" in c[1]
    middle = c[2:10]
    assert sum("--model coder " in x + " " for x in middle) == 3 and sum("--model coder-fast" in x for x in middle) == 5
    assert any("--thinking on" in x and "--seed 1236" in x and "coder-fast" in x for x in middle)
    assert "--rescore --grader-model big" in c[10] and "--model big,coder,coder-fast" in c[10]
    assert "--split-label-source" in c[11] and "evaltest-think-s1234-coder-think" in c[11]
    state = tmp_path / "state"
    steps = [l.split("\t")[0] for l in (state / "steps.tsv").read_text().splitlines()]
    assert steps[0] == "big-off" and steps[-1] == "report" and len(steps) == 12
    assert all((state / f"{s}.status").read_text().strip() == "0" for s in steps)
    out = subprocess.run(["bash", str(stub_repo / "evals/tools/status.sh")], env=env_for(stub_repo, tmp_path),
                         capture_output=True, text=True, timeout=30).stdout
    assert "big-off" in out and "exit 0" in out and "pending" not in out
    notes = (tmp_path / "notify.log").read_text()
    assert "Eval evaltest started" in notes and "Eval evaltest complete" in notes


def test_plan_stops_after_a_failed_step_and_refuses_over_stop(stub_repo, tmp_path):
    env = env_for(stub_repo, tmp_path, STUB_FAILON="--seed 1235")
    r = subprocess.run(["bash", str(stub_repo / "evals/plans/run-b-day1.sh")], env=env, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 1
    assert not any("--rescore" in x for x in calls(tmp_path))  # no grading after a failed phase 2
    assert "stopped: phase 2" in (tmp_path / "state" / "launch.log").read_text()
    (tmp_path / "state" / "STOP").write_text("now\nmanual\n")
    r = subprocess.run(["bash", str(stub_repo / "evals/plans/run-b-day1.sh")], env=env_for(stub_repo, tmp_path),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 1 and "STOP file present" in r.stderr


def test_plan_refuses_without_data(stub_repo, tmp_path):
    (stub_repo / "evals/data/nvd-cwe-nvdlab.sample200.jsonl").unlink()
    r = subprocess.run(["bash", str(stub_repo / "evals/plans/run-b-day1.sh")], env=env_for(stub_repo, tmp_path),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "nvd-cwe-nvdlab.sample200" in r.stderr and "prepare" in r.stderr
    assert not calls(tmp_path)


def _spawn_runner(repo, tmp_path, run_name):
    """A stub run.py with the plan's exact command line, sleeping until signalled."""
    env = env_for(repo, tmp_path, STUB_SLEEP=60)
    return subprocess.Popen(["python3", "-B", "evals/run.py", "--suite", "x", "--run-name", run_name],
                            cwd=repo, env=env)


def test_stop_signals_only_this_runs_runner(stub_repo, tmp_path):
    mine = [_spawn_runner(stub_repo, tmp_path, n) for n in ("evaltest", "evaltest-think-s1")]
    other = _spawn_runner(stub_repo, tmp_path, "evaltestX")      # a different run-name prefix
    decoy = subprocess.Popen(["bash", "-c", "sleep 60 # python3 -B evals/run.py --run-name evaltest"])
    time.sleep(0.5)
    try:
        r = subprocess.run(["bash", str(stub_repo / "evals/tools/stop.sh"), "test stop"],
                           env=env_for(stub_repo, tmp_path), capture_output=True, text=True, timeout=90)
        assert r.returncode == 0 and "stopped" in r.stdout
        for p in mine:
            assert p.wait(timeout=10) == 130
        assert other.poll() is None and decoy.poll() is None
        assert (tmp_path / "state" / "STOP").read_text().splitlines()[1] == "test stop"
    finally:
        for p in [other, decoy] + mine:
            if p.poll() is None:
                p.kill()


def _fake_ssh(tmp_path, temp=50, reasons="0x0000000000000001", power="100.00", fan="40", fail=False):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    smi = textwrap.dedent(f"""\
        0, {temp}, {power}, 350.00, {reasons}, {fan}, 90, 20000, 1800
        1, 50, 100.00, 350.00, 0x0000000000000000, 40, 90, 20000, 1800
        --
        GPU 00000000:01:00.0
                SW Power Capping                  : 100 us
                SW Thermal Slowdown               : 0 us
                HW Thermal Slowdown               : 0 us
                HW Power Braking                  : 0 us
        GPU 00000000:03:00.0
                SW Power Capping                  : 100 us
                SW Thermal Slowdown               : 0 us
                HW Thermal Slowdown               : 0 us
                HW Power Braking                  : 0 us
        """)
    (tmp_path / "smi.txt").write_text(smi)
    body = "exit 255" if fail else f"cat {tmp_path / 'smi.txt'}"
    (bindir / "ssh").write_text(f"#!/bin/sh\n{body}\n")
    (bindir / "ssh").chmod(0o755)
    return bindir


def _guard(repo, tmp_path, bindir, timeout=30, **extra):
    env = env_for(repo, tmp_path, EVAL_GPU_SSH="gpu-host", INTERVAL="0.2", **extra)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    return subprocess.run(["bash", str(repo / "evals/tools/gpu-guard.sh")], env=env, capture_output=True,
                          text=True, timeout=timeout)


@pytest.mark.parametrize("kw,reason", [
    ({"temp": 90}, "GPU0 at 90 C"),
    ({"reasons": "0x0000000000000080"}, "hardware protection"),
    ({"power": "400.00"}, "over its 350.00 W limit"),
    ({"temp": 80, "fan": "0"}, "fan reads 0%"),
])
def test_guard_stops_the_run(stub_repo, tmp_path, kw, reason):
    runner = _spawn_runner(stub_repo, tmp_path, "evaltest")
    time.sleep(0.3)
    try:
        r = _guard(stub_repo, tmp_path, _fake_ssh(tmp_path, **kw))
        assert r.returncode == 0, r.stderr
        assert runner.wait(timeout=10) == 130
        stop = (tmp_path / "state" / "STOP").read_text()
        assert reason in stop
        rows = (tmp_path / "state" / "gpu.csv").read_text().splitlines()
        assert rows[0].startswith("ts,gpu,temp_c") and len(rows) >= 2
    finally:
        if runner.poll() is None:
            runner.kill()


def test_guard_fails_safe_when_it_cannot_read_the_gpus(stub_repo, tmp_path):
    runner = _spawn_runner(stub_repo, tmp_path, "evaltest")
    time.sleep(0.3)
    try:
        r = _guard(stub_repo, tmp_path, _fake_ssh(tmp_path, fail=True), STALE_S=1)
        assert r.returncode == 0
        assert runner.wait(timeout=10) == 130
        assert "no GPU reading" in (tmp_path / "state" / "STOP").read_text()
    finally:
        if runner.poll() is None:
            runner.kill()


def test_guard_dry_run_and_cool_gpus_never_stop(stub_repo, tmp_path):
    runner = _spawn_runner(stub_repo, tmp_path, "evaltest")
    time.sleep(0.3)
    try:
        with pytest.raises(subprocess.TimeoutExpired):  # cool cards: keeps polling
            _guard(stub_repo, tmp_path, _fake_ssh(tmp_path), timeout=2)
        with pytest.raises(subprocess.TimeoutExpired):  # hot cards, DRY_RUN: logs but never stops
            _guard(stub_repo, tmp_path, _fake_ssh(tmp_path, temp=95), timeout=2, DRY_RUN=1)
        assert runner.poll() is None and not (tmp_path / "state" / "STOP").exists()
        assert "DRY_RUN=1: not stopping" in (tmp_path / "state" / "gpu-guard.log").read_text()
    finally:
        runner.kill()


def test_guard_reads_the_host_from_site_env(stub_repo, tmp_path):
    site = tmp_path / "site.env"
    site.write_text("BACKEND_SSH_USER=op   # user\nBACKEND_LAN_IP=192.0.2.10\n")
    bindir = _fake_ssh(tmp_path, temp=95)
    (bindir / "ssh").write_text(f"#!/bin/sh\necho \"$@\" > {tmp_path / 'ssh-args'}\ncat {tmp_path / 'smi.txt'}\n")
    env = env_for(stub_repo, tmp_path, INTERVAL="0.2", SITE_ENV=str(site))
    env["PATH"] = f"{bindir}:{env['PATH']}"
    r = subprocess.run(["bash", str(stub_repo / "evals/tools/gpu-guard.sh")], env=env, capture_output=True,
                       text=True, timeout=30)
    assert r.returncode == 0
    assert "op@192.0.2.10" in (tmp_path / "ssh-args").read_text()


def test_day2_plan_runs_hermes_then_vision_then_grades_and_reports_both_days(stub_repo, tmp_path):
    results = stub_repo / "evals" / "results"
    for d in ("evalb-big", "evalb-coder", "evalb-coder-fast"):   # day 1 dirs the report should include
        (results / d).mkdir()
        (results / d / "run.json").write_text("{}")
    env = env_for(stub_repo, tmp_path, EVAL_RUN="evaltest2", DAY1_RUN="evalb")
    r = subprocess.run(["bash", str(stub_repo / "evals/plans/run-b-day2.sh")], env=env, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    c = calls(tmp_path)
    assert [("hermes" in x, "vision" in x) for x in c[:4]] == [(True, False)] * 2 + [(False, True)] * 2
    assert "--suite ctibench-mcq.sample200in100" in c[0] and "--run-name evaltest2 " in c[0] + " "
    assert "--rescore --grader-model big" in c[4] and "--model hermes,vision" in c[4]
    assert "evals/results/evalb-big" in c[5] and "evals/results/evalb-coder-fast" in c[5]
    assert "evaltest2-hermes" not in c[5]   # the stub made no results dirs for day 2, so none are listed


def _fake_tunnel_ssh(tmp_path):
    """An ssh stand-in that listens on the -L local port (the forward), so the tunnel tool sees it come up."""
    bindir = tmp_path / "tbin"
    bindir.mkdir(exist_ok=True)
    (bindir / "ssh").write_text(textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import socket, sys
        open({str(tmp_path / 'ssh-args')!r}, "a").write(" ".join(sys.argv[1:]) + "\\n")
        spec = sys.argv[sys.argv.index("-L") + 1]
        port = int(spec.split(":")[1])
        s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port)); s.listen()
        while True:
            c, _ = s.accept(); c.close()
        """))
    (bindir / "ssh").chmod(0o755)
    return bindir


def test_gateway_tunnel_starts_restarts_and_stops(stub_repo, tmp_path):
    site = tmp_path / "site.env"
    site.write_text("BACKEND_SSH_USER=op\nBACKEND_LAN_IP=192.0.2.10\nBACKEND_WG_IP=198.51.100.2\nLITELLM_PORT=4000\n")
    env = env_for(stub_repo, tmp_path, SITE_ENV=str(site), EVAL_TUNNEL_PORT="14999")
    env["PATH"] = f"{_fake_tunnel_ssh(tmp_path)}:{env['PATH']}"
    tool = str(stub_repo / "evals/tools/gateway-tunnel.sh")
    r = subprocess.run(["bash", tool, "start"], env=env, capture_output=True, text=True, timeout=30)
    try:
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "EVAL_BASE_URL=http://127.0.0.1:14999/v1"
        args = (tmp_path / "ssh-args").read_text()
        assert "-L 127.0.0.1:14999:198.51.100.2:4000 op@192.0.2.10" in args and "ExitOnForwardFailure=yes" in args
        # a dropped connection is re-established by the supervisor
        fake = f"^python3 {tmp_path / 'tbin' / 'ssh'} "   # anchored: never match a shell that mentions the path
        subprocess.run(["pkill", "-f", fake], check=False)
        deadline = time.time() + 15
        while time.time() < deadline and (tmp_path / "ssh-args").read_text().count("\n") < 2:
            time.sleep(0.2)
        assert (tmp_path / "ssh-args").read_text().count("\n") >= 2
        assert "restarting" in (tmp_path / "state" / "gateway-tunnel.log").read_text()
    finally:
        subprocess.run(["bash", tool, "stop"], env=env, timeout=30)
    time.sleep(0.5)
    assert subprocess.run(["pgrep", "-f", f"^python3 {tmp_path / 'tbin' / 'ssh'} "], capture_output=True).returncode == 1
    assert not (tmp_path / "state" / "gateway-tunnel.pid").exists()
