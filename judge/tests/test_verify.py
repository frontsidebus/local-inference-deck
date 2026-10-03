"""Tests for judge/hooks/verify.py (C3 pre_verify done gate). No network; temp HERMES_HOME/JUDGE_REVIEW_DIR/HOME."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.dont_write_bytecode = True  # keep __pycache__ out of the repo tree

JUDGE = Path(__file__).resolve().parent.parent
HOOK = JUDGE / "hooks" / "verify.py"


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("JUDGE_REVIEW_DIR", str(tmp_path / "review"))
    monkeypatch.delenv("JUDGE_SSH_ALIASES", raising=False)
    work = tmp_path / "work"
    work.mkdir()
    return {"tmp": tmp_path, "home": home, "work": work, "review": tmp_path / "review"}


@pytest.fixture
def v(env, monkeypatch):
    spec = importlib.util.spec_from_file_location("judge_verify_under_test", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # isolate from sibling modules that may or may not exist yet: local fallback queue, no site.env
    monkeypatch.setattr(mod, "_import", lambda name: None)
    mod._site_cache = {}
    return mod


def call(v, env, paths, response="Done.", attempt=0, session="20261002_165907_abc123"):
    payload = {"hook_event_name": "pre_verify", "tool_name": None, "tool_input": None,
               "session_id": session, "cwd": str(env["work"]), "profile": "default",
               "extra": {"platform": "cli", "model": "coder", "coding": True, "attempt": attempt,
                         "final_response": response, "changed_paths": [str(p) for p in paths]}}
    out = io.StringIO()
    v.main(io.StringIO(json.dumps(payload)), out)
    return json.loads(out.getvalue())


def queued(env):
    q = env["review"] / "queue"
    return [json.loads(p.read_text()) for p in sorted(q.glob("*.json"))] if q.is_dir() else []


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# ---------------------------------------------------------------- one-shot
def test_one_shot_attempt_gt_zero_is_noop(v, env):
    bad = write(env["work"] / "bad.py", "def x(:\n")
    assert call(v, env, [bad], attempt=1) == {}
    assert queued(env) == []


# ---------------------------------------------------------------- deterministic verifiers
def test_bash_syntax_error(v, env):
    sh = write(env["work"] / "deploy.sh", "#!/usr/bin/env bash\nif true; then\necho hi\n")
    out = call(v, env, [sh])
    assert out["action"] == "continue"
    assert "bash -n" in out["message"] and "deploy.sh" in out["message"]


def test_bash_shebang_without_extension(v, env):
    sh = write(env["work"] / "tool", "#!/bin/bash\nfor x in; do\n")
    assert call(v, env, [sh]).get("action") == "continue"


def test_python_syntax_error_and_no_pycache(v, env):
    py = write(env["work"] / "app.py", "def f(:\n    pass\n")
    out = call(v, env, [py])
    assert out["action"] == "continue"
    assert "Python syntax error" in out["message"] and "app.py" in out["message"]
    assert not (env["work"] / "__pycache__").exists()


def test_bad_json(v, env):
    js = write(env["work"] / "policy.json", '{"a": 1,}')
    out = call(v, env, [js])
    assert out["action"] == "continue" and "invalid JSON" in out["message"]


def test_bad_yaml_when_pyyaml_available(v, env, monkeypatch):
    try:
        import yaml  # noqa: F401
    except ImportError:
        pytest.skip("no PyYAML in this interpreter")
    monkeypatch.setenv("JUDGE_YAML_PYTHON", sys.executable)
    y = write(env["work"] / "c.yaml", "a: [1, 2\nb: 3\n")
    out = call(v, env, [y])
    assert out["action"] == "continue" and "invalid YAML" in out["message"]


def test_yaml_skipped_with_note_without_pyyaml(v, env, monkeypatch):
    monkeypatch.setattr(v, "yaml_python", lambda budget: None)
    y = write(env["work"] / "c.yaml", "a: [1, 2\n")
    assert call(v, env, [y]) == {}
    [req] = queued(env)
    assert any("YAML not parsed" in n for n in req["detail"]["verify"]["notes"])


def test_sanitizer_failure_in_fake_repo(v, env):
    repo = env["tmp"] / "deck"
    write(repo / "CONVENTIONS.md", "# conventions\n")
    stub = write(repo / "scripts" / "check-sanitized.sh",
                 "#!/usr/bin/env bash\necho 'FORBIDDEN site-specific values found:'\n"
                 "echo 'walter/x.conf:3:example-leak'\nexit 1\n")
    stub.chmod(0o755)
    f = write(repo / "walter" / "x.conf", "server_name example-leak;\n")
    out = call(v, env, [f])
    assert out["action"] == "continue"
    assert "check-sanitized.sh failed" in out["message"]
    [req] = queued(env)
    # request carries only the tag, never the sanitizer's hit lines
    assert req["detail"]["verify"]["failed"] == ["check-sanitized"]
    assert "example-leak" not in json.dumps(req["detail"])


def test_sanitizer_pass_and_nginx_note(v, env):
    repo = env["tmp"] / "deck"
    write(repo / "CONVENTIONS.md", "x\n")
    write(repo / "scripts" / "check-sanitized.sh", "#!/usr/bin/env bash\necho OK\nexit 0\n")
    t = write(repo / "covenant" / "nginx" / "site.conf.tmpl", "server { listen 443; }\n")
    assert call(v, env, [t]) == {}
    notes = queued(env)[0]["detail"]["verify"]["notes"]
    assert any("nginx" in n for n in notes)


# ---------------------------------------------------------------- ssh config
SSH_OLD = "Host edge-alias\n  HostName 203.0.113.10\n  User ubuntu\n\nHost backend\n  HostName 192.168.122.10\n"
SSH_NEW = "Host edge-alias\n  HostName 203.0.113.10\n  User ubuntu\n  IdentityFile ~/.ssh/edge.pem\n\n" \
          "Host backend\n  HostName 192.168.122.10\n"


def test_ssh_alias_probe_failure_only_for_changed_block(v, env, monkeypatch):
    session = "20261002_165907_abc123"
    write(env["review"] / "snapshots" / session / "home" / ".ssh" / "config", SSH_OLD)
    cfg = write(env["home"] / ".ssh" / "config", SSH_NEW)
    probed = []

    def fake_probe(name, args, budget):
        probed.append((name, list(args)))
        return 255, "ssh: connect to host 203.0.113.10 port 22: Connection timed out"

    monkeypatch.setattr(v, "run_probe", fake_probe)
    out = call(v, env, [cfg], response="Updated the edge-alias entry.", session=session)
    assert probed == [("ssh_alias_test", ["edge-alias"])]
    assert out["action"] == "continue" and "edge-alias" in out["message"]


def test_ssh_alias_probe_pass(v, env, monkeypatch):
    session = "20261002_165907_abc123"
    write(env["review"] / "snapshots" / session / ".ssh" / "config", SSH_OLD)
    cfg = write(env["home"] / ".ssh" / "config", SSH_NEW)
    monkeypatch.setattr(v, "run_probe", lambda name, args, budget: (0, "ok"))
    assert call(v, env, [cfg], session=session) == {}


def test_ssh_no_baseline_tests_aliases_named_in_answer(v, env, monkeypatch):
    cfg = write(env["home"] / ".ssh" / "config", SSH_NEW)
    probed = []
    monkeypatch.setattr(v, "run_probe", lambda n, a, b: probed.append(a[0]) or (0, ""))
    call(v, env, [cfg], response="Added IdentityFile to edge-alias in ~/.ssh/config.")
    assert probed == ["edge-alias"]


def test_parse_ssh_hosts(v):
    h = v.parse_ssh_hosts("Host a b *.x\n  User u # c\nHost=c\nMatch host z\n  User q\n")
    assert set(h) == {"a", "b", "c"} and "User u" in h["a"]


# ---------------------------------------------------------------- claims heuristic
def _age(path: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


def test_claims_mismatch_unchanged_ssh_config(v, env):
    """Motivating case: the agent claims it fixed ~/.ssh/config but never touched it."""
    cfg = write(env["home"] / ".ssh" / "config", SSH_OLD)
    _age(cfg, 2 * 86400)
    other = write(env["work"] / "notes.md", "x\n")
    out = call(v, env, [other], response="I fixed the edge-alias entry in ~/.ssh/config, all done.")
    assert out["action"] == "continue"
    assert "~/.ssh/config" in out["message"] and "claims" in out["message"]
    [req] = queued(env)
    assert req["detail"]["claim_flags"][0]["path"] == "~/.ssh/config"


def test_claims_not_flagged_when_path_changed_or_recent_or_negated(v, env):
    cfg = write(env["home"] / ".ssh" / "config", SSH_OLD)
    _age(cfg, 2 * 86400)
    a = write(env["work"] / "a.py", "x = 1\n")
    r = v.Result()
    v.check_claims("Updated a.py and fixed ./a.py.", [str(a)], str(env["work"]), r, 3600)
    v.check_claims("I did not change ~/.ssh/config.", [str(a)], str(env["work"]), r, 3600)
    v.check_claims("~/.ssh/config left unchanged; fixed nothing there.", [str(a)], str(env["work"]), r, 3600)
    v.check_claims("Fixed the redirect for https://example.com/healthz and /healthz.", [str(a)],
                   str(env["work"]), r, 3600)
    b = write(env["work"] / "b.sh", "true\n")  # edited via terminal moments ago
    v.check_claims("Fixed b.sh.", [str(a)], str(env["work"]), r, 3600)
    assert r.failures == [] and r.claim_flags == []


def test_verify_only_claim_is_soft(v, env):
    cfg = write(env["home"] / ".ssh" / "config", SSH_OLD)
    _age(cfg, 2 * 86400)
    a = write(env["work"] / "a.py", "x = 1\n")
    r = v.Result()
    v.check_claims("Verified ~/.ssh/config resolves edge-alias.", [str(a)], str(env["work"]), r, 3600)
    assert r.failures == [] and r.soft_claims and r.soft_claims[0]["path"] == "~/.ssh/config"


def test_claim_of_missing_new_file(v, env):
    a = write(env["work"] / "a.py", "x = 1\n")
    r = v.Result()
    v.check_claims("Created scripts/check.sh as requested.", [str(a)], str(env["work"]), r, 3600)
    assert r.failures == []  # parent dir does not exist -> ignored
    (env["work"] / "scripts").mkdir()
    v.check_claims("Created scripts/check.sh as requested.", [str(a)], str(env["work"]), r, 3600)
    assert r.claim_flags and "does not exist" in r.failures[0]


# ---------------------------------------------------------------- pass / robustness / enqueue
def test_all_pass(v, env):
    py = write(env["work"] / "ok.py", "print('ok')\n")
    js = write(env["work"] / "ok.json", '{"a": [1, 2]}')
    sh = write(env["work"] / "ok.sh", "#!/usr/bin/env bash\nset -euo pipefail\necho ok\n")
    assert call(v, env, [py, js, sh], response="Updated ok.py, ok.json and ok.sh; tests pass.") == {}
    assert not (env["review"] / "hook-errors.log").exists()


def test_internal_exception_prints_empty_and_logs(v, env, monkeypatch):
    def boom(payload):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(v, "verify", boom)
    f = write(env["work"] / "x.py", "x=1\n")
    assert call(v, env, [f]) == {}
    assert "synthetic failure" in (env["review"] / "hook-errors.log").read_text()


def test_garbage_stdin_subprocess(env):
    cp = subprocess.run([sys.executable, str(HOOK)], input="not json{", capture_output=True, text=True,
                        timeout=30)
    assert cp.returncode == 0 and json.loads(cp.stdout) == {}


def test_subprocess_end_to_end(env):
    bad = write(env["work"] / "bad.json", "{")
    payload = {"session_id": "s_e2e123", "cwd": str(env["work"]),
               "extra": {"attempt": 0, "changed_paths": [str(bad)], "final_response": "Done."}}
    cp = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True,
                        text=True, timeout=60)
    assert cp.returncode == 0 and json.loads(cp.stdout)["action"] == "continue"


def test_enqueues_completion_request(v, env):
    py = write(env["work"] / "ok.py", "x = 1\n")
    call(v, env, [py], response="Updated ok.py.")
    [req] = queued(env)
    assert req["kind"] == "completion"
    assert req["id"].endswith("-abc123-completion")
    assert req["changed_paths"] == [str(py)]
    assert req["claims"] == "Updated ok.py."
    assert req["session"] == "20261002_165907_abc123"
    assert req["data_class"] in ("infra", "sensitive")
    mode = (env["review"] / "queue" / f"{req['id']}.json").stat().st_mode & 0o777
    assert mode == 0o600


def test_enqueue_dedupe_same_session_window(v, env):
    py = write(env["work"] / "ok.py", "x = 1\n")
    # an on_session_end request for the same session already covers this path
    existing = {"id": "20261003T035210Z-abc123-completion", "kind": "completion",
                "session": "20261002_165907_abc123", "created": v._utc_iso(),
                "changed_paths": [str(py)], "claims": "", "source_event": "on_session_end"}
    write(env["review"] / "queue" / f"{existing['id']}.json", json.dumps(existing))
    call(v, env, [py])
    assert len(queued(env)) == 1
    # a new path is not a duplicate
    py2 = write(env["work"] / "two.py", "y = 2\n")
    time.sleep(1.1)  # distinct request id second
    call(v, env, [py, py2])
    assert len(queued(env)) == 2
    # another session is never a duplicate
    time.sleep(1.1)
    call(v, env, [py], session="20261002_170000_zzz999")
    assert len(queued(env)) == 3


def test_queue_module_used_when_present(v, env, monkeypatch):
    calls = []

    class FakeQueue:
        @staticmethod
        def write_request(req):
            if req["source_event"] not in ("post_tool_call", "pre_tool_call", "on_session_end", "watch"):
                raise ValueError("schema: bad source_event")
            calls.append(req)
            return "ok"

    monkeypatch.setattr(v, "_import", lambda name: FakeQueue if name == "queue" else None)
    py = write(env["work"] / "ok.py", "x = 1\n")
    call(v, env, [py])
    assert len(calls) == 1 and calls[0]["source_event"] == "on_session_end"
    assert calls[0]["detail"]["hook"] == "pre_verify"


def test_claims_snapshot_hash_decides(v, env):
    """With a session-start snapshot, an identical hash is flagged even if mtime is fresh, and a changed
    hash (edited via the terminal) is not flagged."""
    import hashlib

    cfg = write(env["home"] / ".ssh" / "config", SSH_OLD)  # fresh mtime
    sha = hashlib.sha256(cfg.read_bytes()).hexdigest()
    a = write(env["work"] / "a.py", "x = 1\n")
    r = v.Result()
    v.check_claims("Fixed ~/.ssh/config.", [str(a)], str(env["work"]), r, 3600,
                   snap_index={str(cfg): {"sha256": sha, "size": 1}})
    assert r.claim_flags and "session-start snapshot" in r.failures[0]
    r2 = v.Result()
    v.check_claims("Fixed ~/.ssh/config.", [str(a)], str(env["work"]), r2, 3600,
                   snap_index={str(cfg): {"sha256": "0" * 64, "size": 1}})
    assert r2.failures == []


def test_with_real_lib_modules(env, monkeypatch):
    """Integration with judge/lib/{config,queue}.py when they exist: schema-valid request lands in queue/."""
    if not (JUDGE / "lib" / "queue.py").is_file():
        pytest.skip("lib/queue.py not present yet")
    monkeypatch.setenv("SITE_ENV", str(env["tmp"] / "no-site.env"))
    spec = importlib.util.spec_from_file_location("judge_verify_real_lib", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    py = write(env["work"] / "ok.py", "x = 1\n")
    assert call(mod, env, [py], response="Updated ok.py.") == {}
    [req] = queued(env)
    assert req["kind"] == "completion" and req["detail"]["hook"] == "pre_verify"
    from lib import queue as q  # type: ignore
    assert q.validate_request(req) == []
    assert not (env["review"] / "hook-errors.log").exists()
