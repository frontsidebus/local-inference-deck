"""#51: an interrupted/failed/timed-out turn that changed nothing, used only read-only tools, had no gate
decision or refusal and left no substantive answer is not enqueued (pilot 3 T1: a timeout after 9 read_file and
1 search_files calls cost a frontier run that returned 0 items). Anything security-relevant is still reviewed."""
import io
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

from collector_testlib import make_env

import enqueue
from lib import hermeslog
from lib import queue as q

SESSION = "20261005_180612_620b61"
TZ = timezone(timedelta(hours=-5))  # JUDGE_LOG_TZ of make_env
INTERRUPTED = {"completed": False, "interrupted": True, "reason": "keyboard_interrupt", "model": "coder",
               "platform": "cli"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = make_env(tmp_path, monkeypatch)
    e["cwd"] = tmp_path / "work"
    e["cwd"].mkdir()
    for k in ("JUDGE_SKIP_INTERRUPTED_NOOP", "JUDGE_REVIEW_TEXT_ONLY_MIN_CHARS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(sys, "argv", ["enqueue.py"])
    e["answer"] = {"text": ""}
    monkeypatch.setattr(hermeslog, "last_assistant_message", lambda home, session: e["answer"]["text"])
    e["log"] = e["hermes"] / "logs" / "agent.log"
    e["log"].write_text("")
    run({"hook_event_name": "on_session_start", "session_id": SESSION, "cwd": str(e["cwd"]), "extra": {}})
    return e


def run(payload):
    out = io.StringIO()
    assert enqueue.main(stdin=io.StringIO(json.dumps(payload)), stdout=out) == 0
    assert out.getvalue().strip() == "{}"


def log_line(env, msg, level="INFO"):
    ts = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S,000")
    with open(env["log"], "a") as fh:
        fh.write(f"{ts} {level} [{SESSION}] agent.tool_executor: {msg}\n")


def call(env, tool, tool_input, status="ok", cid="c1"):
    log_line(env, f"tool {tool} completed (0.02s, 900 chars)")
    run({"hook_event_name": "post_tool_call", "session_id": SESSION, "cwd": str(env["cwd"]), "tool_name": tool,
         "tool_input": tool_input, "extra": {"status": status, "tool_call_id": cid}})


def t1_reads(env):
    """Pilot 3 T1: read_file calls and one search_files (search_files is not a hooked tool: agent.log only)."""
    for i in range(3):
        call(env, "read_file", {"path": str(env["cwd"] / f"f{i}.py")}, cid=f"r{i}")
    log_line(env, "tool search_files completed (0.10s, 300 chars)")


def end(env, extra=None, answer=""):
    env["answer"]["text"] = answer
    run({"hook_event_name": "on_session_end", "session_id": SESSION, "cwd": str(env["cwd"]),
         "extra": dict(INTERRUPTED if extra is None else extra)})


def skipped(env):
    meta = json.loads((env["review"] / "snapshots" / SESSION / "meta.json").read_text())
    return meta.get("last_skipped")


def test_t1_shape_is_not_enqueued_and_is_logged(env):
    t1_reads(env)
    end(env)
    assert q.list_pending() == []
    s = skipped(env)
    assert s["reason"] == "keyboard_interrupt" and s["tools"] == "read_file+search_files" and s["events"] == 3
    line = (env["review"] / "enqueue.log").read_text()
    assert "interrupted turn with no change not reviewed (#51)" in line and SESSION in line
    assert str(env["cwd"]) not in line  # no paths in the log


def test_completed_turn_with_reads_is_still_reviewed(env):
    t1_reads(env)
    end(env, {"completed": True, "turn_id": "t1"})
    (r,) = q.list_pending()
    assert r["kind"] == "completion" and r["changed_paths"] == []


@pytest.mark.parametrize("extra", [
    {"completed": False, "failed": True},
    {"completed": False, "turn_exit_reason": "max_iterations_reached(90/90)"},
    {"completed": False, "turn_exit_reason": "context_compression_timeout"},
    {"completed": False, "interrupted": True, "reason": "keyboard_interrupt"},
])
def test_failed_and_timeout_shapes_are_skipped(env, extra):
    t1_reads(env)
    end(env, extra)
    assert q.list_pending() == []


def test_ended_badly():
    assert not enqueue.ended_badly({"completed": True})
    assert not enqueue.ended_badly({"completed": False, "turn_exit_reason": "text_response(finish_reason=stop)"})
    assert not enqueue.ended_badly({})
    assert not enqueue.ended_badly(None)
    assert enqueue.ended_badly({"interrupted": True})
    assert enqueue.ended_badly({"completed": False, "turn_exit_reason": "interrupted_by_user"})


def test_edits_are_reviewed(env):
    """Pilot 3 T2: a timeout after edits is reviewed as before."""
    t1_reads(env)
    f = env["cwd"] / "test_x.py"
    f.write_text("x = 1\n")
    call(env, "write_file", {"path": str(f), "content": "x = 1\n"}, cid="w1")
    end(env)
    (r,) = q.list_pending()
    assert r["changed_paths"] == [str(f)] and r["detail"]["interrupted"] is True


def test_terminal_or_other_tools_are_reviewed(env):
    t1_reads(env)
    call(env, "terminal", {"command": "ls"}, cid="t1")
    end(env)
    assert len(q.list_pending()) == 1


def test_unhooked_non_read_tool_in_log_is_reviewed(env):
    t1_reads(env)
    log_line(env, "tool web_extract completed (1.20s, 5000 chars)")
    end(env)
    assert len(q.list_pending()) == 1


def test_gate_decision_in_window_is_reviewed(env):
    t1_reads(env)
    rec = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "session": SESSION,
           "decision": "approve", "tool": "read_file", "rule": "secret-output"}
    (env["review"] / "gate.log").write_text(json.dumps(rec) + "\n")
    end(env)
    assert len(q.list_pending()) == 1


def test_refused_call_is_reviewed(env):
    t1_reads(env)
    call(env, "read_file", {"path": str(env["cwd"] / "x.env")}, status="blocked", cid="rb")
    end(env)
    assert len(q.list_pending()) == 1


def test_hermes_native_refusal_is_reviewed(env):
    """A Hermes-native BLOCKED result (agent.log) on a read: lib/refusals finds it, so the turn is reviewed."""
    t1_reads(env)
    run({"hook_event_name": "post_tool_call", "session_id": SESSION, "cwd": str(env["cwd"]), "tool_name": "read_file",
         "tool_input": {"path": str(env["cwd"] / "y.txt")}, "extra": {"status": "error", "tool_call_id": "rn"}})
    log_line(env, 'Tool read_file returned error (0.01s): {"error": "BLOCKED: Security scan \\u2014 [HIGH] '
                  'prompt injection in file", "success": false}', level="WARNING")
    end(env)
    assert len(q.list_pending()) == 1


@pytest.mark.parametrize("answer,reviewed", [
    ("", False),
    ("Still reading the pipeline", False),
    ("Fixed.", True),  # a claim word, however short
    ("I looked at the pipeline and the collector in some depth and found where newness is decided; " * 3, True),
])
def test_final_answer(env, answer, reviewed):
    t1_reads(env)
    end(env, answer=answer)
    assert len(q.list_pending()) == (1 if reviewed else 0)


def test_off_switch_and_missing_log(env, monkeypatch):
    t1_reads(env)
    monkeypatch.setenv("JUDGE_SKIP_INTERRUPTED_NOOP", "0")
    end(env)
    assert len(q.list_pending()) == 1


def test_missing_agent_log_is_reviewed(env):
    t1_reads(env)
    env["log"].unlink()
    end(env)
    assert len(q.list_pending()) == 1
