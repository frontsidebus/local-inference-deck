"""C5 provider (sec-evals): eval and gate runs show up the way evals/contracts/eval-liveness.json promises, so
the deck's live-run guard (clients/bin/spark-model, tested against a vendored copy in
contracts/tests/test_c5_eval_liveness.py) can see them.

Already covered elsewhere, not repeated here:
  - the guard-liveness rule (0a): test_gate.py test_guard_that_dies_at_start_refuses_the_run,
    test_live_guard_runs_with_the_gates_site_env_and_is_stopped, test_no_guard_is_the_only_bypass,
    test_guard_liveness_is_not_part_of_the_harness_signature;
  - site-env resolution for the guard and lib.sh: test_site_env.py.
No GPU, no ssh, no network.
"""
import fnmatch
import json
import os
import subprocess
from pathlib import Path

import gate as G

EVALS = Path(__file__).resolve().parents[1]
REPO = EVALS.parent
C5 = json.loads((EVALS / "contracts" / "eval-liveness.json").read_text())


def live(argv):
    """The contract's argv rule, as a consumer implements it."""
    return any(a.endswith(s) for a in argv[:C5["argv_window"]] for s in C5["argv_suffixes"])


def test_contract_header_and_examples():
    assert C5["contract"] == "eval-liveness" and C5["contract_version"].startswith("1.")
    assert all(live(a) for a in C5["examples"]["live_argv"])
    assert not any(live(a) for a in C5["examples"]["not_live_argv"])


def test_entry_points_are_at_the_frozen_paths():
    for s in C5["argv_suffixes"]:
        assert (REPO / s).is_file(), s
    assert G.GUARD.relative_to(REPO).as_posix() == "evals/tools/gpu-guard.sh"


def test_gate_starts_run_py_on_the_contract_command_line(monkeypatch):
    seen = {}

    def call(argv, cwd=None):
        seen.update(argv=argv, cwd=cwd)
        return 0
    monkeypatch.setattr(G.subprocess, "call", call)
    assert G._subprocess_runner(["--run-name", "gate"]) == 0
    assert seen["argv"][:3] == ["python3", "-B", "evals/run.py"] and seen["cwd"] == str(REPO)
    assert live(seen["argv"])


def test_gate_starts_the_guard_on_the_contract_command_line(monkeypatch, tmp_path):
    import argparse
    seen = {}

    class P:
        def __init__(self, argv, **kw):
            seen["argv"] = argv

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(seen["argv"], timeout)
    monkeypatch.setattr(G.subprocess, "Popen", P)
    monkeypatch.setenv("EVAL_GPU_SSH", "backend-alias")
    G.start_guard(argparse.Namespace(no_guard=False, site_env=str(tmp_path / "site.env")), tmp_path)
    assert seen["argv"] == ["bash", str(G.GUARD)] and live(seen["argv"])


def test_plans_and_lib_sh_use_the_contract_command_line():
    lib = (EVALS / "tools" / "lib.sh").read_text()
    assert 'RUN_PATTERN="^python3 -B evals/run.py ' in lib
    for plan in sorted((EVALS / "plans").glob("*.sh")):
        text = plan.read_text()
        if "evals/run.py" in text:
            assert "PY=(python3 -B evals/run.py)" in text, plan.name


def _state_dir(tmp_path, run):
    r = subprocess.run(["bash", "-c", '. "$1"; printf %s "$EVAL_STATE_DIR"', "x", str(EVALS / "tools" / "lib.sh")],
                       env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "EVAL_RUN": run,
                            "REPO": str(tmp_path)}, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return Path(r.stdout)


def test_pid_files_land_under_the_contract_glob(tmp_path, monkeypatch):
    state = _state_dir(tmp_path, "evalb")
    tunnel_pid = (EVALS / "tools" / "gateway-tunnel.sh").read_text()
    assert 'PIDF="$EVAL_STATE_DIR/gateway-tunnel.pid"' in tunnel_pid
    rel = (state / "gateway-tunnel.pid").relative_to(tmp_path).as_posix()
    assert fnmatch.fnmatch(rel, C5["pid_glob"]), rel
    monkeypatch.delenv("EVAL_STATE_DIR", raising=False)
    rel = (G._state_dir() / "x.pid").relative_to(REPO).as_posix()
    assert fnmatch.fnmatch(rel, C5["pid_glob"]), rel
