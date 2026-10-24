"""tools/deck-tools/deck-tools: verify (content, modes, version, changelog, --index, --base), lock, sync and
install-hooks, plus the pre-commit hook it wires. Throwaway git repos only; HOME is a temp dir.
Run: python3 -m pytest tools/tests -q -p no:cacheprovider
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DT = REPO / "tools" / "deck-tools"
CLI = DT / "deck-tools"


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {"PATH": os.environ["PATH"], "HOME": str(home), "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def run(argv, env, cwd=None):
    return subprocess.run([str(a) for a in argv], env=env, cwd=cwd, capture_output=True, text=True, timeout=60)


def git(repo, env, *args):
    return subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True, text=True, timeout=60,
                          check=True).stdout


def dt(repo, env, *args, cli=None):
    return run(["python3", cli or (repo / "tools/deck-tools/deck-tools"), *args], env)


def make(tmp_path, env, name="repo", commit=True):
    r = tmp_path / name
    shutil.copytree(DT, r / "tools" / "deck-tools", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(REPO / "tools" / "tools.lock", r / "tools" / "tools.lock")
    if commit:
        subprocess.run(["git", "init", "-q", str(r)], check=True, env=env)
        git(r, env, "add", "-A")
        git(r, env, "commit", "-qm", "init")
    return r


def bump(repo, version):
    (repo / "tools/deck-tools/VERSION").write_text(version + "\n")
    cl = repo / "tools/deck-tools/CHANGELOG.md"
    cl.write_text(cl.read_text().replace("## 1.0.0", f"## {version}\n\n- test release\n\n## 1.0.0", 1))


# ---- this repo -------------------------------------------------------------------------------------------------

def test_this_repo_verifies(env):
    r = dt(REPO, env, "verify")
    assert r.returncode == 0, r.stderr
    assert "verify OK (version" in r.stdout


def test_this_repo_is_the_origin_and_versions_agree(env):
    lock = (REPO / "tools/tools.lock").read_text()
    assert "\nsource: origin\n" in lock
    version = (DT / "VERSION").read_text().strip()
    assert f"\nversion: {version}\n" in lock
    assert f"\n## {version}\n" in (DT / "CHANGELOG.md").read_text()
    assert dt(REPO, env, "version").stdout.strip() == version


def test_lock_lists_every_engine_with_its_mode():
    lines = [l.split() for l in (REPO / "tools/tools.lock").read_text().splitlines() if l.startswith("100")]
    modes = {p: m for m, _, p in lines}
    for x in ("render.sh", "gen-secrets.sh", "check-sanitized.sh", "deck-tools", "pre-commit"):
        assert modes[f"tools/deck-tools/{x}"] == "100755", x
    for x in ("VERSION", "README.md", "CHANGELOG.md"):
        assert modes[f"tools/deck-tools/{x}"] == "100644", x
    assert [p for _, _, p in lines] == sorted(p for _, _, p in lines)


def test_lock_is_current_and_holds_no_local_paths(tmp_path, env):
    r = make(tmp_path, env)
    before = (r / "tools/tools.lock").read_text()
    out = dt(r, env, "lock")
    assert out.returncode == 0 and "unchanged" in out.stdout
    assert (r / "tools/tools.lock").read_text() == before
    assert "/home/" not in before and str(REPO) not in before


# ---- verify failures -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("change,needle", [
    ("edit", "content differs from the lock: tools/deck-tools/render.sh"),
    ("chmod", "tools/deck-tools/gen-secrets.sh lost its executable bit"),
    ("exec-doc", "tools/deck-tools/README.md is executable but locked as 100644"),
    ("delete", "missing: tools/deck-tools/check-sanitized.sh"),
    ("extra", "not in the lock: tools/deck-tools/extra.sh"),
    ("version", "VERSION says '9.9.9'"),
    ("symlink", "not a regular file: tools/deck-tools/link"),
])
def test_verify_fails_on_drift(tmp_path, env, change, needle):
    r = make(tmp_path, env, commit=False)
    d = r / "tools/deck-tools"
    if change == "edit":
        with open(d / "render.sh", "a") as f:
            f.write("# drift\n")
    elif change == "chmod":
        (d / "gen-secrets.sh").chmod(0o644)
    elif change == "exec-doc":
        (d / "README.md").chmod(0o755)
    elif change == "delete":
        (d / "check-sanitized.sh").unlink()
    elif change == "extra":
        (d / "extra.sh").write_text("#!/bin/sh\n")
    elif change == "version":
        (d / "VERSION").write_text("9.9.9\n")
    elif change == "symlink":
        (d / "link").symlink_to("render.sh")
    out = dt(r, env, "verify")
    assert out.returncode == 1 and needle in out.stderr, out.stderr


def test_verify_ignores_pycache(tmp_path, env):
    r = make(tmp_path, env, commit=False)
    (r / "tools/deck-tools/__pycache__").mkdir()
    (r / "tools/deck-tools/__pycache__/x.pyc").write_bytes(b"\0")
    assert dt(r, env, "verify").returncode == 0


@pytest.mark.parametrize("lock", [None, "garbage\n", "lock_format: 2\nversion: 1.0.0\nsource: origin\nfiles:\n"])
def test_verify_without_a_valid_lock_is_exit_2(tmp_path, env, lock):
    r = make(tmp_path, env, commit=False)
    if lock is None:
        (r / "tools/tools.lock").unlink()
    else:
        (r / "tools/tools.lock").write_text(lock)
    assert dt(r, env, "verify").returncode == 2


def test_verify_checks_the_mode_git_records(tmp_path, env):
    r = make(tmp_path, env)
    git(r, env, "update-index", "--chmod=-x", "tools/deck-tools/render.sh")
    git(r, env, "commit", "-qm", "lose x in git only")
    out = dt(r, env, "verify")
    assert out.returncode == 1 and "git records tools/deck-tools/render.sh as 100644" in out.stderr


def test_verify_changelog_needs_the_version(tmp_path, env):
    r = make(tmp_path, env, commit=False)
    (r / "tools/deck-tools/VERSION").write_text("1.0.1\n")
    assert dt(r, env, "lock").returncode == 0
    out = dt(r, env, "verify")
    assert out.returncode == 1 and "no '## 1.0.1' entry" in out.stderr
    bump(r, "1.0.1")
    assert dt(r, env, "lock").returncode == 0 and dt(r, env, "verify").returncode == 0


def test_verify_index_checks_what_is_staged(tmp_path, env):
    r = make(tmp_path, env)
    eng = r / "tools/deck-tools/render.sh"
    eng.write_text(eng.read_text() + "# change\n")
    bump(r, "1.0.1")
    assert dt(r, env, "lock").returncode == 0
    assert dt(r, env, "verify").returncode == 0                       # the work tree matches the new lock
    git(r, env, "add", "tools/deck-tools/render.sh")                   # ...but the lock bump is not staged
    out = dt(r, env, "verify", "--index")
    assert out.returncode == 1 and "content differs from the lock: tools/deck-tools/render.sh" in out.stderr
    git(r, env, "add", "-A")
    assert dt(r, env, "verify", "--index").returncode == 0


def test_verify_base_needs_a_version_bump(tmp_path, env):
    r = make(tmp_path, env)
    base = git(r, env, "rev-parse", "HEAD").strip()
    eng = r / "tools/deck-tools/check-sanitized.sh"
    eng.write_text(eng.read_text() + "# change\n")
    assert dt(r, env, "lock").returncode == 0                         # lock regenerated, version not bumped
    git(r, env, "commit", "-qam", "edit without bump")
    assert dt(r, env, "verify").returncode == 0
    out = dt(r, env, "verify", "--base", base)
    assert out.returncode == 1 and "but the version is still 1.0.0" in out.stderr
    bump(r, "1.1.0")
    assert dt(r, env, "lock").returncode == 0
    git(r, env, "commit", "-qam", "bump")
    assert dt(r, env, "verify", "--base", base).returncode == 0
    head = git(r, env, "rev-parse", "HEAD").strip()
    bump(r, "0.9.0")
    assert dt(r, env, "lock").returncode == 0
    out = dt(r, env, "verify", "--base", head)
    assert out.returncode == 1 and "version went down" in out.stderr


def test_forgotten_lock_after_an_engine_edit_fails(tmp_path, env):
    r = make(tmp_path, env)
    eng = r / "tools/deck-tools/render.sh"
    eng.write_text(eng.read_text() + "# change\n")
    out = dt(r, env, "verify")
    assert out.returncode == 1 and "render.sh" in out.stderr


# ---- sync --------------------------------------------------------------------------------------------------------

@pytest.fixture
def deck(tmp_path, env):
    """A fake origin: tools/deck-tools + lock, committed and tagged like a release."""
    d = make(tmp_path, env, "deck")
    git(d, env, "tag", "deck-tools-v1.0.0")
    return d


def test_sync_vendors_a_release_with_modes_and_a_clean_lock(tmp_path, env, deck):
    c = tmp_path / "consumer"
    c.mkdir()
    subprocess.run(["git", "init", "-q", str(c)], check=True, env=env)
    out = dt(c, env, "sync", "--from", deck, "--repo", c, cli=deck / "tools/deck-tools/deck-tools")
    assert out.returncode == 0, out.stderr
    assert os.access(c / "tools/deck-tools/gen-secrets.sh", os.X_OK)
    assert not os.access(c / "tools/deck-tools/README.md", os.X_OK)
    lock = (c / "tools/tools.lock").read_text()
    commit = git(deck, env, "rev-parse", "HEAD").strip()
    assert f"\nsource: local-inference-deck deck-tools-v1.0.0 {commit}\n" in lock
    assert str(tmp_path) not in lock
    assert dt(c, env, "verify").returncode == 0
    # a vendored copy refuses `lock`; sync is the only way to change it
    r = dt(c, env, "lock")
    assert r.returncode == 1 and "vendored copy" in r.stderr


def test_sync_updates_and_removes(tmp_path, env, deck):
    c = make(tmp_path, env, "consumer")
    (c / "tools/deck-tools/obsolete.sh").write_text("#!/bin/sh\n")
    eng = deck / "tools/deck-tools/render.sh"
    eng.write_text(eng.read_text() + "# 1.0.1\n")
    bump(deck, "1.0.1")
    assert dt(deck, env, "lock").returncode == 0
    git(deck, env, "commit", "-qam", "1.0.1")
    out = dt(c, env, "sync", "--from", deck)
    assert out.returncode == 0, out.stderr
    assert "changed  tools/deck-tools/render.sh" in out.stdout and "removed  tools/deck-tools/obsolete.sh" in out.stdout
    assert "untagged" in (c / "tools/tools.lock").read_text()
    assert not (c / "tools/deck-tools/obsolete.sh").exists()
    assert dt(c, env, "version").stdout.strip() == "1.0.1"


def test_sync_refuses_a_dirty_or_drifted_source_and_itself(tmp_path, env, deck):
    c = make(tmp_path, env, "consumer")
    eng = deck / "tools/deck-tools/render.sh"
    eng.write_text(eng.read_text() + "# drift\n")
    out = dt(c, env, "sync", "--from", deck)
    assert out.returncode == 1 and "does not match its own lock" in out.stderr
    assert dt(deck, env, "lock").returncode == 0                       # consistent but uncommitted
    out = dt(c, env, "sync", "--from", deck)
    assert out.returncode == 1 and "uncommitted" in out.stderr
    out = dt(c, env, "sync", "--from", deck, "--allow-dirty")
    assert out.returncode == 0 and "+uncommitted" in (c / "tools/tools.lock").read_text()
    out = dt(c, env, "sync", "--from", c)
    assert out.returncode == 2


# ---- install-hooks and the pre-commit hook ---------------------------------------------------------------------

def hook_repo(tmp_path, env):
    r = make(tmp_path, env, commit=False)
    (r / "scripts").mkdir()
    shutil.copy2(REPO / "scripts/check-sanitized.sh", r / "scripts/check-sanitized.sh")
    (r / "hooks").mkdir()
    shutil.copy2(REPO / "hooks/pre-commit", r / "hooks/pre-commit")
    (r / "site.env.example").write_text("FOO_HOST=example.org\n")
    (r / ".gitignore").write_text("site.env\n")
    subprocess.run(["git", "init", "-q", str(r)], check=True, env=env)
    git(r, env, "add", "-A")
    git(r, env, "commit", "-qm", "init", "--no-verify")
    return r


def try_commit(r, env, name, text):
    (r / name).write_text(text)
    git(r, env, "add", "-A")
    return subprocess.run(["git", "-C", str(r), "commit", "-qm", name], env=env, capture_output=True, text=True,
                          timeout=60)


def test_install_hooks_then_commits_are_checked(tmp_path, env):
    r = hook_repo(tmp_path, env)
    out = dt(r, env, "install-hooks")
    assert out.returncode == 0, out.stderr
    hook = r / ".git/hooks/pre-commit"
    assert os.access(hook, os.X_OK) and "deck-tools-hook-stub" in hook.read_text()
    assert "already installed" in dt(r, env, "install-hooks").stdout             # idempotent
    res = try_commit(r, env, "a.md", "fine\n")                                   # no real-value source
    assert res.returncode != 0 and "no real-value source" in res.stderr
    (r / "site.env").write_text("FOO_HOST=fakesite-lima.invalid\n")
    assert try_commit(r, env, "a.md", "fine\n").returncode == 0
    res = try_commit(r, env, "b.md", "fakesite-lima.invalid\n")
    assert res.returncode != 0 and "FORBIDDEN site-specific values" in res.stdout + res.stderr
    (r / "b.md").unlink()
    eng = r / "tools/deck-tools/render.sh"                                     # engine edit, no lock bump
    eng.write_text(eng.read_text() + "# drift\n")
    git(r, env, "add", "-A")
    res = subprocess.run(["git", "-C", str(r), "commit", "-qm", "drift"], env=env, capture_output=True, text=True)
    assert res.returncode != 0 and "does not match tools/tools.lock" in res.stderr


def test_install_hooks_keeps_a_foreign_hook_unless_forced(tmp_path, env):
    r = hook_repo(tmp_path, env)
    hook = r / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\necho mine\n")
    out = dt(r, env, "install-hooks")
    assert out.returncode == 1 and "not ours" in out.stderr and "mine" in hook.read_text()
    out = dt(r, env, "install-hooks", "--force")
    assert out.returncode == 0 and "mine" in (r / ".git/hooks/pre-commit.pre-deck-tools").read_text()


def test_install_hooks_with_core_hookspath_hooks_changes_nothing(tmp_path, env):
    r = hook_repo(tmp_path, env)
    git(r, env, "config", "core.hooksPath", "hooks")
    before = (r / "hooks/pre-commit").read_text()
    out = dt(r, env, "install-hooks")
    assert out.returncode == 0 and "already wired" in out.stdout
    assert (r / "hooks/pre-commit").read_text() == before
    git(r, env, "config", "core.hooksPath", "/elsewhere")
    assert dt(r, env, "install-hooks").returncode == 1
