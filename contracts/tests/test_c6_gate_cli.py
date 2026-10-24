"""C6 consumer (deck): bin/deck and the upgrade runbook use the gate's exit codes as sec-evals' gate-cli contract
defines them (vendored: contracts/vendored/sec-evals/gate-cli.json, source evals/contracts/gate-cli.json; provider
test: evals/tests/test_contract_gate_cli.py).
Run: python3 -m pytest contracts/tests -q -p no:cacheprovider
"""
import re

from contract_helpers import CONTRACTS, REPO, load_json

C6 = load_json(CONTRACTS / "vendored" / "sec-evals" / "gate-cli.json")
CODES = {int(k): v["name"] for k, v in C6["exit_codes"].items()}


def test_contract_version_is_accepted():
    assert C6["contract"] == "gate-cli" and C6["contract_version"].startswith("1.")
    assert CODES == {0: "PASS", 1: "FAIL", 2: "INCONCLUSIVE", 3: "INVALID", 4: "REFUSED"}


def test_deck_evals_gate_passes_the_exit_code_through():
    """`deck evals gate` execs gate.py (no wrapper exit code of its own) on the argv the C5 guard matches."""
    text = (REPO / "bin/deck").read_text()
    assert re.search(r"^\s*exec python3 -B evals/gate\.py ", text, re.M)


def test_upgrade_runbook_states_the_contract_codes():
    text = (REPO / "docs/runbooks/upgrade.md").read_text()
    for code, name in CODES.items():
        if name in ("PASS", "FAIL", "INCONCLUSIVE"):
            assert re.search(rf"\*\*{name}\*\* \(exit {code}\b", text), name
    assert "Exit 3 (invalid run) or 4 (refused)" in text
    assert "refuses (exit 4) if the GPU guard dies at start" in text
