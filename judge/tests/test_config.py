import os
import subprocess

import pytest

from collector_testlib import JUDGE_DIR, make_env

from lib import config


@pytest.fixture
def env(tmp_path, monkeypatch):
    return make_env(tmp_path, monkeypatch)


def test_parse_env_file(env):
    d = config.parse_env_file(env["site"])
    assert d["BACKEND_LAN_IP"] == "192.168.122.10"
    assert d["JUDGE_SSH_ALIASES"] == "edge-alias other-alias"
    assert d["QUOTED"] == "single # not a comment"
    assert "synthetic" not in "".join(d)


def test_load_config_defaults_and_paths(env, monkeypatch):
    cfg = config.load_config()
    assert cfg["JUDGE_MODE"] == "frontier"
    assert cfg["JUDGE_RUNAWAY_TOKENS"] == "20000"
    assert cfg["HERMES_HOME"] == str(env["hermes"])
    assert cfg["JUDGE_REVIEW_DIR"] == str(env["review"])
    assert cfg["SITE_ENV"] == str(env["site"])
    monkeypatch.delenv("JUDGE_REVIEW_DIR")
    assert config.load_config()["JUDGE_REVIEW_DIR"] == os.path.join(str(env["hermes"]), "review")
    monkeypatch.setenv("JUDGE_MODE", "local")
    assert config.load_config()["JUDGE_MODE"] == "local"
    assert all(isinstance(v, str) for v in config.load_config().values())


def test_review_dir_create_mode(env):
    d = config.review_dir(create=True)
    assert d.is_dir() and (d.stat().st_mode & 0o777) == 0o700


def test_classify(env, tmp_path):
    hh = env["hermes"]
    assert config.classify([str(hh / "config.yaml"), str(hh / "skills/demo/SKILL.md")]) == "infra"
    assert config.classify(["~/.ssh/config", "/etc/nginx/nginx.conf", "/srv/x"]) == "infra"
    assert config.classify(["/work/deck/.hermes/plans/p.md"]) == "infra"
    assert config.classify([str(env["repo"] / "walter/x.tmpl")]) == "infra"
    assert config.classify(["walter:/etc/llama-swap/config.yaml"]) == "infra"
    # anything else -> sensitive; one sensitive path taints the whole set
    assert config.classify([str(hh / "config.yaml"), "/home/someone/company/code.py"]) == "sensitive"
    assert config.classify([str(hh / ".env")]) == "sensitive"
    assert config.classify(["~/.ssh/id_ed25519"]) == "sensitive"
    assert config.classify(["/etc/../home/x"]) == "sensitive"
    assert config.classify([]) == "sensitive"
    assert config.classify([], cwd=str(env["repo"])) == "infra"
    assert config.classify(["walter/x.tmpl"], cwd=str(env["repo"])) == "infra"


def test_classify_symlink_out_of_infra(env, tmp_path):
    secret = tmp_path / "private.txt"
    secret.write_text("x")
    link = env["hermes"] / "skills" / "link.md"
    link.symlink_to(secret)
    assert config.classify([str(link)]) == "sensitive"


def test_host_ssh(env, monkeypatch):
    w = config.host_ssh("walter")
    assert w[0] == "ssh" and "BatchMode=yes" in w and w[-1] == "operator@192.168.122.10"
    c = config.host_ssh("covenant")
    assert c[-1] == "edge-alias"  # no explicit EDGE_SSH_* -> first alias
    monkeypatch.setenv("EDGE_SSH_KEY", "~/.ssh/k.pem")
    c = config.host_ssh("covenant")
    assert c[-1] == "ubuntu@203.0.113.10" and c[c.index("-i") + 1].endswith("/.ssh/k.pem")
    with pytest.raises(ValueError):
        config.host_ssh("elsewhere")


def test_config_sh(env):
    sh = JUDGE_DIR / "lib" / "config.sh"
    assert subprocess.run(["bash", "-n", str(sh)]).returncode == 0
    out = subprocess.run(["bash", "-c", f'. "{sh}"; judge_load_config; echo "$JUDGE_REVIEW_DIR|$QUOTED|$JUDGE_MODE"'],
                         capture_output=True, text=True, env=os.environ.copy())
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == f"{env['review']}|single # not a comment|frontier"
