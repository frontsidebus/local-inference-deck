import io
import json
import subprocess
import sys

import pytest

from collector_testlib import JUDGE_DIR, install_logs, make_env

import enqueue
from lib import queue as q
from lib import snapshot

SESSION = "20261003_031000_a1b2c3"


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = make_env(tmp_path, monkeypatch)
    e["cwd"] = tmp_path / "work"
    (e["cwd"] / ".hermes" / "plans").mkdir(parents=True)
    monkeypatch.setattr(sys, "argv", ["enqueue.py"])
    return e


def run(payload):
    out = io.StringIO()
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    assert enqueue.main(stdin=io.StringIO(raw), stdout=out) == 0
    assert out.getvalue().strip() == "{}"


def ev(env, event, **kw):
    p = {"hook_event_name": event, "tool_name": None, "tool_input": None, "session_id": SESSION,
         "cwd": str(env["cwd"]), "profile": "default", "extra": {}}
    p.update(kw)
    return p


def test_session_start_snapshot(env):
    run(ev(env, "on_session_start", extra={"model": "coder", "platform": "cli"}))
    d = env["review"] / "snapshots" / SESSION
    meta = json.loads((d / "meta.json").read_text())
    assert meta["session"] == SESSION and not meta["late"]
    idx = json.loads((d / "index.json").read_text())
    cfg_path = str(env["hermes"] / "config.yaml")
    assert cfg_path in idx and len(idx[cfg_path]["sha256"]) == 64
    assert (d / "files" / cfg_path.lstrip("/")).read_text().startswith("model: coder")
    assert ((d / "files" / cfg_path.lstrip("/")).stat().st_mode & 0o777) == 0o600
    assert (d.stat().st_mode & 0o777) == 0o700
    # idempotent
    (env["hermes"] / "config.yaml").write_text("changed\n")
    run(ev(env, "on_session_start"))
    assert json.loads((d / "meta.json").read_text())["started"] == meta["started"]


def test_plan_write_enqueues_and_refreshes(env):
    run(ev(env, "on_session_start"))
    plan = env["cwd"] / ".hermes" / "plans" / "2026-10-03-master.md"
    plan.write_text("# plan\n1. do thing\n")
    run(ev(env, "post_tool_call", tool_name="write_file", tool_input={"path": str(plan), "content": "x"},
           extra={"status": "ok", "tool_call_id": "c1"}))
    pending = q.list_pending()
    assert len(pending) == 1
    r = pending[0]
    assert r["kind"] == "plan" and r["plan"] == str(plan) and r["data_class"] == "infra"
    assert r["source_event"] == "post_tool_call" and r["claims"].startswith("# plan")
    # second patch to the same plan refreshes the pending request instead of adding one
    plan.write_text("# plan v2\n")
    run(ev(env, "post_tool_call", tool_name="patch",
           tool_input={"path": ".hermes/plans/2026-10-03-master.md", "old_string": "a", "new_string": "b"},
           extra={"status": "ok"}))
    pending = q.list_pending()
    assert len(pending) == 1 and pending[0]["claims"].startswith("# plan v2") and pending[0]["id"] == r["id"]


def test_non_plan_and_failed_writes_do_not_enqueue(env):
    run(ev(env, "post_tool_call", tool_name="write_file", tool_input={"path": str(env["cwd"] / "notes.md")},
           extra={"status": "ok"}))
    plan = env["cwd"] / ".hermes" / "plans" / "p.md"
    run(ev(env, "post_tool_call", tool_name="write_file", tool_input={"path": str(plan)}, extra={"status": "error"}))
    assert q.list_pending() == []
    # but both were recorded, and a late snapshot was created
    d = env["review"] / "snapshots" / SESSION
    assert json.loads((d / "meta.json").read_text())["late"] is True
    assert len(snapshot.events(d)) == 2


def test_v4a_patch_paths(env):
    paths = enqueue.tool_paths("patch", {"mode": "patch", "patch": "*** Begin Patch\n*** Update File: a/x.py\n@@\n"
                                         "*** Add File: /abs/.hermes/plans/new.md\n+hi\n*** End Patch"}, "/w")
    assert paths == ["/w/a/x.py", "/abs/.hermes/plans/new.md"]


def test_session_end_completion(env):
    install_logs(env["hermes"])
    run(ev(env, "on_session_start"))
    (env["hermes"] / "memories" / "MEMORY.md").write_text("fact one\nfact two\n")
    run(ev(env, "post_tool_call", tool_name="terminal", tool_input={"command": "ls"}, extra={"status": "ok"}))
    run(ev(env, "on_session_end", extra={"completed": True, "turn_id": "t1", "model": "coder"}))
    pending = q.list_pending()
    assert len(pending) == 1
    r = pending[0]
    assert r["kind"] == "completion" and r["source_event"] == "on_session_end"
    assert str(env["hermes"] / "memories" / "MEMORY.md") in r["changed_paths"]
    assert r["data_class"] == "infra" and r["detail"]["completed"] is True
    # next turn with no activity and no new changes: skipped
    run(ev(env, "on_session_end", extra={"completed": True}))
    assert len(q.list_pending()) == 1


def test_session_end_dedupes_against_pre_verify(env):
    run(ev(env, "on_session_start"))
    (env["hermes"] / "memories" / "MEMORY.md").write_text("changed\n")
    mem = str(env["hermes"] / "memories" / "MEMORY.md")
    first = q.make_request("completion", SESSION, "2026-10-03T03:20:00Z", source_event="pre_verify",
                           changed_paths=[mem, "/etc/x"], data_class="infra")
    q.write_request(first)
    run(ev(env, "post_tool_call", tool_name="terminal", tool_input={"command": "true"}, extra={}))
    run(ev(env, "on_session_end", extra={}))
    assert [r["id"] for r in q.list_pending()] == [first["id"]]


def test_never_crashes(env, monkeypatch):
    run("not json at all")
    run({"hook_event_name": "post_tool_call", "tool_input": "weird", "session_id": SESSION, "tool_name": "patch"})
    monkeypatch.setattr(snapshot, "take", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom token=abcdefgh123")))
    run(ev(env, "on_session_start"))
    log = (env["review"] / "hook-errors.log").read_text()
    assert "boom" in log and "abcdefgh123" not in log


def test_cli_prints_empty_object(env):
    cp = subprocess.run([sys.executable, str(JUDGE_DIR / "hooks" / "enqueue.py")], input="{bad",
                        capture_output=True, text=True)
    assert cp.returncode == 0 and cp.stdout.strip() == "{}"
