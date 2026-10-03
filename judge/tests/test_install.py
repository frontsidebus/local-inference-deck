"""Tests for judge/install.sh.

Every run uses a temp HERMES_HOME, a temp review dir, a temp site.env, a temp unit dir and HOME pointed at a
temp dir, so the real ~/.hermes is never read for config or written. The only thing borrowed from the real
machine is the Hermes venv's Python interpreter (for PyYAML); it is executed, never modified.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

JUDGE = Path(__file__).resolve().parents[1]
INSTALL = JUDGE / "install.sh"


def _find_venv_python() -> str | None:
    cands = [os.environ.get("HERMES_VENV_PYTHON"), str(Path.home() / ".hermes/hermes-agent/venv/bin/python")]
    for c in cands:
        if c and os.access(c, os.X_OK):
            ok = subprocess.run([c, "-c", "import yaml"], capture_output=True).returncode == 0
            if ok:
                return c
    return None


VENV_PY = _find_venv_python()
pytestmark = pytest.mark.skipif(
    VENV_PY is None,
    reason="Hermes venv Python with PyYAML not found (set HERMES_VENV_PYTHON or install Hermes); "
           "install.sh edits config.yaml with it",
)

SAMPLE_CONFIG = """\
# Hermes profile config (synthetic test fixture)
model:
  default: coder        # local alias
  provider: custom
agent:
  max_turns: 90
  max_verify_nudges: 2

# Shell hooks the user added by hand.
hooks:
  # block rm -rf
  pre_tool_call:
    - matcher: "terminal"
      command: "~/.hermes/agent-hooks/block-rm-rf.sh"
      timeout: 5
  post_tool_call:
    - matcher: "write_file"
      command: "~/.hermes/agent-hooks/auto-format.sh"
  outbound:
    - url: "https://ci.example.com/hermes"
      events: ["on_session_end"]

# Consent: leave this false.
hooks_auto_accept: false
security:
  redact_secrets: true   # keep
command_allowlist:
  - "git status"
"""

SAMPLE_NO_HOOKS = """\
# minimal profile
model:
  default: coder
hooks_auto_accept: false
"""

FAKE_PLUGINS = """\
from typing import Set
VALID_HOOKS: Set[str] = {
    "pre_tool_call", "post_tool_call", "pre_llm_call", "post_llm_call",
    # comment inside the set
    "pre_verify", "on_session_start", "on_session_end", "transform_api_error_classification",
}
SHELL_UNSUPPORTED_HOOKS: Set[str] = {"transform_api_error_classification"}
"""

POLICY_TMPL = '{"version": 1, "hosts": {"domain": "${SPARK_DOMAIN}", "edge_user": "${EDGE_SSH_USER}"}}\n'


def _yaml(text: str):
    out = subprocess.run([VENV_PY, "-c", "import sys, json, yaml; print(json.dumps(yaml.safe_load(sys.stdin.read())))"],
                         input=text, capture_output=True, text=True, check=True).stdout
    import json
    return json.loads(out)


@pytest.fixture
def env(tmp_path):
    """A throwaway judge checkout, Hermes home, hermes source stub, site.env and HOME."""
    judge = tmp_path / "repo" / "judge"
    (judge / "hooks").mkdir(parents=True)
    shutil.copy2(INSTALL, judge / "install.sh")
    if (JUDGE / "lib").is_dir():          # exercise the shared site.env loader when it exists
        shutil.copytree(JUDGE / "lib", judge / "lib", ignore=shutil.ignore_patterns("__pycache__"))
    for h in ("gate", "verify", "enqueue", "inject"):
        (judge / "hooks" / f"{h}.py").write_text("import sys; sys.stdin.read(); print('{}')\n")
    (judge / "policy").mkdir()
    (judge / "policy" / "gate-policy.json.tmpl").write_text(POLICY_TMPL)

    home = tmp_path / "home"
    home.mkdir()
    hermes = tmp_path / "hermes-home"
    hermes.mkdir()
    (hermes / "config.yaml").write_text(SAMPLE_CONFIG)
    src = tmp_path / "hermes-src"
    (src / "hermes_cli").mkdir(parents=True)
    (src / "hermes_cli" / "plugins.py").write_text(FAKE_PLUGINS)
    site = tmp_path / "repo" / "site.env"
    site.write_text("SPARK_DOMAIN=example.com\nEDGE_SSH_USER=ubuntu\nJUDGE_MODE=frontier\n")

    class E:
        pass
    e = E()
    e.tmp, e.judge, e.home, e.hermes, e.src, e.site = tmp_path, judge, home, hermes, src, site
    e.config = hermes / "config.yaml"
    e.review = tmp_path / "review"
    e.units = tmp_path / "units"

    def run(*args, check=True):
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("HERMES_HOME", "JUDGE_REVIEW_DIR", "JUDGE_SITE_ENV", "SITE_ENV", "XDG_CONFIG_HOME")
                 and not k.startswith(("JUDGE_", "SPARK_", "EDGE_", "BACKEND_"))}
        clean["HOME"] = str(home)
        cmd = ["bash", str(judge / "install.sh"), "--hermes-home", str(hermes), "--hermes-python", VENV_PY,
               "--hermes-src", str(src), "--site-env", str(site), "--review-dir", str(e.review),
               "--unit-dir", str(e.units), *args]
        p = subprocess.run(cmd, capture_output=True, text=True, env=clean, timeout=60)
        if check and p.returncode != 0:
            raise AssertionError(f"install.sh {args} failed rc={p.returncode}\n{p.stdout}\n{p.stderr}")
        return p
    e.run = run
    e.backups = lambda: sorted(hermes.glob("config.yaml.bak-judge-*"))
    return e


def _managed(entries):
    return [x for x in entries or [] if x.get("managed_by") == "agent-judge"]


def test_bash_syntax():
    assert subprocess.run(["bash", "-n", str(INSTALL)]).returncode == 0


def test_dry_run_changes_nothing(env):
    before = env.config.read_bytes()
    p = env.run()                       # default mode is dry-run
    assert "dry run" in p.stdout.lower()
    assert "gate.py" in p.stdout and "fail_closed: true" in p.stdout
    assert "Diff of config.yaml" in p.stdout
    assert env.config.read_bytes() == before
    assert env.backups() == []
    assert not env.review.exists()
    assert sorted(x.name for x in env.hermes.iterdir()) == ["config.yaml"]


def test_apply_merges_and_preserves(env):
    original = _yaml(SAMPLE_CONFIG)
    env.run("--apply")
    text = env.config.read_text()
    data = _yaml(text)

    # Everything outside hooks: is untouched, comments outside the block included.
    for k, v in original.items():
        if k != "hooks":
            assert data[k] == v
    assert "# Hermes profile config (synthetic test fixture)" in text
    assert "default: coder        # local alias" in text
    assert "# Consent: leave this false." in text
    assert "redact_secrets: true   # keep" in text
    assert data["hooks_auto_accept"] is False
    assert "hooks_auto_accept: true" not in text

    hooks = data["hooks"]
    # The user's own entries are kept, first, unchanged.
    assert hooks["pre_tool_call"][0] == original["hooks"]["pre_tool_call"][0]
    assert hooks["post_tool_call"][0] == original["hooks"]["post_tool_call"][0]
    assert hooks["outbound"] == original["hooks"]["outbound"]

    gate = _managed(hooks["pre_tool_call"])
    assert len(gate) == 1
    assert gate[0]["matcher"] == "terminal|write_file|patch|read_file"
    assert gate[0]["command"].endswith(str(env.judge / "hooks" / "gate.py"))
    assert gate[0]["timeout"] == 10 and gate[0]["fail_closed"] is True
    post = _managed(hooks["post_tool_call"])
    assert len(post) == 1 and post[0]["matcher"] == "write_file|patch|terminal|memory|skill_manage|read_file" and post[0]["command"].endswith("enqueue.py")
    for ev in ("on_session_start", "on_session_end"):
        assert len(hooks[ev]) == 1 and hooks[ev][0]["command"].endswith("hooks/enqueue.py")
    assert hooks["pre_verify"][0]["command"].endswith("hooks/verify.py") and hooks["pre_verify"][0]["timeout"] == 60
    assert hooks["pre_llm_call"][0]["command"].endswith("hooks/inject.py") and hooks["pre_llm_call"][0]["timeout"] == 10
    for ev, entries in hooks.items():
        if ev == "outbound":
            continue
        for x in _managed(entries):
            assert "fail_closed" not in x or ev == "pre_tool_call"

    # Backup of the original, review dir 700, policy rendered 600.
    backups = env.backups()
    assert len(backups) == 1 and backups[0].read_text() == SAMPLE_CONFIG
    assert stat.S_IMODE(env.review.stat().st_mode) == 0o700
    for d in ("queue", "evidence", "findings", "acks", "done", "snapshots"):
        assert stat.S_IMODE((env.review / d).stat().st_mode) == 0o700
    pol = env.review / "gate-policy.json"
    assert stat.S_IMODE(pol.stat().st_mode) == 0o600
    import json
    assert json.loads(pol.read_text())["hosts"] == {"domain": "example.com", "edge_user": "ubuntu"}


def test_reapply_is_idempotent(env):
    env.run("--apply")
    first = env.config.read_bytes()
    p = env.run("--apply")
    assert "no change needed" in p.stdout
    assert env.config.read_bytes() == first
    assert len(env.backups()) == 1
    hooks = _yaml(first.decode())["hooks"]
    assert all(len(_managed(v)) <= 1 for k, v in hooks.items() if k != "outbound")


def test_consent_step_printed_and_auto_accept_untouched(env):
    p = env.run("--apply")
    assert "consent" in p.stdout.lower()
    assert "hooks list" in p.stdout and "hooks doctor" in p.stdout
    assert _yaml(env.config.read_text())["hooks_auto_accept"] is False


def test_uninstall_removes_only_managed(env):
    env.run("--apply")
    env.review.joinpath("findings", "keep.json").write_text("{}")
    p = env.run("--uninstall")
    data = _yaml(env.config.read_text())
    assert data == _yaml(SAMPLE_CONFIG)           # back to exactly the user's entries
    assert (env.review / "findings" / "keep.json").exists()   # review data is left alone
    assert "hooks revoke" in p.stdout
    assert len(env.backups()) == 2


def test_uninstall_dry_run_changes_nothing(env):
    env.run("--apply")
    before = env.config.read_bytes()
    env.run("--uninstall", "--dry-run")
    assert env.config.read_bytes() == before


def test_install_into_config_without_hooks_then_uninstall(env):
    env.config.write_text(SAMPLE_NO_HOOKS)
    env.run("--apply")
    text = env.config.read_text()
    assert text.startswith(SAMPLE_NO_HOOKS)       # appended, original text intact
    assert len(_yaml(text)["hooks"]) == 6
    env.run("--uninstall")
    assert _yaml(env.config.read_text()) == _yaml(SAMPLE_NO_HOOKS)
    assert "hooks:" not in env.config.read_text()


def test_untagged_entry_from_old_checkout_is_replaced_not_duplicated(env):
    # An entry whose tag was lost but whose command points at a judge hook is still managed.
    old = SAMPLE_CONFIG.replace(
        '      timeout: 5\n',
        '      timeout: 5\n    - matcher: "terminal"\n      command: "/usr/bin/python3 /opt/old/judge/hooks/gate.py"\n', 1)
    env.config.write_text(old)
    env.run("--apply")
    pre = _yaml(env.config.read_text())["hooks"]["pre_tool_call"]
    assert [x["command"] for x in pre if "gate.py" in x["command"]] == [
        x["command"] for x in _managed(pre)]
    assert len(pre) == 2 and pre[0]["command"] == "~/.hermes/agent-hooks/block-rm-rf.sh"


def test_inline_loader_without_shared_lib(env):
    shutil.rmtree(env.judge / "lib", ignore_errors=True)
    env.site.write_text('SPARK_DOMAIN="example.org"   # quoted\nEDGE_SSH_USER=admin # trailing\n')
    env.run("--apply")
    import json
    assert json.loads((env.review / "gate-policy.json").read_text())["hosts"] == {
        "domain": "example.org", "edge_user": "admin"}


def test_apply_refuses_when_hook_scripts_missing(env):
    (env.judge / "hooks" / "gate.py").unlink()
    before = env.config.read_bytes()
    p = env.run("--apply", check=False)
    assert p.returncode != 0 and "gate.py" in p.stderr
    assert env.config.read_bytes() == before


def test_unsupported_event_is_skipped(env):
    (env.src / "hermes_cli" / "plugins.py").write_text(FAKE_PLUGINS.replace('"pre_verify", ', ""))
    p = env.run("--apply")
    assert "pre_verify" in p.stdout and "WARNING" in p.stdout
    assert "pre_verify" not in _yaml(env.config.read_text())["hooks"]


def test_with_units_installs_files_and_only_prints_systemctl(env):
    (env.judge / "runner" / "units").mkdir(parents=True)
    (env.judge / "runner" / "units" / "judge-runner.path").write_text("[Path]\nPathChanged=${JUDGE_REVIEW_DIR}/queue\n")
    (env.judge / "runner" / "units" / "judge-runner.service.tmpl").write_text(
        "[Service]\nExecStart=${JUDGE_PYTHON} ${JUDGE_DIR}/runner/run.py\nEnvironment=X=${NOT_A_SITE_VAR}\n")
    (env.judge / "watch").mkdir()
    (env.judge / "watch" / "judge-watch.service").write_text("[Service]\nExecStart=/bin/true\n")
    p = env.run("--apply", "--with-units")
    svc = (env.units / "judge-runner.service").read_text()
    assert str(env.judge) in svc and "${NOT_A_SITE_VAR}" in svc
    assert (env.units / "judge-runner.path").read_text() == f"[Path]\nPathChanged={env.review}/queue\n"
    assert (env.units / "judge-watch.service").exists()
    assert "systemctl --user enable --now judge-runner.path judge-watch.service" in p.stdout
    p = env.run("--uninstall", "--with-units")
    assert not any(env.units.iterdir())
    assert "systemctl --user disable --now" in p.stdout


def test_real_units_render_completely(env):
    """The shipped units: every ${...} is rendered, both services use the installer's interpreter, the
    review service finds `claude` in ~/.local/bin and never inherits a gateway endpoint."""
    for sub, names in (("runner/units", ("judge-review.service", "judge-review.path")),
                       ("watch", ("judge-runaway-watch.service",))):
        (env.judge / sub).mkdir(parents=True, exist_ok=True)
        for n in names:
            shutil.copy2(JUDGE / sub / n, env.judge / sub / n)
    py = env.tmp / "bin" / "python3"            # not /usr/bin/python3: catches a hardcoded interpreter
    py.parent.mkdir()
    py.symlink_to(sys.executable)
    p = env.run("--apply", "--with-units", "--python", str(py))
    assert "systemctl --user enable --now judge-review.path judge-runaway-watch.service" in p.stdout
    rendered = {n: (env.units / n).read_text() for n in
                ("judge-review.service", "judge-review.path", "judge-runaway-watch.service")}
    for n, text in rendered.items():
        body = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        assert "${" not in body, f"{n} has an unrendered variable"
    svc = rendered["judge-review.service"]
    assert f"ExecStart={py} {env.judge}/runner/run_judge.py --pending" in svc
    assert "Environment=PATH=%h/.local/bin:" in svc
    assert "UnsetEnvironment=ANTHROPIC_BASE_URL" in svc
    assert f"Environment=JUDGE_REVIEW_DIR={env.review}" in svc
    assert f"PathChanged={env.review}/queue" in rendered["judge-review.path"]
    assert f"ExecStart={py} {env.judge}/watch/runaway.py" in rendered["judge-runaway-watch.service"]
