"""scripts/check-sanitized.sh with contracts/sanitize-policy.yaml versus its built-in table: every key of
contracts/site-keys.yaml, plus keys the registry does not know, gets the same check/skip/split behaviour.
Throwaway git repos with fake "real" values only; HOME is a temp dir. Run:
    python3 -m pytest scripts/tests -q -p no:cacheprovider
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check-sanitized.sh"
POLICY = REPO / "contracts" / "sanitize-policy.yaml"
sys.path.insert(0, str(REPO / "tools"))
import site_keys as sk  # noqa: E402

# keys the registry does not list: the policy's fallback globs and default must match the built-in table
UNKNOWN = ["NEW_THING_PORT", "NEW_LAN_IF", "NEW_VIEW_GROUP", "NEW_OIDC_CLIENT_ID", "JUDGE_NEW_KNOB",
           "NEW_SITE_HOST", "NEW_SSH_USER", "JUDGE_NEW_PORT", "X9_HOST"]


def fake(key):
    # distinct, >= 6 chars, not numeric; no value is a substring of another
    return f"fakeval-{key.lower()}-zz"


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return {"PATH": os.environ["PATH"], "HOME": str(home), "LC_ALL": "C",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def make_repo(root, env, with_policy, keys):
    r = root / ("with-policy" if with_policy else "builtin")
    (r / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, r / "scripts" / "check-sanitized.sh")
    shutil.copy2(REPO / "site.env.example", r / "site.env.example")   # example values only
    if with_policy:
        (r / "contracts").mkdir()
        shutil.copy2(POLICY, r / "contracts" / "sanitize-policy.yaml")
    lines = []
    for k in keys:
        lines += [fake(k) + "-one", fake(k) + "-two"] if k == "ADMIN_SOURCE_IPS" else [fake(k)]
    (r / "planted.txt").write_text("\n".join(lines) + "\n")
    (r / ".gitignore").write_text("site.env\n")
    subprocess.run(["git", "init", "-q", str(r)], check=True, env=env)
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(r), "commit", "-qm", "init"], check=True, env=env)
    return r


def flagged(repo, env, site_env):
    r = subprocess.run(["bash", str(repo / "scripts" / "check-sanitized.sh"), "--all", "--site-env", str(site_env)],
                       cwd=repo, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode in (0, 1), r.stderr
    hits = set()
    for line in r.stdout.splitlines():
        if line.startswith("planted.txt:"):
            hits.add(line.split(":", 2)[2])
    return r.returncode, hits


def test_policy_and_builtin_flag_the_same_values(tmp_path, env):
    reg = sk.load_registry()
    keys = [k["name"] for k in reg["keys"]] + UNKNOWN
    site_env = tmp_path / "site.env"
    site_env.write_text("".join(
        f'{k}="{fake(k)}-one {fake(k)}-two"\n' if k == "ADMIN_SOURCE_IPS" else f'{k}="{fake(k)}"\n' for k in keys))
    rc_b, builtin = flagged(make_repo(tmp_path, env, False, keys), env, site_env)
    rc_p, policy = flagged(make_repo(tmp_path, env, True, keys), env, site_env)
    assert builtin == policy
    assert rc_b == rc_p == 1
    # and both agree with the registry's classes
    expect = set()
    for k in keys:
        cls = sk.classify(reg, k)
        if sk.action_of(reg, cls) == "skip" or not sk.SANITIZER_KEY_RE.match(k):
            continue
        expect |= {fake(k) + "-one", fake(k) + "-two"} if k == "ADMIN_SOURCE_IPS" else {fake(k)}
    assert policy == expect
    assert fake("JUDGE_REPO_DIR") in policy and fake("SPARK_USERS") not in policy
    assert fake("NEW_SITE_HOST") in policy and fake("JUDGE_NEW_KNOB") not in policy


def test_malformed_or_dangling_policy_fails_closed(tmp_path, env):
    keys = ["SPARK_DOMAIN"]
    site_env = tmp_path / "site.env"
    site_env.write_text(f"SPARK_DOMAIN={fake('SPARK_DOMAIN')}\n")
    repo = make_repo(tmp_path, env, True, keys)
    pol = repo / "contracts" / "sanitize-policy.yaml"
    pol.write_text(pol.read_text() + "  - {key: SPARK_DOMAIN, action: skip}\n")
    r = subprocess.run(["bash", "scripts/check-sanitized.sh", "--all", "--site-env", str(site_env)],
                       cwd=repo, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "unexpected line" in r.stderr
    pol.unlink()
    pol.symlink_to(tmp_path / "gone.yaml")
    r = subprocess.run(["bash", "scripts/check-sanitized.sh", "--all", "--site-env", str(site_env)],
                       cwd=repo, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "dangling" in r.stderr
