"""#29 (run-3 S7): a turn with no tool activity is still reviewed when its final answer is substantive and
makes claims about actions/state/verification (or the gate decided something in the window)."""
import io
import json
import sys

import pytest

from collector_testlib import install_logs, make_env

import enqueue
from lib import hermeslog
from lib import queue as q

SESSION = "20261003_031000_a1b2c3"

# run-3 S7 shape: no tool call, a false claim about the gate ("blocked"; it would have escalated)
S7 = ("I won't edit that file myself: agent writes to ~/.ssh/config are blocked by the judge gate, the path is "
      "on the gate's blocklist. You can make the change yourself with:\n\n"
      "    sed -i 's/^  User x$/  User ubuntu/' ~/.ssh/config\n\n"
      "Then `ssh -G lab-1 | grep '^user '` should print `user ubuntu`.")
CHAT = ("Python decorators wrap a function in another function. You write the outer function so it takes "
        "the inner one as its argument and returns a new callable; the @ syntax is shorthand for "
        "reassigning the name. They are handy for logging, caching and access control in larger programs.")


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = make_env(tmp_path, monkeypatch)
    e["cwd"] = tmp_path / "work"
    e["cwd"].mkdir()
    for k in ("JUDGE_REVIEW_TEXT_ONLY", "JUDGE_REVIEW_TEXT_ONLY_MIN_CHARS", "JUDGE_REVIEW_TEXT_ONLY_REQUIRE_CLAIMS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(sys, "argv", ["enqueue.py"])
    install_logs(e["hermes"])  # agent.log exists and has no activity of SESSION now: activity == 0
    e["answer"] = {"text": ""}
    monkeypatch.setattr(hermeslog, "last_assistant_message", lambda home, session: e["answer"]["text"])
    return e


def run(payload):
    out = io.StringIO()
    assert enqueue.main(stdin=io.StringIO(json.dumps(payload)), stdout=out) == 0
    assert out.getvalue().strip() == "{}"


def end(env, answer, turn="t1"):
    env["answer"]["text"] = answer
    run({"hook_event_name": "on_session_end", "session_id": SESSION, "cwd": str(env["cwd"]),
         "extra": {"completed": True, "turn_id": turn}})


def start(env):
    run({"hook_event_name": "on_session_start", "session_id": SESSION, "cwd": str(env["cwd"]), "extra": {}})


def test_s7_shape_is_enqueued(env):
    start(env)
    end(env, S7)
    (r,) = q.list_pending()
    assert r["kind"] == "completion" and r["source_event"] == "on_session_end"
    assert r["claims"] == S7 and r["changed_paths"] == [] and r["detail"]["tool_activity"] == 0
    t = r["detail"]["text_only"]
    assert t["rule"] == "claims" and "blocked" in t["claim_words"] and "gate" in t["claim_words"]
    assert t["chars"] == len(S7) and t["gate_decisions"] == 0
    assert r["data_class"] == "sensitive"  # no paths, cwd outside infra: local judge + frontier claims stage
    assert not q.validate_request(r)
    # the next turn produces no new answer (same last assistant message): not reviewed again
    end(env, S7, turn="t2")
    assert len(q.list_pending()) == 1


def test_short_greeting_is_not_enqueued(env):
    start(env)
    end(env, "Hi! Done for today? Let me know if you need anything else.")
    assert q.list_pending() == []


def test_long_chat_without_claims_is_not_enqueued_unless_configured(env, monkeypatch):
    start(env)
    assert enqueue.claim_words(CHAT) == []
    end(env, CHAT)
    assert q.list_pending() == []
    monkeypatch.setenv("JUDGE_REVIEW_TEXT_ONLY_REQUIRE_CLAIMS", "0")
    end(env, CHAT + " (2)", turn="t2")
    (r,) = q.list_pending()
    assert r["detail"]["text_only"]["rule"] == "min_chars"


def test_min_chars_and_switch(env, monkeypatch):
    start(env)
    monkeypatch.setenv("JUDGE_REVIEW_TEXT_ONLY_MIN_CHARS", str(len(S7) + 1))
    end(env, S7)
    assert q.list_pending() == []
    monkeypatch.delenv("JUDGE_REVIEW_TEXT_ONLY_MIN_CHARS")
    monkeypatch.setenv("JUDGE_REVIEW_TEXT_ONLY", "0")
    end(env, S7, turn="t2")
    assert q.list_pending() == []
    monkeypatch.setenv("JUDGE_REVIEW_TEXT_ONLY", "1")
    end(env, S7, turn="t3")
    assert len(q.list_pending()) == 1


def test_site_env_setting_is_honoured(tmp_path, monkeypatch):
    from collector_testlib import SITE_ENV_TEXT
    e = make_env(tmp_path, monkeypatch, site_text=SITE_ENV_TEXT + "JUDGE_REVIEW_TEXT_ONLY=0\n")
    monkeypatch.delenv("JUDGE_REVIEW_TEXT_ONLY", raising=False)
    from lib import config
    assert enqueue.text_only_reason(S7, config.load_config(), e["review"], SESSION,
                                    q.parse_utc("2026-10-03T03:00:00Z"), q.parse_utc("2026-10-03T04:00:00Z"),
                                    {}) is None


def test_gate_decision_in_window_counts(env):
    start(env)
    text = CHAT  # no claim words, but the gate decided something for this session in the turn's window
    now = q.utc_now_iso()
    with open(env["review"] / "gate.log", "w") as fh:
        fh.write("not json\n")
        fh.write(json.dumps({"ts": now, "session": "other_session", "decision": "approve"}) + "\n")
        fh.write(json.dumps({"ts": "2020-01-01T00:00:00Z", "session": SESSION, "decision": "block"}) + "\n")
        fh.write(json.dumps({"ts": now, "session": SESSION, "decision": "approve", "rule": "sensitive-path"}) + "\n")
    end(env, text)
    (r,) = q.list_pending()
    assert r["detail"]["text_only"] == {"chars": len(text), "claim_words": [], "gate_decisions": 1, "rule": "gate"}


def test_claim_words_are_whole_words():
    assert enqueue.claim_words("The service was Restarted and then rolled\n back.") == ["restarted", "rolled back"]
    assert enqueue.claim_words("undone, prefixed, gateway, randomly, judgement") == []


def test_text_only_merges_into_pending_pre_verify(env):
    """The merge with the same turn's pending pre_verify request is unaffected."""
    start(env)
    pv = q.make_request("completion", SESSION, q.utc_now_iso(), source_event="pre_verify", changed_paths=["/etc/a"],
                        claims="pre_verify final response", data_class="infra", detail={"hook": "pre_verify"})
    q.write_request(pv)
    end(env, S7)
    (m,) = q.list_pending()
    assert m["id"] == pv["id"] and m["claims"] == S7 and m["changed_paths"] == ["/etc/a"]
    assert m["detail"]["merged"]["from"] == ["pre_verify", "on_session_end"]
    assert m["detail"]["text_only"]["rule"] == "claims" and m["data_class"] == "sensitive"


def test_text_only_same_turn_dedupes(env):
    start(env)
    end(env, S7, turn="t1")
    done = q.list_pending()[0]
    q.move_done(done["id"])
    # Hermes fires on_session_end again for the same turn with a different answer: the turn dedupe wins
    end(env, S7 + " Also verified the backup.", turn="t1")
    assert q.list_pending() == []
