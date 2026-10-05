"""walter/deploy.sh and scripts/render.sh, exercised with --destdir only.

--destdir installs under a temp prefix and skips every system action (apt, systemctl, docker, ufw),
so these tests never touch the host. Site values come from site.env.example (offsite and digest off)
or a copy with fake example values that turns both on. Run:
    python3 -m pytest walter/tests -q -p no:cacheprovider
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "site.env.example"


def write_env(tmp_path, name="site.env", **overrides):
    text = EXAMPLE.read_text()
    for k, v in overrides.items():
        text, n = re.subn(rf"^{k}=.*$", f'{k}="{v}"', text, flags=re.M)
        if not n:
            text += f'\n{k}="{v}"\n'
    p = tmp_path / name
    p.write_text(text)
    return p


def env_on(tmp_path):
    """Offsite and digest on (fake example values)."""
    return write_env(tmp_path, "site-on.env", RESTIC_BUCKET="spark-backups-example",
                     SPARK_DIGEST_HOST="digest.example.com",
                     HARNESS_KEYS="claude-code codex hermes opencode open-webui digest")


def repo_copy(tmp_path):
    """A throwaway copy of the parts deploy.sh needs, so tests can edit "repo" files."""
    dst = tmp_path / "repo"
    ign = shutil.ignore_patterns("__pycache__", "tests", ".pytest_cache")
    for d in ("walter", "scripts"):
        shutil.copytree(REPO / d, dst / d, ignore=ign, symlinks=True)
    shutil.copy2(EXAMPLE, dst / "site.env.example")
    return dst


def deploy(root, env, *args, repo=REPO, check=True):
    r = subprocess.run(["bash", str(repo / "walter" / "deploy.sh"), "--destdir", str(root),
                        "--site-env", str(env), *args],
                       capture_output=True, text=True, timeout=120)
    if check:
        assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    return r.stdout + r.stderr


def render(env, *args, repo=REPO):
    return subprocess.run(["bash", str(repo / "scripts" / "render.sh"), "-e", str(env), *args],
                          capture_output=True, text=True, timeout=60)


def seeded_root(tmp_path):
    """A destdir that already has the digest LiteLLM key, so the digest stack step runs."""
    root = tmp_path / "root"
    (root / "srv/gateway/keys").mkdir(parents=True)
    (root / "srv/gateway/keys/digest.key").write_text("dummy-test-key\n")
    return root


def changed_lines(out):
    return [l for l in out.splitlines() if re.match(r"^  (new|unreadable) ", l)]


def compose_lines(out, stack):
    return [l for l in out.splitlines() if f"/srv/{stack}/compose.yaml up -d" in l]


# ---- render.sh: optional blocks and excludes ---------------------------------------------------

def test_render_backup_offsite_off_with_excludes(tmp_path):
    env = write_env(tmp_path)  # RESTIC_BUCKET="" as in site.env.example
    out = tmp_path / "out"
    r = render(env, "-x", "spark-offsite*", "-x", "offsite-*", str(REPO / "walter/backup"), str(out))
    assert r.returncode == 0, r.stderr
    assert not list(out.glob("*offsite*.json")) and not list(out.glob("spark-offsite*"))
    restore = (out / "RESTORE.md").read_text()
    assert "## 7. Offsite copy (not configured)" in restore
    assert "no offsite copy" in restore and "@@" not in restore and "restic restore" not in restore


def test_render_backup_offsite_off_without_excludes_still_refuses_iam_policy(tmp_path):
    # Strictness is kept: the IAM policy really needs the bucket, so only deploy's -x skips it.
    r = render(write_env(tmp_path), str(REPO / "walter/backup"), str(tmp_path / "out"))
    assert r.returncode != 0 and "offsite-iam-policy.json.tmpl" in r.stderr
    assert "RESTORE.md.tmpl" not in r.stderr


@pytest.mark.parametrize("bucket,on", [("spark-backups-example", True), ("CHANGEME", False)])
def test_render_restore_md_bucket_set_or_changeme(tmp_path, bucket, on):
    env = write_env(tmp_path, RESTIC_BUCKET=bucket)
    r = render(env, str(REPO / "walter/backup"), str(tmp_path / "out"))
    assert r.returncode == 0, r.stderr
    restore = (tmp_path / "out/RESTORE.md").read_text()
    assert "@@" not in restore
    assert ("s3.us-east-1.amazonaws.com/spark-backups-example/walter" in restore) is on
    assert ("## 7. Offsite copy (not configured)" in restore) is not on


def test_render_cond_blocks(tmp_path):
    env = write_env(tmp_path, RESTIC_BUCKET="", SPARK_DIGEST_HOST="digest.example.com")
    t = tmp_path / "x.tmpl"
    t.write_text("a\n# @@if SPARK_DIGEST_HOST@@\nhost=${SPARK_DIGEST_HOST}\n# @@else@@\nno digest\n"
                 "# @@endif@@\n<!-- @@if RESTIC_BUCKET@@ -->\nb=${RESTIC_BUCKET}\n<!-- @@endif@@ -->\nz\n")
    r = render(env, str(t), str(tmp_path / "x"))
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "x").read_text() == "a\nhost=digest.example.com\nz\n"


@pytest.mark.parametrize("body,msg", [
    ("# @@if RESTIC_BUCKET@@\n# @@if RESTIC_BUCKET@@\n# @@endif@@\n", "nested"),
    ("# @@if RESTIC_BUCKET@@\nx\n", "unterminated"),
    ("x\n# @@endif@@\n", "without"),
])
def test_render_cond_errors(tmp_path, body, msg):
    t = tmp_path / "bad.tmpl"
    t.write_text(body)
    r = render(write_env(tmp_path), str(t), str(tmp_path / "bad"))
    assert r.returncode != 0 and msg in r.stderr


def test_render_without_blocks_is_byte_for_byte(tmp_path):
    t = tmp_path / "plain.tmpl"
    t.write_bytes(b"user=${BACKEND_SSH_USER} keep=${NOT_A_SITE_VAR} $host\nno newline at end")
    r = render(write_env(tmp_path), str(t), str(tmp_path / "plain"))
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "plain").read_bytes() == b"user=operator keep=${NOT_A_SITE_VAR} $host\nno newline at end"


# ---- deploy.sh --destdir: offsite off ---------------------------------------------------------

def test_deploy_example_env_offsite_off(tmp_path):
    root = tmp_path / "root"
    out = deploy(root, write_env(tmp_path))
    assert "  offsite: off (RESTIC_BUCKET is empty or CHANGEME in site.env)" in out
    assert "== offsite backups: off" in out
    restore = (root / "models/backups/RESTORE.md").read_text()
    assert "## 7. Offsite copy (not configured)" in restore and "@@" not in restore
    assert not list(root.rglob("*offsite*")) and not (root / "etc/spark-restic").exists()


def test_deploy_changeme_still_works(tmp_path):
    out = deploy(tmp_path / "root", write_env(tmp_path, RESTIC_BUCKET="CHANGEME"))
    assert "== offsite backups: off" in out


# ---- deploy.sh --destdir: persistenced restart and image rebuilds only on change ---------------

def test_second_run_no_restart_no_rebuild(tmp_path):
    root, env = seeded_root(tmp_path), env_on(tmp_path)
    first = deploy(root, env)
    assert "systemctl restart nvidia-persistenced.service" in first
    for stack in ("telemetry", "digest"):
        assert "--build" in compose_lines(first, stack)[0]
        assert (root / f"srv/{stack}/.build-hash").is_file()

    second = deploy(root, env)
    assert changed_lines(second) == []
    assert "systemctl restart nvidia-persistenced" not in second
    assert "systemctl start nvidia-persistenced.service" in second
    for stack in ("telemetry", "digest"):
        line = compose_lines(second, stack)[0]
        assert "--build" not in line and "--force-recreate" not in line
    assert second.count("rebuild: no (build inputs unchanged") == 2

    dry = deploy(root, env, "--dry-run")
    assert "--build" not in dry and "systemctl restart nvidia-persistenced" not in dry

    forced = deploy(root, env, "--rebuild")
    assert forced.count("rebuild: yes (--rebuild)") == 2
    assert all("--build" in compose_lines(forced, s)[0] for s in ("telemetry", "digest"))
    assert "systemctl restart nvidia-persistenced" not in forced


def test_repo_build_change_rebuilds_only_that_stack(tmp_path):
    repo, root, env = repo_copy(tmp_path), seeded_root(tmp_path), env_on(tmp_path)
    deploy(root, env, repo=repo)
    with open(repo / "walter/telemetry/build/requirements.txt", "a") as f:
        f.write("# test change\n")
    out = deploy(root, env, repo=repo)
    assert changed_lines(out) == ["  new      /srv/telemetry/build/requirements.txt"]
    assert "--build" in compose_lines(out, "telemetry")[0]
    assert "--build" not in compose_lines(out, "digest")[0]
    assert "systemctl restart nvidia-persistenced" not in out


def test_build_change_installed_with_no_start_rebuilds_next_run(tmp_path):
    # The marker, not just this run's changes, decides: --no-start installs but builds nothing.
    repo, root, env = repo_copy(tmp_path), seeded_root(tmp_path), env_on(tmp_path)
    deploy(root, env, repo=repo)
    with open(repo / "walter/digest/build/requirements.txt", "a") as f:
        f.write("# test change\n")
    out = deploy(root, env, "--no-start", repo=repo)
    assert "compose stacks: skipped (--no-start)" in out
    out = deploy(root, env, repo=repo)
    assert changed_lines(out) == []
    assert "rebuild: yes (build inputs differ from the last build" in out
    assert "--build" in compose_lines(out, "digest")[0]
    assert "--build" not in compose_lines(out, "telemetry")[0]


def test_missing_marker_rebuilds_once(tmp_path):
    # A host deployed before build tracking has no .build-hash: one rebuild, then none.
    root, env = seeded_root(tmp_path), env_on(tmp_path)
    deploy(root, env)
    (root / "srv/telemetry/.build-hash").unlink()
    out = deploy(root, env)
    assert "rebuild: yes (no /srv/telemetry/.build-hash yet" in out
    assert "--build" not in compose_lines(out, "digest")[0]
    assert "--build" not in compose_lines(deploy(root, env), "telemetry")[0]


def test_persistenced_dropin_change_restarts(tmp_path):
    repo, root, env = repo_copy(tmp_path), seeded_root(tmp_path), env_on(tmp_path)
    deploy(root, env, repo=repo)
    with open(repo / "walter/base/nvidia-persistenced.service.d/override.conf", "a") as f:
        f.write("# test change\n")
    out = deploy(root, env, repo=repo)
    assert "systemctl restart nvidia-persistenced.service" in out
    assert all("--build" not in compose_lines(out, s)[0] for s in ("telemetry", "digest"))
