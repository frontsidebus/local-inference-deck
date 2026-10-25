"""bin/deck evals, bin/deck install evals and the sec-evals parts of bin/deck check (plan step 3: the harness is the
sec-evals repo, checked out at $SEC_EVALS_DIR, else ~/src/deck/sec-evals).

Everything runs in temp dirs: a copy of the deck parts bin/deck uses, a fake sec-evals checkout (a git repo with stub
evals/run.py and evals/gate.py), a stub python3 first on PATH that records its cwd, argv and SITE_ENV for `deck
evals`, and an empty HOME. Every site env is example-valued. No network, GPU or real site.env. Moved here from the
monorepo's evals/tests/test_site_env.py, which kept the evals/tools/lib.sh tests. Run:
    python3 -m pytest tools/tests -q -p no:cacheprovider
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None,
                                reason="needs bash and git")


def _git(cwd, *args):
    env = {"PATH": os.environ["PATH"], "HOME": str(cwd), "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C"}
    return subprocess.run(["git", "-C", str(cwd), *args], env=env, capture_output=True, text=True, check=True,
                          timeout=60).stdout.strip()


def _commit(se, msg):
    _git(se, "add", "-A")
    _git(se, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", msg)
    return _git(se, "rev-parse", "HEAD")


@pytest.fixture
def layout(tmp_path):
    """(deck copy, sec-evals checkout, HOME). The deck copy has bin/, tools/, contracts/, the example and the
    llama-swap template; sec-evals ignores site.env as its .gitignore does."""
    deck = tmp_path / "deck"
    for d in ("bin", "tools", "contracts"):
        shutil.copytree(REPO / d, deck / d, ignore=shutil.ignore_patterns("tests", "__pycache__"))
    shutil.copy2(REPO / "site.env.example", deck / "site.env.example")
    (deck / "walter" / "llama-swap").mkdir(parents=True)
    (deck / "walter" / "llama-swap" / "config.yaml.tmpl").write_text("models: {}\n")
    se = tmp_path / "sec-evals"
    (se / "evals").mkdir(parents=True)
    (se / "evals" / "run.py").write_text("# stub\n")
    (se / "evals" / "gate.py").write_text("# stub\n")
    (se / ".gitignore").write_text("site.env\nevals/results/\n")
    _git(se, "init", "-q", "-b", "main")
    _commit(se, "init")
    home = tmp_path / "home"
    home.mkdir()
    return deck, se, home


def _env(home, se, **extra):
    e = {k: v for k, v in os.environ.items()
         if k not in ("SITE_ENV", "SEC_EVALS_DIR", "AGENT_JUDGE_DIR", "JUDGE_ENV", "EVAL_GPU_SSH")}
    e.update(HOME=str(home), LC_ALL="C")
    if se is not None:
        e["SEC_EVALS_DIR"] = str(se)
    e.update({k: str(v) for k, v in extra.items()})
    return e


def _site(path: Path, ip: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"BACKEND_SSH_USER=op\nBACKEND_LAN_IP={ip}   # example\n")
    return path


def _full_site(deck, path: Path) -> Path:
    """An example-valued site env that passes the registry check."""
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(deck / "site.env.example", path)
    return path


def run_deck(deck, home, se, *args, **extra):
    return subprocess.run([str(deck / "bin" / "deck"), *args], env=_env(home, se, **extra), capture_output=True,
                          text=True, timeout=60)


# ------------------------------------------------------------------------------------------- deck evals
@pytest.fixture
def evals(layout, tmp_path):
    deck, se, home = layout
    bindir = tmp_path / "stubbin"
    bindir.mkdir()
    rec = tmp_path / "python3.calls"
    stub = bindir / "python3"
    stub.write_text(f'#!/bin/sh\n{{ echo "cwd=$(pwd)"; echo "SITE_ENV=$SITE_ENV"; for a in "$@"; do echo "arg=$a"; '
                    f'done; }} > "{rec}"\n')
    stub.chmod(0o755)

    def run(*args, se_dir=se, **extra):
        e = _env(home, se_dir, **extra)
        e["PATH"] = f"{bindir}:{e['PATH']}"
        r = subprocess.run([str(deck / "bin" / "deck"), *args], env=e, capture_output=True, text=True, timeout=30)
        call = None
        if rec.exists():
            lines = rec.read_text().splitlines()
            call = {"cwd": lines[0][4:], "SITE_ENV": lines[1][9:], "argv": [x[4:] for x in lines[2:]]}
            rec.unlink()
        return r, call
    return deck, se, home, run


def _flag(argv, name):
    return argv[argv.index(name) + 1]


def test_deck_fails_closed_without_a_site_env(evals):
    deck, se, home, run = evals
    r, call = run("evals", "gate", "check", "--model", "coder")
    assert r.returncode == 2 and call is None and "no site.env" in r.stderr


def test_deck_runs_from_the_sec_evals_root_with_the_deck_template(evals, tmp_path):
    deck, se, home, run = evals
    root = _site(deck / "site.env", "192.0.2.30")
    r, call = run("evals", "gate", "check", "--model", "coder", "--yes")
    assert r.returncode == 0, r.stderr
    assert call["cwd"] == str(se)                                       # the sec-evals root, not the deck
    assert call["argv"][:3] == ["-B", "evals/gate.py", "check"]       # the command line the guard matches
    assert call["SITE_ENV"] == str(root) == _flag(call["argv"], "--site-env")
    assert _flag(call["argv"], "--llama-swap-config") == str(deck / "walter/llama-swap/config.yaml.tmpl")

    user = _site(home / ".config" / "deck" / "site.env", "192.0.2.20")
    _, call = run("evals", "run", "--suite", "evals/samples", "--model", "coder")
    assert call["cwd"] == str(se) and call["argv"][:2] == ["-B", "evals/run.py"]
    assert call["SITE_ENV"] == str(user) == _flag(call["argv"], "--site-env")

    env_site = _site(tmp_path / "env.env", "192.0.2.10")
    _, call = run("evals", "run", "--model", "coder", SITE_ENV=env_site)
    assert call["SITE_ENV"] == str(env_site) == _flag(call["argv"], "--site-env")

    flag_site = _site(tmp_path / "flag.env", "192.0.2.40")
    _, call = run("evals", "gate", "baseline", "--site-env", str(flag_site), "--model", "coder", SITE_ENV=env_site)
    assert call["SITE_ENV"] == str(flag_site)
    assert call["argv"].count("--site-env") == 1 and _flag(call["argv"], "--site-env") == str(flag_site)

    _, call = run("evals", "run", f"--site-env={flag_site}", "--llama-swap-config", "other.yaml")
    assert call["argv"].count("--llama-swap-config") == 1 and _flag(call["argv"], "--llama-swap-config") == "other.yaml"


def test_deck_default_checkout_path(evals, tmp_path):
    deck, se, home, run = evals
    _site(deck / "site.env", "192.0.2.30")
    default = home / "src" / "deck" / "sec-evals"
    default.parent.mkdir(parents=True)
    shutil.copytree(se, default)
    r, call = run("evals", "gate", "compare", "a", "b", se_dir=None)
    assert r.returncode == 0, r.stderr
    assert call["cwd"] == str(default)


def test_deck_without_a_sec_evals_checkout_exits_2(evals, tmp_path):
    deck, se, home, run = evals
    _site(deck / "site.env", "192.0.2.30")
    r, call = run("evals", "gate", "check", "--model", "coder", se_dir=tmp_path / "absent")
    assert r.returncode == 2 and call is None and "no sec-evals checkout" in r.stderr
    r, call = run("evals", "gate", "compare", "a", "b", se_dir=None)        # nothing at ~/src/deck/sec-evals
    assert r.returncode == 2 and call is None and "SEC_EVALS_DIR" in r.stderr


def test_deck_named_path_must_exist(evals, tmp_path):
    deck, se, home, run = evals
    _site(deck / "site.env", "192.0.2.30")                 # a fallback exists, but a named path never falls back
    r, call = run("evals", "run", "--model", "coder", SITE_ENV=tmp_path / "missing.env")
    assert r.returncode == 2 and call is None and "missing.env" in r.stderr
    r, call = run("evals", "gate", "check", "--site-env", str(tmp_path / "gone.env"))
    assert r.returncode == 2 and call is None and "gone.env" in r.stderr


def test_deck_gate_compare_and_help_need_no_site_env(evals):
    deck, se, home, run = evals
    r, call = run("evals", "gate", "compare", "a", "b")
    assert r.returncode == 0 and call["argv"] == ["-B", "evals/gate.py", "compare", "a", "b"]
    assert call["cwd"] == str(se)
    r, call = run("--help")
    assert r.returncode == 0 and "deck evals" in r.stdout and "install evals" in r.stdout and call is None
    r, call = run("evals", "bogus")
    assert r.returncode == 2 and call is None


# ------------------------------------------------------------------------------------------- deck install evals
def test_install_evals_links_the_resolved_site_env(layout, tmp_path):
    deck, se, home = layout
    site = _site(home / ".config" / "deck" / "site.env", "192.0.2.20")
    r = run_deck(deck, home, se, "install", "evals")
    assert r.returncode == 0, r.stderr
    link = se / "site.env"
    assert link.is_symlink() and os.path.isabs(os.readlink(link)) and link.resolve() == site.resolve()
    assert "192.0.2.20" not in r.stdout + r.stderr
    assert _git(se, "status", "--porcelain") == ""                   # gitignored: the checkout stays clean
    r = run_deck(deck, home, se, "install", "evals")                 # idempotent
    assert r.returncode == 0 and "already in place" in r.stdout


def test_install_evals_refuses_another_target_unless_forced(layout, tmp_path):
    deck, se, home = layout
    a = _site(tmp_path / "a.env", "192.0.2.10")
    b = _site(tmp_path / "b.env", "192.0.2.40")
    assert run_deck(deck, home, se, "install", "evals", "--site-env", str(a)).returncode == 0
    r = run_deck(deck, home, se, "install", "evals", "--site-env", str(b))
    assert r.returncode == 2 and "--force" in r.stderr and (se / "site.env").resolve() == a.resolve()
    r = run_deck(deck, home, se, "install", "evals", "--site-env", str(b), "--force")
    assert r.returncode == 0 and (se / "site.env").resolve() == b.resolve()


def test_install_evals_never_replaces_a_regular_file(layout, tmp_path):
    deck, se, home = layout
    a = _site(tmp_path / "a.env", "192.0.2.10")
    (se / "site.env").write_text("BACKEND_LAN_IP=192.0.2.99\n")
    for extra in ((), ("--force",)):
        r = run_deck(deck, home, se, "install", "evals", "--site-env", str(a), *extra)
        assert r.returncode == 2 and "regular file" in r.stderr
    assert not (se / "site.env").is_symlink()


def test_install_evals_refuses_when_site_env_is_not_ignored(layout, tmp_path):
    deck, se, home = layout
    a = _site(tmp_path / "a.env", "192.0.2.10")
    (se / ".gitignore").write_text("evals/results/\n")
    _commit(se, "drop the ignore")
    r = run_deck(deck, home, se, "install", "evals", "--site-env", str(a))
    assert r.returncode == 2 and "not gitignored" in r.stderr and not (se / "site.env").exists()


def test_install_evals_needs_a_checkout_and_a_site_env(layout, tmp_path):
    deck, se, home = layout
    r = run_deck(deck, home, se, "install", "evals")
    assert r.returncode == 2 and "no site.env" in r.stderr
    r = run_deck(deck, home, tmp_path / "absent", "install", "evals", "--site-env", str(_site(tmp_path / "a.env", "192.0.2.10")))
    assert r.returncode == 2 and "no sec-evals checkout" in r.stderr
    r = run_deck(deck, home, se, "install", "bogus")
    assert r.returncode == 2


# ------------------------------------------------------------------------------------------- deck check (sec-evals)
def test_check_evals_link(layout, tmp_path):
    deck, se, home = layout
    full = _full_site(deck, home / ".config" / "deck" / "site.env")
    r = run_deck(deck, home, se, "check")
    assert r.returncode == 1 and "deck install evals" in r.stdout and "is missing" in r.stdout
    assert run_deck(deck, home, se, "install", "evals").returncode == 0
    r = run_deck(deck, home, se, "check")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "site.env link ok" in r.stdout
    other = _full_site(deck, tmp_path / "other.env")
    (se / "site.env").unlink()
    (se / "site.env").symlink_to(other)
    r = run_deck(deck, home, se, "check")
    assert r.returncode == 1 and "different file" in r.stdout
    (se / "site.env").unlink()
    (se / "site.env").symlink_to(tmp_path / "gone.env")
    r = run_deck(deck, home, se, "check")
    assert r.returncode == 1 and "dangling" in r.stdout
    assert full.exists()


def test_check_without_a_checkout_is_a_note(layout, tmp_path):
    deck, se, home = layout
    _full_site(deck, home / ".config" / "deck" / "site.env")
    r = run_deck(deck, home, tmp_path / "absent", "check")
    assert r.returncode == 0 and "no checkout at" in r.stdout


def _lock(deck, commit):
    c = "null" if commit is None else f'"{commit}"'
    (deck / "deck.lock").write_text(
        "lock_format: 1\ncomponents:\n"
        f'  - {{"name": "sec-evals", "repo": null, "tag": null, "commit": {c}, "mode": "minimum", '
        '"path": "~/src/deck/sec-evals", "requires_contracts": {"serving-identity": "1"}}\n')


def test_check_lock_minimum_warns_and_never_moves_head(layout, tmp_path):
    deck, se, home = layout
    _full_site(deck, home / ".config" / "deck" / "site.env")
    assert run_deck(deck, home, se, "install", "evals").returncode == 0
    first = _git(se, "rev-parse", "HEAD")
    (se / "evals" / "plans").mkdir()
    (se / "evals" / "plans" / "attempts-C1.jsonl").write_text("{}\n")
    second = _commit(se, "ledger")                                     # the operator's ledger commit
    _lock(deck, None)
    r = run_deck(deck, home, se, "check")
    assert r.returncode == 0 and "no commit locked yet" in r.stdout
    _lock(deck, first)
    r = run_deck(deck, home, se, "check")
    assert r.returncode == 0 and "contains the locked commit" in r.stdout
    _git(se, "checkout", "-q", "--detach", first)
    _lock(deck, second)                                                # HEAD is behind the lock
    r = run_deck(deck, home, se, "check")
    assert r.returncode == 0 and "WARN the checkout's HEAD does not contain" in r.stdout
    assert _git(se, "rev-parse", "HEAD") == first                     # deck never moved HEAD
    _lock(deck, "0" * 40)
    r = run_deck(deck, home, se, "check")
    assert r.returncode == 0 and "is not in the checkout" in r.stdout


def test_check_lock_rejects_an_unknown_mode(layout):
    deck, se, home = layout
    _full_site(deck, home / ".config" / "deck" / "site.env")
    assert run_deck(deck, home, se, "install", "evals").returncode == 0
    _lock(deck, None)
    (deck / "deck.lock").write_text((deck / "deck.lock").read_text().replace('"minimum"', '"floating"'))
    r = run_deck(deck, home, se, "check")
    assert r.returncode == 1 and "mode must be pinned or minimum" in r.stdout


def _fake_agent_judge(tmp_path):
    aj = tmp_path / "agent-judge"
    (aj / "judge" / "contracts").mkdir(parents=True)
    (aj / "judge" / "contracts" / "infra-repo.json").write_text('{"contract_version": "1.0"}\n')
    _git(aj, "init", "-q")
    return aj, _commit(aj, "v1")


def _lock_aj(deck, commit):
    (deck / "deck.lock").write_text(
        "lock_format: 1\ncomponents:\n"
        f'  - {{"name": "agent-judge", "repo": null, "tag": null, "commit": "{commit}", "mode": "pinned", '
        '"path": "~/src/deck/agent-judge", "requires_contracts": {"site-keys": "1"}}\n')


def test_check_lock_pinned_fails_off_the_locked_commit_and_never_moves_head(layout, tmp_path):
    deck, se, home = layout
    _full_site(deck, home / ".config" / "deck" / "site.env")
    assert run_deck(deck, home, se, "install", "evals").returncode == 0
    aj, first = _fake_agent_judge(tmp_path)
    _lock_aj(deck, first)
    r = run_deck(deck, home, se, "check")                               # no checkout at the lock's path
    assert r.returncode == 0 and "agent-judge: no checkout (skipped)" in r.stdout
    r = run_deck(deck, home, se, "check", AGENT_JUDGE_DIR=aj)
    assert r.returncode == 0 and "checkout is at the locked commit (pinned)" in r.stdout
    (aj / "judge" / "README.md").write_text("x\n")
    second = _commit(aj, "v2")                                          # the checkout moved past the pin
    r = run_deck(deck, home, se, "check", AGENT_JUDGE_DIR=aj)
    assert r.returncode == 1 and "is not the locked commit" in r.stdout
    assert _git(aj, "rev-parse", "HEAD") == second                     # deck never moved HEAD
    (home / "src" / "deck").mkdir(parents=True)
    (aj).rename(home / "src" / "deck" / "agent-judge")                  # the lock's path, under HOME
    _lock_aj(deck, second)
    r = run_deck(deck, home, se, "check")
    assert r.returncode == 0 and "checkout is at the locked commit (pinned)" in r.stdout


def test_the_committed_lock_parses():
    import sys
    sys.path.insert(0, str(REPO / "tools"))
    import site_keys as sk
    lock = sk.load_registry(REPO / "deck.lock")
    (se,) = [c for c in lock["components"] if c["name"] == "sec-evals"]
    assert se["mode"] == "minimum" and se["path"] == "~/src/deck/sec-evals"
    assert set(se["requires_contracts"]) == {"serving-identity", "eval-liveness", "gate-cli"}
    (aj,) = [c for c in lock["components"] if c["name"] == "agent-judge"]
    assert aj["mode"] == "pinned" and aj["path"] == "~/src/deck/agent-judge" and len(aj["commit"]) == 40
