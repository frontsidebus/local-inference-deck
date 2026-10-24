"""C5 consumer (deck): clients/bin/spark-model's live-run guard sees eval and gate runs the way sec-evals'
eval-liveness contract describes them (vendored: contracts/vendored/sec-evals/eval-liveness.json, source
evals/contracts/eval-liveness.json; provider test: evals/tests/test_contract_liveness.py).

The fake-pid behaviour itself is walter/tests/test_spark_model_client.py test_live_run_detection; this test drives
the same function from the contract's examples and rules instead of hand-written ones.
Run: python3 -m pytest contracts/tests -q -p no:cacheprovider
"""
import importlib.machinery
import importlib.util
import os

import pytest

from contract_helpers import CONTRACTS, REPO, load_json

C5 = load_json(CONTRACTS / "vendored" / "sec-evals" / "eval-liveness.json")


@pytest.fixture
def client():
    loader = importlib.machinery.SourceFileLoader("spark_model_c5", str(REPO / "clients/bin/spark-model"))
    spec = importlib.util.spec_from_loader("spark_model_c5", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_contract_version_is_accepted():
    assert C5["contract"] == "eval-liveness" and C5["contract_version"].startswith("1.")


def test_argv_examples(client, monkeypatch):
    live, dead = C5["examples"]["live_argv"], C5["examples"]["not_live_argv"]
    monkeypatch.setattr(client, "_cmdlines", lambda: iter([(100 + i, a) for i, a in enumerate(live + dead)]))
    found = client.live_runs(repos=[])
    assert [f.split(":")[0] for f in found] == [f"pid {100 + i}" for i in range(len(live))]


def test_argv_window(client, monkeypatch):
    w = C5["argv_window"]
    pad = ["x"] * (w - 1)
    monkeypatch.setattr(client, "_cmdlines", lambda: iter([(1, pad + [C5["argv_suffixes"][0]]),
                                                             (2, pad + ["y", C5["argv_suffixes"][0]])]))
    assert [f.split(":")[0] for f in client.live_runs(repos=[])] == ["pid 1"]


def _plant(root, pid):
    d = root / os.path.dirname(C5["pid_glob"]).replace("*", "gate")
    d.mkdir(parents=True)
    (d / "guard.pid").write_text(f"{pid} extra tokens\n")
    return d / "guard.pid"


def test_pid_layout_and_repos_env(client, monkeypatch, tmp_path):
    monkeypatch.setattr(client, "_cmdlines", lambda: iter([]))
    a, b = tmp_path / "sec-evals", tmp_path / "other"
    pa, pb = _plant(a, os.getpid()), _plant(b, os.getpid())
    monkeypatch.setenv(C5["repos_env"], C5["repos_separator"].join([str(a), str(b)]))
    monkeypatch.setattr(client, "REPO", tmp_path / "deck")
    found = client.live_runs()
    assert found == [f"{pa} (pid {os.getpid()} alive)", f"{pb} (pid {os.getpid()} alive)"]
