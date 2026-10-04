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
    mem = env["hermes"] / "memories" / "MEMORY.md"
    mem.write_text("fact one\nfact two\n")
    run(ev(env, "post_tool_call", tool_name="patch",
           tool_input={"path": str(mem), "old_string": "one", "new_string": "one\nfact two"}, extra={"status": "ok"}))
    run(ev(env, "post_tool_call", tool_name="terminal", tool_input={"command": "ls"}, extra={"status": "ok"}))
    run(ev(env, "on_session_end", extra={"completed": True, "turn_id": "t1", "model": "coder"}))
    pending = q.list_pending()
    assert len(pending) == 1
    r = pending[0]
    assert r["kind"] == "completion" and r["source_event"] == "on_session_end"
    assert str(mem) in r["changed_paths"] and r["detail"]["changed_by_others"] == []
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


def test_read_file_records_call_marker_not_a_change(env):
    """#23 (run-2 N6): read_file is in the post_tool_call matcher so gated reads get an outcome. The event
    carries call_id/call_hash for the gate.log match, but no paths: a read never makes a change the agent's."""
    from lib import redact
    sys.path.insert(0, str(JUDGE_DIR / "collector"))
    try:
        import collect
    finally:
        sys.path.remove(str(JUDGE_DIR / "collector"))
    run(ev(env, "on_session_start"))
    mem = env["hermes"] / "memories" / "MEMORY.md"
    ti = {"path": str(mem), "offset": 1, "limit": 50}
    run(ev(env, "post_tool_call", tool_name="read_file", tool_input=ti,
           extra={"status": "ok", "tool_call_id": "call-read-1"}))
    blocked = {"path": str(env["home"] / ".ssh" / "id_ed25519")}
    run(ev(env, "post_tool_call", tool_name="read_file", tool_input=blocked,
           extra={"status": "blocked", "tool_call_id": "call-read-2"}))
    d = env["review"] / "snapshots" / SESSION
    evs = snapshot.events(d)
    assert [(e["tool"], e["paths"], e["status"], e["call_id"]) for e in evs] == [
        ("read_file", [], "ok", "call-read-1"), ("read_file", [], "blocked", "call-read-2")]
    assert evs[0]["call_hash"] == redact.call_hash("read_file", ti)
    assert evs[1]["call_hash"] == redact.call_hash("read_file", blocked)
    # the executed read matches its gate decision (by id, and by hash when Hermes sent no id); the blocked one not
    from datetime import timedelta
    t = q.parse_utc(evs[0]["t"])
    recs = [{"tool": "read_file", "tool_call_id": "call-read-1", "call_hash": evs[0]["call_hash"], "_ts": t},
            {"tool": "read_file", "call_hash": evs[0]["call_hash"], "_ts": t - timedelta(seconds=1)},
            {"tool": "read_file", "tool_call_id": "call-read-2", "call_hash": evs[1]["call_hash"], "_ts": t}]
    executed = [dict(e, _t=q.parse_utc(e["t"])) for e in evs if snapshot.ran(e)]
    assert collect._match_executions(recs, executed) == {0: "tool_call_id"}
    no_id = [dict(e, _t=q.parse_utc(e["t"]), call_id=None) for e in evs if snapshot.ran(e)]
    assert collect._match_executions(recs[1:], no_id) == {0: "call_hash"}
    # someone else changes the file the agent only read: not the agent's change
    mem.write_text("changed by someone else\n")
    run(ev(env, "on_session_end", extra={"completed": True}))
    (r,) = q.list_pending()
    assert str(mem) not in r["changed_paths"]
    assert str(mem) in r["detail"]["changed_by_others"]


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


# ---------------------------------------------------------------- #34 plan coalescing / debounce
def _plan_patch(env, plan, n, turn="turn-1"):
    run(ev(env, "post_tool_call", tool_name="patch",
           tool_input={"path": str(plan), "old_string": "a", "new_string": "b"},
           extra={"status": "ok", "tool_call_id": f"call-{n}", "turn_id": turn}))


def _runner(monkeypatch):
    sys.path.insert(0, str(JUDGE_DIR / "runner"))
    import run_judge as RJ
    judged = []

    def fake_judge(rid):
        judged.append(rid)
        q.move_done(rid)
        return True
    monkeypatch.setattr(RJ, "judge_request", fake_judge)
    return RJ, judged


def test_five_plan_patches_one_request_judged_once_after_turn_end(env, monkeypatch):
    """Phase A: 5 patches to the plan in one turn produced 5 plan reviews of half-applied edits."""
    RJ, judged = _runner(monkeypatch)
    run(ev(env, "on_session_start"))
    plan = env["cwd"] / ".hermes" / "plans" / "2026-10-02-master.md"
    for n in range(1, 6):
        plan.write_text(f"# plan v{n}\n")
        _plan_patch(env, plan, n)
        assert RJ.main(["--pending"]) == 0  # the runner fires on every queue change: nothing is ready yet
    pending = q.list_pending()
    assert len(pending) == 1 and judged == []
    r = pending[0]
    assert r["kind"] == "plan" and r["not_before"] > r["created"] and not q.is_ready(r)
    assert r["detail"]["coalesced"]["writes"] == 5
    assert r["detail"]["coalesced"]["tool_call_ids"] == [f"call-{n}" for n in range(1, 6)]
    assert r["claims"].startswith("# plan v5") and r["changed_paths"] == [str(plan)]
    # the turn ends: the request becomes ready and is judged once, on the final plan text
    plan.write_text("# plan final\n")
    run(ev(env, "on_session_end", extra={"turn_id": "turn-1", "completed": True}))
    [done_plan] = [x for x in q.list_pending() if x["kind"] == "plan"]
    assert q.is_ready(done_plan) and done_plan["detail"]["turn_ended"]
    assert done_plan["claims"].startswith("# plan final")
    assert RJ.main(["--pending"]) == 0
    assert [x for x in judged if x.endswith("-plan")] == [r["id"]]
    assert RJ.main(["--pending"]) == 0
    assert [x for x in judged if x.endswith("-plan")] == [r["id"]]


def test_plan_write_in_next_turn_is_a_new_request(env, monkeypatch):
    run(ev(env, "on_session_start"))
    plan = env["cwd"] / ".hermes" / "plans" / "p.md"
    plan.write_text("# v1\n")
    _plan_patch(env, plan, 1, turn="turn-1")
    run(ev(env, "on_session_end", extra={"turn_id": "turn-1"}))
    plan.write_text("# v2\n")
    _plan_patch(env, plan, 2, turn="turn-2")
    plans = [x for x in q.list_pending() if x["kind"] == "plan"]
    assert len(plans) == 2
    assert [bool(x["detail"].get("turn_ended")) for x in plans] == [True, False]


def test_plan_debounce_expiry_releases_without_turn_end(env, monkeypatch):
    monkeypatch.setenv("JUDGE_PLAN_DEBOUNCE_S", "0")
    run(ev(env, "on_session_start"))
    plan = env["cwd"] / ".hermes" / "plans" / "p.md"
    plan.write_text("# v1\n")
    _plan_patch(env, plan, 1)
    [r] = q.list_pending()
    assert "not_before" not in r and q.is_ready(r)
    monkeypatch.setenv("JUDGE_PLAN_DEBOUNCE_S", "120")
    _plan_patch(env, plan, 2)
    [r] = q.list_pending()
    assert not q.is_ready(r)
    # 121 s later (no plan write): ready, and any hook event rewrites it so judge-review.path fires
    from datetime import datetime, timedelta, timezone
    later = datetime.now(timezone.utc) + timedelta(seconds=121)
    assert q.is_ready(r, now=later)
    assert q.release_due(now=later) == [r["id"]]
    assert q.list_pending()[0]["detail"]["released"]
    assert q.release_due(now=later) == []
