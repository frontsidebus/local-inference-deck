"""C9 provider (agent-judge): run_judge.frontier_env strips exactly what judge/contracts/frontier-env.json lists.

sec-evals tests parity of its own list (evals/run.py, a harness file) against a vendored copy of the contract.
"""
import json
import sys
from pathlib import Path

import pytest

JUDGE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(JUDGE / "runner"))
import run_judge as RJ  # noqa: E402

CONTRACT = json.loads((JUDGE / "contracts" / "frontier-env.json").read_text())


def test_contract_header():
    assert CONTRACT["contract"] == "frontier-env" and CONTRACT["contract_version"].startswith("1.")


def test_lists_are_the_runners():
    assert list(RJ._ENDPOINT_VARS) == CONTRACT["endpoint_vars"]
    assert list(RJ._STRIP_VARS) == CONTRACT["endpoint_vars"] + CONTRACT["strip_vars"]


@pytest.mark.parametrize("vec", CONTRACT["vectors"], ids=lambda v: ",".join(sorted(v["env"])))
def test_frontier_env_vectors(vec, monkeypatch):
    monkeypatch.setattr(RJ.C, "setting", lambda k, d="": vec["spark_api_host"] if k == "SPARK_API_HOST" else d)
    assert sorted(RJ.frontier_env(dict(vec["env"]))) == sorted(vec["keeps"])
