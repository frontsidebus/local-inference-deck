"""Judge hardening: confirmed changed paths (collector) and the code version stamp (requests, manifest, rejudge).

Item 1: a request's changed_paths count only where the session's own tool events confirm them (write_file/patch
target, terminal cp/mv/install/redirect/tee target, or a snapshot change attributed to the agent). The rest are
"unconfirmed": no content, out of classification, surfaced in the manifest.
Item 2: every request carries lib/version.code_version (files only, no subprocess); the collector records its own
and the request's in the manifest; rejudge notes a mismatch (a warning, never a failure).
Everything under tmp_path; no network, no real ssh, stub judge backends.
"""
import io
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from collector_testlib import FakeRunner, make_env

import collect
import enqueue
from lib import config, snapshot, version
from lib import queue as q

SESSION = "20261005_101500_c0ffee"
T0 = datetime(2026, 10, 5, 10, 15, 0, tzinfo=timezone.utc)
EV_T = T0 + timedelta(minutes=2)
CREATED = T0 + timedelta(minutes=10)
IN_WINDOW = (T0 + timedelta(minutes=3)).timestamp()


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = make_env(tmp_path, monkeypatch)
    monkeypatch.setenv("JUDGE_LOG_TZ", "UTC")
    monkeypatch.setattr(sys, "argv", ["enqueue.py"])
    monkeypatch.setitem(sys.modules, "extras", None)
    e["cwd"] = tmp_path / "work"
    e["cwd"].mkdir()
    return e


def runner():
    return FakeRunner([(r"find /etc", (0, "# 0 path(s)\n", ""))])


def take(env):
    return snapshot.take(SESSION, str(env["cwd"]), config.load_config(), now=T0)


def event(d, tool, *paths, call_id=None, status="ok"):
    snapshot.record_event(d, tool, [str(p) for p in paths], status, now=EV_T, call_id=call_id)


def state_db(hermes, calls):
    con = sqlite3.connect(hermes / "state.db")
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, "
                "tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL)")
    con.execute("INSERT INTO messages (session_id, role, tool_calls, timestamp) VALUES (?, 'assistant', ?, 0)",
                (SESSION, json.dumps([{"id": cid, "type": "function",
                                       "function": {"name": "terminal", "arguments": json.dumps(args)}}
                                      for cid, args in calls])))
    con.commit()
    con.close()


def touch(*paths):
    for p in paths:
        os.utime(p, (IN_WINDOW, IN_WINDOW))


def request(paths, data_class="infra", kind="completion", cwd=None, **extra):
    r = q.make_request(kind, SESSION, q.utc_now_iso(T0), source_event="on_session_end", changed_paths=paths,
                       claims="Done.", data_class=data_class, created=q.utc_now_iso(CREATED),
                       detail={"cwd": str(cwd)} if cwd else {})
    r.update(extra)
    q.write_request(r)
    return r


def collect_it(r):
    ev = collect.collect(r["id"], runner=runner(), now=CREATED + timedelta(minutes=1))
    return ev, json.loads((ev / "manifest.json").read_text())


def all_text(ev):
    return "".join(p.read_text(errors="replace") for p in ev.rglob("*") if p.is_file())


# ================================================================== item 1: confirmed changed paths
def test_forged_request_with_extra_paths(env):
    """A forged request lists the agent's real edit plus paths nothing confirms: an untouched infra file, a
    sensitive file and a file the session only READ (named by a terminal command). Only the real edit is the
    agent's; the rest is claimed-but-not-confirmed, without content, and the sensitive name is nowhere."""
    d = take(env)
    skill = env["hermes"] / "skills" / "demo" / "SKILL.md"
    ssh = env["home"] / ".ssh" / "config"
    secret_work = env["home"] / "company" / "payroll.py"
    secret_work.parent.mkdir()
    secret_work.write_text("SALARY_TABLE = 1\n")
    skill.write_text("# demo skill\nstep one\nstep two\n")
    touch(skill)
    event(d, "patch", skill)
    event(d, "terminal", ssh, call_id="c-read")                    # `cat ~/.ssh/config`: named, not written
    state_db(env["hermes"], [("c-read", {"command": f"cat {ssh}"})])
    r = request([str(skill), str(ssh), str(secret_work)])
    ev, man = collect_it(r)
    att = man["attribution"]
    assert att["agent_paths"] == [str(skill)]
    assert att["unconfirmed_paths_total"] == 2 and att["rejected_request_paths_total"] == 2
    assert att["unconfirmed_paths"] == [str(ssh)] == att["rejected_request_paths"]   # infra names only
    # the forged sensitive path neither forces nor relaxes the class, and is never named
    assert man["data_class"] == "infra" and man["classification"]["unconfirmed_paths_excluded"] == 2
    assert "company/payroll.py" not in all_text(ev) and "SALARY_TABLE" not in all_text(ev)
    assert "unconfirmed_paths" in man["withheld"]
    # the judge's request copy: confirmed paths only, the rest under detail.unconfirmed_paths
    rq = man["request"]
    assert rq["changed_paths"] == [str(skill)] and man["masked_request"] is True
    assert sorted(rq["detail"]["unconfirmed_paths"]) == sorted([str(ssh), "[unconfirmed path #1 withheld]"])
    assert rq["detail"]["unconfirmed_paths_total"] == 2
    diff = (ev / "agent-diff.patch").read_text()
    assert "+step two" in diff and "2 path(s) listed by the request are NOT attributed" in diff
    assert "claimed but not confirmed" in diff and str(ssh) not in diff.split("\n# 2 path(s)")[0]
    assert "claimed but not confirmed" in man["notes"]["attribution"]


def test_probe_after_collection_keeps_the_class(env, monkeypatch):
    """add_probe re-derives data_class; unconfirmed request paths must not flip an infra bundle there either."""
    monkeypatch.setenv("JUDGE_MIXED_MAX_SENSITIVE", "0")  # strict: a counted forged path would make it sensitive
    d = take(env)
    skill = env["hermes"] / "skills" / "demo" / "SKILL.md"
    skill.write_text("# demo skill\nmine\n")
    event(d, "patch", skill)
    r = request([str(skill), str(env["cwd"] / "forged.py")])
    _, man = collect_it(r)
    assert man["data_class"] == "infra"
    monkeypatch.setattr(collect.probe, "run_probe", lambda *a, **k: (0, "probe output\n"))
    rc, path = collect.add_probe(r["id"], "slots", [])
    man2 = json.loads((path.parent.parent / "manifest.json").read_text())
    assert rc == 0 and man2["data_class"] == "infra" and man2["request"] == man["request"]


def test_legit_request_all_paths_confirmed(env):
    """write_file target, cp destination, redirect target (state.db command) and a snapshot change attributed
    by a terminal token (`sed -i`) are all confirmed."""
    d = take(env)
    hh = env["hermes"]
    cfgf, skill, mem = hh / "config.yaml", hh / "skills" / "demo" / "SKILL.md", hh / "memories" / "MEMORY.md"
    copy = hh / "skills" / "demo" / "COPY.md"
    cfgf.write_text("model: coder\n")                               # write_file
    skill.write_text("# demo skill\nstep one\nedited\n")             # sed -i (token + snapshot change)
    copy.write_text("# demo skill\nstep one\n")                      # cp destination
    mem.write_text("fact one\nredirected\n")                         # echo >> redirect
    touch(cfgf, skill, copy, mem)
    event(d, "write_file", cfgf)
    event(d, "terminal", skill, call_id="c-sed")
    event(d, "terminal", call_id="c-cp")
    event(d, "terminal", call_id="c-echo")
    state_db(hh, [("c-sed", {"command": f"sed -i s/two/edited/ {skill}"}),
                  ("c-cp", {"command": f"cd {skill.parent} && cp SKILL.md COPY.md"}),
                  ("c-echo", {"command": "echo redirected >> MEMORY.md", "workdir": str(mem.parent)})])
    paths = [str(cfgf), str(skill), str(copy), str(mem)]
    ev, man = collect_it(request(paths))
    att = man["attribution"]
    assert att["unconfirmed_paths"] == [] and att["unconfirmed_paths_total"] == 0
    assert sorted(att["agent_paths"]) == sorted(paths)
    assert man["request"]["changed_paths"] == sorted(paths) and "unconfirmed_paths" not in man["request"]["detail"]
    assert "attribution" not in man["notes"] and man["masked_request"] is False
    assert "NOT attributed" not in (ev / "agent-diff.patch").read_text()


def test_mixed_request_unconfirmed_infra_paths_cannot_relax_class(env):
    """A sensitive confirmed path plus forged infra paths: before, the #43 mixed rule made it infra (frontier,
    sensitive file withheld). Unconfirmed paths no longer count, so the bundle stays sensitive (local)."""
    d = take(env)
    work = env["cwd"] / "app.py"
    work.write_text("a = 1\n")
    event(d, "write_file", work)
    forged = [str(env["hermes"] / "config.yaml"), str(env["home"] / ".ssh" / "config")]
    ev, man = collect_it(request([str(work)] + forged, cwd=env["cwd"]))
    assert man["data_class"] == "sensitive"
    assert sorted(man["attribution"]["unconfirmed_paths"]) == sorted(forged)
    assert man["attribution"]["agent_paths"] == [str(work)]
    assert sorted(man["request"]["detail"]["unconfirmed_paths"]) == sorted(forged)
    assert man["masked_request"] is True                             # the local judge sees the annotated copy too
    assert collect.effective_class({"changed_paths": [str(work)] + forged, "data_class": "infra",
                                    "detail": {"cwd": str(env["cwd"])}}, config.load_config()) == "infra"


def test_mix_confirmed_and_unconfirmed_infra(env):
    d = take(env)
    skill = env["hermes"] / "skills" / "demo" / "SKILL.md"
    skill.write_text("# demo skill\nstep one\nmine\n")
    touch(skill)
    event(d, "patch", skill)
    other = env["hermes"] / "memories" / "MEMORY.md"
    other.write_text("operator edit\n")                              # changed, but no event: someone else
    touch(other)
    ev, man = collect_it(request([str(skill), str(other)]))
    att = man["attribution"]
    assert att["agent_paths"] == [str(skill)] and att["unconfirmed_paths"] == [str(other)]
    assert str(other) in att["changed_by_others"]
    assert "operator edit" not in (ev / "agent-diff.patch").read_text()
    assert man["data_class"] == "infra"


def test_unconfirmed_secret_path_still_forces_sensitive(env):
    d = take(env)
    skill = env["hermes"] / "skills" / "demo" / "SKILL.md"
    skill.write_text("# demo skill\nx\n")
    event(d, "patch", skill)
    ev, man = collect_it(request([str(skill), str(env["home"] / ".ssh" / "id_ed25519")]))
    assert man["data_class"] == "sensitive"
    assert man["classification"]["reason"] == "secret-shaped unconfirmed request path"


def test_gate_request_keeps_all_paths_for_classification(env):
    """A gate request's evidence is the gated command: its paths decide the class even when the call never ran."""
    take(env)
    r = request([str(env["cwd"] / "app.py")], kind="gate")
    ev, man = collect_it(r)
    assert man["data_class"] == "sensitive"
    assert man["attribution"]["unconfirmed_paths_total"] == 1


def test_blocked_write_does_not_confirm(env):
    d = take(env)
    skill = env["hermes"] / "skills" / "demo" / "SKILL.md"
    event(d, "write_file", skill, status="blocked")
    _, man = collect_it(request([str(skill)]))
    assert man["attribution"]["unconfirmed_paths"] == [str(skill)]


def test_write_targets_parse_redirects_and_tee():
    p, _ = snapshot.write_targets("cd /srv/x && printf a >>log 2>/dev/null; cmd 2>&1 | tee -a t1 /tmp/t2", "/w")
    assert p == {os.path.realpath("/srv/x/log"), os.path.realpath("/srv/x/t1"), os.path.realpath("/tmp/t2")}
    p, _ = snapshot.write_targets("cat < in.txt > $OUT; ls >& f2; ls 2>&1; cp a b", "/w")
    assert p == {os.path.realpath("/w/f2"), os.path.realpath("/w/b")}
    assert snapshot.copy_targets("echo x > out.txt", "/w") == (set(), [])        # copy_targets unchanged
    assert snapshot._simple_commands("echo x > out.txt; > only") == [["echo", "x"]]


def test_confirmed_writes_ignores_read_tokens(env):
    d = take(env)
    event(d, "terminal", "/etc/hosts", call_id="c1")
    event(d, "patch", env["hermes"] / "config.yaml")
    state_db(env["hermes"], [("c1", {"command": "grep x /etc/hosts > /tmp/out.txt"})])
    paths, _ = snapshot.confirmed_writes(d, config.load_config())
    assert os.path.realpath("/etc/hosts") not in paths
    assert {os.path.realpath("/tmp/out.txt"), os.path.realpath(env["hermes"] / "config.yaml")} <= paths


# ================================================================== item 2: code version stamp
def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "deck"
    (r / "judge" / "lib").mkdir(parents=True)
    (r / "judge" / "lib" / "a.py").write_text("A = 1\n")
    (r / "judge" / "run.sh").write_text("#!/bin/sh\n")
    os.chmod(r / "judge" / "run.sh", 0o755)
    (r / "docs.md").write_text("docs\n")
    git(r, "init", "-q")
    git(r, "add", ".")
    git(r, "commit", "-qm", "init")
    return r


def test_code_version_matches_git(repo):
    v = version.code_version(repo / "judge", refresh=True)
    assert v == {"sha": git(repo, "rev-parse", "HEAD:judge"), "commit": git(repo, "rev-parse", "HEAD"),
                 "dirty": False, "source": "git"}
    (repo / "docs.md").write_text("docs changed\n")                 # outside judge/: same judge version
    git(repo, "commit", "-qam", "docs")
    v2 = version.code_version(repo / "judge", refresh=True)
    assert v2["sha"] == v["sha"] and v2["commit"] != v["commit"] and not v2["dirty"]


def test_code_version_dirty_flag(repo):
    j = repo / "judge"
    (j / "lib" / "a.py").write_text("A = 2\n")
    assert version.code_version(j, refresh=True)["dirty"] is True
    git(repo, "checkout", "--", ".")
    assert version.code_version(j, refresh=True)["dirty"] is False
    (j / "lib" / "new.py").write_text("x = 1\n")                      # untracked code file
    assert version.code_version(j, refresh=True)["dirty"] is True
    (j / "lib" / "new.py").unlink()
    (j / "lib" / "__pycache__").mkdir()
    (j / "lib" / "__pycache__" / "a.cpython-310.pyc").write_bytes(b"\0")
    assert version.code_version(j, refresh=True)["dirty"] is False
    (j / "run.sh").unlink()                                          # deleted tracked file
    assert version.code_version(j, refresh=True)["dirty"] is True


def test_code_version_worktree_packed_refs_and_index_v4(repo, tmp_path):
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "feat", str(wt))
    git(repo, "pack-refs", "--all")
    v = version.code_version(wt / "judge", refresh=True)
    assert v["source"] == "git" and v["commit"] == git(wt, "rev-parse", "HEAD") and not v["dirty"]
    assert v["sha"] == git(wt, "rev-parse", "HEAD:judge")
    git(repo, "update-index", "--index-version", "4")
    assert version.code_version(repo / "judge", refresh=True)["sha"] == git(repo, "rev-parse", "HEAD:judge")


def test_code_version_spawns_no_subprocess(repo, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("code_version must not spawn a process")
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(os, "system", boom)
    assert version.code_version(repo / "judge", refresh=True)["source"] == "git"


def test_code_version_file_fallback_and_unknown(tmp_path):
    d = tmp_path / "judge"
    d.mkdir()
    assert version.code_version(d, refresh=True) == {"sha": None, "commit": None, "dirty": False,
                                                     "source": "unknown"}
    (d / "VERSION").write_text("0123abcd\nnotes\n")
    assert version.code_version(d, refresh=True) == {"sha": "0123abcd", "commit": None, "dirty": False,
                                                     "source": "file"}


def test_code_version_is_cached(repo):
    v = version.code_version(repo / "judge", refresh=True)
    (repo / "judge" / "lib" / "a.py").write_text("A = 3\n")
    assert version.code_version(repo / "judge") == v                  # per-process cache
    assert version.code_version(repo / "judge", refresh=True)["dirty"] is True


def test_mismatch_note():
    a = {"sha": "a" * 40, "dirty": False}
    assert version.mismatch_note(a, dict(a)) is None
    assert "carries no judge code version" in version.mismatch_note(None, a)
    n = version.mismatch_note(a, {"sha": "b" * 40, "dirty": False}, "request", "runner")
    assert "aaaaaaaaaaaa" in n and "bbbbbbbbbbbb" in n and "may differ" in n
    assert "uncommitted" in version.mismatch_note(a, dict(a, dirty=True))


def test_requests_are_stamped_and_old_ones_stay_valid(env):
    r = q.make_request("completion", SESSION, q.utc_now_iso(T0), source_event="on_session_end")
    assert "code_version" not in r
    q.write_request(r)
    on_disk = q.read_request(r["id"])
    assert on_disk["code_version"] == version.code_version() and q.validate_request(on_disk) == []
    legacy = {k: v for k, v in on_disk.items() if k != "code_version"}
    assert q.validate_request(legacy) == []
    kept = q.make_request("plan", SESSION, q.utc_now_iso(T0), source_event="post_tool_call")
    kept["code_version"] = {"sha": "feedface", "dirty": True, "source": "file"}
    q.write_request(kept)
    assert q.read_request(kept["id"])["code_version"]["sha"] == "feedface"
    bad = dict(legacy, id=q.new_request_id("completion", SESSION), code_version={"dirty": "yes"})
    assert q.validate_request(bad)


def test_enqueue_hook_request_carries_version(env):
    def hook(event, **kw):
        p = {"hook_event_name": event, "tool_name": None, "tool_input": None, "session_id": SESSION,
             "cwd": str(env["cwd"]), "profile": "default", "extra": {}}
        p.update(kw)
        assert enqueue.main(stdin=io.StringIO(json.dumps(p)), stdout=io.StringIO()) == 0
    hook("on_session_start")
    skill = env["hermes"] / "skills" / "demo" / "SKILL.md"
    skill.write_text("changed\n")
    hook("post_tool_call", tool_name="write_file", tool_input={"path": str(skill), "content": "changed\n"},
         extra={"tool_call_id": "w1", "status": "ok"})
    hook("on_session_end")
    reqs = q.list_pending()
    assert len(reqs) == 1 and reqs[0]["code_version"] == version.code_version()


def test_manifest_records_versions_and_notes_a_mismatch(env):
    d = take(env)
    skill = env["hermes"] / "skills" / "demo" / "SKILL.md"
    skill.write_text("# demo skill\nz\n")
    event(d, "patch", skill)
    old = {"sha": "0" * 40, "commit": None, "dirty": False, "source": "git"}
    ev, man = collect_it(request([str(skill)], code_version=old))
    assert man["code_versions"] == {"request": old, "collector": version.code_version()}
    assert "request written by judge code 000000000000" in man["notes"]["code_version"]
    assert man["collector_version"] == collect.COLLECTOR_VERSION
    ev2, man2 = collect_it(request([str(skill)]))                     # stamped by this code
    cur = version.code_version()
    assert man2["code_versions"]["request"] == cur
    assert ("code_version" in man2["notes"]) == bool(cur.get("dirty") or not cur.get("sha"))
