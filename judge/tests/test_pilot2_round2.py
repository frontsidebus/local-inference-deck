"""Pilot 2, round 2 bugs: #43 (per-path classification: scratch paths never decide the class, a few sensitive paths
are withheld from an infra bundle, nothing sensitive or secret reaches a frontier input), #44 (water-filled bundle
budget), #45 (cp/mv/install destinations are the agent's)."""
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from collector_testlib import JUDGE_DIR, FakeRunner, install_logs, make_env

import collect
from lib import config, snapshot
from lib import queue as q

sys.path.insert(0, str(JUDGE_DIR / "runner"))
import run_judge as RJ  # noqa: E402

SESSION = "20261003_031000_a1b2c3"
NOW = datetime(2026, 10, 3, 3, 30, 0, tzinfo=timezone.utc)
EV_T = datetime(2026, 10, 3, 3, 25, 0, tzinfo=timezone.utc)
IN_WINDOW = EV_T.timestamp()
MARK = "PRIVATE-MARKER-7f3a9c"          # content of a sensitive file: must never reach an infra bundle
SCRATCH_MARK = "SCRATCH-MARKER-21b8e0"  # content of an agent scratch file


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = make_env(tmp_path, monkeypatch)
    install_logs(e["hermes"])
    return e


def _runner():
    return FakeRunner(default=(255, "", "offline"))


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t",
                    "-c", "init.defaultBranch=main", *args], check=True, capture_output=True)


def _init_repo(repo, files):
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")


def _wrote(*paths, tool="patch", call_id=None):
    snapshot.record_event(q.snapshot_dir(SESSION), tool, [str(p) for p in paths], "ok", now=EV_T, call_id=call_id)
    for p in paths:
        if os.path.exists(p):
            os.utime(p, (IN_WINDOW, IN_WINDOW))


def _request(paths, data_class=None, kind="completion", cwd=None):
    paths = [str(p) for p in paths]
    data_class = data_class or config.classify(paths, None, cwd)
    r = q.make_request(kind, SESSION, "2026-10-03T03:20:00Z", source_event="on_session_end",
                       changed_paths=[str(p) for p in paths], claims="Done.", data_class=data_class,
                       detail={"cwd": cwd} if cwd else None, created="2026-10-03T03:30:00Z")
    q.write_request(r)
    return r


def _all_text(ev: Path) -> str:
    return "\n".join(f.read_text(errors="replace") for f in sorted(ev.rglob("*")) if f.is_file())


def _frontier_input(ev: Path, req) -> str:
    bundle = RJ.bundle_text(ev, 150000)
    return RJ.build_user_message(RJ.bundle_request(req, ev), bundle, False)


# ---------------------------------------------------------------- #43 classification
def test_scratch_paths_do_not_decide_the_class(env, monkeypatch):
    hh, repo = env["hermes"], env["repo"]
    scratch = str(hh / "cache" / "scratch" / "b2_dryrun.py")
    infra = str(repo / "walter" / "digest" / "pipeline.py")
    assert config.path_class(scratch) == "scratch" and config.is_scratch_path(str(hh / "tmp" / "x"))
    # pilot-2 B2: one scratch path used to make the whole request sensitive
    assert config.classify([scratch, infra]) == "infra"
    det = config.classify_detail([scratch, infra])
    assert det["scratch"] == [scratch] and det["infra"] == [infra] and det["reason"] == "all paths infra"
    # only scratch: the cwd decides, as with no paths
    assert config.classify([scratch]) == "sensitive"
    assert config.classify([scratch], cwd=str(repo)) == "infra"
    # tool caches
    assert config.path_class(str(env["home"] / ".cache" / "pip" / "x.whl")) == "scratch"
    assert config.path_class(str(repo / "pkg" / "__pycache__" / "m.cpython-312.pyc")) == "scratch"
    # a secret-shaped file is secret wherever it lives, scratch included
    assert config.path_class(str(hh / "cache" / "scratch" / "site.env")) == "secret"
    assert config.classify([str(hh / "cache" / "x" / "gw.key"), infra]) == "sensitive"
    # configurable
    assert config.path_class("/var/tmp/agent-work/a.py") == "sensitive"
    monkeypatch.setenv("JUDGE_SCRATCH_GLOBS", "/var/tmp/agent-work/*")
    assert config.path_class("/var/tmp/agent-work/a.py") == "scratch"
    assert config.classify(["/var/tmp/agent-work/a.py", infra]) == "infra"


def test_scratch_symlink_to_a_private_file_is_not_scratch(env, tmp_path):
    private = tmp_path / "private.txt"
    private.write_text("x")
    link = env["hermes"] / "cache" / "link.txt"
    link.parent.mkdir(parents=True)
    link.symlink_to(private)
    assert config.path_class(str(link)) == "sensitive"


def test_mixed_policy_limits(env, monkeypatch):
    repo = env["repo"]
    infra = [str(repo / f"f{i}.sh") for i in range(4)]
    sens = [f"/home/x/p{i}.txt" for i in range(4)]
    assert config.classify(infra[:1] + sens[:1]) == "infra"
    assert config.classify(infra[:1] + sens[:2]) == "sensitive"       # more sensitive than infra paths
    assert config.classify(infra[:4] + sens[:3]) == "infra"
    assert config.classify(infra[:4] + sens[:4]) == "sensitive"       # above JUDGE_MIXED_MAX_SENSITIVE (3)
    monkeypatch.setenv("JUDGE_MIXED_MAX_SENSITIVE", "0")
    assert config.classify(infra[:1] + sens[:1]) == "sensitive"
    assert config.classify(infra[:1] + sens[:1], max_mixed=1) == "infra"
    # a secret path always wins
    monkeypatch.delenv("JUDGE_MIXED_MAX_SENSITIVE")
    assert config.classify(infra + [str(repo / "site.env")]) == "sensitive"


def test_gate_requests_are_classified_strictly(env):
    req = {"kind": "gate", "changed_paths": [str(env["repo"] / "a.sh"), "/home/x/private.txt"],
           "data_class": "infra", "detail": {"rules": ["remote-copy"]}}
    assert collect.effective_class(req, config.load_config()) == "sensitive"
    req["kind"] = "completion"
    assert collect.effective_class(req, config.load_config()) == "infra"


# ---------------------------------------------------------------- #43 bundles: nothing sensitive reaches the frontier
def _mixed_session(env, tmp_path):
    """cwd = an unrelated (sensitive) git repo; the agent changes one infra file in the deck repo, one file in
    the private repo, and writes a scratch file under $HERMES_HOME/cache."""
    deck, private = env["repo"], tmp_path / "private-repo"
    _init_repo(deck, {"walter/deploy.sh": "echo one\n", "walter/README.md": "# r\n"})
    _init_repo(private, {"notes.txt": "public line\n"})
    snapshot.take(SESSION, str(private), config.load_config())
    (deck / "walter" / "deploy.sh").write_text("echo one\necho two-infra-change\n")
    (deck / "walter" / "README.md").write_text("# r\nmore docs\n")
    (private / "notes.txt").write_text(f"public line\n{MARK}\n")
    (private / "brand-new.txt").write_text(f"{MARK} again\n")
    scratch = env["hermes"] / "cache" / "scratch" / "dry.py"
    scratch.parent.mkdir(parents=True)
    scratch.write_text(f"print('{SCRATCH_MARK}')\n")
    paths = [deck / "walter" / "deploy.sh", deck / "walter" / "README.md", private / "notes.txt",
             private / "brand-new.txt", scratch]
    _wrote(*paths)
    return deck, private, scratch, paths


def test_mixed_bundle_withholds_sensitive_content_and_names(env, tmp_path):
    deck, private, scratch, paths = _mixed_session(env, tmp_path)
    # gate log line of this session naming the private file (a gate excerpt can quote a file)
    with open(env["review"] / "gate.log", "a") as fh:
        fh.write(json.dumps({"ts": "2026-10-03T03:24:00Z", "session": SESSION, "tool": "terminal",
                             "decision": "approve", "rule": "sensitive-path",
                             "excerpt": f"cat >> {private / 'notes.txt'} <<EOF {MARK} EOF"}) + "\n")
    r = _request(paths, cwd=str(private))
    assert r["data_class"] == "infra"  # 2 infra + 2 sensitive (withheld) + 1 scratch
    ev = collect.collect(r["id"], runner=_runner(), now=NOW)
    man = json.loads((ev / "manifest.json").read_text())
    assert man["data_class"] == "infra" and man["classification"]["reason"] == "mixed"
    diff = (ev / "agent-diff.patch").read_text()
    assert "+echo two-infra-change" in diff                          # the infra file is reviewable
    assert "sensitive path in an infra bundle; name not shown): [sensitive path #" in diff
    everything = _all_text(ev)
    assert MARK not in everything and SCRATCH_MARK not in everything
    assert str(private) not in everything and "notes.txt" not in everything and "brand-new.txt" not in everything
    assert man["withheld"]["sensitive_paths"].startswith("2 non-infra path(s)")
    assert man["attribution"]["scratch_paths"] == [str(scratch)]
    gl = (ev / "gate-decisions.jsonl").read_text().splitlines()
    assert gl and all(json.loads(x) == {"withheld": collect.LINE_WITHHELD} for x in gl)
    fin = _frontier_input(ev, r)
    assert MARK not in fin and SCRATCH_MARK not in fin and str(private) not in fin
    assert "[sensitive path #1 withheld]" in fin and "+echo two-infra-change" in fin


def test_secret_path_keeps_the_whole_bundle_sensitive(env, tmp_path):
    deck = env["repo"]
    _init_repo(deck, {"walter/deploy.sh": "echo one\n", ".gitignore": "site.env\n"})
    snapshot.take(SESSION, str(deck), config.load_config())
    (deck / "walter" / "deploy.sh").write_text("echo changed\n")
    (deck / "site.env").write_text(f"SPARK_DOMAIN={MARK}\n")
    _wrote(deck / "walter" / "deploy.sh", deck / "site.env")
    r = _request([deck / "walter" / "deploy.sh", deck / "site.env"], data_class="infra", cwd=str(deck))
    ev = collect.collect(r["id"], runner=_runner(), now=NOW)
    man = json.loads((ev / "manifest.json").read_text())
    assert man["data_class"] == "sensitive" and man["classification"]["reason"] == "secret path"
    assert MARK not in _all_text(ev) and "echo changed" not in (ev / "agent-diff.patch").read_text()


def test_scratch_only_change_next_to_infra_b2_shape(env, tmp_path):
    """Pilot-2 B2: pipeline change in the deck repo + a dry-run helper under ~/.hermes/cache: infra bundle with
    the pipeline diff, the helper as metadata only."""
    deck = env["repo"]
    _init_repo(deck, {"walter/digest/pipeline.py": "def run():\n    return 1\n"})
    snapshot.take(SESSION, str(deck), config.load_config())
    (deck / "walter" / "digest" / "pipeline.py").write_text("def run():\n    return 2\n")
    scratch = env["hermes"] / "cache" / "scratch" / "b2_dryrun.py"
    scratch.parent.mkdir(parents=True)
    scratch.write_text(f"# {SCRATCH_MARK}\n")
    _wrote(deck / "walter" / "digest" / "pipeline.py", tool="patch")
    _wrote(scratch, tool="write_file")
    r = _request([scratch, deck / "walter" / "digest" / "pipeline.py"], cwd=str(deck))
    assert r["data_class"] == "infra"
    ev = collect.collect(r["id"], runner=_runner(), now=NOW)
    man = json.loads((ev / "manifest.json").read_text())
    assert man["data_class"] == "infra" and "+    return 2" in (ev / "agent-diff.patch").read_text()
    assert SCRATCH_MARK not in _all_text(ev) and man["withheld"]["scratch_paths"].startswith("1 agent scratch")
    assert man["attribution"]["scratch_paths"] == [str(scratch)]


def test_diff_text_rechecks_every_shown_path(env, tmp_path):
    """Defence in depth: a sensitive path the request did not list (so not pre-labelled) is still withheld, and
    a repo whose paths are all withheld gets no header naming it."""
    private = tmp_path / "priv"
    _init_repo(private, {"a.txt": "x\n"})
    snapshot.take(SESSION, str(private), config.load_config())
    (private / "a.txt").write_text(f"x\n{MARK}\n")
    wh = collect.Withholding(config.load_config(), None)
    text = snapshot.diff_text(q.snapshot_dir(SESSION), config.load_config(), sensitive=False,
                              include=[str(private / "a.txt")], withhold=wh.hold)
    assert MARK not in text and str(private) not in text
    assert "[sensitive path #1 withheld] — 1 lines changed (+1/-0) [modified]" in text


def test_literal_pathspec_does_not_widen_the_diff(env, tmp_path):
    deck = env["repo"]
    _init_repo(deck, {"a.sh": "1\n", "b.sh": "1\n"})
    snapshot.take(SESSION, str(deck), config.load_config())
    (deck / "a.sh").write_text("2\n")
    (deck / "b.sh").write_text(f"{MARK}\n")
    text = snapshot.diff_text(q.snapshot_dir(SESSION), config.load_config(), sensitive=False,
                              include=[str(deck / "a.sh"), str(deck / "*.sh")])
    assert "+2" in text and MARK not in text


# ---------------------------------------------------------------- #44 water-filled budget
def _bundle(tmp_path, files):
    ev = tmp_path / "ev"
    ev.mkdir()
    for name, text in files.items():
        (ev / name).write_text(text)
    return ev


def _diff(n_files, lines_per_file, width=60, prefix="code"):
    out = []
    for i in range(n_files):
        name = f"/repo/app/{prefix}{i}.py"
        out += [f"--- a{name}", f"+++ b{name}", f"@@ -0,0 +1,{lines_per_file} @@"]
        out += [f"+{prefix}{i} line {j} ".ljust(width, "x") for j in range(lines_per_file)]
    return "\n".join(out) + "\n"


def test_water_fill():
    assert RJ.water_fill({"a": 10, "b": 100, "c": 1000}, 600) == {"a": 10, "b": 100, "c": 490}
    assert RJ.water_fill({"a": 10, "b": 10}, 1000) == {"a": 10, "b": 10}
    assert sum(RJ.water_fill({"a": 500, "b": 500, "c": 500}, 900).values()) <= 900
    assert RJ.water_fill({"a": 5}, -3) == {"a": 0}


def test_large_diff_takes_the_unused_budget(tmp_path):
    """Pilot-2 B1: a 70K diff in a ~50K bundle was cut to its per-file share (~13K)."""
    diff = _diff(10, 110)  # ~70K
    assert 65000 < len(diff) < 80000
    files = {"manifest.json": json.dumps({"request": {"session": "s"}}), "agent-diff.patch": diff,
             "hermes-log.txt": "x" * 18000, "tool-calls.jsonl": "y" * 8700, "others-changed.txt": "z" * 1300,
             "host-walter.txt": "h" * 700, "host-covenant.txt": "h" * 700, "slots.json": "{}",
             "refusals.jsonl": "", "c3-results.jsonl": "", "gate-decisions.jsonl": ""}
    text = RJ.bundle_text(_bundle(tmp_path, files), 150000)
    assert "truncated by runner" not in text and "runner omitted" not in text
    assert "code9 line 109" in text and len(text) <= 150000


def test_diff_cut_keeps_whole_hunks_and_every_file(tmp_path):
    diff = _diff(6, 300)  # ~110K
    files = {"manifest.json": "{}", "agent-diff.patch": diff, "tool-calls.jsonl": "y" * 9000}
    text = RJ.bundle_text(_bundle(tmp_path, files), 60000)
    assert len(text) <= 60000
    assert "y" * 9000 in text                                  # the small file is whole
    for i in range(6):                                         # every file keeps its header
        assert f"+++ b/repo/app/code{i}.py" in text
    assert text.count("hunk(s) of this file") == 6
    assert "truncated by runner" not in text.split("=== FILE: agent-diff.patch ===")[1].split("=== FILE:")[0]


def test_diff_cut_prefers_code_over_static_assets(tmp_path):
    code = _diff(1, 200, prefix="main")
    asset = "\n".join(["--- a/repo/static/app.css", "+++ b/repo/static/app.css", "@@ -0,0 +1,900 @@"]
                      + [f"+.c{j} {{ color: red; }}".ljust(60, " ") for j in range(900)]) + "\n"
    files = {"manifest.json": "{}", "agent-diff.patch": asset + code}
    text = RJ.bundle_text(_bundle(tmp_path, files), 30000)
    assert "main0 line 199" in text                            # all of the code file
    assert "app.css" in text and "hunk(s) of this file" in text


def test_priority_reservations_are_kept(tmp_path):
    session = "20261003_031000_a1b2c3"
    log = "\n".join(f"2026-10-03 03:21:{i % 60:02d},000 INFO [{session}] agent.x: line {i} " + "s" * 80
                    for i in range(600))
    gates = "\n".join(json.dumps({"n": i, "pad": "g" * 100}) for i in range(100))
    files = {"manifest.json": json.dumps({"request": {"session": session}}), "hermes-log.txt": log,
             "gate-decisions.jsonl": gates, "agent-diff.patch": _diff(4, 400)}
    text = RJ.bundle_text(_bundle(tmp_path, files), 100000)
    assert '"n": 99' in text and '"n": 0' in text            # gate decisions whole (under GATE_SHARE)
    assert "line 599 " in text and "line 0 " in text            # session lines whole (under SESSION_LOG_SHARE)
    assert len(text) <= 100000 + 200


# ---------------------------------------------------------------- #45 cp/mv/install destinations
@pytest.mark.parametrize("command,expect,prefixes", [
    ("cp a.txt b.txt", {"/w/b.txt"}, []),
    ("cp a.txt /d/", {"/d/a.txt"}, []),
    ("cp -t /d a.txt b.txt", {"/d/a.txt", "/d/b.txt"}, []),
    ("cp --target-directory=/d a.txt", {"/d/a.txt"}, []),
    ("cd /x && cp ../a.txt sub/c.txt", {"/x/sub/c.txt"}, []),
    ("sudo install -m 0644 -o root a.conf /etc/x/ 2>/dev/null", {"/etc/x/a.conf"}, []),
    ("install -D -m 0755 bin/tool /srv/t/bin/tool", {"/srv/t/bin/tool"}, []),
    ("install -d /srv/a /srv/b", set(), []),
    ("cp $SRC /d/x", set(), []),
    ("echo cp a b", set(), []),
    ("mv old.txt new.txt", {"/w/new.txt"}, ["/w/new.txt/old.txt/", "/w/new.txt/"]),
])
def test_copy_targets(command, expect, prefixes):
    paths, pre = snapshot.copy_targets(command, "/w")
    assert {p for p in paths} == expect and pre == prefixes


def test_copy_targets_recursive_maps_source_files(tmp_path):
    src = tmp_path / "telemetry" / "fonts"
    src.mkdir(parents=True)
    (src / "a.woff2").write_bytes(b"\0")
    (src / "LICENSE.txt").write_text("l")
    paths, _ = snapshot.copy_targets(f"cd {tmp_path} && cp -r telemetry/fonts digest/fonts &", "/")
    dst = os.path.realpath(tmp_path) + "/digest/fonts"
    assert {f"{dst}/a.woff2", f"{dst}/LICENSE.txt"} <= paths


def _state_db(hermes, calls):
    con = sqlite3.connect(hermes / "state.db")
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, "
                "tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL)")
    con.execute("INSERT INTO messages (session_id, role, tool_calls, timestamp) VALUES (?, 'assistant', ?, 0)",
                (SESSION, json.dumps([{"id": cid, "type": "function",
                                       "function": {"name": "terminal", "arguments": json.dumps(args)}}
                                      for cid, args in calls])))
    con.commit()
    con.close()


def test_cp_created_files_are_attributed_to_the_agent(env, tmp_path):
    """Pilot-2 B1: font files copied with `cp -r` landed in changed_by_others."""
    deck = env["repo"]
    _init_repo(deck, {"walter/telemetry/fonts/a.woff2": "font-a\n", "walter/telemetry/fonts/LICENSE.txt": "lic\n"})
    snapshot.take(SESSION, str(deck), config.load_config())
    cmd = (f"cd {deck} && cp -r walter/telemetry/fonts walter/digest/fonts && "
           "cp walter/telemetry/fonts/LICENSE.txt walter/digest/LICENSE-copy.txt")
    _state_db(env["hermes"], [("call-cp", {"command": cmd, "workdir": str(deck)})])
    (deck / "walter" / "digest").mkdir()
    subprocess.run(["cp", "-r", str(deck / "walter/telemetry/fonts"), str(deck / "walter/digest/fonts")], check=True)
    subprocess.run(["cp", str(deck / "walter/telemetry/fonts/LICENSE.txt"), str(deck / "walter/digest/LICENSE-copy.txt")],
                   check=True)
    (deck / "walter" / "unrelated.txt").write_text("someone else\n")
    for p in (deck / "walter").rglob("*"):
        os.utime(p, (IN_WINDOW, IN_WINDOW))
    # the hook records only path-like tokens: here the destination DIR, never its files
    _wrote(deck, deck / "walter/telemetry/fonts", deck / "walter/digest/fonts", tool="terminal", call_id="call-cp")
    r = _request([], data_class="infra", cwd=str(deck))
    ev = collect.collect(r["id"], runner=_runner(), now=NOW)
    man = json.loads((ev / "manifest.json").read_text())
    agent = set(man["attribution"]["agent_paths"])
    assert {str(deck / "walter/digest/fonts/a.woff2"), str(deck / "walter/digest/fonts/LICENSE.txt"),
            str(deck / "walter/digest/LICENSE-copy.txt")} <= agent
    assert str(deck / "walter/unrelated.txt") in man["attribution"]["changed_by_others"]
    # the command text is used for attribution only: it never reaches a bundle
    assert "cp -r" not in _all_text(ev) and "LICENSE-copy.txt" in (ev / "agent-diff.patch").read_text()


def test_agent_touched_without_state_db_is_unchanged(env, tmp_path):
    deck = env["repo"]
    _init_repo(deck, {"a.txt": "1\n"})
    snapshot.take(SESSION, str(deck), config.load_config())
    _wrote(deck / "a.txt", tool="terminal", call_id="nope")
    paths, pre = snapshot.agent_touched(q.snapshot_dir(SESSION), config.load_config())
    assert paths == {os.path.realpath(deck / "a.txt")} and pre == []


def test_copy_targets_unreadable_dest_never_raises(env, tmp_path, monkeypatch):
    deck = env["repo"]
    _init_repo(deck, {"a.txt": "1\n"})
    snapshot.take(SESSION, str(deck), config.load_config())
    _wrote(deck, tool="terminal", call_id="c1")

    def boom(*a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(snapshot, "copy_targets", boom)
    paths, _ = snapshot.agent_touched(q.snapshot_dir(SESSION), config.load_config(),
                                      commands={"c1": ("cp x y", "/")})
    assert paths == {os.path.realpath(deck)}
