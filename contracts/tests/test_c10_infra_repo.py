"""C10 consumer (deck): this repo is a valid agent-judge infra repo (JUDGE_REPO_DIR), per the vendored
contracts/vendored/agent-judge/infra-repo.json (source: judge/contracts/infra-repo.json; agent-judge's provider
test is judge/tests/test_contract_infra_repo.py). The sanitizer's exit codes 0/1/2 are tested by
scripts/tests/test_check_sanitized.py.
Run: python3 -m pytest contracts/tests -q -p no:cacheprovider
"""
import os
import subprocess

import pytest

from contract_helpers import CONTRACTS, REPO, load_json

C10 = load_json(CONTRACTS / "vendored" / "agent-judge" / "infra-repo.json")


def test_contract_version_is_accepted():
    assert C10["contract"] == "infra-repo" and C10["contract_version"].startswith("1.")


@pytest.mark.parametrize("rel", C10["repo_root_files"] + C10["resolved_under_judge_repo_dir"])
def test_required_files_are_at_the_root(rel):
    assert (REPO / rel).is_file(), rel


@pytest.mark.parametrize("rel", C10["executable"])
def test_required_scripts_are_executable_in_git_and_on_disk(rel):
    assert os.access(REPO / rel, os.X_OK), rel
    r = subprocess.run(["git", "ls-files", "-s", "--", rel], cwd=REPO, capture_output=True, text=True, timeout=30)
    if r.returncode != 0 or not r.stdout:
        pytest.skip("not a git checkout")
    assert r.stdout.split()[0] == "100755", rel

