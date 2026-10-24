"""C10 provider (agent-judge): the verify hook and the probes require of an infra repo exactly what
judge/contracts/infra-repo.json says. The deck (and every other repo in JUDGE_INFRA_REPOS) tests that it complies,
against a vendored copy (deck: contracts/tests/test_c10_infra_repo.py).

Already covered elsewhere, not repeated here: the sanitizer exit-code handling (test_verify.py
test_sanitizer_*), and that check_sanitized / render_and_diff resolve under JUDGE_REPO_DIR only
(test_probes.py test_check_sanitized_uses_judge_repo_script, test_render_and_diff; test_boundary_0a.py).
"""
import importlib.util
import json
import re
from pathlib import Path

import pytest

JUDGE = Path(__file__).resolve().parent.parent
CONTRACT = json.loads((JUDGE / "contracts" / "infra-repo.json").read_text())


@pytest.fixture
def v(monkeypatch):
    spec = importlib.util.spec_from_file_location("judge_verify_contract", JUDGE / "hooks" / "verify.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_import", lambda name: None)
    return mod


def _repo(root: Path, files=CONTRACT["repo_root_files"]):
    for f in files:
        p = root / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("#!/usr/bin/env bash\nexit 0\n" if f.endswith(".sh") else "# conventions\n")
        if f in CONTRACT["executable"]:
            p.chmod(0o755)
    return root


def test_contract_header():
    assert CONTRACT["contract"] == "infra-repo" and CONTRACT["contract_version"].startswith("1.")
    assert set(CONTRACT["executable"]) <= set(CONTRACT["repo_root_files"])


def test_find_repo_needs_every_root_file(v, tmp_path):
    full = _repo(tmp_path / "deck")
    (full / "walter").mkdir()
    assert v.find_repo(str(full / "walter" / "x.conf")) == str(full)
    for missing in CONTRACT["repo_root_files"]:
        part = _repo(tmp_path / f"part-{missing.replace('/', '-')}",
                     [f for f in CONTRACT["repo_root_files"] if f != missing])
        assert v.find_repo(str(part / "x.conf")) is None, missing


def test_multi_repo_layout_each_repo_is_its_own_root(v, tmp_path):
    """Sibling checkouts (deck, agent-judge, sec-evals) are found separately; a nested path finds the nearest."""
    roots = [_repo(tmp_path / "src" / n) for n in ("local-inference-deck", "agent-judge", "sec-evals")]
    for r in roots:
        (r / "sub").mkdir()
        assert v.find_repo(str(r / "sub" / "f.py")) == str(r)
    assert v.find_repo(str(tmp_path / "src" / "f.py")) is None


def test_sanitizer_invocation_is_the_contracts(v):
    src = (JUDGE / "hooks" / "verify.py").read_text()
    assert 'run(["bash", "scripts/check-sanitized.sh"], budget, cwd=root' in src
    assert CONTRACT["sanitizer"]["invocation"].startswith("bash scripts/check-sanitized.sh")


def test_probes_resolve_the_listed_scripts_under_judge_repo_dir():
    src = (JUDGE / "probes" / "probe.py").read_text()
    for rel in CONTRACT["resolved_under_judge_repo_dir"]:
        d, f = rel.split("/")
        assert re.search(rf'os\.path\.join\(repo, "{d}", "{re.escape(f)}"\)', src), rel
