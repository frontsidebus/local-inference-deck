"""Site-env lookup for the eval tools (evals/tools/lib.sh site_value) and bin/deck evals, in a copy of the checkout
with example values only: no network, GPU or real site.env."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

EVALS = Path(__file__).resolve().parent.parent
REPO = EVALS.parent

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


@pytest.fixture
def checkout(tmp_path):
    """A copy of the parts of the checkout these tools use, with no root site.env, and an empty HOME."""
    repo = tmp_path / "repo"
    shutil.copytree(EVALS / "tools", repo / "evals" / "tools", ignore=shutil.ignore_patterns("__pycache__"))
    (repo / "bin").mkdir()
    shutil.copy2(REPO / "bin" / "deck", repo / "bin" / "deck")
    (repo / "walter" / "llama-swap").mkdir(parents=True)
    (repo / "walter" / "llama-swap" / "config.yaml.tmpl").write_text("models: {}\n")
    home = tmp_path / "home"
    home.mkdir()
    return repo, home


def _env(home, **extra):
    e = {k: v for k, v in os.environ.items() if k not in ("SITE_ENV", "REPO", "EVAL_GPU_SSH")}
    e.update(HOME=str(home), EVAL_RUN="sitetest", EVAL_STATE_DIR=str(home / "state"))
    e.update({k: str(v) for k, v in extra.items()})
    return e


def site_value(repo, home, key, **extra):
    r = subprocess.run(["bash", "-c", f'source "{repo}/evals/tools/lib.sh"; site_value {key}'],
                       env=_env(home, **extra), capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _site(path: Path, ip: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"BACKEND_SSH_USER=op\nBACKEND_LAN_IP={ip}   # example\n")
    return path


# ------------------------------------------------------------------------------------------- lib.sh site_value
def test_site_value_reads_SITE_ENV_without_a_root_site_env(checkout, tmp_path):
    repo, home = checkout
    site = _site(tmp_path / "example.env", "192.0.2.10")
    assert not (repo / "site.env").exists()
    assert site_value(repo, home, "BACKEND_LAN_IP", SITE_ENV=site) == "192.0.2.10"


def test_site_value_falls_back_to_the_user_config(checkout):
    repo, home = checkout
    _site(home / ".config" / "deck" / "site.env", "192.0.2.20")
    assert site_value(repo, home, "BACKEND_LAN_IP") == "192.0.2.20"


def test_site_value_order(checkout, tmp_path):
    repo, home = checkout
    assert site_value(repo, home, "BACKEND_LAN_IP") == ""                          # nothing anywhere
    _site(repo / "site.env", "192.0.2.30")
    assert site_value(repo, home, "BACKEND_LAN_IP") == "192.0.2.30"                # the repo root, last
    _site(home / ".config" / "deck" / "site.env", "192.0.2.20")
    assert site_value(repo, home, "BACKEND_LAN_IP") == "192.0.2.20"                # the user config beats it
    site = _site(tmp_path / "example.env", "192.0.2.10")
    assert site_value(repo, home, "BACKEND_LAN_IP", SITE_ENV=site) == "192.0.2.10"  # $SITE_ENV beats both
    # a SITE_ENV that names a missing file is used as given (empty), never silently replaced by a fallback
    assert site_value(repo, home, "BACKEND_LAN_IP", SITE_ENV=tmp_path / "missing.env") == ""


def test_guard_without_a_host_exits_2_and_names_the_file(checkout, tmp_path):
    repo, home = checkout
    missing = tmp_path / "missing.env"
    r = subprocess.run(["bash", str(repo / "evals" / "tools" / "gpu-guard.sh")], env=_env(home, SITE_ENV=missing),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and str(missing) in r.stderr


def test_tunnel_without_a_backend_exits_2_and_names_the_file(checkout, tmp_path):
    repo, home = checkout
    missing = tmp_path / "missing.env"
    r = subprocess.run(["bash", str(repo / "evals" / "tools" / "gateway-tunnel.sh"), "start"],
                       env=_env(home, SITE_ENV=missing), capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and str(missing) in r.stderr


# ------------------------------------------------------------------------------------------- bin/deck evals
@pytest.fixture
def deck(checkout, tmp_path):
    """bin/deck with a stub python3 first on PATH that records its cwd, argv and SITE_ENV."""
    repo, home = checkout
    bindir = tmp_path / "stubbin"
    bindir.mkdir()
    rec = tmp_path / "python3.calls"
    stub = bindir / "python3"
    stub.write_text(f'#!/bin/sh\n{{ echo "cwd=$(pwd)"; echo "SITE_ENV=$SITE_ENV"; for a in "$@"; do echo "arg=$a"; '
                    f'done; }} > "{rec}"\n')
    stub.chmod(0o755)

    def run(*args, **extra):
        e = _env(home, **extra)
        e["PATH"] = f"{bindir}:{e['PATH']}"
        r = subprocess.run([str(repo / "bin" / "deck"), *args], env=e, capture_output=True, text=True, timeout=30)
        call = None
        if rec.exists():
            lines = rec.read_text().splitlines()
            call = {"cwd": lines[0][4:], "SITE_ENV": lines[1][9:], "argv": [x[4:] for x in lines[2:]]}
            rec.unlink()
        return r, call
    return repo, home, run


def _flag(argv, name):
    return argv[argv.index(name) + 1]


def test_deck_fails_closed_without_a_site_env(deck):
    repo, home, run = deck
    r, call = run("evals", "gate", "check", "--model", "coder")
    assert r.returncode == 2 and call is None and "no site.env" in r.stderr


def test_deck_resolution_order_and_export(deck, tmp_path):
    repo, home, run = deck
    root = _site(repo / "site.env", "192.0.2.30")
    r, call = run("evals", "gate", "check", "--model", "coder", "--yes")
    assert r.returncode == 0, r.stderr
    assert call["cwd"] == str(repo)
    assert call["argv"][:3] == ["-B", "evals/gate.py", "check"]       # the command line the guard matches
    assert call["SITE_ENV"] == str(root) == _flag(call["argv"], "--site-env")
    assert _flag(call["argv"], "--llama-swap-config") == str(repo / "walter/llama-swap/config.yaml.tmpl")

    user = _site(home / ".config" / "deck" / "site.env", "192.0.2.20")
    _, call = run("evals", "run", "--suite", "evals/samples", "--model", "coder")
    assert call["argv"][:2] == ["-B", "evals/run.py"]
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


def test_deck_named_path_must_exist(deck, tmp_path):
    repo, home, run = deck
    _site(repo / "site.env", "192.0.2.30")                 # a fallback exists, but a named path never falls back
    r, call = run("evals", "run", "--model", "coder", SITE_ENV=tmp_path / "missing.env")
    assert r.returncode == 2 and call is None and "missing.env" in r.stderr
    r, call = run("evals", "gate", "check", "--site-env", str(tmp_path / "gone.env"))
    assert r.returncode == 2 and call is None and "gone.env" in r.stderr


def test_deck_gate_compare_and_help_need_no_site_env(deck):
    repo, home, run = deck
    r, call = run("evals", "gate", "compare", "a", "b")
    assert r.returncode == 0 and call["argv"] == ["-B", "evals/gate.py", "compare", "a", "b"]
    r, call = run("--help")
    assert r.returncode == 0 and "deck evals" in r.stdout and call is None
    r, call = run("evals", "bogus")
    assert r.returncode == 2 and call is None
