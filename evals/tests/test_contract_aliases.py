"""C3 consumer (sec-evals): the gateway aliases this harness relies on are the deck's contract.

Reads the vendored fixtures/contracts/aliases.json (a copy of the deck's contracts/aliases.json; refresh with
`python3 contracts/tools/contracts.py vendor --write` in the deck repo) and run.py's constants. run.py,
comparisons-C1.json and the gate manifests are only READ here, never edited (run.py and comparisons-C1.json are
harness files). No network.
"""
import json
from pathlib import Path

import pytest

import run as RUN

FIX = Path(__file__).resolve().parent / "fixtures" / "contracts"
CONTRACT = json.loads((FIX / "aliases.json").read_text())
ALIASES = CONTRACT["aliases"]
PLANS = RUN.EVALS_DIR / "plans"
ACCEPTS = "1."


def test_contract_version_is_accepted():
    assert CONTRACT["contract"] == "gateway" and CONTRACT["contract_version"].startswith(ACCEPTS)


def test_swapping_aliases_are_the_ones_that_evict_the_coding_pair():
    assert RUN.SWAPPING_ALIASES == {n for n, a in ALIASES.items() if a["evicts_coding_pair"]}


def test_frontier_prefix_never_names_a_gateway_alias():
    """run.py sends any model starting with FRONTIER_PREFIX to the frontier CLI; the gateway answers claude-* with
    coder (its safety net). Neither may shadow a real alias."""
    assert CONTRACT["frontier_safety_net"].startswith(RUN.FRONTIER_PREFIX)
    assert not any(n.startswith(RUN.FRONTIER_PREFIX) for n in ALIASES)


def test_output_cap_covers_every_alias():
    assert RUN.HARD_MAX_TOKENS >= max(a["max_output_tokens"] for a in ALIASES.values())


@pytest.mark.parametrize("path", sorted(PLANS.glob("gate-v1-*.json")), ids=lambda p: p.name)
def test_gate_manifests_name_aliases(path):
    m = json.loads(path.read_text())
    if m.get("schema") != "gate-manifest-v1":
        pytest.skip("not a gate manifest")
    assert m["model"] in ALIASES


def test_c1_contestants_are_aliases_or_local_ids():
    contestants = json.loads((PLANS / "comparisons-C1.json").read_text())["contestants"]
    for c in contestants:
        assert c in ALIASES or c.startswith("local/"), c
        assert not c.startswith("local/" + CONTRACT["lab_prefix"]), c
