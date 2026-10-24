"""C3 consumer (agent-judge): every gateway alias the judge names by default is in the deck's gateway contract.

Reads the vendored fixtures/contracts/aliases.json (a copy of the deck's contracts/aliases.json; refresh with
`python3 contracts/tools/contracts.py vendor --write` in the deck repo).
"""
import json
import re
from pathlib import Path

from lib import config

JUDGE = Path(__file__).resolve().parent.parent
CONTRACT = json.loads((Path(__file__).resolve().parent / "fixtures" / "contracts" / "aliases.json").read_text())
ALIASES = CONTRACT["aliases"]


def test_contract_version_is_accepted():
    assert CONTRACT["contract"] == "gateway" and CONTRACT["contract_version"].startswith("1.")


def test_default_local_judge_model_is_an_alias():
    assert config.DEFAULTS["JUDGE_LOCAL_MODEL"] in ALIASES
    assert re.search(r'C\.setting\("JUDGE_LOCAL_MODEL", "([^"]+)"\)',
                     (JUDGE / "runner" / "run_judge.py").read_text())[1] in ALIASES
    assert re.search(r'"\$\{JUDGE_LOCAL_MODEL:=([^}]+)\}"', (JUDGE / "install.sh").read_text())[1] in ALIASES


def test_documented_judge_models_are_aliases():
    for f in ("judge.env.example", "README.md"):
        for m in re.findall(r"^#?\s*JUDGE_LOCAL_MODEL=([\w-]+)", (JUDGE / f).read_text(), re.M):
            assert m in ALIASES, (f, m)
