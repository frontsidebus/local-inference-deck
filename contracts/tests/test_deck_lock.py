"""deck.lock (plan §3): agent-judge is a pinned component whose requires_contracts name contracts this repo
publishes, at their current major version. Read with tools/site_keys.py's stdlib parser (the same constrained
YAML subset as contracts/site-keys.yaml). Offline; no checkout is needed.
Run: python3 -m pytest contracts/tests -q -p no:cacheprovider
"""
import importlib.util
import json

from contract_helpers import CONTRACTS, REPO, load_yaml

spec = importlib.util.spec_from_file_location("site_keys_for_lock", REPO / "tools" / "site_keys.py")
SK = importlib.util.module_from_spec(spec)
spec.loader.exec_module(SK)
LOCK = SK.load_registry(REPO / "deck.lock")

# contract name (as deck.lock spells it) -> the deck artifact that carries its contract_version
PUBLISHED = {"site-keys": CONTRACTS / "site-keys.yaml", "gateway": CONTRACTS / "gateway.yaml",
             "observability": CONTRACTS / "observability.yaml", "hermes-profile": CONTRACTS / "hermes-profile.yaml",
             "edge": CONTRACTS / "edge.yaml", "serving-identity": CONTRACTS / "serving-identity" / "vectors.json"}


def component(name):
    got = [c for c in LOCK["components"] if c["name"] == name]
    assert len(got) == 1, name
    return got[0]


def major(path):
    data = json.loads(path.read_text()) if path.suffix == ".json" else load_yaml(path)
    return str(data["contract_version"]).split(".")[0]


def test_lock_format():
    assert LOCK["lock_format"] == 1
    for c in LOCK["components"]:
        assert set(c) == {"name", "repo", "tag", "commit", "mode", "path", "requires_contracts"}, c["name"]
        assert c["mode"] in ("pinned", "minimum") and c["path"].startswith("~/src/deck/")


def test_agent_judge_is_pinned_at_its_stable_path():
    aj = component("agent-judge")
    assert aj["mode"] == "pinned" and aj["path"] == "~/src/deck/agent-judge"
    assert aj["commit"] is None or len(aj["commit"]) == 40


def test_required_contracts_exist_at_a_matching_major():
    for c in LOCK["components"]:
        for name, want in c["requires_contracts"].items():
            if name not in PUBLISHED:
                continue    # a contract the component publishes itself (for example sec-evals' gate-cli)
            assert major(PUBLISHED[name]) == str(want), (c["name"], name)
    assert set(component("agent-judge")["requires_contracts"]) <= set(PUBLISHED)


def test_no_site_values_in_the_lock():
    text = (REPO / "deck.lock").read_text()
    assert "/home/" not in text and "@" not in text
