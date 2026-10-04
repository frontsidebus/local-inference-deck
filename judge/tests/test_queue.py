import json
import os
from datetime import datetime, timezone

import pytest

from collector_testlib import make_env

from lib import queue as q

NOW = datetime(2026, 10, 3, 3, 52, 10, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path, monkeypatch):
    return make_env(tmp_path, monkeypatch)


def _req(**kw):
    base = dict(kind="completion", session="20261002_165907_fdc8ec", since="2026-10-03T03:20:00Z",
                source_event="on_session_end", changed_paths=["/etc/x"], claims="done", data_class="infra",
                created=q.utc_now_iso(NOW))
    base.update(kw)
    return q.make_request(**base)


def test_request_id_format_and_uniqueness(env):
    rid = q.new_request_id("completion", "20261002_165907_fdc8ec", NOW)
    assert rid == "20261003T035210Z-fdc8ec-completion"
    q.write_request(_req())
    assert q.new_request_id("completion", "20261002_165907_fdc8ec", NOW) == "20261003T035211Z-fdc8ec-completion"
    assert q.session_short("api-a4ccd7933b12df86") == "12df86"
    assert q.session_short("") == "nosess"
    with pytest.raises(ValueError):
        q.new_request_id("bogus", "s", NOW)


def test_write_request_atomic_modes(env):
    p = q.write_request(_req())
    assert (p.stat().st_mode & 0o777) == 0o600
    assert (p.parent.stat().st_mode & 0o777) == 0o700
    assert (env["review"].stat().st_mode & 0o777) == 0o700
    assert not [f for f in os.listdir(p.parent) if f.endswith(".tmp")]
    assert json.loads(p.read_text())["id"] == "20261003T035210Z-fdc8ec-completion"


def test_write_request_rejects_invalid(env):
    r = _req()
    r["kind"] = "nope"
    with pytest.raises(q.ValidationError) as e:
        q.write_request(r)
    assert any("kind" in m for m in e.value.errors)
    r = _req()
    del r["since"]
    with pytest.raises(q.ValidationError):
        q.write_request(r)
    r = _req()
    r["id"] = "../../etc/passwd"
    with pytest.raises(q.ValidationError):
        q.write_request(r)


def test_claims_truncated_and_validator_subset():
    r = q.make_request("plan", "s1", "2026-10-03T03:20:00Z", source_event="post_tool_call", claims="x" * 5000,
                       created="2026-10-03T03:52:10Z", request_id="20261003T035210Z-s1-plan")
    assert len(r["claims"]) == 4000 and not q.validate_request(r)
    schema = {"type": "object", "required": ["a"], "properties": {
        "a": {"type": "array", "items": {"type": "string", "enum": ["x", "y"]}},
        "b": {"type": ["string", "null"]}, "n": {"type": "integer"}}}
    assert q.validate({"a": ["x"], "b": None, "n": 3}, schema) == []
    errs = q.validate({"a": ["z", 1], "b": 2, "n": True}, schema)
    assert len(errs) == 4
    assert q.validate({}, schema) == ["$: missing required 'a'"]


def test_list_pending_move_done_read(env):
    r1 = q.write_request(_req())
    r2 = q.write_request(_req(kind="plan", plan="/w/.hermes/plans/a.md", source_event="post_tool_call"))
    (env["review"] / "queue" / "garbage.json").write_text("{")
    ids = [r["id"] for r in q.list_pending()]
    assert ids == sorted([r1.stem, r2.stem])
    q.move_done(r1.stem)
    assert [r["id"] for r in q.list_pending()] == [r2.stem]
    assert q.read_request(r1.stem)["id"] == r1.stem
    with pytest.raises(FileNotFoundError):
        q.read_request("20200101T000000Z-aaaaaa-plan")


def _finding(items):
    return {"request": "20261003T035210Z-fdc8ec-completion", "judge": "test-model", "created": "2026-10-03T04:00:00Z",
            "mode": "frontier", "items": items}


ITEM = {"id": "F1", "rubric": "R1", "severity": "high", "claim": "alias fixed",
        "evidence": "ssh -o BatchMode=yes edge-alias true -> Permission denied (publickey)", "verdict": "false",
        "recommendation": "set User and IdentityFile"}


def test_findings_roundtrip_markdown_and_drop(env):
    empty = dict(ITEM, id="F2", evidence="  ")
    jp, mp = q.write_finding(_finding([ITEM, empty]))
    data = json.loads(jp.read_text())
    assert [i["id"] for i in data["items"]] == ["F1"]
    md = mp.read_text()
    assert "## F1 [high] R1: verdict false" in md and "Permission denied" in md
    assert (jp.stat().st_mode & 0o777) == 0o600 and (mp.stat().st_mode & 0o777) == 0o600
    assert [f["request"] for f in q.read_findings()] == ["20261003T035210Z-fdc8ec-completion"]
    with pytest.raises(q.ValidationError):
        q.write_finding(_finding([dict(ITEM, rubric="R9")]))


def test_ack(env):
    q.write_finding(_finding([ITEM, dict(ITEM, id="F2")]))
    rid = "20261003T035210Z-fdc8ec-completion"
    assert not q.is_acked(rid, "F1")
    assert len(q.unacked_items()) == 2
    p = q.ack(rid, "F1", "fixed in place\nsecond line ignored", now=NOW)
    assert json.loads(p.read_text()) == {"actor": "human", "reason": "fixed in place", "ts": "2026-10-03T03:52:10Z"}
    assert (p.stat().st_mode & 0o777) == 0o600
    assert q.is_acked(rid, "F1") and not q.is_acked(rid, "F2")
    assert [i["id"] for _, i in q.unacked_items()] == ["F2"]
    q.write_ack(rid, "F2")
    assert q.is_acked(rid, "F2") and json.loads((env["review"] / "acks" / f"{rid}.F2").read_text())["reason"] == ""
    with pytest.raises(ValueError):
        q.ack(rid, "../x")
    assert q.is_acked(rid, "../x") is False


def test_is_duplicate(env):
    first = _req(changed_paths=["/etc/a", "/etc/b"], source_event="pre_verify")
    q.write_request(first)
    later = _req(changed_paths=["/etc/a"], created="2026-10-03T04:00:00Z")
    assert q.is_duplicate(later) == first["id"]
    assert q.is_duplicate(dict(later, changed_paths=[])) == first["id"]
    assert q.is_duplicate(dict(later, changed_paths=["/etc/c"])) is None
    assert q.is_duplicate(dict(later, created="2026-10-03T05:00:00Z")) is None
    assert q.is_duplicate(dict(later, session="other")) is None
    q.move_done(first["id"])
    assert q.is_duplicate(later) == first["id"]


def test_is_duplicate_turn_aware(env):
    """Live bug: two clean read-only turns (no changed paths) in one session within 900 s; the second
    turn's review was dropped as a 'duplicate'. Different Hermes turn ids are never duplicates."""
    t1 = _req(changed_paths=[], source_event="on_session_end", detail={"turn_id": "sess:sess:aaaa"})
    q.write_request(t1)
    t2 = _req(changed_paths=[], source_event="on_session_end", created="2026-10-03T04:00:00Z",
              detail={"turn_id": "sess:sess:bbbb"})
    assert q.is_duplicate(t2) is None                                    # different turn: keep it
    assert q.is_duplicate(dict(t2, detail={"turn_id": "sess:sess:aaaa"})) == t1["id"]  # same turn: dup


def test_is_duplicate_pre_verify_without_turn_id_still_dedupes(env):
    """pre_verify payloads carry no turn id; the session_end request of the same turn still dedupes."""
    pv = _req(changed_paths=["/etc/a"], source_event="pre_verify")       # no detail.turn_id
    q.write_request(pv)
    end = _req(changed_paths=[], source_event="on_session_end", created="2026-10-03T04:00:00Z",
               detail={"turn_id": "sess:sess:aaaa"})
    assert q.is_duplicate(end) == pv["id"]


# ---------------------------------------------------------------- #34 deferred plan requests
def _plan(session="s-1", turn="t1", plan="/w/.hermes/plans/p.md", created="2026-10-04T04:46:52Z",
          not_before="2026-10-04T04:48:52Z", call="c1", root=None):
    return q.make_request("plan", session, "2026-10-04T04:43:18Z", source_event="post_tool_call",
                          changed_paths=[plan], claims="# plan", plan=plan, data_class="infra",
                          detail={"turn_id": turn, "tool_call_id": call}, created=created,
                          not_before=not_before, root=root)


def test_is_ready_not_before(tmp_path):
    from datetime import datetime, timezone
    r = _plan(root=tmp_path)
    assert not q.is_ready(r, now=datetime(2026, 10, 4, 4, 48, tzinfo=timezone.utc))
    assert q.is_ready(r, now=datetime(2026, 10, 4, 4, 49, tzinfo=timezone.utc))
    assert q.is_ready({"not_before": "garbage"}) and q.is_ready({})
    p = q.write_request(r, tmp_path)
    early = datetime(2026, 10, 4, 4, 47, tzinfo=timezone.utc)
    assert q.is_ready(p, now=early) is False and q.is_ready(tmp_path / "missing.json", now=early) is True
    assert not q.validate_request(r)


def test_merge_into_pending_plan_rules(tmp_path):
    first = _plan(root=tmp_path)
    q.write_request(first, tmp_path)
    second = _plan(created="2026-10-04T04:47:24Z", not_before="2026-10-04T04:49:24Z", call="c2", root=tmp_path)
    second["since"] = "2026-10-04T04:40:00Z"
    second["data_class"] = "sensitive"
    assert q.merge_into_pending_plan(second, tmp_path) == first["id"]
    [m] = q.list_pending(tmp_path)
    assert m["since"] == "2026-10-04T04:40:00Z" and m["created"] == "2026-10-04T04:47:24Z"
    assert m["not_before"] == "2026-10-04T04:49:24Z" and m["data_class"] == "sensitive"
    assert m["detail"]["coalesced"] == {"writes": 2, "first_created": "2026-10-04T04:46:52Z",
                                        "tool_call_ids": ["c1", "c2"]}
    # other plan path, other turn, other session: no merge
    assert q.merge_into_pending_plan(_plan(plan="/w/.hermes/plans/other.md", root=tmp_path), tmp_path) is None
    assert q.merge_into_pending_plan(_plan(turn="t2", root=tmp_path), tmp_path) is None
    assert q.merge_into_pending_plan(_plan(session="s-2", root=tmp_path), tmp_path) is None
    # the runner took it (evidence dir exists): no merge
    (tmp_path / "evidence" / first["id"]).mkdir(parents=True)
    assert q.merge_into_pending_plan(_plan(call="c3", root=tmp_path), tmp_path) is None


def test_release_plan_requests_at_turn_end(tmp_path):
    from datetime import datetime, timezone
    r = _plan(root=tmp_path)
    q.write_request(r, tmp_path)
    q.write_request(q.make_request("completion", "s-1", "2026-10-04T04:43:18Z", source_event="pre_verify",
                                   root=tmp_path), tmp_path)
    now = datetime(2026, 10, 4, 4, 47, tzinfo=timezone.utc)
    assert q.release_plan_requests("s-1", tmp_path, now=now) == [r["id"]]
    rel = q.read_request(r["id"], tmp_path)
    assert rel["not_before"] == "2026-10-04T04:47:00Z" and rel["detail"]["turn_ended"] == "2026-10-04T04:47:00Z"
    assert q.is_ready(rel, now=now)
    assert q.release_plan_requests("s-1", tmp_path, now=now) == []  # already released
    # a released request takes no more merges: the next write starts a new request
    assert q.merge_into_pending_plan(_plan(root=tmp_path), tmp_path) is None


# ---------------------------------------------------------------- #40 queue/deferred/
def test_deferred_requests_live_outside_the_watched_dir(tmp_path):
    from datetime import datetime, timezone
    r = _plan(not_before="2999-01-01T00:00:00Z", root=tmp_path)
    p = q.write_request(r, tmp_path)
    assert p == tmp_path / "queue" / q.DEFERRED / f"{r['id']}.json"
    assert [x["id"] for x in q.list_pending(tmp_path)] == [r["id"]] and q.list_ready(tmp_path) == []
    assert q.read_request(r["id"], tmp_path)["id"] == r["id"] and q.pending_path(r["id"], tmp_path) == p
    # ids stay unique across queue/, queue/deferred/ and done/
    assert q.new_request_id("plan", "s-1", q.parse_utc(r["created"]), tmp_path) != r["id"]
    # a ready request goes straight to queue/
    c = q.make_request("completion", "s-1", "2026-10-04T04:43:18Z", source_event="pre_verify", root=tmp_path)
    assert q.write_request(c, tmp_path).parent == tmp_path / "queue"
    assert [x["id"] for x in q.list_ready(tmp_path)] == [c["id"]]
    # a merge keeps it deferred; the turn end moves it into queue/
    assert q.merge_into_pending_plan(_plan(not_before="2999-01-01T00:00:00Z", call="c2", root=tmp_path),
                                     tmp_path) == r["id"]
    assert p.is_file() and not (tmp_path / "queue" / f"{r['id']}.json").exists()
    now = datetime(2026, 10, 4, 5, 0, tzinfo=timezone.utc)
    assert q.release_plan_requests("s-1", tmp_path, now=now) == [r["id"]]
    assert not p.exists() and (tmp_path / "queue" / f"{r['id']}.json").is_file()
    assert {x["id"] for x in q.list_ready(tmp_path, now=now)} == {r["id"], c["id"]}


def test_release_due_moves_deferred_into_queue(tmp_path):
    from datetime import datetime, timezone
    r = _plan(not_before="2026-10-04T04:48:52Z", root=tmp_path)
    q.write_request(r, tmp_path)  # written "now" (2026+): not_before already passed -> ready -> queue/
    assert q.pending_path(r["id"], tmp_path).parent == tmp_path / "queue"
    late = _plan(session="s-2", not_before="2999-01-01T00:00:00Z", root=tmp_path)
    q.write_request(late, tmp_path)
    assert q.pending_path(late["id"], tmp_path).parent == q.deferred_dir(tmp_path)
    assert q.release_due(tmp_path, now=datetime(2998, 1, 1, tzinfo=timezone.utc)) == [r["id"]]  # legacy: in place
    assert q.release_due(tmp_path, now=datetime(2999, 1, 2, tzinfo=timezone.utc)) == [late["id"]]
    assert q.pending_path(late["id"], tmp_path) == tmp_path / "queue" / f"{late['id']}.json"
    assert not (q.deferred_dir(tmp_path) / f"{late['id']}.json").exists()
    assert q.read_request(late["id"], tmp_path)["detail"]["released"] == "2999-01-02T00:00:00Z"


def test_deferred_rewrite_loses_to_a_concurrent_release(tmp_path, monkeypatch):
    """A merge that rewrites the deferred copy while another hook released the request into queue/ must not
    leave a second copy behind: the queue/ copy wins and the merge reports failure (the caller then writes a
    new request)."""
    r = _plan(not_before="2999-01-01T00:00:00Z", root=tmp_path)
    q.write_request(r, tmp_path)
    released = dict(r, not_before="2026-10-04T04:47:00Z", detail=dict(r["detail"], turn_ended="2026-10-04T04:47:00Z"))
    real = q.atomic_write_json

    def racing_write(path, obj):
        out = real(path, obj)
        if path.parent == q.deferred_dir(tmp_path):  # the other process releases right after our write
            real(tmp_path / "queue" / f"{r['id']}.json", released)
        return out
    monkeypatch.setattr(q, "atomic_write_json", racing_write)
    assert q.merge_into_pending_plan(_plan(not_before="2999-01-01T00:00:00Z", call="c2", root=tmp_path),
                                     tmp_path) is None
    monkeypatch.setattr(q, "atomic_write_json", real)
    assert not (q.deferred_dir(tmp_path) / f"{r['id']}.json").exists()
    [only] = q.list_pending(tmp_path)
    assert only["detail"]["turn_ended"] and q.pending_path(r["id"], tmp_path).parent == tmp_path / "queue"


# ---------------------------------------------------------------- #41 stall detection
def test_stall_status(tmp_path, monkeypatch):
    import fcntl
    import os
    import time
    from datetime import datetime, timezone
    assert q.stall_status(tmp_path) is None                    # no queue dir
    c = q.make_request("completion", "s-1", "2026-10-04T04:43:18Z", source_event="pre_verify", root=tmp_path)
    p = q.write_request(c, tmp_path)
    assert q.stall_status(tmp_path, minutes=15) is None        # fresh
    old = time.time() - 20 * 60
    os.utime(p, (old, old))
    st = q.stall_status(tmp_path, minutes=15)
    assert st["count"] == 1 and st["oldest"] == c["id"] and st["minutes"] >= 19 and st["limit"] == 15
    assert "judge runner is not running or stalled" in q.stall_line(st) and "reset-failed" in q.stall_line(st)
    assert q.stall_status(tmp_path, minutes=0) is None         # off
    assert q.stall_status(tmp_path, minutes=30) is None
    monkeypatch.setenv("JUDGE_STALL_MINUTES", "60")
    assert q.stall_status(tmp_path) is None                    # the setting is read
    monkeypatch.delenv("JUDGE_STALL_MINUTES")
    # a deferred request is not a stall (not ready, and not in queue/ at all)
    d = _plan(not_before="2999-01-01T00:00:00Z", root=tmp_path)
    dp = q.write_request(d, tmp_path)
    os.utime(dp, (old, old))
    assert q.stall_status(tmp_path, minutes=15)["count"] == 1
    # a runner holding the lock is busy, not stalled
    with open(tmp_path / ".runner.lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        assert q.runner_busy(tmp_path) and q.stall_status(tmp_path, minutes=15) is None
    assert not q.runner_busy(tmp_path) and q.stall_status(tmp_path, minutes=15) is not None
