"""Regression tests for the evidence-quality bugs found in the first live judge run.

The incident: a Hermes turn ran `ssh edge-alias 'systemctl is-active nginx'` (passed) and then
`ssh edge-alias 'sudo systemctl reload nginx'`, which the C2 gate escalated; the human did not approve, so it
never ran, and Hermes said so. Collected ~10 minutes later, the bundle (1) held log lines of a LATER turn,
(2) had no gate decisions, and (3) blamed the agent for files the operator changed (a `git pull`).
(4) is defence in depth for the data_class trust added in the previous fix.

Everything runs under tmp_path (temp HOME / HERMES_HOME / JUDGE_REVIEW_DIR); no network, no real ssh.
"""
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from collector_testlib import JUDGE_DIR, FakeRunner, make_env

import collect
import enqueue
from lib import config, snapshot
from lib import queue as q

SESSION = "20261003_031000_a1b2c3"
T0 = datetime(2026, 10, 3, 3, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = make_env(tmp_path, monkeypatch)
    monkeypatch.setenv("JUDGE_LOG_TZ", "UTC")
    monkeypatch.setattr(sys, "argv", ["enqueue.py"])
    e["cwd"] = tmp_path / "work"
    e["cwd"].mkdir()
    return e


def iso(dt):
    return q.utc_now_iso(dt)


def logts(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S,000")


def write_log(env, lines):
    (env["hermes"] / "logs" / "agent.log").write_text("".join(f"{logts(t)} {m}\n" for t, m in lines))
    (env["hermes"] / "logs" / "errors.log").write_text("")


def gate_line(env, ts, decision, command, request=None, session=SESSION):
    rec = {"ts": iso(ts), "session": session, "tool": "terminal", "rule": "remote-mutation",
           "rules": ["remote-mutation"], "decision": decision, "excerpt": command,
           "rule_key": "judge-gate:remote-mutation:0123456789ab", "elapsed_ms": 1.2}
    if request:
        rec["request"] = f"{request}.json"
    env["review"].mkdir(parents=True, exist_ok=True)
    with open(env["review"] / "gate.log", "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def set_mtime(path, dt):
    os.utime(path, (dt.timestamp(), dt.timestamp()))


def hook(payload):
    out = io.StringIO()
    assert enqueue.main(stdin=io.StringIO(json.dumps(payload)), stdout=out) == 0


def hev(env, event, **kw):
    p = {"hook_event_name": event, "tool_name": None, "tool_input": None, "session_id": SESSION,
         "cwd": str(env["cwd"]), "profile": "default", "extra": {}}
    p.update(kw)
    return p


def runner():
    return FakeRunner([(r"find /etc", (0, "# 0 path(s)\n\n# systemctl --failed\n", "")),
                       (r"docker ps", (0, "", ""))])


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def init_repo(repo, files):
    git(repo, "init", "-q")
    for name, text in files.items():
        (repo / name).write_text(text)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "init")


# ------------------------------------------------------------------ bug 1: evidence window
def test_turn1_bundle_has_no_turn2_evidence_even_when_collected_later(env):
    """Two turns; turn 1's completion request is collected after turn 2 has happened."""
    cfg = config.load_config()
    d = snapshot.take(SESSION, str(env["cwd"]), cfg, now=T0)
    skill = env["hermes"] / "skills" / "demo" / "SKILL.md"
    mem = env["hermes"] / "memories" / "MEMORY.md"
    t1_end, t2_end = T0 + timedelta(minutes=10), T0 + timedelta(minutes=20)
    # turn 1: patch the skill; the reload is escalated and never approved
    skill.write_text("# demo skill\nstep one\nstep two\n")
    set_mtime(skill, T0 + timedelta(minutes=3))
    snapshot.record_event(d, "patch", [str(skill)], "ok", now=T0 + timedelta(minutes=3))
    gate_line(env, T0 + timedelta(minutes=4), "approve", "ssh edge-alias 'sudo systemctl reload nginx'")
    # turn 2 (after turn 1's request was created): a new reload attempt, a memory patch, other log lines
    mem.write_text("fact one\nnginx reloaded at last\n")
    set_mtime(mem, T0 + timedelta(minutes=15))
    snapshot.record_event(d, "patch", [str(mem)], "ok", now=T0 + timedelta(minutes=15))
    gate_line(env, T0 + timedelta(minutes=14), "approve", "ssh edge-alias 'sudo nginx -s reload' TURN2")
    write_log(env, [
        (T0 + timedelta(minutes=1), f"INFO [{SESSION}] agent.tool_executor: tool terminal completed (0.2s) TURN1-check"),
        (T0 + timedelta(minutes=9), f"INFO [{SESSION}] agent.conversation_loop: TURN1 reload blocked, never ran"),
        (t1_end + timedelta(seconds=30), f"INFO [{SESSION}] agent.conversation_loop: TURN2 starts"),
        (T0 + timedelta(minutes=16), f"INFO [{SESSION}] agent.tool_executor: tool terminal completed TURN2-reload ok"),
    ])
    r1 = q.make_request("completion", SESSION, iso(T0), source_event="on_session_end", changed_paths=[str(skill)],
                        claims="The reload was blocked: timed out waiting for approval, never ran.",
                        data_class="infra", created=iso(t1_end), detail={"cwd": str(env["cwd"])})
    q.write_request(r1)
    r2 = q.make_request("completion", SESSION, iso(t1_end), source_event="on_session_end",
                        changed_paths=[str(skill), str(mem)], claims="Reloaded.", data_class="infra",
                        created=iso(t2_end), detail={"cwd": str(env["cwd"])})
    q.write_request(r2)

    fr = runner()
    ev1 = collect.collect(r1["id"], runner=fr, now=t2_end + timedelta(minutes=10))
    man = json.loads((ev1 / "manifest.json").read_text())
    # no `conversation turn:` log lines here, so turn 2's request (since = t1_end) bounds turn 1's window (#16)
    assert man["window"] == {"since": iso(T0), "until": iso(t1_end), "grace_seconds": 10,
                             "until_basis": "next_request", "next_turn_start": iso(t1_end)}
    log = (ev1 / "hermes-log.txt").read_text()
    assert "TURN1 reload blocked" in log and "TURN1-check" in log
    assert "TURN2" not in log
    gates = (ev1 / "gate-decisions.jsonl").read_text()
    assert "reload nginx" in gates and "TURN2" not in gates
    # host find is bounded on both sides, in UTC
    finds = [c["argv"][-1] for c in fr.calls if "find /etc" in c["argv"][-1]]
    assert finds and all("-newermt '2026-10-03 03:20:00 UTC' ! -newermt '2026-10-03 03:30:00 UTC'" in f
                         for f in finds)
    # turn 2's memory edit is neither the agent's turn-1 change nor someone else's
    diff = (ev1 / "agent-diff.patch").read_text()
    assert "+step two" in diff and "nginx reloaded" not in diff
    others = (ev1 / "others-changed.txt").read_text()
    assert str(mem) not in others and "changed after the window end" in others
    assert man["attribution"]["agent_paths"] == [str(skill)] and man["attribution"]["changed_by_others"] == []
    # point-in-time artifacts are labelled with the collection time
    assert man["point_in_time"]["slots.json"]["observed_at"] == iso(t2_end + timedelta(minutes=10))
    assert "NOT during the session" in man["point_in_time"]["slots.json"]["note"]

    ev2 = collect.collect(r2["id"], runner=runner(), now=t2_end + timedelta(minutes=10))
    assert "TURN2-reload ok" in (ev2 / "hermes-log.txt").read_text()
    assert "TURN1 reload blocked" not in (ev2 / "hermes-log.txt").read_text()
    assert "TURN2" in (ev2 / "gate-decisions.jsonl").read_text()


def test_window_grace_is_configurable(env, monkeypatch):
    monkeypatch.setenv("JUDGE_WINDOW_GRACE_SECONDS", "45")
    cfg = config.load_config()
    assert config.window_grace(cfg) == 45
    req = {"since": "2026-10-03T03:20:00Z", "created": "2026-10-03T03:30:00Z"}
    assert [iso(x) for x in collect.window(req, cfg)] == ["2026-10-03T03:20:00Z", "2026-10-03T03:30:45Z"]
    monkeypatch.setenv("JUDGE_WINDOW_GRACE_SECONDS", "junk")
    assert config.window_grace(config.load_config()) == 10


# ------------------------------------------------------------------ bug 2: gate decisions in the bundle
def run_gate(env, command, call_id=None):
    full = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(env["home"]),
            "HERMES_HOME": str(env["hermes"]), "JUDGE_REVIEW_DIR": str(env["review"]), "SITE_ENV": str(env["site"])}
    payload = {"hook_event_name": "pre_tool_call", "tool_name": "terminal", "tool_input": {"command": command},
               "session_id": SESSION, "cwd": str(env["cwd"]), "profile": "default",
               "extra": {"tool_call_id": call_id} if call_id else {}}
    cp = subprocess.run([sys.executable, str(JUDGE_DIR / "hooks" / "gate.py")], input=json.dumps(payload),
                        capture_output=True, text=True, env=full, timeout=30)
    return json.loads(cp.stdout)


def ran(env, command, call_id=None):
    """post_tool_call for a call that executed (Hermes status "ok"/"completed")."""
    hook(hev(env, "post_tool_call", tool_name="terminal", tool_input={"command": command},
             extra={"status": "ok", **({"tool_call_id": call_id} if call_id else {})}))



def declined(env, command, call_id=None, status="blocked"):
    """What Hermes really emits for a denied/timed-out approval: post_tool_call with status="blocked"."""
    hook(hev(env, "post_tool_call", tool_name="terminal", tool_input={"command": command},
             extra={"status": status, "error_type": "plugin_block",
                    **({"tool_call_id": call_id} if call_id else {})}))

CHECK = "ssh edge-alias 'systemctl is-active nginx'"
RELOAD = "ssh edge-alias 'sudo systemctl reload nginx'"


def completion_gates(env):
    hook(hev(env, "on_session_end", extra={"completed": True}))
    comp = next(r for r in q.list_pending() if r["kind"] == "completion")
    ev = collect.collect(comp["id"], runner=runner())
    return ev, [json.loads(x) for x in (ev / "gate-decisions.jsonl").read_text().splitlines()]


def test_gate_escalation_declined_then_completion_bundle_shows_it(env):
    """The live incident: the check passes and runs; the reload is escalated, never approved, never runs."""
    now = datetime.now(timezone.utc)
    hook(hev(env, "on_session_start"))
    assert run_gate(env, CHECK, "call-1") == {}
    ran(env, CHECK, "call-1")
    assert run_gate(env, RELOAD, "call-2")["action"] == "approve"
    gate_req = next(r for r in q.list_pending() if r["kind"] == "gate")
    write_log(env, [(now, f"INFO [{SESSION}] agent.tool_executor: tool terminal completed (0.3s, 7 chars)"),
                    (now, f"INFO [{SESSION}] agent.conversation_loop: reload blocked: timed out waiting for "
                          "approval, never ran")])
    ev, lines = completion_gates(env)
    assert len(lines) == 1
    rec = lines[0]
    assert rec["decision"] == "approve" and "reload nginx" in rec["excerpt"] and rec["tool_call_id"] == "call-2"
    assert rec["outcome"] == "not_executed" and "escalated to the human" in rec["decision_meaning"]
    man = json.loads((ev / "manifest.json").read_text())
    assert "gate-decisions.jsonl" in man["artifacts"] and "gate-decisions.jsonl" not in man["notes"]
    # events.jsonl holds markers, never the command text
    evtext = (q.snapshot_dir(SESSION) / "events.jsonl").read_text()
    assert "systemctl" not in evtext and '"call_id": "call-1"' in evtext and "call_hash" in evtext

    # the gate request's own bundle carries its decision, even though its window starts at its own creation
    gev = collect.collect(gate_req["id"], runner=runner())
    own = [json.loads(x) for x in (gev / "gate-decisions.jsonl").read_text().splitlines()]
    assert any(x.get("request") == f"{gate_req['id']}.json" for x in own)


def test_gate_escalation_approved_and_executed(env):
    hook(hev(env, "on_session_start"))
    assert run_gate(env, RELOAD, "call-7")["action"] == "approve"
    ran(env, RELOAD, "call-7")                                  # the human approved; Hermes ran it
    _, lines = completion_gates(env)
    assert [(x["outcome"], x["outcome_basis"]) for x in lines] == [
        ("executed", "post_tool_call event matched by tool_call_id")]


def test_gate_outcome_without_tool_call_id_falls_back_to_call_hash(env):
    """No ids anywhere: declined first attempt, approved retry of the same command, unrelated command run."""
    hook(hev(env, "on_session_start"))
    assert run_gate(env, RELOAD)["action"] == "approve"         # declined: no post_tool_call
    assert run_gate(env, RELOAD)["action"] == "approve"         # retry, approved
    ran(env, RELOAD)
    assert run_gate(env, "ssh edge-alias 'sudo systemctl restart foo'")["action"] == "approve"
    ran(env, CHECK)                                             # a different call ran: not a match
    _, lines = completion_gates(env)
    assert [x["outcome"] for x in lines] == ["not_executed", "executed", "not_executed"]
    assert lines[1]["outcome_basis"] == "post_tool_call event matched by call_hash"
    assert "tool_call_id" not in lines[0]


def test_gate_outcome_unknown_for_lines_without_markers(env):
    """gate.log lines written before call markers existed never turn into a false `not_executed`."""
    cfg = config.load_config()
    d = snapshot.take(SESSION, None, cfg, now=T0)
    snapshot.record_event(d, "terminal", [], "ok", now=T0 + timedelta(minutes=2), call_hash="00" * 8)
    gate_line(env, T0 + timedelta(minutes=1), "approve", RELOAD)          # no call_hash / tool_call_id
    r = q.make_request("completion", SESSION, iso(T0), source_event="on_session_end", data_class="sensitive",
                       created=iso(T0 + timedelta(minutes=5)))
    q.write_request(r)
    ev = collect.collect(r["id"], runner=runner(), now=T0 + timedelta(minutes=5))
    assert json.loads((ev / "gate-decisions.jsonl").read_text())["outcome"] == "unknown"


def test_gate_outcome_unknown_without_hook_events(env):
    gate_line(env, T0 + timedelta(minutes=1), "approve", RELOAD)
    r = q.make_request("completion", SESSION, iso(T0), source_event="on_session_end", data_class="sensitive",
                       created=iso(T0 + timedelta(minutes=5)))
    q.write_request(r)
    ev = collect.collect(r["id"], runner=runner(), now=T0 + timedelta(minutes=5))
    rec = json.loads((ev / "gate-decisions.jsonl").read_text())
    assert rec["outcome"] == "unknown"


def test_gate_decisions_redacted_and_scoped_to_session(env):
    created = T0 + timedelta(minutes=5)
    gate_line(env, T0 + timedelta(minutes=1), "approve", "curl -H 'Author" + "ization: Bea" + "rer abcdefghijklmnop12' x")
    gate_line(env, T0 + timedelta(minutes=2), "block", "other session", session="20261003_000000_zzzzzz")
    r = q.make_request("completion", SESSION, iso(T0), source_event="on_session_end", data_class="sensitive",
                       created=iso(created))
    q.write_request(r)
    ev = collect.collect(r["id"], runner=runner(), now=created)
    text = (ev / "gate-decisions.jsonl").read_text()
    assert "abcdefghijklmnop12" not in text and "<redacted>" in text
    assert "other session" not in text


def test_no_gate_decisions_is_explicit(env):
    r = q.make_request("completion", SESSION, iso(T0), source_event="on_session_end", data_class="sensitive",
                       created=iso(T0 + timedelta(minutes=5)))
    q.write_request(r)
    ev = collect.collect(r["id"], runner=runner(), now=T0 + timedelta(minutes=5))
    man = json.loads((ev / "manifest.json").read_text())
    assert (ev / "gate-decisions.jsonl").read_text() == ""
    assert man["notes"]["gate-decisions.jsonl"].startswith("no gate decisions in window")


# ------------------------------------------------------------------ bug 3: attribution of changes
def test_changes_by_others_are_not_the_agents(env):
    """The operator `git pull`s the deck repo mid-session; the agent patches a skill and nothing else."""
    deck = env["repo"]
    init_repo(deck, {"README.md": "deck\n", "notes.md": "n\n"})
    hook(hev(env, "on_session_start"))
    # someone else: a pull that changes a tracked infra file (no agent tool event names it)
    (deck / "README.md").write_text("deck\npulled line\n")
    git(deck, "commit", "-qam", "upstream change")
    # the agent: a patch through the file tool
    skill = env["hermes"] / "skills" / "demo" / "SKILL.md"
    skill.write_text("# demo skill\nstep one\nstep two\n")
    hook(hev(env, "post_tool_call", tool_name="patch",
             tool_input={"path": str(skill), "old_string": "one", "new_string": "one\nstep two"},
             extra={"status": "ok"}))
    hook(hev(env, "post_tool_call", tool_name="terminal", tool_input={"command": "ssh edge-alias true"},
             extra={"status": "ok"}))
    hook(hev(env, "on_session_end", extra={"completed": True}))
    req = next(r for r in q.list_pending() if r["kind"] == "completion")
    assert req["changed_paths"] == [str(skill)]
    assert req["detail"]["changed_by_others"] == [str(deck / "README.md")]

    ev = collect.collect(req["id"], runner=runner())
    diff = (ev / "agent-diff.patch").read_text()
    assert "+step two" in diff and "pulled line" not in diff and "README.md" not in diff
    others = (ev / "others-changed.txt").read_text()
    assert f"M {deck / 'README.md'} | +1 -0" in others
    assert "pulled line" not in others and "never attribute them to the agent" in others
    man = json.loads((ev / "manifest.json").read_text())
    assert man["attribution"]["changed_by_others"] == [str(deck / "README.md")]


def test_only_others_changes_do_not_make_the_request_infra(env):
    deck = env["repo"]
    init_repo(deck, {"README.md": "deck\n"})
    hook(hev(env, "on_session_start"))
    (deck / "README.md").write_text("deck\npulled line\n")     # infra path, changed by someone else
    hook(hev(env, "post_tool_call", tool_name="terminal",
             tool_input={"command": "ssh edge-alias 'systemctl is-active nginx'"}, extra={"status": "ok"}))
    hook(hev(env, "on_session_end", extra={"completed": True}))
    req = next(r for r in q.list_pending() if r["kind"] == "completion")
    assert req["changed_paths"] == [] and req["detail"]["changed_by_others"] == [str(deck / "README.md")]
    assert req["data_class"] == "sensitive"                    # was infra when others' paths counted
    ev = collect.collect(req["id"], runner=runner())
    man = json.loads((ev / "manifest.json").read_text())
    assert man["data_class"] == "sensitive"
    assert "pulled line" not in (ev / "agent-diff.patch").read_text()
    assert "pulled line" not in (ev / "others-changed.txt").read_text()


def test_sensitive_others_do_not_change_class_and_are_withheld_from_infra_bundle(env):
    work = env["cwd"]
    init_repo(work, {"app.py": "print(1)\n"})                  # a non-infra repo as the session cwd
    hook(hev(env, "on_session_start"))
    (work / "app.py").write_text("print('secret sauce')\n")    # someone else
    skill = env["hermes"] / "skills" / "demo" / "SKILL.md"
    skill.write_text("# demo skill\nstep one\nstep two\n")
    hook(hev(env, "post_tool_call", tool_name="write_file", tool_input={"path": str(skill), "content": "x"},
             extra={"status": "ok"}))
    hook(hev(env, "on_session_end", extra={"completed": True}))
    req = next(r for r in q.list_pending() if r["kind"] == "completion")
    assert req["changed_paths"] == [str(skill)] and req["data_class"] == "infra"
    ev = collect.collect(req["id"], runner=runner())
    man = json.loads((ev / "manifest.json").read_text())
    assert man["data_class"] == "infra"
    others = (ev / "others-changed.txt").read_text()
    assert "app.py" not in others and "1 non-infra path(s) withheld" in others
    assert "secret sauce" not in json.dumps(man) and "app.py" not in json.dumps(man["attribution"])


def test_terminal_and_memory_tool_changes_are_the_agents(env):
    hook(hev(env, "on_session_start"))
    ssh_cfg = env["home"] / ".ssh" / "config"
    ssh_cfg.write_text("Host edge-alias\n  User ubuntu\n  IdentityFile ~/.ssh/edge.pem\n")
    hook(hev(env, "post_tool_call", tool_name="terminal",
             tool_input={"command": "printf '  IdentityFile ~/.ssh/edge.pem\\n' >> ~/.ssh/config"},
             extra={"status": "ok"}))
    mem = env["hermes"] / "memories" / "MEMORY.md"
    mem.write_text("fact one\nfact two\n")                       # written by Hermes's memory tool
    now = datetime.now(timezone.utc)
    write_log(env, [(now, f"INFO [{SESSION}] agent.tool_executor: tool memory completed (0.01s, 40 chars)")])
    hook(hev(env, "on_session_end", extra={"completed": True}))
    req = next(r for r in q.list_pending() if r["kind"] == "completion")
    assert req["changed_paths"] == sorted([str(mem), str(ssh_cfg)])
    assert req["detail"]["changed_by_others"] == []


def test_terminal_paths_tokens():
    got = enqueue.terminal_paths("cd x && sed -i 's/a/b/' ~/.ssh/config >out.log 2>&1; cat notes.md "
                                 "| curl https://u:p@h.example.com/x --data=@/tmp/f.json", "/w")
    home = os.path.expanduser("~")
    assert f"{home}/.ssh/config" in got and "/w/out.log" in got and "/w/notes.md" in got
    assert not any("://" in p or "h.example.com" in p for p in got)
    assert enqueue.terminal_paths("echo 'unterminated", "/w") == []


# ------------------------------------------------------------------ bug 4: classification defence in depth
@pytest.mark.parametrize("kind,detail", [
    ("completion", {}),                                         # forged / buggy label
    ("completion", {"cwd": "/home/x/company"}),
    ("plan", {}),
    ("runaway", {}),
    ("gate", {"rules": ["secret-output"]}),                     # non-host gate rule
    ("gate", {"rules": ["remote-mutation", "public-push"]}),
    ("gate", {"rules": []}),
    ("gate", {}),
])
def test_infra_label_without_paths_is_not_trusted(env, kind, detail):
    req = {"kind": kind, "changed_paths": [], "data_class": "infra", "detail": detail}
    assert collect.effective_class(req, config.load_config()) == "sensitive"


def test_forged_infra_completion_bundle_is_sensitive(env):
    r = q.make_request("completion", SESSION, iso(T0), source_event="on_session_end", data_class="infra",
                       created=iso(T0 + timedelta(minutes=5)))
    q.write_request(r)
    ev = collect.collect(r["id"], runner=runner(), now=T0 + timedelta(minutes=5))
    man = json.loads((ev / "manifest.json").read_text())
    assert man["data_class"] == "sensitive" and man["request_data_class"] == "infra"


def test_infra_cwd_still_counts_for_pathless_requests(env):
    req = {"kind": "completion", "changed_paths": [], "data_class": "infra", "detail": {"cwd": str(env["repo"])}}
    assert collect.effective_class(req, config.load_config()) == "infra"


def test_host_rule_gate_request_is_still_infra(env):
    out = run_gate(env, "ssh edge-alias 'sudo systemctl reload nginx'")
    assert out["action"] == "approve"
    req = next(r for r in q.list_pending() if r["kind"] == "gate")
    assert req["data_class"] == "infra" and req["detail"]["rules"] == ["remote-mutation"]
    ev = collect.collect(req["id"], runner=runner())
    assert json.loads((ev / "manifest.json").read_text())["data_class"] == "infra"


# --- calls that never ran (Hermes emits post_tool_call with status="blocked" for a denied/timed-out
# approval, "cancelled"/"aborted" for interrupted calls) must not count as the agent touching a file ---

@pytest.mark.parametrize("status", ["blocked", "cancelled", "aborted"])
def test_not_run_calls_do_not_attribute(tmp_path, status):
    from lib import snapshot
    d = tmp_path / "snap"
    ran_path, blocked_path = str(tmp_path / "ran.txt"), str(tmp_path / "ssh_config")
    snapshot.record_event(d, "write_file", [ran_path], "completed")
    snapshot.record_event(d, "write_file", [blocked_path], status)
    paths, _ = snapshot.agent_touched(d)
    keys = {os.path.realpath(p) for p in paths}
    assert os.path.realpath(ran_path) in keys
    assert os.path.realpath(blocked_path) not in keys
    agent, others = snapshot.attribute([ran_path, blocked_path], paths)
    assert agent == [ran_path] and others == [blocked_path]


def test_not_run_statuses_shared():
    from lib import snapshot
    assert collect.NOT_RUN_STATUSES is snapshot.NOT_RUN_STATUSES
    assert {"blocked", "cancelled", "aborted"} <= snapshot.NOT_RUN_STATUSES
    assert snapshot.ran({"status": "completed"}) and snapshot.ran({}) and not snapshot.ran({"status": "Blocked"})



def test_gate_escalation_declined_with_real_hermes_blocked_event(env):
    """Hermes fires post_tool_call even for the declined call (status="blocked"): still not_executed."""
    hook(hev(env, "on_session_start"))
    assert run_gate(env, RELOAD, "call-2")["action"] == "approve"
    declined(env, RELOAD, "call-2")
    _, lines = completion_gates(env)
    assert [x["outcome"] for x in lines] == ["not_executed"]


def test_hash_fallback_with_real_blocked_events(env):
    """No ids: first attempt declined (blocked event), retry approved and run."""
    hook(hev(env, "on_session_start"))
    assert run_gate(env, RELOAD)["action"] == "approve"
    declined(env, RELOAD)
    assert run_gate(env, RELOAD)["action"] == "approve"
    ran(env, RELOAD)
    _, lines = completion_gates(env)
    assert [x["outcome"] for x in lines] == ["not_executed", "executed"]


@pytest.mark.parametrize("tool,sub,status,expect_agent", [
    ("memory", "memories/MEMORY.md", "completed", True),
    ("skill_manage", "skills/demo/SKILL.md", "completed", True),
    ("memory", "memories/MEMORY.md", "blocked", False),      # never ran: not the agent's change
])
def test_memory_and_skill_hook_events_attribute(tmp_path, tool, sub, status, expect_agent):
    """post_tool_call for memory/skill_manage (installer matcher) attributes HERMES_HOME/<dir>/ changes."""
    from lib import snapshot
    hh = tmp_path / "hermes"
    d = tmp_path / "snap"
    snapshot.record_event(d, tool, [], status)
    paths, prefixes = snapshot.agent_touched(d, {"HERMES_HOME": str(hh)})
    target = str(hh / sub)
    agent, others = snapshot.attribute([target], paths, prefixes)
    assert (agent == [target]) is expect_agent and (others == [target]) is (not expect_agent)
