"""bin/deck env slice and bin/deck check, in a copy of the checkout (bin/, tools/, contracts/, scripts/,
site.env.example) so no real site.env next to the repo is ever read. HOME is a temp dir; every site env here is
site.env.example with example values. Run:
    python3 -m pytest tools/tests -q -p no:cacheprovider
"""
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))
import site_keys as sk  # noqa: E402

DIGEST = "digest.example.com"      # an example value; marks "the digest is on"
MARK = "unknown-marker.example.org"


@pytest.fixture
def checkout(tmp_path):
    r = tmp_path / "repo"
    for d in ("bin", "tools", "contracts", "scripts"):
        shutil.copytree(REPO / d, r / d, ignore=shutil.ignore_patterns("tests", "__pycache__"))
    shutil.copy2(REPO / "site.env.example", r / "site.env.example")
    home = tmp_path / "home"
    home.mkdir()
    full = tmp_path / "full.env"
    text = (REPO / "site.env.example").read_text().replace('SPARK_DIGEST_HOST=""', f"SPARK_DIGEST_HOST={DIGEST}")
    full.write_text(text + f"EXTRA_UNKNOWN_KEY={MARK}\n")
    full.chmod(0o600)
    return r, home, full


def deck(checkout, *args, **extra):
    repo, home, _ = checkout
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "LC_ALL": "C", **extra}
    return subprocess.run([str(repo / "bin" / "deck"), *args], env=env, capture_output=True, text=True, timeout=60)


def values_of(path):
    return {v for _, _, v in sk.parse_example(path)} if path else set()


def no_values_printed(r, full):
    """stdout/stderr hold key names, counts and paths only: no value of the site env (other than tiny or
    numeric ones that also occur in key names or counts)."""
    out = r.stdout + r.stderr
    for line in full.read_text().splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        v = line.split("=", 1)[1].split("#", 1)[0].strip().strip('"')
        if len(v) >= 6 and not v.isdigit():
            assert v not in out, "a site value was printed"


def assignments(path):
    return [l for l in path.read_text().splitlines() if l and not l.startswith("#")]


# ------------------------------------------------------------------------------------------- env slice
def test_slice_covenant(checkout, tmp_path):
    repo, home, full = checkout
    out = tmp_path / "covenant.env"
    r = deck(checkout, "env", "slice", "covenant", "--site-env", str(full), "-o", str(out))
    assert r.returncode == 0, r.stderr
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    reg = sk.load_registry()
    want = sk.component_keys(reg, "covenant")
    got = [l.split("=", 1)[0] for l in assignments(out)]
    assert got == [k for k in want if k in got] and set(got) == set(want) - {"TELEMETRY_GPU_EXPECTED_WIDTH"}
    full_lines = full.read_text().splitlines()
    assert all(l in full_lines for l in assignments(out))       # copied verbatim
    assert any(l.startswith(f"SPARK_DIGEST_HOST={DIGEST} ") for l in assignments(out))  # the secrets spec needs it
    assert "EXTRA_UNKNOWN_KEY" not in out.read_text() and "JUDGE_" not in out.read_text()
    assert "EXTRA_UNKNOWN_KEY" in r.stdout and f"{len(got)} keys" in r.stdout
    no_values_printed(r, full)


def test_slice_default_output_and_lookup_order(checkout, tmp_path):
    repo, home, full = checkout
    cfg = home / ".config" / "deck" / "site.env"
    cfg.parent.mkdir(parents=True)
    shutil.copy2(full, cfg)
    r = deck(checkout, "env", "slice", "digest")
    assert r.returncode == 0, r.stderr
    out = home / ".config" / "deck" / "slices" / "digest.env"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600 and stat.S_IMODE(out.parent.stat().st_mode) == 0o700
    assert any(l.startswith(f"SPARK_DIGEST_HOST={DIGEST} ") for l in assignments(out))
    # $SITE_ENV beats ~/.config/deck/site.env; a named path must exist
    other = tmp_path / "other.env"
    other.write_text("SPARK_DIGEST_HOST=other.example.net\n")
    r = deck(checkout, "env", "slice", "digest", "-o", str(tmp_path / "d.env"), SITE_ENV=str(other))
    assert r.returncode == 0 and assignments(tmp_path / "d.env") == ["SPARK_DIGEST_HOST=other.example.net"]
    r = deck(checkout, "env", "slice", "digest", SITE_ENV=str(tmp_path / "missing.env"))
    assert r.returncode == 2
    cfg.unlink()
    r = deck(checkout, "env", "slice", "digest")
    assert r.returncode == 2 and "no site.env" in r.stderr


def test_slice_refuses_the_judge_and_unknown_components(checkout):
    _, _, full = checkout
    r = deck(checkout, "env", "slice", "judge", "--site-env", str(full))
    assert r.returncode == 2 and "exempt from slicing" in r.stderr and "sitemask" in r.stderr
    r = subprocess.run([sys.executable, "-B", str(checkout[0] / "tools/site_keys.py"), "slice", "judge",
                        "--site-env", str(full)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "exempt from slicing" in r.stderr
    r = deck(checkout, "env", "slice", "nosuch", "--site-env", str(full))
    assert r.returncode == 2 and "unknown component" in r.stderr


def test_slice_output_must_be_a_regular_file(checkout, tmp_path):
    _, _, full = checkout
    target = tmp_path / "target.txt"
    target.write_text("keep\n")
    link = tmp_path / "link.env"
    link.symlink_to(target)
    r = deck(checkout, "env", "slice", "covenant", "--site-env", str(full), "-o", str(link))
    assert r.returncode == 2 and "not a regular file" in r.stderr and target.read_text() == "keep\n"
    d = tmp_path / "dir.env"
    d.mkdir()
    r = deck(checkout, "env", "slice", "covenant", "--site-env", str(full), "-o", str(d))
    assert r.returncode == 2 and "not a regular file" in r.stderr
    r = deck(checkout, "env", "slice", "covenant", "--site-env", str(full), "-o", str(full))
    assert r.returncode == 2 and "site env itself" in r.stderr
    old = tmp_path / "old.env"
    old.write_text("stale\n")
    old.chmod(0o644)
    r = deck(checkout, "env", "slice", "covenant", "--site-env", str(full), "-o", str(old))
    assert r.returncode == 0 and stat.S_IMODE(old.stat().st_mode) == 0o600 and "stale" not in old.read_text()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".slice-")]


def test_slice_refuses_a_value_that_would_change(checkout, tmp_path):
    _, _, full = checkout
    bad = tmp_path / "bad.env"
    # CHAT_GROUP is not in the covenant slice, so this value would change there
    bad.write_text(full.read_text() + 'SPARK_CHAT_HOST="chat-${CHAT_GROUP}.example.com"\n')
    out = tmp_path / "c.env"
    r = deck(checkout, "env", "slice", "covenant", "--site-env", str(bad), "-o", str(out))
    assert r.returncode == 1 and "SPARK_CHAT_HOST" in r.stderr and not out.exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".slice-")]


def test_gen_secrets_covenant_sees_the_digest_through_a_slice(checkout, tmp_path):
    repo, home, full = checkout
    out = tmp_path / "covenant.env"
    assert deck(checkout, "env", "slice", "covenant", "--site-env", str(full), "-o", str(out)).returncode == 0
    dest = tmp_path / "dest"
    env = {"PATH": os.environ["PATH"], "HOME": str(home)}
    for extra, args in (({}, ["--site-env", str(out)]), ({"SITE_ENV": str(out)}, [])):
        r = subprocess.run(["bash", str(repo / "scripts/gen-secrets.sh"), "covenant", "--dry-run",
                            "--destdir", str(dest), *args], env={**env, **extra},
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert "would create /etc/oauth2-proxy-digest/cookie-secret" in r.stderr
    assert not dest.exists() or not any(dest.rglob("*"))     # a dry run writes nothing


# ------------------------------------------------------------------------------------------- check
def test_check_ok_and_unknown_key_warns(checkout):
    _, _, full = checkout
    r = deck(checkout, "check", "--site-env", str(full))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WARN unknown key" in r.stdout and "EXTRA_UNKNOWN_KEY" in r.stdout and "deck check: OK" in r.stdout
    assert "judge.env: none" in r.stdout
    no_values_printed(r, full)


def test_check_missing_required_key_fails(checkout, tmp_path):
    _, _, full = checkout
    env = tmp_path / "s.env"
    env.write_text("".join(l + "\n" for l in full.read_text().splitlines()
                           if not l.startswith(("WG_BACKEND_PUBLIC_KEY=", "MODELS_DIR="))) + 'BACKEND_LAN_IF=""\n')
    r = deck(checkout, "check", "--site-env", str(env))
    assert r.returncode == 1
    for k in ("WG_BACKEND_PUBLIC_KEY", "MODELS_DIR", "BACKEND_LAN_IF"):
        assert f"required key missing or empty: {k}" in r.stdout
    no_values_printed(r, full)


def test_check_without_a_site_env_exits_2(checkout):
    r = deck(checkout, "check")
    assert r.returncode == 2 and "no site.env" in r.stderr


def test_check_judge_env(checkout, tmp_path):
    _, home, full = checkout
    je = home / ".config" / "judge" / "judge.env"
    je.parent.mkdir(parents=True)
    je.write_text("SITE_ENV=~/.config/deck/site.env\nJUDGE_MODE=local\nJUDGE_STALL_MINUTES=5\n")
    r = deck(checkout, "check", "--site-env", str(full))
    assert r.returncode == 0 and "judge.env: 3 keys" in r.stdout
    je.write_text("SITE_ENV=~/.config/deck/site.env\nJUDGE_REPO_DIR=/srv/fake/deck\nEDGE_SSH_USER=ubuntu\n"
                  "JUDGE_SOMETHING_NEW=1\n")
    r = deck(checkout, "check", "--site-env", str(full))
    assert r.returncode == 1
    assert "site-identifying key in judge.env (move it to site.env): JUDGE_REPO_DIR" in r.stdout
    assert "site-identifying key in judge.env (move it to site.env): EDGE_SSH_USER" in r.stdout
    assert "WARN unknown judge knob" in r.stdout and "/srv/fake/deck" not in r.stdout
    r = deck(checkout, "check", "--site-env", str(full), JUDGE_ENV=str(tmp_path / "missing.env"))
    assert r.returncode == 1 and "not a readable file" in r.stdout


def test_help_lists_the_new_commands(checkout):
    r = deck(checkout, "--help")
    assert r.returncode == 0 and "env slice <component>" in r.stdout and "deck check" in r.stdout
