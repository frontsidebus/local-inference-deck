"""Regression tests for the collector bugs found in live judge run 1 (#6, #9, #11, #13, #16) and for the
collector side of the C3-results / host-state-probe interface (collector/extras.py, stubbed here).

Everything runs under tmp_path (temp HOME / HERMES_HOME / JUDGE_REVIEW_DIR); no network, no real ssh.
"""
import io
import json
import os
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

from collector_testlib import FakeRunner, make_env

import collect
import enqueue
from lib import config, snapshot
from lib import queue as q

SESSION = "20261003_082327_95e0c3"
T0 = datetime(2026, 10, 3, 8, 30, 0, tzinfo=timezone.utc)
CREATED = T0 + timedelta(minutes=10)
EM = "—"


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = make_env(tmp_path, monkeypatch)
    monkeypatch.setenv("JUDGE_LOG_TZ", "UTC")
    monkeypatch.setattr(sys, "argv", ["enqueue.py"])
    monkeypatch.setitem(sys.modules, "extras", None)  # default: extras.py absent (tests opt in with a stub)
    e["cwd"] = tmp_path / "work"
    e["cwd"].mkdir()
    return e


def iso(dt):
    return q.utc_now_iso(dt)


def runner():
    return FakeRunner([(r"find /etc", (0, "# 0 path(s)\n", ""))])


def write_log(env, lines):
    (env["hermes"] / "logs" / "agent.log").write_text(
        "".join(f"{t.strftime('%Y-%m-%d %H:%M:%S')},000 {m}\n" for t, m in lines))
    (env["hermes"] / "logs" / "errors.log").write_text("")


def request(paths, data_class="infra", kind="completion", since=T0, created=CREATED, cwd=None):
    r = q.make_request(kind, SESSION, iso(since), source_event="on_session_end", changed_paths=paths,
                       claims="Done.", data_class=data_class, created=iso(created),
                       detail={"cwd": str(cwd)} if cwd else {})
    q.write_request(r)
    return r


def take(env, cwd=None):
    return snapshot.take(SESSION, str(cwd or env["cwd"]), config.load_config(), now=T0)


def patched(d, *paths, at=T0 + timedelta(minutes=2), tool="patch"):
    snapshot.record_event(d, tool, [str(p) for p in paths], "ok", now=at)


def manifest(ev):
    return json.loads((ev / "manifest.json").read_text())


def hook(env, event, **kw):
    p = {"hook_event_name": event, "tool_name": None, "tool_input": None, "session_id": SESSION,
         "cwd": str(env["cwd"]), "profile": "default", "extra": {}}
    p.update(kw)
    assert enqueue.main(stdin=io.StringIO(json.dumps(p)), stdout=io.StringIO()) == 0


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


# ------------------------------------------------------------------ #6 request paths are not trusted
def test_forged_request_paths_are_rejected(env):
    d = take(env)
    skill = env["hermes"] / "skills" / "demo" / "SKILL.md"
    mem = env["hermes"] / "memories" / "MEMORY.md"
    skill.write_text("# demo skill\nstep one\nstep two\n")       # the agent's patch (event recorded)
    mem.write_text("fact one\noperator pulled this\n")           # an operator's change (no event)
    for f in (skill, mem):
        os.utime(f, ((T0 + timedelta(minutes=1)).timestamp(),) * 2)
    patched(d, skill)
    r = request([str(skill), str(mem)])
    ev = collect.collect(r["id"], runner=runner(), now=CREATED + timedelta(minutes=1))
    man = manifest(ev)
    att = man["attribution"]
    assert att["agent_paths"] == [str(skill)]
    assert att["rejected_request_paths"] == [str(mem)] and att["rejected_request_paths_total"] == 1
    assert str(mem) in att["changed_by_others"]
    assert "rejected" in man["notes"]["attribution"] and "NOT the agent's" in man["notes"]["attribution"]
    diff = (ev / "agent-diff.patch").read_text()
    assert "+step two" in diff and "operator pulled" not in diff
    assert "1 path(s) listed by the request are NOT attributed to the agent" in diff
    assert str(mem) in (ev / "others-changed.txt").read_text()


def test_request_without_snapshot_or_events_gets_no_agent_paths(env):
    """Legacy request (no snapshot, no events): nothing confirms its paths, so none is the agent's."""
    skill = env["hermes"] / "skills" / "demo" / "SKILL.md"
    r = request([str(skill)])
    man = manifest(collect.collect(r["id"], runner=runner(), now=CREATED))
    assert man["attribution"]["agent_paths"] == []
    assert man["attribution"]["rejected_request_paths"] == [str(skill)]


def test_blocked_event_does_not_back_a_request_path(env):
    d = take(env)
    skill = env["hermes"] / "skills" / "demo" / "SKILL.md"
    skill.write_text("changed by someone else\n")
    snapshot.record_event(d, "patch", [str(skill)], "blocked", now=T0 + timedelta(minutes=1))
    r = request([str(skill)])
    man = manifest(collect.collect(r["id"], runner=runner(), now=CREATED))
    assert man["attribution"]["agent_paths"] == [] and man["attribution"]["rejected_request_paths"] == [str(skill)]


def test_self_write_tool_prefix_backs_request_path(env):
    d = take(env)
    mem = env["hermes"] / "memories" / "MEMORY.md"
    mem.write_text("fact one\nfact two\n")
    patched(d, at=T0 + timedelta(minutes=1), tool="memory")     # memory tool: no path argument
    r = request([str(mem)])
    man = manifest(collect.collect(r["id"], runner=runner(), now=CREATED))
    assert man["attribution"]["agent_paths"] == [str(mem)] and man["attribution"]["rejected_request_paths"] == []


# ------------------------------------------------------------------ #9 withheld content is explained
def test_sensitive_bundle_has_explicit_withheld_lines(env):
    work = env["cwd"]                                            # a non-infra git repo: sensitive
    git(work, "init", "-q")
    (work / "app.py").write_text("a = 1\nb = 2\n")
    git(work, "add", ".")
    git(work, "commit", "-qm", "init")
    d = take(env, work)
    (work / "app.py").write_text("a = 1\nb = 3\nc = 4\nTOPSECRETBODY = 1\n")
    (work / "new.py").write_text("x\ny\n")
    cfgf = env["hermes"] / "config.yaml"
    cfgf.write_text("model: coder\napprovals:\n  mode: off\n")
    patched(d, work / "app.py", work / "new.py", cfgf)
    r = request([str(work / "app.py"), str(work / "new.py"), str(cfgf)], data_class="sensitive", cwd=work)
    ev = collect.collect(r["id"], runner=runner(), now=CREATED)
    man = manifest(ev)
    diff = (ev / "agent-diff.patch").read_text()
    assert man["data_class"] == "sensitive"
    assert f"# content withheld (data_class=sensitive): {work / 'app.py'} {EM} 4 lines changed (+3/-1) [modified]" in diff
    assert f"# content withheld (data_class=sensitive): {work / 'new.py'} {EM} 2 lines changed (+2/-0) [added]" in diff
    assert f"# content withheld (data_class=sensitive): {cfgf} {EM} 2 lines changed (+1/-1) [modified]" in diff
    assert "TOPSECRETBODY" not in diff and "mode: off" not in diff and "c = 4" not in diff
    assert "CONTENT WITHHELD" in diff and "it is not 'no change'" in man["withheld"]["agent-diff.patch"]
    assert man["content_policy"].startswith("stat summaries only")


def test_infra_bundle_has_no_withheld_marker(env):
    d = take(env)
    skill = env["hermes"] / "skills" / "demo" / "SKILL.md"
    skill.write_text("# demo skill\nstep one\nstep two\n")
    patched(d, skill)
    ev = collect.collect(request([str(skill)])["id"], runner=runner(), now=CREATED)
    diff = (ev / "agent-diff.patch").read_text()
    assert "+step two" in diff and "content withheld" not in diff
    assert manifest(ev)["withheld"] == {}


# ------------------------------------------------------------------ #11 Hermes bookkeeping is noise
def _noise_files(env):
    hh = env["hermes"]
    (hh / "skills" / ".locks").mkdir(parents=True, exist_ok=True)
    return [hh / "skills" / ".usage.json", hh / "skills" / ".locks" / "demo.lock",
            hh / "skills" / ".curator_ledger.jsonl", hh / "gateway.lock"]


def test_noise_files_are_not_changes_or_agent_paths(env):
    files = _noise_files(env)
    files[0].write_text('{"demo": 1}\n')
    d = take(env)
    for i, f in enumerate(files):
        f.write_text(f"bump {i}\n")
    patched(d, files[0], tool="skill_manage")                    # self-write tool + an explicit noise path
    changed = [p for _, p in snapshot.changed_files(d, config.load_config())]
    assert changed == []
    assert snapshot.load_meta(d)["noise_globs"][:4] == list(snapshot.NOISE_GLOBS)
    assert not any(str(f) in json.loads((d / "index.json").read_text()) for f in files)
    keys, _ = snapshot.agent_touched(d, config.load_config())
    assert keys == set()


def test_noise_does_not_flip_class_or_count_as_agent(env):
    """A read-only turn whose only 'changes' are bookkeeping files stays as classified by the cwd."""
    files = _noise_files(env)
    take(env)
    for f in files:
        f.write_text("x\n")
    r = request([str(f) for f in files], data_class="infra", cwd=env["cwd"])
    man = manifest(collect.collect(r["id"], runner=runner(), now=CREATED))
    assert man["data_class"] == "sensitive"                       # cwd is not infra; noise does not count
    assert man["attribution"]["agent_paths"] == [] and man["attribution"]["ignored_noise_paths"] == 4
    assert man["attribution"]["rejected_request_paths"] == [] and man["attribution"]["changed_by_others"] == []
    assert collect.effective_class({"changed_paths": [str(files[0])], "data_class": "infra",
                                    "detail": {"cwd": str(env["repo"])}}, config.load_config()) == "infra"


def test_enqueue_read_only_turn_with_noise_writes_no_paths(env):
    hook(env, "on_session_start")
    for f in _noise_files(env):
        f.write_text("x\n")
    hook(env, "post_tool_call", tool_name="terminal", tool_input={"command": "uptime"}, extra={"status": "ok"})
    hook(env, "on_session_end", extra={"completed": True})
    reqs = [r for r in q.list_pending() if r["kind"] == "completion"]
    assert reqs and reqs[0]["changed_paths"] == [] and reqs[0]["detail"]["changed_by_others"] == []
    assert reqs[0]["data_class"] == "sensitive"


def test_judge_noise_globs_extends(env, monkeypatch):
    monkeypatch.setenv("JUDGE_NOISE_GLOBS", "*/memories/.scratch-*, */skills/*.tmp")
    cfg = config.load_config()
    d = take(env)
    (env["hermes"] / "memories" / ".scratch-1").write_text("x\n")
    (env["hermes"] / "skills" / "demo" / "a.tmp").write_text("x\n")
    (env["hermes"] / "memories" / "MEMORY.md").write_text("real change\n")
    assert [p for _, p in snapshot.changed_files(d, cfg)] == [str(env["hermes"] / "memories" / "MEMORY.md")]
    assert snapshot.is_noise(str(env["hermes"] / "skills" / "x" / "a.tmp"), snapshot.noise_globs(cfg))


# ------------------------------------------------------------------ #13 opted-in dirs are snapshotted
def test_sandbox_dir_is_snapshotted_and_diffed(env, tmp_path, monkeypatch):
    sandbox = tmp_path / "sandbox"
    (sandbox / "node_modules").mkdir(parents=True)
    (sandbox / "node_modules" / "dep.js").write_text("ignored\n")
    (sandbox / "service-notes.md").write_text("grafana port 3002\n")
    monkeypatch.setenv("JUDGE_INFRA_REPOS", str(sandbox))
    d = take(env)
    meta = snapshot.load_meta(d)
    assert meta["dir_roots"] == [{"root": str(sandbox), "files": 1, "truncated": False, "too_large": 0}]
    idx = json.loads((d / "index.json").read_text())
    assert str(sandbox / "service-notes.md") in idx and not any("node_modules" in p for p in idx)
    (sandbox / "service-notes.md").write_text("grafana port 3001\n")
    (sandbox / "new.md").write_text("added\n")
    patched(d, sandbox / "service-notes.md", sandbox / "new.md")
    r = request([str(sandbox / "service-notes.md"), str(sandbox / "new.md")], cwd=env["cwd"])
    ev = collect.collect(r["id"], runner=runner(), now=CREATED)
    man = manifest(ev)
    assert man["data_class"] == "infra" and man["snapshot"]["dir_roots"] == [str(sandbox)]
    diff = (ev / "agent-diff.patch").read_text()
    assert "-grafana port 3002" in diff and "+grafana port 3001" in diff and "+added" in diff


def test_sandbox_caps_and_skipped_roots(env, tmp_path, monkeypatch):
    big, small = tmp_path / "big", tmp_path / "small"
    big.mkdir()
    small.mkdir()
    for i in range(5):
        (big / f"f{i}.txt").write_text(f"{i}\n")
    (small / "huge.bin").write_text("z" * 2048)
    (small / "ok.txt").write_text("ok\n")
    monkeypatch.setenv("JUDGE_INFRA_REPOS", f"{big} {small} {tmp_path / 'missing'}")
    monkeypatch.setenv("JUDGE_SNAPSHOT_MAX_FILES", "3")
    monkeypatch.setenv("JUDGE_SNAPSHOT_MAX_BYTES", "1024")
    d = take(env)
    meta = snapshot.load_meta(d)
    info = {x["root"]: x for x in meta["dir_roots"]}
    assert info[str(big)]["truncated"] is True and info[str(big)]["files"] == 3
    assert info[str(small)] == {"root": str(small), "files": 2, "truncated": False, "too_large": 1}
    assert meta["skipped_roots"] == [{"root": str(tmp_path / "missing"), "reason": "missing"}]
    assert meta["snapshot_caps"] == {"max_files": 3, "max_bytes": 1024}
    idx = json.loads((d / "index.json").read_text())
    assert idx[str(small / "huge.bin")]["skipped"] == "too large"
    # truncated root: indexed files still diff, unindexed ones are not reported as added
    (big / "f0.txt").write_text("changed\n")
    (big / "f9.txt").write_text("new\n")
    changed = dict((p, s) for s, p in snapshot.changed_files(d, config.load_config()))
    assert changed == {str(big / "f0.txt"): "M"}
    patched(d, big / "f0.txt")
    ev = collect.collect(request([str(big / "f0.txt")])["id"], runner=runner(), now=CREATED)
    man = manifest(ev)
    assert man["snapshot"]["truncated_roots"] == [{"root": str(big), "files": 3}]
    assert man["snapshot"]["skipped_roots"] == [{"root": str(tmp_path / "missing"), "reason": "missing"}]
    assert "truncated" in man["notes"]["snapshot"]
    diff = (ev / "agent-diff.patch").read_text()
    assert f"opted-in dir {big} was truncated at 3 files" in diff and "+changed" in diff


def test_git_infra_repo_is_diffed_via_git(env, tmp_path, monkeypatch):
    other = tmp_path / "infra-repo"
    other.mkdir()
    git(other, "init", "-q")
    (other / "a.txt").write_text("one\n")
    git(other, "add", ".")
    git(other, "commit", "-qm", "init")
    monkeypatch.setenv("JUDGE_INFRA_REPOS", str(other))
    d = take(env)
    meta = snapshot.load_meta(d)
    assert meta["dir_roots"] == [] and any(r["root"] == str(other) for r in meta["repos"])
    (other / "a.txt").write_text("one\ntwo\n")
    patched(d, other / "a.txt")
    ev = collect.collect(request([str(other / "a.txt")])["id"], runner=runner(), now=CREATED)
    assert "+two" in (ev / "agent-diff.patch").read_text()


# ------------------------------------------------------------------ #16 next turn cuts the window
def _gate(env, ts, text):
    rec = {"ts": iso(ts), "session": SESSION, "tool": "terminal", "rule": "remote-mutation",
           "rules": ["remote-mutation"], "decision": "approve", "excerpt": text}
    env["review"].mkdir(parents=True, exist_ok=True)
    with open(env["review"] / "gate.log", "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def test_next_turn_log_line_cuts_grace_window(env):
    take(env)
    turn = f"INFO [{SESSION}] agent.turn_context: conversation turn: session={SESSION} model=coder msg='x'"
    write_log(env, [(T0 + timedelta(seconds=5), turn),
                    (CREATED - timedelta(seconds=1), f"INFO [{SESSION}] agent.conversation_loop: TURN1 done"),
                    (CREATED + timedelta(seconds=3), turn),
                    (CREATED + timedelta(seconds=5), f"INFO [{SESSION}] agent.conversation_loop: TURN2 work")])
    _gate(env, CREATED - timedelta(seconds=30), "TURN1 gate")
    _gate(env, CREATED + timedelta(seconds=6), "TURN2 gate")
    r = request([])
    w = collect.window_info(r, config.load_config(), env["review"])
    assert (w["basis"], w["until"], w["next_turn_start"]) == (
        "next_turn_log", CREATED + timedelta(seconds=2), CREATED + timedelta(seconds=3))
    ev = collect.collect(r["id"], runner=runner(), now=CREATED + timedelta(hours=1))
    man = manifest(ev)
    assert man["window"]["until"] == iso(CREATED + timedelta(seconds=2))
    assert man["window"]["until_basis"] == "next_turn_log"
    assert man["window"]["next_turn_start"] == iso(CREATED + timedelta(seconds=3))
    log = (ev / "hermes-log.txt").read_text()
    assert "TURN1 done" in log and "TURN2" not in log
    gates = (ev / "gate-decisions.jsonl").read_text()
    assert "TURN1 gate" in gates and "TURN2 gate" not in gates


def test_next_turn_in_same_second_as_created(env):
    turn = f"INFO [{SESSION}] agent.turn_context: conversation turn: session={SESSION} msg='Review the conversation'"
    write_log(env, [(T0 + timedelta(seconds=5), turn), (CREATED, turn)])
    w = collect.window_info(request([]), config.load_config(), env["review"])
    assert w["until"] == CREATED and w["basis"] == "next_turn_log"


def test_log_without_next_turn_keeps_grace_and_ignores_requests(env):
    """The log is authoritative when it has turn lines: a later request's `since` does not cut the grace."""
    turn = f"INFO [{SESSION}] agent.turn_context: conversation turn: session={SESSION} msg='x'"
    write_log(env, [(T0 + timedelta(seconds=5), turn)])
    r = request([])
    request([], since=CREATED, created=CREATED + timedelta(minutes=5))
    w = collect.window_info(r, config.load_config(), env["review"])
    assert w["basis"] == "grace" and w["until"] == CREATED + timedelta(seconds=10)


def test_next_request_fallback_without_turn_lines(env):
    write_log(env, [])
    r = request([])
    request([], kind="gate", since=CREATED + timedelta(seconds=4), created=CREATED + timedelta(seconds=4))
    w = collect.window_info(r, config.load_config(), env["review"])
    assert w["basis"] == "next_request" and w["until"] == CREATED + timedelta(seconds=3)
    # a request of the same turn (window starting before `created`) is not a boundary
    r2 = request([], since=T0 - timedelta(minutes=1), created=T0 + timedelta(minutes=20))
    assert collect.window_info(r2, config.load_config(), env["review"])["basis"] == "grace"


# ------------------------------------------------------------------ extras (B2 interface 1)
def _stub(monkeypatch, c3=None, probes=None, fail=False):
    calls = {}
    mod = types.ModuleType("extras")

    def c3_results(session, since, until, root):
        calls["c3"] = (session, since, until, root)
        if fail:
            raise RuntimeError("boom")
        return c3 or []

    def host_state_probes(req, cfg, runner=None, root=None, gate_lines=None):
        calls["probes"] = (req["id"], root, gate_lines)
        if fail:
            raise RuntimeError("boom")
        return probes or {}

    mod.c3_results, mod.host_state_probes = c3_results, host_state_probes
    monkeypatch.setitem(sys.modules, "extras", mod)
    return calls


C3 = [{"t": "2026-10-03T08:35:00Z", "attempt": 0, "path": "/srv/x.json", "check": "json", "ok": False,
       "detail": "parse error line 3 password=hunter2hunter2"},
      {"t": "2026-10-03T08:36:00Z", "attempt": 1, "path": "/srv/x.json", "check": "json", "ok": True, "detail": ""}]


def test_extras_artifacts_written(env, monkeypatch):
    calls = _stub(monkeypatch, c3=[dict(C3[0], final=False), dict(C3[1], final=True)],
                  probes={"unit_state covenant nginx": "# POINT IN TIME\nactive\n# exit 0\n",
                          "host-journal.txt": "# WINDOWED journal\nline\n"})
    r = request([])
    ev = collect.collect(r["id"], runner=runner(), now=CREATED + timedelta(minutes=5))
    sess, since, until, root = calls["c3"]
    # no next turn known: C3 results run to the collection time (post-fix re-runs land after created + grace)
    assert sess == SESSION and since == T0 and until == CREATED + timedelta(minutes=5) and root == env["review"]
    assert calls["probes"] == (r["id"], env["review"], [])
    lines = [json.loads(x) for x in (ev / "c3-results.jsonl").read_text().splitlines()]
    assert [(x["ok"], x["final"]) for x in lines] == [(False, False), (True, True)]
    assert "hunter2hunter2" not in (ev / "c3-results.jsonl").read_text()
    man = manifest(ev)
    assert "c3-results.jsonl" in man["windowed"] and "final: true" in man["notes"]["c3-results.jsonl"]
    assert {"probes/host-unit_state_covenant_nginx.txt", "probes/host-journal.txt"} <= set(man["artifacts"])
    assert man["point_in_time"]["probes/host-unit_state_covenant_nginx.txt"]["observed_at"] == iso(
        CREATED + timedelta(minutes=5))
    assert "probes/host-journal.txt" in man["windowed"] and "probes/host-journal.txt" not in man["point_in_time"]
    assert "active" in (ev / "probes" / "host-unit_state_covenant_nginx.txt").read_text()
    assert man["extras"]["available"] is True and man["extras"]["c3_results"] == 2


def test_c3_window_runs_to_turn_end_not_grace(env, monkeypatch):
    """C3 re-runs after created + grace belong to the turn; the next turn's start still bounds them."""
    calls = _stub(monkeypatch)
    turn = f"INFO [{SESSION}] agent.turn_context: conversation turn: session={SESSION} msg='x'"
    write_log(env, [(T0 + timedelta(seconds=5), turn), (CREATED + timedelta(minutes=2), turn)])
    _gate(env, CREATED - timedelta(seconds=20), "ssh edge-alias 'sudo systemctl reload nginx'")
    r = request([])
    ev = collect.collect(r["id"], runner=runner(), now=CREATED + timedelta(hours=1))
    assert calls["c3"][2] == CREATED + timedelta(minutes=2) - timedelta(seconds=1)
    assert manifest(ev)["window"]["until"] == iso(CREATED + timedelta(seconds=10))
    assert calls["probes"][2] == ["ssh edge-alias 'sudo systemctl reload nginx'"]


def test_extras_finals_derived_when_unflagged(env, monkeypatch):
    _stub(monkeypatch, c3=C3)
    ev = collect.collect(request([])["id"], runner=runner(), now=CREATED)
    lines = [json.loads(x) for x in (ev / "c3-results.jsonl").read_text().splitlines()]
    assert [x["final"] for x in lines] == [False, True]
    assert manifest(ev)["notes"]["probes/host-*.txt"] == "no host-state claims to probe"


def test_extras_absent_is_graceful(env):
    ev = collect.collect(request([])["id"], runner=runner(), now=CREATED)
    man = manifest(ev)
    assert not (ev / "c3-results.jsonl").exists()
    assert man["extras"] == {"available": False}
    assert "not installed" in man["notes"]["c3-results.jsonl"]
    assert not any(a.startswith("probes/host-") for a in man["artifacts"])


def test_extras_failure_never_breaks_bundle(env, monkeypatch):
    _stub(monkeypatch, fail=True)
    ev = collect.collect(request([])["id"], runner=runner(), now=CREATED)
    man = manifest(ev)
    assert "RuntimeError" in man["notes"]["c3-results.jsonl"] and "RuntimeError" in man["notes"]["probes/host-*.txt"]
    assert (ev / "agent-diff.patch").is_file()


def test_probe_artifact_name_is_sanitised():
    assert collect.probe_artifact_name("../../etc/passwd") == "probes/host-etc_passwd.txt"
    assert collect.probe_artifact_name("host-unit_state-covenant-nginx.txt") == "probes/host-unit_state-covenant-nginx.txt"
    assert collect.probe_artifact_name("") == "probes/host-probe.txt"
