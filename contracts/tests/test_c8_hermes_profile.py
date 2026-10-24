"""C8 provider (deck): clients/install.sh merges its Hermes block idempotently into a config that already holds
the judge's block (contracts/hermes-profile.yaml), and leaves that block alone; while judge/ is in this repo, both
real installers are run together, in both orders.

Every run uses a temp HOME (so $HOME/.hermes is a temp HERMES_HOME), a temp judge.env, review dir and site.env
copied from site.env.example. Nothing outside tmp_path is read for config or written. Needs bash, envsubst and a
python3 with PyYAML (both installers edit config.yaml with it).
Run: python3 -m pytest contracts/tests -q -p no:cacheprovider
"""
import os
import shutil
import subprocess
import sys

import pytest

from contract_helpers import CONTRACTS, EXAMPLE, REPO, load_yaml

yaml = pytest.importorskip("yaml")
C8 = load_yaml(CONTRACTS / "hermes-profile.yaml")
CLIENTS_EX = C8["blocks"]["clients"]["example"]
JUDGE_EX = C8["blocks"]["judge"]["example"]
MARK = C8["blocks"]["judge"]["marker"]
PERSONAL = {"model": {"default": "big", "personal": "kept"}, "agent": {"max_turns": 90},
            "custom_providers": [{"name": "other", "base_url": "https://other.example.org/v1"}],
            "hooks_auto_accept": False}
pytestmark = pytest.mark.skipif(shutil.which("envsubst") is None, reason="envsubst (gettext-base) is needed")


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".hermes").mkdir(parents=True)
    shutil.copyfile(EXAMPLE, tmp_path / "site.env")
    return h


def _env(home):
    env = {k: v for k, v in os.environ.items()
           if k in ("PATH", "LANG", "LC_ALL", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")}
    env.update(HOME=str(home), HERMES_HOME=str(home / ".hermes"), PYTHONDONTWRITEBYTECODE="1",
               JUDGE_ENV=str(home.parent / "judge.env"))
    return env


def clients_install(home):
    r = subprocess.run(["bash", str(REPO / "clients/install.sh"), "--force", "--api-host", "api.example.com",
                        "--site-env", str(home.parent / "site.env")],
                       capture_output=True, text=True, timeout=120, env=_env(home))
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def _judge_checkout(tmp):
    """A throwaway judge checkout: the real install.sh and lib/, stub hook scripts, a stub policy template."""
    j = tmp / "checkout" / "judge"
    if not j.is_dir():
        (j / "hooks").mkdir(parents=True)
        shutil.copy2(REPO / "judge/install.sh", j / "install.sh")
        shutil.copytree(REPO / "judge/lib", j / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        for h in ("gate", "verify", "enqueue", "inject"):
            (j / "hooks" / f"{h}.py").write_text("import sys; sys.stdin.read(); print('{}')\n")
        (j / "policy").mkdir()
        (j / "policy" / "gate-policy.json.tmpl").write_text('{"version": 1}\n')
    return j


def judge_install(home):
    j = _judge_checkout(home.parent)
    r = subprocess.run(["bash", str(j / "install.sh"), "--apply", "--hermes-home", str(home / ".hermes"),
                        "--hermes-python", sys.executable, "--site-env", str(home.parent / "site.env"),
                        "--review-dir", str(home.parent / "review")],
                       capture_output=True, text=True, timeout=120, env=_env(home))
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def _cfg(home):
    return home / ".hermes" / "config.yaml"


def assert_clients_block(data):
    for k in ("model", "security"):
        assert {**data[k], **CLIENTS_EX[k]} == data[k], k
    spark = [p for p in data["custom_providers"] if p.get("name") == "spark"]
    assert spark == CLIENTS_EX["custom_providers"]
    assert {"name": "other", "base_url": "https://other.example.org/v1"} in data["custom_providers"]
    assert data["agent"] == PERSONAL["agent"] and data["model"]["personal"] == "kept"
    assert data["hooks_auto_accept"] is False


def assert_judge_block(data):
    for event, want in JUDGE_EX["hooks"].items():
        got = [e for e in data["hooks"][event] if e.get("managed_by") == MARK["managed_by"]]
        assert len(got) == 1, event
        strip = lambda e: {k: v for k, v in e.items() if k != "command"}  # noqa: E731
        assert strip(got[0]) == strip(want[0]), event
        assert got[0]["command"].endswith(want[0]["command"].rsplit("/judge/", 1)[1]), event


def test_contract_header():
    assert C8["contract"] == "hermes-profile" and C8["contract_version"].startswith("1.")
    assert MARK == {"managed_by": "agent-judge"}


def test_clients_example_is_the_rendered_fragment():
    r = subprocess.run(["envsubst", "${SPARK_API_HOST}"], input=(REPO / "clients/hermes/config.spark.yaml.tmpl").read_text(),
                       capture_output=True, text=True, env={"SPARK_API_HOST": "api.example.com", "PATH": os.environ["PATH"]})
    assert yaml.safe_load(r.stdout) == CLIENTS_EX


def test_clients_install_keeps_the_judge_block_and_is_idempotent(home):
    _cfg(home).write_text(yaml.safe_dump({**PERSONAL, **JUDGE_EX}, sort_keys=False))
    clients_install(home)
    first = _cfg(home).read_bytes()
    data = yaml.safe_load(first)
    assert data["hooks"] == JUDGE_EX["hooks"]
    assert_clients_block(data)
    out = clients_install(home)
    assert "unchanged  " + str(_cfg(home)) in out and _cfg(home).read_bytes() == first


@pytest.mark.skipif(not (REPO / "judge/install.sh").is_file(), reason="judge/ is not in this checkout")
@pytest.mark.parametrize("order", [("clients", "judge"), ("judge", "clients")])
def test_both_installers_in_either_order(home, order):
    run = {"clients": clients_install, "judge": judge_install}
    _cfg(home).write_text(yaml.safe_dump(PERSONAL, sort_keys=False))
    for name in order:
        run[name](home)
    settled = _cfg(home).read_bytes()
    data = yaml.safe_load(settled)
    assert_clients_block(data)
    assert_judge_block(data)
    for name in order:                    # a second round changes nothing
        run[name](home)
        assert _cfg(home).read_bytes() == settled, f"{name} rewrote a settled config"
