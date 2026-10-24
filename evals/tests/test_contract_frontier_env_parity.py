"""C9 consumer (sec-evals): run.py's frontier strip list has parity with agent-judge's contract.

run.py keeps its own list (it is a harness file: read here, never edited). The contract is the vendored
fixtures/contracts/frontier-env.json (a copy of judge/contracts/frontier-env.json; refresh with
`python3 contracts/tools/contracts.py vendor --write` in the deck repo).
"""
import json
from pathlib import Path

import pytest

import run as RUN

CONTRACT = json.loads((Path(__file__).resolve().parent / "fixtures" / "contracts" / "frontier-env.json").read_text())


def test_contract_version_is_accepted():
    assert CONTRACT["contract"] == "frontier-env" and CONTRACT["contract_version"].startswith("1.")


def test_run_py_lists_have_parity():
    assert list(RUN._ENDPOINT_VARS) == CONTRACT["endpoint_vars"]
    assert list(RUN._STRIP_VARS) == CONTRACT["endpoint_vars"] + CONTRACT["strip_vars"]


@pytest.mark.parametrize("vec", CONTRACT["vectors"], ids=lambda v: ",".join(sorted(v["env"])))
def test_frontier_env_vectors(vec):
    assert sorted(RUN.frontier_env(vec["spark_api_host"], dict(vec["env"]))) == sorted(vec["keeps"])
