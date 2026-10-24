"""deck-tools engines (tools/deck-tools/*.sh) with an explicit repo root: render --keys union semantics, --repo and
--spec-dir, argument order through the scripts/ shims, and check-sanitized's repo-local example baseline.

Throwaway repos with example-valued fake "real" values only; HOME is a temp dir, so ~/.config is never read.
The rest of the sanitizer's fail-closed cases (no source, dangling symlinks, judge.env, --regex-only, --history)
are in scripts/tests/test_check_sanitized.py, which runs each of them through the shim and the engine.
Run: python3 -m pytest tools/tests -q -p no:cacheprovider
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DT = REPO / "tools" / "deck-tools"
SHIMS = ("render", "gen-secrets", "check-sanitized")
FAKE = "fakesite-kilo.invalid"          # a fake "real" value: differs from every example, >= 6 chars


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return {"PATH": os.environ["PATH"], "HOME": str(home), "LC_ALL": "C",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def vendor(r):
    """r gets tools/deck-tools/, tools/tools.lock and the scripts/ shims, as a vendoring repo has them."""
    shutil.copytree(DT, r / "tools" / "deck-tools", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(REPO / "tools" / "tools.lock", r / "tools" / "tools.lock")
    (r / "scripts").mkdir(parents=True, exist_ok=True)
    for x in SHIMS:
        shutil.copy2(REPO / "scripts" / f"{x}.sh", r / "scripts" / f"{x}.sh")
    return r


def git_init(r, env):
    subprocess.run(["git", "init", "-q", str(r)], check=True, env=env)
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(r), "commit", "-qm", "init"], check=True, env=env)
    return r


def sh(argv, env, cwd=None):
    return subprocess.run([str(a) for a in argv], env=env, cwd=cwd, capture_output=True, text=True, timeout=60)


# ---- render: --keys keeps today's union semantics ----------------------------------------------------------

@pytest.fixture
def rrepo(tmp_path):
    r = vendor(tmp_path / "repo")
    (r / "site.env.example").write_text("IN_EXAMPLE=example.com\n")
    (r / "t.tmpl").write_text("a=${IN_EXAMPLE} b=${ONLY_IN_SITE} c=${NOT_A_SITE_VAR} d=${IN_KEYS} $host\n")
    return r


def render(entry, repo, env, *args):
    out = repo / "out.txt"
    if out.exists():
        out.unlink()
    argv = ["bash", repo / "scripts/render.sh"] if entry == "shim" else ["bash", repo / "tools/deck-tools/render.sh"]
    r = sh([*argv, *args, repo / "t.tmpl", out], env)
    return r, (out.read_text() if r.returncode == 0 else None)


@pytest.mark.parametrize("entry", ["shim", "engine"])
def test_render_union_a_name_only_in_the_site_env_still_expands(rrepo, env, tmp_path, entry):
    site = tmp_path / "site.env"
    site.write_text("IN_EXAMPLE=one.example.org\nONLY_IN_SITE=two.example.org\nIN_KEYS=three.example.org\n")
    keys = tmp_path / "keys.env"
    keys.write_text("IN_KEYS=placeholder\n")
    # default keys file: <repo>/site.env.example; IN_KEYS is in the site env, so it expands too
    r, out = render(entry, rrepo, env, "-e", site)
    assert r.returncode == 0, r.stderr
    assert out == "a=one.example.org b=two.example.org c=${NOT_A_SITE_VAR} d=three.example.org $host\n"
    # --keys FILE replaces the example as the contract list; the site env's names are still added
    r, out = render(entry, rrepo, env, "--keys", keys, "-e", site)
    assert r.returncode == 0, r.stderr
    assert out == "a=one.example.org b=two.example.org c=${NOT_A_SITE_VAR} d=three.example.org $host\n"


def test_render_vars_is_keys_union_site_env(rrepo, env, tmp_path):
    site = tmp_path / "site.env"
    site.write_text("ONLY_IN_SITE=x\n")
    keys = tmp_path / "keys.env"
    keys.write_text("IN_KEYS=1\nexport ALSO_KEYS=2\n# COMMENTED=3\n")
    r = sh(["bash", rrepo / "scripts/render.sh", "-e", site, "--vars"], env)
    assert r.stdout.split() == ["IN_EXAMPLE", "ONLY_IN_SITE"]
    r = sh(["bash", rrepo / "scripts/render.sh", "--keys", keys, "-e", site, "--vars"], env)
    assert r.stdout.split() == ["ALSO_KEYS", "IN_KEYS", "ONLY_IN_SITE"]


def test_render_a_keys_only_name_that_is_empty_refuses(rrepo, env, tmp_path):
    (rrepo / "t.tmpl").write_text("d=${IN_KEYS}\n")
    site = tmp_path / "site.env"
    site.write_text("IN_EXAMPLE=x\n")
    keys = tmp_path / "keys.env"
    keys.write_text("IN_KEYS=placeholder\n")
    r, _ = render("shim", rrepo, env, "--keys", keys, "-e", site)
    assert r.returncode != 0 and "IN_KEYS" in r.stderr and "missing values" in r.stderr


def test_render_missing_keys_file_or_repo_is_an_error(rrepo, env, tmp_path):
    site = tmp_path / "site.env"
    site.write_text("IN_EXAMPLE=x\n")
    r, _ = render("shim", rrepo, env, "--keys", tmp_path / "nope.env", "-e", site)
    assert r.returncode != 0 and "--keys" in r.stderr
    r, _ = render("engine", rrepo, env, "--repo", tmp_path / "nope", "-e", site)
    assert r.returncode != 0 and "--repo" in r.stderr


def test_render_repo_flag_picks_the_example_and_site_env(rrepo, env, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "site.env.example").write_text("ONLY_IN_OTHER=example.net\n")
    (other / "site.env").write_text("ONLY_IN_OTHER=other.example.net\nIN_EXAMPLE=o\n")
    (rrepo / "t.tmpl").write_text("${ONLY_IN_OTHER}\n")
    # the shim passes --repo <its repo> first; a caller's later --repo wins, in either position
    for args in (["--repo", other], ["--repo=" + str(other)]):
        r, out = render("shim", rrepo, env, *args)
        assert r.returncode == 0, r.stderr
        assert out == "other.example.net\n"


@pytest.mark.parametrize("order", ["e-first", "e-last"])
def test_render_shim_option_order_and_excludes(rrepo, env, tmp_path, order):
    site = tmp_path / "site.env"
    site.write_text("IN_EXAMPLE=x.example.org\nONLY_IN_SITE=y\nIN_KEYS=z\n")
    src = rrepo / "src"
    (src / "sub").mkdir(parents=True)
    (src / "keep.tmpl").write_text("${IN_EXAMPLE}\n")
    (src / "sub" / "skip-me.tmpl").write_text("${UNSET_BUT_SKIPPED}\n")
    opts = ["-e", str(site), "-x", "skip-*"]
    opts = opts if order == "e-first" else opts[2:] + opts[:2]
    dst = tmp_path / "dst"
    r = sh(["bash", rrepo / "scripts/render.sh", *opts, "--repo", rrepo, src, dst], env)
    assert r.returncode == 0, r.stderr
    assert (dst / "keep").read_text() == "x.example.org\n" and not (dst / "sub" / "skip-me").exists()


# ---- gen-secrets: --repo and --spec-dir --------------------------------------------------------------------

SPEC = 'gs_note "spec REPO=$REPO"\ngs_note "site=${GS_SITE_ENV##*/}"\n'


@pytest.mark.parametrize("entry", ["shim", "engine"])
def test_gen_secrets_spec_dir_default_and_flag(tmp_path, env, entry):
    r_ = vendor(tmp_path / "repo")
    (r_ / "scripts" / "secrets.d").mkdir()
    (r_ / "scripts" / "secrets.d" / "demo.sh").write_text(SPEC)
    alt = tmp_path / "alt-specs"
    alt.mkdir()
    (alt / "demo.sh").write_text('gs_note "alt spec"\n')
    exe = r_ / ("scripts/gen-secrets.sh" if entry == "shim" else "tools/deck-tools/gen-secrets.sh")
    root = tmp_path / "root"
    r = sh(["bash", exe, "demo", "--dry-run", "--destdir", root], env)
    assert r.returncode == 0, r.stderr
    assert f"spec REPO={r_.resolve()}" in r.stderr and "site=site.env" in r.stderr
    r = sh(["bash", exe, "--spec-dir", alt, "demo", "--dry-run", "--destdir", root], env)
    assert r.returncode == 0 and "alt spec" in r.stderr
    r = sh(["bash", exe, "demo", "--dry-run", "--spec-dir=" + str(tmp_path / "none")], env)
    assert r.returncode == 1 and "no secrets spec" in r.stderr


def test_gen_secrets_caller_repo_overrides_the_shim(tmp_path, env):
    r_ = vendor(tmp_path / "repo")
    other = tmp_path / "other"
    (other / "scripts" / "secrets.d").mkdir(parents=True)
    (other / "scripts" / "secrets.d" / "demo.sh").write_text(SPEC)
    r = sh(["bash", r_ / "scripts/gen-secrets.sh", "demo", "--repo", other, "--dry-run"], env)
    assert r.returncode == 0, r.stderr
    assert f"spec REPO={other.resolve()}" in r.stderr
    r = sh(["bash", r_ / "scripts/gen-secrets.sh", "--repo", tmp_path / "missing", "demo", "--dry-run"], env)
    assert r.returncode == 2 and "--repo" in r.stderr


def test_gen_secrets_help_still_prints_the_header(tmp_path, env):
    r = sh(["bash", REPO / "scripts/gen-secrets.sh", "--help"], env)
    assert r.returncode == 0 and "NEVER overwrite an existing secret" in r.stdout and "set -euo" not in r.stdout


# ---- the shims -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", SHIMS)
def test_shims_are_executable_in_git_and_on_disk(name):
    """covenant/deploy.sh runs scripts/gen-secrets.sh only when it is executable (else it sources the spec)."""
    rel = f"scripts/{name}.sh"
    assert os.access(REPO / rel, os.X_OK), rel
    assert os.access(DT / f"{name}.sh", os.X_OK)
    r = subprocess.run(["git", "ls-files", "-s", "--", rel, f"tools/deck-tools/{name}.sh"], cwd=REPO,
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0 or not r.stdout:
        pytest.skip("not a git checkout")
    assert [l.split()[0] for l in r.stdout.splitlines()] == ["100755", "100755"]


@pytest.mark.parametrize("name", SHIMS)
def test_shims_only_exec_the_engine(name):
    code = [l for l in (REPO / f"scripts/{name}.sh").read_text().splitlines() if l and not l.startswith("#")]
    repo_arg = ' --repo "$here/.."' if name != "check-sanitized" else ""
    assert code == ['here=$(dirname "${BASH_SOURCE[0]}")',
                    f'exec "$here/../tools/deck-tools/{name}.sh"{repo_arg} "$@"']


def test_covenant_deploy_still_tests_the_executable_shim():
    src = (REPO / "covenant/deploy.sh").read_text()
    assert "[[ -x $REPO/scripts/gen-secrets.sh ]]" in src


# ---- check-sanitized: the repo-local example baseline ----------------------------------------------------------

def csrepo(tmp_path, env, files, name="repo"):
    r = vendor(tmp_path / name)
    for rel, text in files.items():
        p = r / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    (r / ".gitignore").write_text("site.env\n")
    return git_init(r, env)


def check(repo, env, *args, entry="shim", cwd=None):
    exe = repo / ("scripts/check-sanitized.sh" if entry == "shim" else "tools/deck-tools/check-sanitized.sh")
    return sh(["bash", exe, "--all", *args], env, cwd=cwd or repo)


@pytest.mark.parametrize("entry", ["shim", "engine"])
def test_only_foo_env_example_its_defaults_are_not_flagged(tmp_path, env, entry):
    repo = csrepo(tmp_path, env, {"foo.env.example": 'FOO_HOST="foo-default.example.org"   # a comment\n',
                                  "doc.md": "see foo-default.example.org\n"})
    site = tmp_path / "site.env"
    site.write_text("FOO_HOST=foo-default.example.org\n")       # the operator kept the default
    r = check(repo, env, "--site-env", site, entry=entry)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "no example baseline" not in r.stderr
    site.write_text(f"FOO_HOST={FAKE}\n")
    (repo / "doc.md").write_text(f"see {FAKE}\n")
    r = check(repo, env, "--site-env", site, entry=entry)
    assert r.returncode == 1 and "doc.md" in r.stdout


def test_union_of_every_root_env_example(tmp_path, env):
    repo = csrepo(tmp_path, env, {"a.env.example": "A_HOST=alpha-default.example.org\n",
                                  "b.env.example": "B_HOST=bravo-default.example.org\n",
                                  "sub/c.env.example": "C_HOST=charlie-default.example.org\n",
                                  "doc.md": "alpha-default.example.org bravo-default.example.org "
                                            "charlie-default.example.org\n"})
    site = tmp_path / "site.env"
    site.write_text("A_HOST=alpha-default.example.org\nB_HOST=bravo-default.example.org\n")
    assert check(repo, env, "--site-env", site).returncode == 0
    site.write_text("C_HOST=charlie-default.example.org\n")     # only the root's examples count
    r = check(repo, env, "--site-env", site)
    assert r.returncode == 1 and "doc.md" in r.stdout


def test_no_baseline_every_site_value_counts_as_real(tmp_path, env):
    repo = csrepo(tmp_path, env, {"doc.md": "nothing here\n"})
    site = tmp_path / "site.env"
    site.write_text("FOO_HOST=foo-default.example.org\nWEBUI_PORT=13000\n")
    r = check(repo, env, "--site-env", site)
    assert r.returncode == 0 and "no example baseline" in r.stderr     # clean, with the note
    (repo / "doc.md").write_text("foo-default.example.org\n")
    (repo / "port.md").write_text("port 13000\n")
    r = check(repo, env, "--site-env", site)
    assert r.returncode == 1 and "doc.md" in r.stdout                  # stricter, never looser
    assert "port.md" not in r.stdout                                    # the policy still skips *_PORT


def test_no_baseline_without_a_source_is_still_exit_2(tmp_path, env):
    repo = csrepo(tmp_path, env, {"doc.md": "x\n"})
    r = check(repo, env)
    assert r.returncode == 2 and "no real-value source" in r.stderr


REGISTRY = """\
# a contracts/site-keys.yaml-style registry (only the parts check-sanitized reads)
contract_version: 1
components:
  - {"name": "global", "description": "x"}
keys:
  - {"name": "FOO_HOST", "active": true, "default": "\\"reg-default.example.org\\"", "sanitize": "check"}
  - {"name": "OLD_HOST", "active": false, "default": "old-default.example.org", "sanitize": "check"}
"""


def test_registry_beats_env_examples_and_keys_beats_both(tmp_path, env):
    repo = csrepo(tmp_path, env, {"contracts/site-keys.yaml": REGISTRY,
                                  "site.env.example": "FOO_HOST=example-default.example.org\n"
                                                      "OLD_HOST=old-default.example.org\n",
                                  "doc.md": "reg-default.example.org\n"})
    site = tmp_path / "site.env"
    site.write_text("FOO_HOST=reg-default.example.org\n")
    assert check(repo, env, "--site-env", site).returncode == 0                # the registry default is public
    site.write_text("FOO_HOST=example-default.example.org\n")
    (repo / "doc.md").write_text("example-default.example.org\n")
    assert check(repo, env, "--site-env", site).returncode == 1                # the example is not consulted
    site.write_text("OLD_HOST=old-default.example.org\n")                       # inactive key: no default
    (repo / "doc.md").write_text("old-default.example.org\n")
    assert check(repo, env, "--site-env", site).returncode == 1
    keys = tmp_path / "keys.env.example"
    keys.write_text("OLD_HOST=old-default.example.org\n")
    assert check(repo, env, "--site-env", site, "--keys", keys).returncode == 0   # --keys (env style) wins
    reg = tmp_path / "other-keys.yaml"
    reg.write_text(REGISTRY.replace('"active": false', '"active": true'))
    assert check(repo, env, "--site-env", site, "--keys", reg).returncode == 0    # --keys (registry style)


@pytest.mark.parametrize("problem", ["malformed", "dangling", "missing-keys"])
def test_baseline_problems_fail_closed(tmp_path, env, problem):
    repo = csrepo(tmp_path, env, {"contracts/site-keys.yaml": REGISTRY, "doc.md": "x\n"})
    site = tmp_path / "site.env"
    site.write_text(f"FOO_HOST={FAKE}\n")
    reg = repo / "contracts" / "site-keys.yaml"
    args = []
    if problem == "malformed":
        reg.write_text(REGISTRY.replace('"sanitize": "check"}', '"sanitize": check}', 1))
    elif problem == "dangling":
        reg.unlink()
        reg.symlink_to(tmp_path / "gone.yaml")
    else:
        args = ["--keys", tmp_path / "nope.yaml"]
    r = check(repo, env, "--site-env", site, *args)
    assert r.returncode == 2, r.stdout + r.stderr
    assert FAKE not in r.stdout + r.stderr


def deck_baseline_repo(tmp_path, env, kind):
    """This repo's real baseline (contracts/site-keys.yaml or site.env.example), example values only."""
    files = {"doc.md": "x\n"}
    if kind == "registry":
        files["contracts/site-keys.yaml"] = (REPO / "contracts/site-keys.yaml").read_text()
    else:
        files["site.env.example"] = (REPO / "site.env.example").read_text()
    return csrepo(tmp_path, env, files, name=kind)


def test_the_deck_registry_and_example_give_the_same_baseline(tmp_path, env):
    """In this repo the baseline moved from site.env.example to contracts/site-keys.yaml: same result for every
    key. A site env equal to the example is clean under both; one with every value replaced is flagged
    identically (same keys, same lines)."""
    example = (REPO / "site.env.example").read_text()
    reg, ex = deck_baseline_repo(tmp_path, env, "registry"), deck_baseline_repo(tmp_path, env, "example")
    site = tmp_path / "site.env"
    site.write_text(example)
    for repo in (reg, ex):
        (repo / "doc.md").write_text(example)
        r = check(repo, env, "--site-env", site)
        assert r.returncode == 0, r.stdout + r.stderr
    planted, lines = [], []
    for line in example.splitlines():
        k, sep, _ = line.partition("=")
        if sep and k.isupper() and not line.startswith("#"):
            v = f"fake-{k.lower().replace('_', '-')}-value.invalid"
            lines.append(f"{k}={v}")
            planted.append(v)
    site.write_text("\n".join(lines) + "\n")
    outs = []
    for repo in (reg, ex):
        (repo / "doc.md").write_text("\n".join(planted) + "\n")
        r = check(repo, env, "--site-env", site)
        assert r.returncode == 1
        outs.append(r.stdout)
    assert outs[0] == outs[1]


# ---- check-sanitized: which repo is checked --------------------------------------------------------------------

def test_shim_checks_the_current_directory_repo_like_the_judge_probe(tmp_path, env):
    """probe.py check_sanitized runs the deck's scripts/check-sanitized.sh with cwd=<other checkout>."""
    deck = csrepo(tmp_path, env, {"site.env.example": "FOO_HOST=example.org\n", "doc.md": "clean\n"}, "deck")
    other = csrepo(tmp_path, env, {"site.env.example": "FOO_HOST=example.org\n", "doc.md": f"{FAKE}\n"}, "other")
    site = tmp_path / "site.env"
    site.write_text(f"FOO_HOST={FAKE}\n")
    r = sh(["bash", deck / "scripts/check-sanitized.sh", "--all", "--site-env", site], env, cwd=other)
    assert r.returncode == 1 and "doc.md" in r.stdout
    r = sh(["bash", deck / "scripts/check-sanitized.sh", "--all", "--site-env", site], env, cwd=deck)
    assert r.returncode == 0
    # --repo names the repo explicitly, from anywhere; a subdirectory means its work tree
    (other / "sub").mkdir()
    r = sh(["bash", deck / "tools/deck-tools/check-sanitized.sh", "--repo", other / "sub", "--all",
            "--site-env", site], env, cwd=tmp_path)
    assert r.returncode == 1 and "doc.md" in r.stdout
    r = sh(["bash", deck / "scripts/check-sanitized.sh", "--all", "--repo", tmp_path / "home",
            "--site-env", site], env, cwd=deck)
    assert r.returncode == 2 and "not a git work tree" in r.stderr


def test_history_with_repo_flag_finds_a_value_only_in_an_old_commit(tmp_path, env):
    repo = csrepo(tmp_path, env, {"foo.env.example": "FOO_HOST=example.org\n", "doc.md": f"{FAKE}\n"})
    (repo / "doc.md").write_text("gone\n")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "scrub"], check=True, env=env)
    site = tmp_path / "site.env"
    site.write_text(f"FOO_HOST={FAKE}\n")
    assert check(repo, env, "--site-env", site).returncode == 0          # the work tree is clean
    r = sh(["bash", REPO / "scripts/check-sanitized.sh", "--history", "--repo", repo, "--site-env", site], env,
           cwd=tmp_path)
    assert r.returncode == 1 and "doc.md: FOO_HOST" in r.stdout and FAKE not in r.stdout
