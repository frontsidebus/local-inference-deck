import os
import shutil
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
    assert cfg["JUDGE_RUNAWAY_TOKENS"] == "24000"
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


# ---------------------------------------------------------------- #37 git worktrees of the deck repo
def _git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "init.defaultBranch=main",
                    *args], cwd=str(cwd), check=True, capture_output=True)


def _main_repo(path):
    path.mkdir(parents=True)
    _git("init", "-q", cwd=path)
    (path / "README.md").write_text("x\n")
    _git("add", "README.md", cwd=path)
    _git("commit", "-q", "-m", "init", cwd=path)
    return path


@pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
def test_classify_git_worktree_of_repo_is_infra(env, tmp_path, monkeypatch):
    main = _main_repo(tmp_path / "deck")
    wt = tmp_path / "deck-feature"
    _git("worktree", "add", "-q", "-b", "feat", str(wt), cwd=main)
    assert (wt / ".git").is_file() and (wt / ".git").read_text().startswith("gitdir:")
    other = _main_repo(tmp_path / "unrelated")
    monkeypatch.setenv("JUDGE_REPO_DIR", str(main))
    assert config.git_common_dir(str(wt / "walter" / "new.sh")) == os.path.realpath(main / ".git")
    assert config.classify([str(wt / "walter" / "deploy.sh"), str(wt / "README.md")]) == "infra"
    assert config.classify([], cwd=str(wt)) == "infra"
    assert config.classify([str(other / "README.md")]) == "sensitive"
    assert config.classify([str(wt / "README.md"), str(other / "README.md")]) == "sensitive"
    assert config.classify([str(wt / ".env")]) == "sensitive"
    # live layout: JUDGE_REPO_DIR is itself a worktree; a sibling worktree shares its common dir
    wt2 = tmp_path / "deck-main"
    _git("worktree", "add", "-q", "-b", "main2", str(wt2), cwd=main)
    monkeypatch.setenv("JUDGE_REPO_DIR", str(wt2))
    assert config.classify([str(wt / "covenant" / "x.tmpl")]) == "infra"
    assert config.classify([str(main / "README.md")]) == "infra"
    # JUDGE_INFRA_REPOS works the same way
    monkeypatch.setenv("JUDGE_REPO_DIR", str(tmp_path / "nowhere"))
    monkeypatch.setenv("JUDGE_INFRA_REPOS", str(main))
    assert config.classify([str(wt / "README.md")]) == "infra"
    monkeypatch.delenv("JUDGE_INFRA_REPOS")
    assert config.classify([str(wt / "README.md")]) == "sensitive"


def test_classify_unknown_gitdir_stays_sensitive(env, tmp_path, monkeypatch):
    """No git needed: a .git file pointing nowhere useful (or at a submodule dir) is never infra."""
    main = tmp_path / "deck"
    (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
    (main / ".git" / "worktrees" / "wt" / "commondir").write_text("../..\n")
    monkeypatch.setenv("JUDGE_REPO_DIR", str(main))
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {main}/.git/worktrees/wt\n")
    assert config.classify([str(wt / "a.sh")]) == "infra"
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: /nonexistent/.git/worktrees/x\n")
    assert config.classify([str(broken / "a.sh")]) == "sensitive"
    sub = tmp_path / "sub"
    sub.mkdir()
    (main / ".git" / "modules" / "sub").mkdir(parents=True)
    (sub / ".git").write_text(f"gitdir: {main}/.git/modules/sub\n")
    assert config.classify([str(sub / "a.sh")]) == "sensitive"
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / ".git").write_text("not a gitdir line\n")
    assert config.classify([str(plain / "a.sh")]) == "sensitive"
