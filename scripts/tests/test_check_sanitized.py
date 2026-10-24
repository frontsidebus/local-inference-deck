"""scripts/check-sanitized.sh: site-env lookup, judge.env, fail-closed exit codes, --regex-only,
--history. Every test builds a throwaway git repo with example-valued fake "real" values; HOME is a
temp dir, so the operator's ~/.config is never read. Each test runs twice: through the scripts/ shim and
through the tools/deck-tools/ engine it execs. Run:
    python3 -m pytest scripts/tests -q -p no:cacheprovider
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check-sanitized.sh"
DECK_TOOLS = REPO / "tools" / "deck-tools"
ENTRY = {"rel": "scripts/check-sanitized.sh"}      # set per test by the `entry` fixture


@pytest.fixture(autouse=True, params=["shim", "engine"])
def entry(request):
    ENTRY["rel"] = ("scripts/check-sanitized.sh" if request.param == "shim"
                    else "tools/deck-tools/check-sanitized.sh")
    return request.param


def copy_tools(r):
    """The shim, the deck-tools engines it execs and their lock, as a vendoring repo has them."""
    (r / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(SCRIPT, r / "scripts" / "check-sanitized.sh")
    shutil.copytree(DECK_TOOLS, r / "tools" / "deck-tools", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(REPO / "tools" / "tools.lock", r / "tools" / "tools.lock")

EXAMPLE = """\
SPARK_DOMAIN=example.com              # public example
EDGE_PUBLIC_IP=203.0.113.10
WEBUI_PORT=3000
JUDGE_MODE=local
JUDGE_REPO_DIR=
"""
# Fake "real" values. They only need to differ from the example and be >= 6 chars.
VAL_A = "fakesite-alpha.invalid"
VAL_B = "fakesite-bravo.invalid"
VAL_J = "/home/fakeuser-zulu/src/deck"
# Built at runtime so this file itself never contains a secret-shaped string.
FAKE_SECRET = "sk-" + "Q" * 24


def site_env(domain):
    return EXAMPLE.replace("SPARK_DOMAIN=example.com", f"SPARK_DOMAIN={domain}")


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return {"PATH": os.environ["PATH"], "HOME": str(home), "LC_ALL": "C",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


@pytest.fixture
def repo(tmp_path, env):
    r = tmp_path / "repo"
    copy_tools(r)
    (r / "site.env.example").write_text(EXAMPLE)
    (r / ".gitignore").write_text("site.env\n*.env\n!*.env.example\n.sanitize-extra\n.sanitize-words\n")
    (r / "README.md").write_text("Domain: example.com\n")
    subprocess.run(["git", "init", "-q", str(r)], check=True, env=env)
    git(r, "add", "-A")
    subprocess.run(["git", "-C", str(r), "commit", "-qm", "init"], check=True, env=env)
    return r


def run(repo, env, *args, **extra):
    e = dict(env, **extra)
    return subprocess.run(["bash", str(repo / ENTRY["rel"]), *args],
                          cwd=repo, env=e, capture_output=True, text=True, timeout=60)


def commit(repo, env, path, text, msg="c"):
    (repo / path).write_text(text)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", msg], check=True, env=env)


def cfg(env, *parts):
    p = Path(env["HOME"], ".config", *parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ---- basics -----------------------------------------------------------------------------------

def test_clean_repo_with_root_site_env(repo, env):
    (repo / "site.env").write_text(site_env(VAL_A))
    r = run(repo, env, "--all")
    assert r.returncode == 0, r.stdout + r.stderr


def test_planted_value_found_via_root_site_env(repo, env):
    (repo / "site.env").write_text(site_env(VAL_A))
    commit(repo, env, "doc.md", f"see https://{VAL_A}/\n")
    r = run(repo, env, "--all")
    assert r.returncode == 1 and "doc.md" in r.stdout


def test_default_mode_checks_untracked_files(repo, env):
    (repo / "site.env").write_text(site_env(VAL_A))
    (repo / "new.md").write_text(f"{VAL_A}\n")
    assert run(repo, env).returncode == 1


def test_example_values_and_skipped_keys_are_not_flagged(repo, env):
    (repo / "site.env").write_text(site_env(VAL_A).replace("WEBUI_PORT=3000", "WEBUI_PORT=13000")
                                   .replace("JUDGE_MODE=local", "JUDGE_MODE=frontier-only"))
    commit(repo, env, "doc.md", "example.com 13000 frontier-only\n")
    r = run(repo, env, "--all")
    assert r.returncode == 0, r.stdout


# ---- lookup order: --site-env > $SITE_ENV > ~/.config/deck/site.env > <repo>/site.env ---------

@pytest.fixture
def planted(repo, env):
    commit(repo, env, "doc.md", f"host {VAL_A}\n")
    return repo


def test_only_config_site_env(planted, env):
    cfg(env, "deck", "site.env").write_text(site_env(VAL_A))
    assert run(planted, env, "--all").returncode == 1


def test_config_beats_repo_root(planted, env):
    cfg(env, "deck", "site.env").write_text(site_env(VAL_B))
    (planted / "site.env").write_text(site_env(VAL_A))
    assert run(planted, env, "--all").returncode == 0


def test_config_symlink_is_followed(planted, env, tmp_path):
    real = tmp_path / "operator-site.env"
    real.write_text(site_env(VAL_A))
    cfg(env, "deck", "site.env").symlink_to(real)
    assert run(planted, env, "--all").returncode == 1


def test_site_env_var_beats_config(planted, env, tmp_path):
    cfg(env, "deck", "site.env").write_text(site_env(VAL_B))
    se = tmp_path / "se.env"
    se.write_text(site_env(VAL_A))
    assert run(planted, env, "--all", SITE_ENV=str(se)).returncode == 1
    se.write_text(site_env(VAL_B))
    cfg(env, "deck", "site.env").write_text(site_env(VAL_A))
    assert run(planted, env, "--all", SITE_ENV=str(se)).returncode == 0


def test_flag_beats_site_env_var(planted, env, tmp_path):
    a, b = tmp_path / "a.env", tmp_path / "b.env"
    a.write_text(site_env(VAL_A))
    b.write_text(site_env(VAL_B))
    assert run(planted, env, "--all", "--site-env", str(a), SITE_ENV=str(b)).returncode == 1
    assert run(planted, env, "--all", "--site-env", str(b), SITE_ENV=str(a)).returncode == 0


def test_site_env_without_repo_root_file(planted, env, tmp_path):
    """A checkout with no site.env but SITE_ENV set still rejects a planted value."""
    se = tmp_path / "se.env"
    se.write_text(site_env(VAL_A))
    assert not (planted / "site.env").exists()
    assert run(planted, env, "--all", SITE_ENV=str(se)).returncode == 1


# ---- judge.env --------------------------------------------------------------------------------

def test_value_only_in_judge_env_is_caught(repo, env):
    (repo / "site.env").write_text(site_env(VAL_B))
    cfg(env, "judge", "judge.env").write_text(f"SITE_ENV=/nonexistent\nJUDGE_REPO_DIR={VAL_J}\n")
    commit(repo, env, "doc.md", f"repo at {VAL_J}\n")
    r = run(repo, env, "--all")
    assert r.returncode == 1 and "doc.md" in r.stdout


def test_judge_env_var_overrides_default_path(repo, env, tmp_path):
    (repo / "site.env").write_text(site_env(VAL_B))
    je = tmp_path / "j.env"
    je.write_text(f"JUDGE_REPO_DIR={VAL_J}\n")
    commit(repo, env, "doc.md", f"{VAL_J}\n")
    assert run(repo, env, "--all").returncode == 0          # no judge.env at the default path
    assert run(repo, env, "--all", JUDGE_ENV=str(je)).returncode == 1


def test_judge_policy_knobs_stay_public(repo, env):
    (repo / "site.env").write_text(site_env(VAL_B))
    cfg(env, "judge", "judge.env").write_text("JUDGE_MODE=frontier-only\n")
    commit(repo, env, "doc.md", "JUDGE_MODE=frontier-only\n")
    assert run(repo, env, "--all").returncode == 0


def test_missing_explicit_judge_env_is_exit_2(repo, env, tmp_path):
    (repo / "site.env").write_text(site_env(VAL_B))
    r = run(repo, env, "--all", JUDGE_ENV=str(tmp_path / "nope.env"))
    assert r.returncode == 2 and "nope.env" in r.stderr


# ---- fail closed --------------------------------------------------------------------------------

def test_no_source_is_exit_2_and_names_paths(repo, env):
    r = run(repo, env, "--all")
    assert r.returncode == 2
    assert ".config/deck/site.env" in r.stderr and str(repo / "site.env") in r.stderr


def test_no_source_is_exit_2_in_default_mode_too(repo, env):
    assert run(repo, env).returncode == 2


def test_dangling_config_symlink_is_exit_2(repo, env, tmp_path):
    (repo / "site.env").write_text(site_env(VAL_A))    # a fallback exists, but is not used
    cfg(env, "deck", "site.env").symlink_to(tmp_path / "gone.env")
    r = run(repo, env, "--all")
    assert r.returncode == 2 and "dangling" in r.stderr


def test_missing_site_env_var_is_exit_2(repo, env, tmp_path):
    (repo / "site.env").write_text(site_env(VAL_A))
    r = run(repo, env, "--all", SITE_ENV=str(tmp_path / "nope.env"))
    assert r.returncode == 2 and "nope.env" in r.stderr


def test_dangling_judge_env_symlink_is_exit_2(repo, env, tmp_path):
    (repo / "site.env").write_text(site_env(VAL_A))
    cfg(env, "judge", "judge.env").symlink_to(tmp_path / "gone.env")
    assert run(repo, env, "--all").returncode == 2


def test_unknown_argument_is_exit_2(repo, env):
    (repo / "site.env").write_text(site_env(VAL_A))
    assert run(repo, env, "--bogus").returncode == 2


# ---- --regex-only ---------------------------------------------------------------------------------

def test_regex_only_refused_without_ci(repo, env):
    (repo / "site.env").write_text(site_env(VAL_A))
    r = run(repo, env, "--all", "--regex-only")
    assert r.returncode == 2 and "CI" in r.stderr
    assert run(repo, env, "--all", "--regex-only", CI="1").returncode == 2


def test_regex_only_in_ci_needs_no_site_env(repo, env):
    commit(repo, env, "doc.md", f"host {VAL_A}\n")   # not a regex finding
    r = run(repo, env, "--all", "--regex-only", CI="true")
    assert r.returncode == 0, r.stdout + r.stderr


def test_regex_only_in_ci_catches_secret_shapes(repo, env):
    commit(repo, env, "conf.txt", f"key: {FAKE_SECRET}\n")
    r = run(repo, env, "--all", "--regex-only", CI="true")
    assert r.returncode == 1 and "conf.txt" in r.stdout


# ---- --history ----------------------------------------------------------------------------------

def test_history_finds_value_only_in_an_old_commit(repo, env):
    (repo / "site.env").write_text(site_env(VAL_A))
    commit(repo, env, "old.md", f"once: {VAL_A}\n", "leak")
    leak = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()
    commit(repo, env, "old.md", "fixed\n", "fix")
    assert run(repo, env, "--all").returncode == 0
    r = run(repo, env, "--history")
    assert r.returncode == 1
    assert leak[:12] in r.stdout and "old.md" in r.stdout and "SPARK_DOMAIN" in r.stdout
    assert VAL_A not in r.stdout + r.stderr                 # the value itself is never printed


def test_history_words_and_extra(repo, env):
    (repo / "site.env").write_text(site_env(VAL_B))
    cfg(env, "deck", ".sanitize-words").write_text("# names\nZorblat\n")
    (repo / ".sanitize-extra").write_text("extra-literal-xyz\n")
    commit(repo, env, "a.md", "hi zorblat\nand extra-literal-xyz here\n")
    commit(repo, env, "a.md", "clean\n")
    r = run(repo, env, "--history")
    assert r.returncode == 1
    assert "sanitize-words entry" in r.stdout and "sanitize-extra" in r.stdout
    assert "orblat" not in r.stdout.lower() and "extra-literal-xyz" not in r.stdout


def test_history_clean(repo, env):
    (repo / "site.env").write_text(site_env(VAL_A))
    r = run(repo, env, "--history")
    assert r.returncode == 0, r.stdout


def test_history_fails_closed_without_source(repo, env):
    assert run(repo, env, "--history").returncode == 2


# ---- .sanitize-* from ~/.config/deck ----------------------------------------------------------------

def test_sanitize_extra_from_config_dir(repo, env):
    (repo / "site.env").write_text(site_env(VAL_B))
    cfg(env, "deck", ".sanitize-extra").write_text("only-in-config-extra\n")
    commit(repo, env, "doc.md", "only-in-config-extra\n")
    assert run(repo, env, "--all").returncode == 1


def test_sanitize_words_from_repo_root(repo, env):
    (repo / "site.env").write_text(site_env(VAL_B))
    (repo / ".sanitize-words").write_text("Zorblat\n")
    commit(repo, env, "doc.md", "ZORBLAT was here; zorblatty is fine\n")
    r = run(repo, env, "--all")
    assert r.returncode == 1 and "doc.md" in r.stdout


# ---- pre-commit hook --------------------------------------------------------------------------------

def test_pre_commit_hook_blocks_findings_and_missing_source(repo, env, tmp_path):
    hooks = repo / "hooks"
    hooks.mkdir()
    shutil.copy2(REPO / "hooks" / "pre-commit", hooks / "pre-commit")
    git(repo, "config", "core.hooksPath", "hooks")

    def try_commit(name, text):
        (repo / name).write_text(text)
        subprocess.run(["git", "-C", str(repo), "add", name], check=True, env=env)
        return subprocess.run(["git", "-C", str(repo), "commit", "-qm", name], env=env,
                              capture_output=True, text=True)

    r = try_commit("a.md", "fine\n")                       # no real-value source -> blocked
    assert r.returncode != 0 and "no real-value source" in r.stderr
    (repo / "site.env").write_text(site_env(VAL_A))
    assert try_commit("a.md", "fine\n").returncode == 0
    r = try_commit("b.md", f"{VAL_A}\n")
    assert r.returncode != 0 and "b.md" in r.stdout + r.stderr
