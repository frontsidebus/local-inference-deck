"""#12: on_session_end merges into the same turn's pending pre_verify completion request
(lib/queue.merge_into_pending_completion), unit level and through hooks/enqueue.py."""
import io
import json
import sys

import pytest

from collector_testlib import make_env

import enqueue
from lib import hermeslog
from lib import queue as q

SESSION = "20261003_031000_a1b2c3"


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = make_env(tmp_path, monkeypatch)
    e["cwd"] = tmp_path / "work"
    e["cwd"].mkdir()
    monkeypatch.setattr(sys, "argv", ["enqueue.py"])
    return e


def pre_verify(created="2026-10-03T04:00:00Z", paths=("/etc/a",), since="2026-10-03T03:50:00Z", **kw):
    r = q.make_request("completion", SESSION, since, source_event="pre_verify", changed_paths=list(paths),
                       claims="pre_verify final response", data_class="infra", created=created,
                       detail={"hook": "pre_verify", "verify": {"failed": [], "ran": ["bash -n"]}}, **kw)
    q.write_request(r)
    return r


def session_end(created="2026-10-03T04:00:20Z", paths=("/etc/b",), since="2026-10-03T03:55:00Z", turn="t1",
                data_class="infra"):
    return q.make_request("completion", SESSION, since, source_event="on_session_end", changed_paths=list(paths),
                          claims="session_end final response", data_class=data_class, created=created,
                          detail={"turn_id": turn, "completed": True, "changed_by_others": []})


def test_merge_union_claims_turn_and_earliest_since(env):
    pv = pre_verify()
    end = session_end(paths=["/etc/a", "/etc/b"])
    assert q.merge_into_pending_completion(end, since="2026-10-03T03:55:00Z") == pv["id"]
    pending = q.list_pending()
    assert len(pending) == 1
    m = pending[0]
    assert m["id"] == pv["id"] and m["source_event"] == "pre_verify"
    assert m["changed_paths"] == ["/etc/a", "/etc/b"]
    assert m["since"] == "2026-10-03T03:50:00Z"                      # earliest
    assert m["created"] == "2026-10-03T04:00:20Z"                    # the turn's end
    assert m["claims"] == "session_end final response"
    assert m["detail"]["turn_id"] == "t1" and m["detail"]["completed"] is True
    assert m["detail"]["verify"]["ran"] == ["bash -n"]               # pre_verify detail kept underneath
    assert m["detail"]["merged"]["pre_verify_created"] == "2026-10-03T04:00:00Z"
    assert not q.validate_request(m)
    # turn-aware dedupe still works against the merged request
    assert q.is_duplicate(session_end(created="2026-10-03T04:01:00Z", turn="t1")) == pv["id"]
    assert q.is_duplicate(session_end(created="2026-10-03T04:01:00Z", turn="t2")) is None
    # merged once: a second session_end is not folded in again
    assert q.merge_into_pending_completion(session_end(turn="t1"), since="2026-10-03T03:55:00Z") is None


def test_merge_sensitive_wins_and_empty_claims_fall_back(env):
    pv = pre_verify()
    end = session_end(data_class="sensitive")
    end["claims"] = ""
    assert q.merge_into_pending_completion(end, since="2026-10-03T03:55:00Z") == pv["id"]
    m = q.read_request(pv["id"])
    assert m["data_class"] == "sensitive" and m["claims"] == "pre_verify final response"


def test_already_judged_is_not_merged(env):
    pv = pre_verify()
    q.move_done(pv["id"])
    assert q.merge_into_pending_completion(session_end(), since="2026-10-03T03:55:00Z") is None
    assert q.read_request(pv["id"])["changed_paths"] == ["/etc/a"]


def test_being_judged_is_not_merged(env):
    pv = pre_verify()
    q.evidence_dir(pv["id"], create=True)                            # the runner/collector has started
    assert q.merge_into_pending_completion(session_end(), since="2026-10-03T03:55:00Z") is None
    assert q.read_request(pv["id"])["changed_paths"] == ["/etc/a"]


def test_previous_turn_request_stays_separate(env):
    pre_verify(created="2026-10-03T03:40:00Z")                       # last turn's, before the previous end
    assert q.merge_into_pending_completion(session_end(), since="2026-10-03T03:55:00Z") is None


def test_other_session_and_non_pre_verify_not_merged(env):
    q.write_request(q.make_request("completion", "other_session_zzzzzz", "2026-10-03T03:50:00Z",
                                   source_event="pre_verify", changed_paths=["/x"], data_class="infra",
                                   created="2026-10-03T04:00:00Z"))
    q.write_request(q.make_request("completion", SESSION, "2026-10-03T03:50:00Z", source_event="on_session_end",
                                   changed_paths=["/x"], data_class="infra", created="2026-10-03T04:00:05Z"))
    assert q.merge_into_pending_completion(session_end(), since="2026-10-03T03:55:00Z") is None


def test_pre_verify_written_with_fallback_source_event_is_found(env):
    """verify.py retries with source_event on_session_end (detail.hook keeps pre_verify)."""
    r = q.make_request("completion", SESSION, "2026-10-03T03:50:00Z", source_event="on_session_end",
                       changed_paths=["/etc/a"], data_class="infra", created="2026-10-03T04:00:00Z",
                       detail={"hook": "pre_verify"})
    q.write_request(r)
    assert q.merge_into_pending_completion(session_end(), since="2026-10-03T03:55:00Z") == r["id"]


# ------------------------------------------------------------------ through the hook
def run(payload):
    out = io.StringIO()
    assert enqueue.main(stdin=io.StringIO(json.dumps(payload)), stdout=out) == 0


def ev(env, event, **kw):
    p = {"hook_event_name": event, "tool_name": None, "tool_input": None, "session_id": SESSION,
         "cwd": str(env["cwd"]), "profile": "default", "extra": {}}
    p.update(kw)
    return p


def test_enqueue_one_request_per_turn(env, monkeypatch):
    monkeypatch.setattr(hermeslog, "last_assistant_message", lambda *a, **k: "All done: fixed MEMORY.md.")
    run(ev(env, "on_session_start"))
    mem = env["hermes"] / "memories" / "MEMORY.md"
    pv = q.make_request("completion", SESSION, q.utc_now_iso(), source_event="pre_verify",
                        changed_paths=["/etc/x"], claims="pre_verify text", data_class="infra",
                        detail={"hook": "pre_verify"})
    q.write_request(pv)
    mem.write_text("fact one\nfact two\n")
    run(ev(env, "post_tool_call", tool_name="patch", tool_input={"path": str(mem), "old_string": "a",
                                                                   "new_string": "b"}, extra={"status": "ok"}))
    run(ev(env, "on_session_end", extra={"completed": True, "turn_id": "turn-1"}))
    pending = q.list_pending()
    assert [r["id"] for r in pending] == [pv["id"]]
    m = pending[0]
    assert set(m["changed_paths"]) == {"/etc/x", str(mem)}
    assert m["claims"] == "All done: fixed MEMORY.md." and m["detail"]["turn_id"] == "turn-1"
    assert "merged" in m["detail"]
    meta = json.loads((env["review"] / "snapshots" / SESSION / "meta.json").read_text())
    assert meta["last_end"] == m["created"]


def test_enqueue_writes_new_request_when_pre_verify_already_judged(env, monkeypatch):
    monkeypatch.setattr(hermeslog, "last_assistant_message", lambda *a, **k: "done")
    run(ev(env, "on_session_start"))
    pv = q.make_request("completion", SESSION, q.utc_now_iso(), source_event="pre_verify",
                        changed_paths=["/etc/x"], data_class="infra", detail={"hook": "pre_verify"})
    q.write_request(pv)
    q.move_done(pv["id"])
    mem = env["hermes"] / "memories" / "MEMORY.md"
    mem.write_text("new fact\n")
    run(ev(env, "post_tool_call", tool_name="write_file", tool_input={"path": str(mem)}, extra={"status": "ok"}))
    run(ev(env, "on_session_end", extra={"turn_id": "turn-1"}))
    pending = q.list_pending()
    assert len(pending) == 1 and pending[0]["id"] != pv["id"]
    assert pending[0]["source_event"] == "on_session_end" and str(mem) in pending[0]["changed_paths"]
    assert q.read_request(pv["id"])["changed_paths"] == ["/etc/x"]   # the judged request is untouched
