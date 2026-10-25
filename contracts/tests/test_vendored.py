"""Every vendored contract copy matches its source while both are in this repo, and every contract artifact
carries a contract_version (contracts/README.md). The sec-evals copies (its own repo since plan step 3) and the
agent-judge copies (since plan step 2) are compared with a checkout when one is present ($SEC_EVALS_DIR, else
~/src/deck/sec-evals; $AGENT_JUDGE_DIR, else ~/src/deck/agent-judge); fake checkouts below cover the comparison.
Run: python3 -m pytest contracts/tests -q -p no:cacheprovider
"""
import json
import re
import shutil

import pytest

from contract_helpers import REPO, TOOL, load_yaml


def test_vendored_copies_match_their_sources():
    assert TOOL.vendor_drift() == [], "refresh: python3 contracts/tools/contracts.py vendor --write"


def test_generated_files_are_current():
    assert TOOL.main(["aliases"]) == 0 and TOOL.main(["vectors"]) == 0


# The fixture template is a llama-swap config: it is versioned by the vectors generated from it (vectors.json).
UNVERSIONED = {"contracts/serving-identity/llama-swap.contract.yaml"}


VERSIONED = sorted(({s for s, _ in TOOL.VENDORED} | {s for s, _ in TOOL.SEC_EVALS_OUT + TOOL.AGENT_JUDGE_OUT})
                   - UNVERSIONED | {d for _, d in TOOL.SEC_EVALS_IN + TOOL.AGENT_JUDGE_IN if d.endswith(".json")})


@pytest.mark.parametrize("src", VERSIONED)
def test_every_contract_is_versioned(src):
    p = REPO / src
    data = json.loads(p.read_text()) if p.suffix == ".json" else load_yaml(p)
    assert re.fullmatch(r"\d+\.\d+", str(data["contract_version"])), src


def test_contracts_not_vendored_are_versioned_too():
    for p in (REPO / "contracts/edge.yaml", REPO / "contracts/gateway.yaml"):
        assert re.fullmatch(r"\d+\.\d+", str(load_yaml(p)["contract_version"])), p.name


def test_vendored_copies_are_outside_integrity_pinned_sets():
    """No copy sec-evals tests against may be an evals harness file (HARNESS_FILES): refreshing one must never force
    a re-baseline. Read from run.py's source in a sec-evals checkout, never imported for this."""
    se = TOOL.sec_evals_dir()
    if se is None:
        pytest.skip("no sec-evals checkout ($SEC_EVALS_DIR or ~/src/deck/sec-evals)")
    m = re.search(r"^HARNESS_FILES = \((.*?)\)$", (se / "evals/run.py").read_text(), re.S | re.M)
    harness = set(re.findall(r'"([^"]+)"', m.group(1))) if m else set()
    assert harness and not harness & {d for _, d in TOOL.SEC_EVALS_OUT} and not harness & {s for s, _ in TOOL.SEC_EVALS_IN}


def _fake_sec_evals(tmp_path):
    se = tmp_path / "sec-evals"
    (se / "evals").mkdir(parents=True)
    (se / "evals" / "run.py").write_text("# stub\n")
    for src, dst in TOOL.SEC_EVALS_IN:
        (se / src).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / dst, se / src)
    for src, dst in TOOL.SEC_EVALS_OUT:
        (se / dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / src, se / dst)
    return se


def test_sec_evals_copies_are_compared_with_a_checkout(tmp_path):
    se = _fake_sec_evals(tmp_path)
    assert TOOL.vendor_drift(sec_evals=se, agent_judge=False) == []
    (se / "evals/contracts/gate-cli.json").write_text("{}\n")                         # sec-evals changed its contract
    (se / "evals/tests/fixtures/contracts/aliases.json").unlink()                     # and lost a deck copy
    got = {(s, d, w) for s, d, w in TOOL.vendor_drift(sec_evals=se, agent_judge=False)}
    assert got == {("sec-evals:evals/contracts/gate-cli.json", "contracts/vendored/sec-evals/gate-cli.json", "copy differs"),
                   ("contracts/aliases.json", "sec-evals:evals/tests/fixtures/contracts/aliases.json", "copy missing")}


def test_sec_evals_checkout_is_found_through_SEC_EVALS_DIR(tmp_path, monkeypatch):
    se = _fake_sec_evals(tmp_path)
    monkeypatch.setenv("SEC_EVALS_DIR", str(se))
    assert TOOL.sec_evals_dir() == se
    monkeypatch.setenv("SEC_EVALS_DIR", str(tmp_path / "absent"))
    assert TOOL.sec_evals_dir() is None


def _fake_agent_judge(tmp_path):
    aj = tmp_path / "agent-judge"
    for src, dst in TOOL.AGENT_JUDGE_IN:
        (aj / src).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / dst, aj / src)
    for src, dst in TOOL.AGENT_JUDGE_OUT:
        (aj / dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / src, aj / dst)
    return aj


def test_agent_judge_copies_are_compared_with_a_checkout(tmp_path):
    aj = _fake_agent_judge(tmp_path)
    assert TOOL.vendor_drift(sec_evals=False, agent_judge=aj) == []
    (aj / "judge/contracts/infra-repo.json").write_text("{}\n")                        # agent-judge changed C10
    (aj / "judge/tests/fixtures/contracts/observability.yaml").unlink()                 # and lost a deck copy
    got = set(TOOL.vendor_drift(sec_evals=False, agent_judge=aj))
    assert got == {("agent-judge:judge/contracts/infra-repo.json", "contracts/vendored/agent-judge/infra-repo.json",
                    "copy differs"),
                   ("contracts/observability.yaml", "agent-judge:judge/tests/fixtures/contracts/observability.yaml",
                    "copy missing")}


def test_without_checkouts_the_deck_copies_only_have_to_exist():
    assert TOOL.vendor_drift(sec_evals=False, agent_judge=False) == []
    assert not (REPO / "judge" / "contracts").exists()      # the sources are no longer in this repo


def test_agent_judge_checkout_is_found_through_AGENT_JUDGE_DIR(tmp_path, monkeypatch):
    aj = _fake_agent_judge(tmp_path)
    monkeypatch.setenv("AGENT_JUDGE_DIR", str(aj))
    assert TOOL.agent_judge_dir() == aj
    monkeypatch.setenv("AGENT_JUDGE_DIR", str(tmp_path / "absent"))
    assert TOOL.agent_judge_dir() is None
