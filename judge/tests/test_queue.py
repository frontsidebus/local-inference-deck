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
