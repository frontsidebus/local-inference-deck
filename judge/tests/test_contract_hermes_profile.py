"""C8 consumer (agent-judge): judge/install.sh merges the judge's hooks block idempotently into a Hermes config
that already holds the deck's clients block, and never touches that block.

Reads the vendored fixtures/contracts/hermes-profile.yaml (a copy of the deck's contracts/hermes-profile.yaml;
refresh with `python3 contracts/tools/contracts.py vendor --write` in the deck repo). Runs a throwaway copy of
install.sh with a temp HOME, HERMES_HOME, judge.env, review dir and site.env; needs a python3 with PyYAML.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
JUDGE = Path(__file__).resolve().parent.parent
C8 = yaml.safe_load((Path(__file__).resolve().parent / "fixtures" / "contracts" / "hermes-profile.yaml").read_text())
CLIENTS_EX = C8["blocks"]["clients"]["example"]
JUDGE_EX = C8["blocks"]["judge"]["example"]
MARK = C8["blocks"]["judge"]["marker"]
PERSONAL = {"agent": {"max_turns": 90}, "hooks_auto_accept": False,
            "hooks": {"pre_tool_call": [{"matcher": "terminal", "command": "~/.hermes/agent-hooks/mine.sh"}]}}


@pytest.fixture
def e(tmp_path):
    j = tmp_path / "checkout" / "judge"
    (j / "hooks").mkdir(parents=True)
    shutil.copy2(JUDGE / "install.sh", j / "install.sh")
    shutil.copytree(JUDGE / "lib", j / "lib", ignore=shutil.ignore_patterns("__pycache__"))
    for h in ("gate", "verify", "enqueue", "inject"):
        (j / "hooks" / f"{h}.py").write_text("import sys; sys.stdin.read(); print('{}')\n")
    (j / "policy").mkdir()
    (j / "policy" / "gate-policy.json.tmpl").write_text('{"version": 1}\n')
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    (tmp_path / "site.env").write_text("SPARK_DOMAIN=example.com\nSPARK_API_HOST=api.example.com\n")
    cfg = home / ".hermes" / "config.yaml"
    cfg.write_text(yaml.safe_dump({**CLIENTS_EX, **PERSONAL}, sort_keys=False))

    def run(*args):
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL")}
        env.update(HOME=str(home), PYTHONDONTWRITEBYTECODE="1", JUDGE_ENV=str(tmp_path / "judge.env"))
        r = subprocess.run(["bash", str(j / "install.sh"), "--hermes-home", str(home / ".hermes"),
                            "--hermes-python", sys.executable, "--site-env", str(tmp_path / "site.env"),
                            "--review-dir", str(tmp_path / "review"), *args],
                           capture_output=True, text=True, timeout=120, env=env)
        assert r.returncode == 0, r.stdout + r.stderr
        return r.stdout
    return {"cfg": cfg, "run": run}


def _clients_block(data):
    return {"model": data["model"], "security": data["security"],
            "custom_providers": [p for p in data["custom_providers"] if p.get("name") == "spark"]}


def test_contract_version_is_accepted():
    assert C8["contract"] == "hermes-profile" and C8["contract_version"].startswith("1.")


def test_apply_keeps_the_clients_block_and_is_idempotent(e):
    e["run"]("--apply")
    first = e["cfg"].read_bytes()
    data = yaml.safe_load(first)
    assert _clients_block(data) == CLIENTS_EX
    assert data["agent"] == PERSONAL["agent"] and data["hooks_auto_accept"] is False
    assert data["hooks"]["pre_tool_call"][0] == PERSONAL["hooks"]["pre_tool_call"][0]
    for event, want in JUDGE_EX["hooks"].items():
        got = [x for x in data["hooks"][event] if x.get("managed_by") == MARK["managed_by"]]
        assert len(got) == 1, event
        assert {k: v for k, v in got[0].items() if k != "command"} == \
            {k: v for k, v in want[0].items() if k != "command"}, event
        assert got[0]["command"].endswith("/judge/hooks/" + want[0]["command"].rsplit("/", 1)[1]), event
    out = e["run"]("--apply")
    assert "no change needed" in out and e["cfg"].read_bytes() == first


def test_uninstall_removes_only_the_judge_block(e):
    e["run"]("--apply")
    e["run"]("--uninstall")
    data = yaml.safe_load(e["cfg"].read_text())
    assert _clients_block(data) == CLIENTS_EX
    managed = [x for v in (data.get("hooks") or {}).values() if isinstance(v, list) for x in v
               if isinstance(x, dict) and x.get("managed_by") == MARK["managed_by"]]
    assert managed == [] and data["hooks"]["pre_tool_call"] == PERSONAL["hooks"]["pre_tool_call"]
