"""walter/deploy.sh and scripts/render.sh (the shim) / tools/deck-tools/render.sh (the engine), exercised with
--destdir only.

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
    for d in ("walter", "scripts", "tools"):   # tools/: the deck-tools engines behind the scripts/ shims
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


RENDER_ENTRY = {"shim": Path("scripts/render.sh"), "engine": Path("tools/deck-tools/render.sh")}


@pytest.fixture(params=sorted(RENDER_ENTRY))
def entry(request):
    """The render tests run through the scripts/ shim and through the engine it execs."""
    return request.param


def render(env, *args, repo=REPO, entry="shim"):
    return subprocess.run(["bash", str(repo / RENDER_ENTRY[entry]), "-e", str(env), *args],
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

def test_render_backup_offsite_off_with_excludes(tmp_path, entry):
    env = write_env(tmp_path)  # RESTIC_BUCKET="" as in site.env.example
    out = tmp_path / "out"
    r = render(env, "-x", "spark-offsite*", "-x", "offsite-*", str(REPO / "walter/backup"), str(out),
               entry=entry)
    assert r.returncode == 0, r.stderr
    assert not list(out.glob("*offsite*.json")) and not list(out.glob("spark-offsite*"))
    restore = (out / "RESTORE.md").read_text()
    assert "## 7. Offsite copy (not configured)" in restore
    assert "no offsite copy" in restore and "@@" not in restore and "restic restore" not in restore


def test_render_backup_offsite_off_without_excludes_still_refuses_iam_policy(tmp_path, entry):
    # Strictness is kept: the IAM policy really needs the bucket, so only deploy's -x skips it.
    r = render(write_env(tmp_path), str(REPO / "walter/backup"), str(tmp_path / "out"), entry=entry)
    assert r.returncode != 0 and "offsite-iam-policy.json.tmpl" in r.stderr
    assert "RESTORE.md.tmpl" not in r.stderr


@pytest.mark.parametrize("bucket,on", [("spark-backups-example", True), ("CHANGEME", False)])
def test_render_restore_md_bucket_set_or_changeme(tmp_path, entry, bucket, on):
    env = write_env(tmp_path, RESTIC_BUCKET=bucket)
    r = render(env, str(REPO / "walter/backup"), str(tmp_path / "out"), entry=entry)
    assert r.returncode == 0, r.stderr
    restore = (tmp_path / "out/RESTORE.md").read_text()
    assert "@@" not in restore
    assert ("s3.us-east-1.amazonaws.com/spark-backups-example/walter" in restore) is on
    assert ("## 7. Offsite copy (not configured)" in restore) is not on


def test_render_cond_blocks(tmp_path, entry):
    env = write_env(tmp_path, RESTIC_BUCKET="", SPARK_DIGEST_HOST="digest.example.com")
    t = tmp_path / "x.tmpl"
    t.write_text("a\n# @@if SPARK_DIGEST_HOST@@\nhost=${SPARK_DIGEST_HOST}\n# @@else@@\nno digest\n"
                 "# @@endif@@\n<!-- @@if RESTIC_BUCKET@@ -->\nb=${RESTIC_BUCKET}\n<!-- @@endif@@ -->\nz\n")
    r = render(env, str(t), str(tmp_path / "x"), entry=entry)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "x").read_text() == "a\nhost=digest.example.com\nz\n"


@pytest.mark.parametrize("body,msg", [
    ("# @@if RESTIC_BUCKET@@\n# @@if RESTIC_BUCKET@@\n# @@endif@@\n", "nested"),
    ("# @@if RESTIC_BUCKET@@\nx\n", "unterminated"),
    ("x\n# @@endif@@\n", "without"),
])
def test_render_cond_errors(tmp_path, entry, body, msg):
    t = tmp_path / "bad.tmpl"
    t.write_text(body)
    r = render(write_env(tmp_path), str(t), str(tmp_path / "bad"), entry=entry)
    assert r.returncode != 0 and msg in r.stderr


def test_render_without_blocks_is_byte_for_byte(tmp_path, entry):
    t = tmp_path / "plain.tmpl"
    t.write_bytes(b"user=${BACKEND_SSH_USER} keep=${NOT_A_SITE_VAR} $host\nno newline at end")
    r = render(write_env(tmp_path), str(t), str(tmp_path / "plain"), entry=entry)
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


# ---- deploy.sh --destdir: docs never recreate or rebuild ---------------------------------------

def append(path, text="\n<!-- test change -->\n"):
    with open(path, "a") as f:
        f.write(text)


def test_monitoring_readme_only_change_no_recreate(tmp_path):
    repo, root, env = repo_copy(tmp_path), seeded_root(tmp_path), env_on(tmp_path)
    deploy(root, env, repo=repo)
    append(repo / "walter/monitoring/README.md.tmpl")
    out = deploy(root, env, repo=repo)
    assert changed_lines(out) == ["  new      /srv/monitoring/README.md"]
    assert "<!-- test change -->" in (root / "srv/monitoring/README.md").read_text()   # still installed
    assert "--force-recreate" not in compose_lines(out, "monitoring")[0]
    assert "  force-recreate: no (only docs changed: /srv/monitoring/README.md)" in out
    assert "--force-recreate" not in out


def test_monitoring_config_change_recreates(tmp_path):
    repo, root, env = repo_copy(tmp_path), seeded_root(tmp_path), env_on(tmp_path)
    deploy(root, env, repo=repo)
    append(repo / "walter/monitoring/blackbox/blackbox.yml", "# test change\n")
    append(repo / "walter/monitoring/README.md.tmpl")
    out = deploy(root, env, repo=repo)
    assert "--force-recreate" in compose_lines(out, "monitoring")[0]
    assert "  force-recreate: yes (changed in this run: /srv/monitoring/blackbox/blackbox.yml)" in out
    for stack in ("gateway", "webui", "telemetry", "digest"):
        assert "--force-recreate" not in compose_lines(out, stack)[0]


def test_dashboard_generator_change_no_recreate(tmp_path):
    # grafana/build_spark_overview.py is not mounted; only its output under grafana/provisioning is.
    repo, root, env = repo_copy(tmp_path), seeded_root(tmp_path), env_on(tmp_path)
    deploy(root, env, repo=repo)
    append(repo / "walter/monitoring/grafana/build_spark_overview.py.tmpl", "# test change\n")
    out = deploy(root, env, repo=repo)
    assert changed_lines(out) == ["  new      /srv/monitoring/grafana/build_spark_overview.py"]
    assert "--force-recreate" not in compose_lines(out, "monitoring")[0]


@pytest.mark.parametrize("stack,readme", [("telemetry", "README.md.tmpl"), ("digest", "README.md")])
def test_build_stack_readme_change_no_rebuild(tmp_path, stack, readme):
    repo, root, env = repo_copy(tmp_path), seeded_root(tmp_path), env_on(tmp_path)
    deploy(root, env, repo=repo)
    hash_before = (root / f"srv/{stack}/.build-hash").read_text()
    append(repo / f"walter/{stack}/{readme}")
    out = deploy(root, env, repo=repo)
    assert changed_lines(out) == [f"  new      /srv/{stack}/README.md"]
    assert out.count("rebuild: no (build inputs unchanged") == 2
    assert f"docs changed: /srv/{stack}/README.md" in out
    line = compose_lines(out, stack)[0]
    assert "--build" not in line and "--force-recreate" not in line
    assert (root / f"srv/{stack}/.build-hash").read_text() == hash_before


def test_doc_under_build_tree_no_rebuild_code_change_rebuilds(tmp_path):
    repo, root, env = repo_copy(tmp_path), seeded_root(tmp_path), env_on(tmp_path)
    deploy(root, env, repo=repo)
    (repo / "walter/digest/build/app/NOTES.md").write_text("notes\n")
    out = deploy(root, env, repo=repo)
    assert changed_lines(out) == ["  new      /srv/digest/build/app/NOTES.md"]
    assert "--build" not in compose_lines(out, "digest")[0]
    append(repo / "walter/digest/build/app/main.py", "# test change\n")
    out = deploy(root, env, repo=repo)
    assert "rebuild: yes (compose.yaml or build/ changed in this run: /srv/digest/build/app/main.py)" in out
    assert "--build" in compose_lines(out, "digest")[0]
    assert "--build" not in compose_lines(out, "telemetry")[0]


def test_build_hash_unchanged_for_trees_without_docs(tmp_path):
    # The doc exclusion must not invalidate markers written before it: same hash as the old definition
    # (compose.yaml + every file under build/) when build/ holds no docs, so no one-time rebuild.
    root, env = seeded_root(tmp_path), env_on(tmp_path)
    deploy(root, env)
    for stack in ("telemetry", "digest"):
        d = root / "srv" / stack
        old = subprocess.run("find compose.yaml build -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum"
                             " | sha256sum | cut -c1-64", shell=True, cwd=d, capture_output=True,
                             text=True, check=True).stdout.strip()
        assert (d / ".build-hash").read_text().strip() == old


def test_no_stack_bind_mounts_a_doc_file():
    # is_doc() in deploy.sh relies on this: a mounted single doc file would need a recreate on change.
    for tmpl in sorted((REPO / "walter").glob("*/compose.yaml.tmpl")):
        for m in re.finditer(r"^\s*-\s*(\./[^:\s]+):", tmpl.read_text(), flags=re.M):
            assert not re.search(r"\.(md|example)$", m.group(1)), f"{tmpl}: {m.group(1)}"


# ---- deploy.sh --destdir: model container hardening ------------------------------------------

def test_model_files_made_world_readable_and_network_step(tmp_path):
    # The model containers run as uid 10010: deploy makes every GGUF under MODELS_DIR/gguf world-readable,
    # and (destdir) only reports the llama-net step.
    root = tmp_path / "root"
    gguf = root / "models/gguf/m"
    gguf.mkdir(parents=True)
    f = gguf / "x.gguf"
    f.write_text("GGUF")
    f.chmod(0o600)
    gguf.chmod(0o700)
    dry = deploy(root, write_env(tmp_path), "--dry-run")
    assert "would chmod o+rX 2 paths under /models/gguf" in dry and f.stat().st_mode & 0o777 == 0o600
    out = deploy(root, write_env(tmp_path))
    assert "chmod o+rX 2 paths under /models/gguf (model containers run as uid 10010)" in out
    assert f.stat().st_mode & 0o777 == 0o604 and gguf.stat().st_mode & 0o777 == 0o705
    assert "would ensure docker network llama-net (bridge br-llama, icc off)" in out
    again = deploy(root, write_env(tmp_path))
    assert "model files readable by uid 10010" in again and "chmod o+rX" not in again
    # the config is installed after the firewall script and the network step (llama-swap reloads at once)
    cfg, fw, net = (out.index(s) for s in ("/etc/llama-swap/config.yaml", "/usr/local/sbin/docker-user-rules.sh",
                                          "would ensure docker network llama-net"))
    assert fw < net < cfg


# ---- deploy.sh --destdir: lab models (walter/models/README.md) ------------------------------------------------

LAB_SHA = "0123456789abcdef0123456789abcdef01234567"


def lab_registry(root, *entries):
    """Write MODELS_DIR/registry.yaml under a destdir (entries: (id, enabled))."""
    models = []
    for eid, enabled in entries:
        models.append({"id": eid, "repo": "someorg/Some-Model-GGUF", "revision": LAB_SHA, "placement": "gpu1",
                       "ctx": 8192, "class": "abliterated", "added": "2026-10-14T00:00:00Z", "enabled": enabled,
                       "files": [{"path": f"{eid}.gguf", "bytes": 100, "sha256": "a" * 64, "role": "model"}]})
    p = root / "models/registry.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    import yaml
    p.write_text(yaml.safe_dump({"schema": "spark-model-registry-v1", "models": models}, sort_keys=False))
    return p


def rendered_template(tmp_path, env):
    out = tmp_path / "template-config.yaml"
    r = render(env, str(REPO / "walter/llama-swap/config.yaml.tmpl"), str(out))
    assert r.returncode == 0, r.stderr
    return out.read_text()


def test_plain_deploy_installs_agent_and_config_equals_template(tmp_path):
    root, env = tmp_path / "root", write_env(tmp_path)
    out = deploy(root, env)
    tmpl = rendered_template(tmp_path, env)
    assert (root / "etc/llama-swap/config.base.yaml").read_text() == tmpl
    assert (root / "etc/llama-swap/config.yaml").read_text() == tmpl          # no registry: exactly the template
    assert not (root / "etc/llama-swap/config.yaml.prev").exists()
    assert "  new      /etc/llama-swap/config.yaml (rendered: base + 0 enabled lab model(s)" in out
    for f in ("registry.py", "fetch.py", "gguf.py", "hf.py"):
        assert (root / "usr/local/lib/spark-model" / f).is_file(), f
    agent = root / "usr/local/sbin/spark-model-agent"
    assert agent.stat().st_mode & 0o777 == 0o755 and agent.read_text().startswith("#!/usr/bin/python3 -IB\n")
    sudo = (root / "etc/sudoers.d/spark-model")
    assert sudo.stat().st_mode & 0o777 == 0o440 and sudo.parent.stat().st_mode & 0o777 == 0o750
    rules = [l for l in sudo.read_text().splitlines() if l and not l.startswith("#")]
    assert rules == ["Defaults!/usr/local/sbin/spark-model-agent env_reset, !setenv",
                     "operator ALL=(root) NOPASSWD: /usr/local/sbin/spark-model-agent"]
    aenv = (root / "etc/spark-model/agent.env").read_text()
    assert "LAB_MODELS_QUOTA_GB=1500" in aenv and "LAB_HF_ORGS" not in aenv     # empty in site.env.example
    assert (root / "models/quarantine").stat().st_mode & 0o777 == 0o700
    assert (root / "models/gguf/lab").stat().st_mode & 0o777 == 0o755
    # the agent was installed before the config it renders; the config still comes after the network step
    assert out.index("/usr/local/sbin/spark-model-agent") < out.index("/etc/llama-swap/config.yaml")
    assert out.index("/etc/llama-swap/config.base.yaml") < out.index("/etc/llama-swap/config.yaml")


def test_deploy_keeps_enabled_lab_entries_and_does_not_rewrite(tmp_path):
    root, env = tmp_path / "root", write_env(tmp_path)
    deploy(root, env)
    cfg = root / "etc/llama-swap/config.yaml"
    lab_registry(root, ("on-model", True), ("off-model", False))
    out = deploy(root, env)
    assert changed_lines(out) == ["  new      /etc/llama-swap/config.yaml (rendered: base + 1 enabled lab model(s);"
                                  " previous kept as config.yaml.prev; llama-swap binary not found: -validate skipped"
                                  " (structural checks passed))"]
    text = cfg.read_text()
    assert '"lab-on-model":' in text and "lab-off-model" not in text
    assert (root / "etc/llama-swap/config.yaml.prev").read_text() == rendered_template(tmp_path, env)
    mtime = cfg.stat().st_mtime_ns
    again = deploy(root, env)                       # plain re-deploy: lab entry kept, file not touched (no reload)
    assert changed_lines(again) == [] and cfg.read_text() == text and cfg.stat().st_mtime_ns == mtime
    dry = deploy(root, env, "--dry-run")
    assert "/etc/llama-swap/config.yaml" not in "\n".join(l for l in dry.splitlines() if l.startswith("  new "))


def test_template_change_rerenders_with_lab_entries(tmp_path):
    repo, root, env = repo_copy(tmp_path), tmp_path / "root", write_env(tmp_path)
    deploy(root, env, repo=repo)
    lab_registry(root, ("on-model", True))
    deploy(root, env, repo=repo)
    t = repo / "walter/llama-swap/config.yaml.tmpl"
    t.write_text(t.read_text().replace("healthCheckTimeout: 600", "healthCheckTimeout: 601"))
    out = deploy(root, env, repo=repo)
    assert "  new      /etc/llama-swap/config.base.yaml" in out
    text = (root / "etc/llama-swap/config.yaml").read_text()
    assert "healthCheckTimeout: 601" in text and '"lab-on-model":' in text


def test_disabled_only_registry_keeps_the_template_and_dry_run_reports_changes(tmp_path):
    root, env = tmp_path / "root", write_env(tmp_path)
    deploy(root, env)
    lab_registry(root, ("off-model", False))
    assert changed_lines(deploy(root, env)) == []
    lab_registry(root, ("on-model", True))
    before = (root / "etc/llama-swap/config.yaml").read_text()
    dry = deploy(root, env, "--dry-run")
    assert "  new      /etc/llama-swap/config.yaml (base + lab registry" in dry
    assert (root / "etc/llama-swap/config.yaml").read_text() == before


def test_invalid_registry_fails_the_deploy_and_keeps_the_config(tmp_path):
    root, env = tmp_path / "root", write_env(tmp_path)
    deploy(root, env)
    before = (root / "etc/llama-swap/config.yaml").read_text()
    (root / "models/registry.yaml").write_text("schema: spark-model-registry-v1\nmodels:\n  - id: Bad!\n")
    r = subprocess.run(["bash", str(REPO / "walter/deploy.sh"), "--destdir", str(root), "--site-env", str(env)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode != 0 and "spark-model-agent render --apply failed" in r.stderr
    assert "id 'Bad!'" in r.stderr
    assert (root / "etc/llama-swap/config.yaml").read_text() == before
    dry = subprocess.run(["bash", str(REPO / "walter/deploy.sh"), "--destdir", str(root), "--site-env", str(env),
                          "--dry-run"], capture_output=True, text=True, timeout=120)
    assert dry.returncode != 0 and "spark-model-agent render failed" in dry.stderr


def test_lab_orgs_reach_the_agent_env(tmp_path):
    root = tmp_path / "root"
    deploy(root, write_env(tmp_path, LAB_HF_ORGS="someorg otherorg", LAB_MODELS_QUOTA_GB="900"))
    aenv = (root / "etc/spark-model/agent.env").read_text()
    assert 'LAB_HF_ORGS="someorg otherorg"' in aenv and "LAB_MODELS_QUOTA_GB=900" in aenv


def test_provision_keys_env_change_recreates_webui():
    """Live runs only (DESTDIR runs skip provision-keys' writes): a .env rewritten by provision-keys
    (connection keys, LAB_WEBUI) marks the webui stack changed before its compose_up."""
    text = (REPO / "walter" / "deploy.sh").read_text()
    pk = text.index("    /srv/gateway/provision-keys.py\n")
    mark = text.index('CHANGED[webui]+=" /srv/webui/.env(provision-keys)"')
    up = text.index("compose_up /srv/webui webui webui-theme")
    before = text.index("webui_env_before=$(sha256sum /srv/webui/.env")
    assert before < pk < mark < up
