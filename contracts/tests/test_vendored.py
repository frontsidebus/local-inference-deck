"""Every vendored contract copy matches its source while both are in this repo, and every contract artifact
carries a contract_version (contracts/README.md). After the split, `deck check --integration` makes the same
comparison across the fetched checkouts.
Run: python3 -m pytest contracts/tests -q -p no:cacheprovider
"""
import json
import re

import pytest

from contract_helpers import REPO, TOOL, load_yaml


def test_vendored_copies_match_their_sources():
    assert TOOL.vendor_drift() == [], "refresh: python3 contracts/tools/contracts.py vendor --write"


def test_generated_files_are_current():
    assert TOOL.main(["aliases"]) == 0 and TOOL.main(["vectors"]) == 0


# The fixture template is a llama-swap config: it is versioned by the vectors generated from it (vectors.json).
UNVERSIONED = {"contracts/serving-identity/llama-swap.contract.yaml"}


@pytest.mark.parametrize("src", sorted({s for s, _ in TOOL.VENDORED} - UNVERSIONED))
def test_every_contract_is_versioned(src):
    p = REPO / src
    data = json.loads(p.read_text()) if p.suffix == ".json" else load_yaml(p)
    assert re.fullmatch(r"\d+\.\d+", str(data["contract_version"])), src


def test_contracts_not_vendored_are_versioned_too():
    for p in (REPO / "contracts/edge.yaml", REPO / "contracts/gateway.yaml"):
        assert re.fullmatch(r"\d+\.\d+", str(load_yaml(p)["contract_version"])), p.name


def test_vendored_copies_are_outside_integrity_pinned_sets():
    """No vendored copy may be an evals harness file (HARNESS_FILES): refreshing one must never force a
    re-baseline. Read from run.py's source, never imported for this."""
    m = re.search(r"^HARNESS_FILES = \((.*?)\)$", (REPO / "evals/run.py").read_text(), re.S | re.M)
    if not m:
        pytest.skip("evals/run.py is not in this checkout")
    harness = set(re.findall(r'"([^"]+)"', m.group(1)))
    assert harness and not harness & {d for _, d in TOOL.VENDORED} and not harness & {s for s, _ in TOOL.VENDORED}
