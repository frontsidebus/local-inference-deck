"""Data files for the R8 code review (collector/datafiles.py, data-files.txt): which files the changed code reads
are included, which are withheld and why, the cap, redaction, excerpts, and that sensitive and claims-only
bundles never carry data file content."""
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from collector_testlib import JUDGE_DIR, FakeRunner, install_logs, make_env

import claims_only as CO
import collect
import datafiles as DF
from lib import config, snapshot
from lib import queue as q

sys.path.insert(0, str(JUDGE_DIR / "runner"))
import run_judge as RJ  # noqa: E402

SESSION = "20261003_031000_d4e5f6"
NOW = datetime(2026, 10, 3, 3, 30, 0, tzinfo=timezone.utc)
EV_T = datetime(2026, 10, 3, 3, 25, 0, tzinfo=timezone.utc)
IN_WINDOW = EV_T.timestamp()
DATA_MARK = "SEEN-ITEMS-MARKER-5c1d"      # content of an infra data file: may be shown
PRIVATE_MARK = "PRIVATE-DATA-MARKER-9e2f"  # content of a non-infra / secret / scratch file: never shown
CODE = ('import json\nfrom pathlib import Path\n\n'
        'def load(state_dir):\n'
        '    seen = json.loads(Path(state_dir, "watch.json").read_text())["seen"]\n'
        '    return seen.get("events") or {}\n')


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = make_env(tmp_path, monkeypatch)
    install_logs(e["hermes"])
    for k in ("JUDGE_DATA_FILES", "JUDGE_DATA_FILES_MAX", "JUDGE_DATA_FILE_BYTES"):
        monkeypatch.delenv(k, raising=False)
    return e


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


def _state_db(hermes, calls):
    """calls: [(call_id, tool, args)]"""
    con = sqlite3.connect(hermes / "state.db")
    con.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
                "content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL)")
    con.execute("INSERT INTO messages (session_id, role, tool_calls, timestamp) VALUES (?, 'assistant', ?, 0)",
                (SESSION, json.dumps([{"id": cid, "type": "function",
                                       "function": {"name": tool, "arguments": json.dumps(args)}}
                                      for cid, tool, args in calls])))
    con.commit()
    con.close()


def _event(tool, paths=(), call_id=None):
    snapshot.record_event(q.snapshot_dir(SESSION), tool, [str(p) for p in paths], "ok", now=EV_T, call_id=call_id)


def _session(env, code=CODE, reads=(), terminal=(), files=None):
    """Deck repo with app/pipeline.py; the agent rewrites it (patch event), reads *reads* with read_file and runs
    the *terminal* commands (state.db carries the arguments, events.jsonl the call ids)."""
    deck = env["repo"]
    _init_repo(deck, {"app/pipeline.py": "def load(state_dir):\n    return {}\n", **(files or {})})
    snapshot.take(SESSION, str(deck), config.load_config())
    (deck / "app" / "pipeline.py").write_text(code)
    os.utime(deck / "app" / "pipeline.py", (IN_WINDOW, IN_WINDOW))
    _event("patch", [deck / "app" / "pipeline.py"])
    calls = []
    for i, p in enumerate(reads):
        calls.append((f"call-r{i}", "read_file", {"path": str(p)}))
        _event("read_file", call_id=f"call-r{i}")
    for i, cmd in enumerate(terminal):
        calls.append((f"call-t{i}", "terminal", {"command": cmd, "workdir": str(deck)}))
        _event("terminal", call_id=f"call-t{i}")
    if calls:
        _state_db(env["hermes"], calls)
    return deck


def _collect(paths, data_class="infra", kind="completion", cwd=None):
    r = q.make_request(kind, SESSION, "2026-10-03T03:20:00Z", source_event="on_session_end",
                       changed_paths=[str(p) for p in paths], claims="Done.", data_class=data_class,
                       detail={"cwd": cwd} if cwd else None, created="2026-10-03T03:30:00Z")
    q.write_request(r)
    ev = collect.collect(r["id"], runner=FakeRunner(default=(255, "", "offline")), now=NOW)
    return r, ev, json.loads((ev / "manifest.json").read_text())


def _all_text(ev: Path) -> str:
    return "\n".join(f.read_text(errors="replace") for f in sorted(ev.rglob("*")) if f.is_file())


def _seed(path: Path, extra=None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"watch": "w", "seen": {"items": {DATA_MARK: "2026-10-01"}}, **(extra or {})},
                               indent=1))
    return path


# ---------------------------------------------------------------- included
def test_infra_data_file_read_by_the_agent_is_included(env):
    seed = _seed(env["repo"] / "state" / "watch.json")
    deck = _session(env, reads=[seed])
    r, ev, man = _collect([deck / "app" / "pipeline.py"], cwd=str(deck))
    text = (ev / DF.ARTIFACT).read_text()
    assert DATA_MARK in text and f"=== data file: {seed}" in text
    assert "read by the agent" in text and "named in agent-diff.patch" in text  # "watch.json" literal in the diff
    inc = man["data_files"]["included"]
    assert [x["path"] for x in inc] == [str(seed)]
    assert inc[0]["bytes"] == seed.stat().st_size and inc[0]["excerpt"] == "whole"
    assert inc[0]["why"] == ["read by the agent", "named in agent-diff.patch"]
    assert DF.ARTIFACT in man["artifacts"] and DF.ARTIFACT in man["point_in_time"]
    # it reaches the judge's input
    assert DATA_MARK in RJ.bundle_text(ev, 150000)


def test_terminal_cat_jq_head_targets_count_as_reads(env):
    a = _seed(env["repo"] / "state" / "a.json")
    b = _seed(env["repo"] / "state" / "b.yaml")
    c = _seed(env["repo"] / "state" / "c.csv")
    deck = _session(env, code="x = 1\n",
                    terminal=["cd state && jq '.seen | keys' a.json", f"head -n 5 {b} 2>/dev/null",
                              "cat state/c.csv | wc -l"])
    r, ev, man = _collect([deck / "app" / "pipeline.py"], cwd=str(deck))
    assert sorted(x["path"] for x in man["data_files"]["included"]) == sorted(map(str, (a, b, c)))
    # the command text never reaches the bundle
    assert "jq '.seen" not in _all_text(ev) and "head -n 5" not in _all_text(ev)


def test_file_named_by_the_diff_but_not_read_is_included(env):
    deck = env["repo"]
    seed = _seed(deck / "app" / "watch.json")  # Path(state_dir, "watch.json") with state_dir = app/
    deck = _session(env, files={"app/watch.json": seed.read_text()})
    r, ev, man = _collect([deck / "app" / "pipeline.py"], cwd=str(deck))
    inc = man["data_files"]["included"]
    assert [x["path"] for x in inc] == [str(deck / "app" / "watch.json")]
    assert inc[0]["why"] == ["named in agent-diff.patch"]


# ---------------------------------------------------------------- withheld
def test_secret_env_sensitive_and_scratch_files_are_withheld_without_names(env, tmp_path):
    deck = env["repo"]
    envfile = deck / "site.env"
    envfile.write_text(f"API_TOKEN={PRIVATE_MARK}\n")
    keyjson = deck / "state" / "credentials.json"
    keyjson.parent.mkdir(parents=True)
    keyjson.write_text(json.dumps({"k": PRIVATE_MARK}))
    private = _seed(env["home"] / "private-watches" / "w.json", {"note": PRIVATE_MARK})
    scratch = _seed(env["hermes"] / "cache" / "scratch" / "dry.json", {"note": PRIVATE_MARK})
    deck = _session(env, reads=[envfile, keyjson, private, scratch], terminal=[f"cat {envfile}"])
    r, ev, man = _collect([deck / "app" / "pipeline.py"], cwd=str(deck))
    assert man["data_class"] == "infra"
    df = man["data_files"]
    assert df["included"] == [] and not (ev / DF.ARTIFACT).exists()
    assert df["withheld_counts"] == {DF.R_SECRET: 2, DF.R_SENSITIVE: 1, DF.R_SCRATCH: 1}
    assert df["withheld"] == []  # non-infra files are counted, never named
    everything = _all_text(ev)
    assert PRIVATE_MARK not in everything
    for p in (envfile, keyjson, private, scratch):
        assert str(p) not in everything and p.name not in json.dumps(df)
    assert man["withheld"][DF.ARTIFACT].startswith("4 data file(s)")


def test_too_big_unreadable_and_key_material_are_withheld_by_name(env, monkeypatch):
    deck = env["repo"]
    big = deck / "state" / "big.json"
    big.parent.mkdir(parents=True)
    big.write_text("[" + ",".join(['"x"'] * 400) + "]")
    binary = deck / "state" / "blob.csv"
    binary.write_bytes(b"\xff\xfe\x00bad")
    pem = deck / "state" / "cert.xml"
    pem.write_text("<x>-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----</x>\n")
    monkeypatch.setattr(DF, "READ_MAX", 1000)
    deck = _session(env, code="x = 1\n", reads=[big, binary, pem])
    r, ev, man = _collect([deck / "app" / "pipeline.py"], cwd=str(deck))
    why = {x["path"]: x["reason"] for x in man["data_files"]["withheld"]}
    assert why[str(big)].startswith(DF.R_BIG)
    assert why[str(binary)] == DF.R_BINARY and why[str(pem)] == DF.R_KEY
    assert "MIIabc" not in _all_text(ev)


def test_unrelated_files_and_non_data_types_are_ignored(env):
    deck = env["repo"]
    unrelated = _seed(deck / "state" / "other.json")
    notes = deck / "docs" / "notes.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("# notes\n")
    reqs = deck / "requirements.txt"
    reqs.write_text("httpx\n")
    deck = _session(env, code="x = 1\n", reads=[notes, reqs])
    r, ev, man = _collect([deck / "app" / "pipeline.py"], cwd=str(deck))
    df = man["data_files"]
    assert df["included"] == []
    assert str(unrelated) not in json.dumps(df) and str(notes) not in json.dumps(df)  # neither read nor named
    assert df["withheld"] == [{"path": str(reqs), "reason": DF.R_TYPE}]           # .txt only when named


def test_text_sample_named_by_the_diff_is_included(env):
    deck = env["repo"]
    deck = _session(env, code='SAMPLE = "fixtures/sample.txt"\n', files={"fixtures/sample.txt": "line one\n"})
    r, ev, man = _collect([deck / "app" / "pipeline.py"], cwd=str(deck))
    assert [x["path"] for x in man["data_files"]["included"]] == [str(deck / "fixtures" / "sample.txt")]


def test_files_the_agent_changed_are_not_repeated(env):
    deck = env["repo"]
    deck = _session(env, files={"state/watch.json": "{}\n"})
    (deck / "state" / "watch.json").write_text('{"seen": {}}\n')
    os.utime(deck / "state" / "watch.json", (IN_WINDOW, IN_WINDOW))
    _event("write_file", [deck / "state" / "watch.json"])
    r, ev, man = _collect([deck / "app" / "pipeline.py", deck / "state" / "watch.json"], cwd=str(deck))
    assert man["data_files"]["included"] == []  # its content is in agent-diff.patch


# ---------------------------------------------------------------- cap, excerpt, redaction
def test_cap_keeps_named_and_most_read_files_first(env, monkeypatch):
    deck = env["repo"]
    files = [_seed(deck / "state" / f"f{i}.json") for i in range(4)]
    named = _seed(deck / "state" / "watch.json")  # read and named by the diff: first
    deck = _session(env, reads=[files[0], files[1], files[1], files[2], files[3], named])
    r, ev, man = _collect([deck / "app" / "pipeline.py"], cwd=str(deck))
    inc = [x["path"] for x in man["data_files"]["included"]]
    assert len(inc) == DF.DEFAULT_MAX_FILES == 3
    assert inc[0] == str(named) and inc[1] == str(files[1])  # then the file read twice
    capped = [x for x in man["data_files"]["withheld"] if x["reason"] == DF.R_CAP]
    assert len(capped) == 2
    monkeypatch.setenv("JUDGE_DATA_FILES_MAX", "1")
    r, ev, man = _collect([deck / "app" / "pipeline.py"], cwd=str(deck))
    assert [x["path"] for x in man["data_files"]["included"]] == [str(named)]
    monkeypatch.setenv("JUDGE_DATA_FILES", "0")
    r, ev, man = _collect([deck / "app" / "pipeline.py"], cwd=str(deck))
    assert "disabled" in man["data_files"]["skipped"] and not (ev / DF.ARTIFACT).exists()


def test_per_file_byte_cap_and_json_structure_excerpt(env, monkeypatch):
    deck = env["repo"]
    seed = deck / "state" / "watch.json"
    seed.parent.mkdir(parents=True)
    big = {"watch": "w", "sources": {f"s{i}": {"url": f"https://feed.example.com/{i}"} for i in range(30)},
           "seen": {"papers": {f"2509.{i:05d}": "t" for i in range(500)},
                    "items": {f"news-{i}": "t" for i in range(500)}}, "notes": "n" * 1000}
    seed.write_text(json.dumps(big, indent=2))
    deck = _session(env, reads=[seed])
    r, ev, man = _collect([deck / "app" / "pipeline.py"], cwd=str(deck))
    inc = man["data_files"]["included"][0]
    assert inc["excerpt"] == "json-structure" and inc["shown_chars"] <= DF.DEFAULT_MAX_BYTES
    text = (ev / DF.ARTIFACT).read_text()
    # every top-level key and both seen buckets survive; long maps are elided with a count
    for key in ('"watch"', '"sources"', '"seen"', '"papers"', '"items"', '"notes"'):
        assert key in text
    assert "more key(s) elided by the collector" in text
    monkeypatch.setenv("JUDGE_DATA_FILE_BYTES", "1000")
    r, ev, man = _collect([deck / "app" / "pipeline.py"], cwd=str(deck))
    assert man["data_files"]["included"][0]["shown_chars"] <= 1000


def test_middle_cut_marks_omitted_lines():
    text = "\n".join(f"row {i}, value" for i in range(1000))
    out, how = DF.excerpt(text, "/x/rows.csv", 600)
    assert how == "middle-cut" and len(out) <= 600
    assert out.startswith("row 0,") and out.rstrip().endswith("row 999, value")
    assert "line(s) omitted by the collector" in out
    out, how = DF.excerpt("{not json" + "x" * 2000, "/x/a.json", 500)
    assert how == "middle-cut" and len(out) <= 600


def test_data_file_content_is_redacted(env):
    deck = env["repo"]
    seed = _seed(deck / "state" / "watch.json",
                 {"api_key": "placeholder-value-for-test", "url": "https://user:hunter2pass@feed.example.com/x",
                  "token_note": "sk-" + "A" * 30})
    deck = _session(env, reads=[seed])
    r, ev, man = _collect([deck / "app" / "pipeline.py"], cwd=str(deck))
    text = (ev / DF.ARTIFACT).read_text()
    assert DATA_MARK in text
    assert "placeholder-value-for-test" not in text and "hunter2pass" not in text and "A" * 30 not in text
    assert "<redacted>" in text


# ---------------------------------------------------------------- sensitive / claims-only / other kinds
def test_sensitive_bundle_gets_no_data_files(env):
    seed = _seed(env["repo"] / "state" / "watch.json")
    deck = _session(env, reads=[seed])
    r, ev, man = _collect([deck / "app" / "pipeline.py"], data_class="sensitive", cwd=str(deck))
    assert man["data_class"] == "sensitive"
    assert not (ev / DF.ARTIFACT).exists() and DATA_MARK not in _all_text(ev)
    assert man["data_files"]["included"] == [] and "data_class=sensitive" in man["data_files"]["skipped"]


def test_runner_never_sends_data_files_of_a_non_infra_bundle(env, tmp_path):
    ev = tmp_path / "ev"
    ev.mkdir()
    (ev / "manifest.json").write_text(json.dumps({"data_class": "sensitive", "request": {"session": SESSION}}))
    (ev / DF.ARTIFACT).write_text(f"=== data file: /x.json ===\n{DATA_MARK}\n")
    assert DATA_MARK not in RJ.bundle_text(ev, 150000)
    (ev / "manifest.json").write_text(json.dumps({"data_class": "infra", "request": {"session": SESSION}}))
    assert DATA_MARK in RJ.bundle_text(ev, 150000)
    assert RJ.DATA_FILES == DF.ARTIFACT


def test_claims_only_bundle_never_carries_data_files(env):
    seed = _seed(env["repo"] / "state" / "watch.json")
    deck = _session(env, reads=[seed])
    r, ev, man = _collect([deck / "app" / "pipeline.py"], cwd=str(deck))
    assert (ev / DF.ARTIFACT).exists()
    built = CO.build(r, ev)
    assert DATA_MARK not in built.message and "watch.json" not in built.message
    assert RJ.code_review_decision(r, ev, RJ.CLAIMS_MODE, "infra")[0] is False


def test_gate_requests_get_no_data_files(env):
    seed = _seed(env["repo"] / "state" / "watch.json")
    deck = _session(env, reads=[seed])
    r, ev, man = _collect([deck / "app" / "pipeline.py"], kind="gate", cwd=str(deck))
    assert not (ev / DF.ARTIFACT).exists() and "kind=gate" in man["data_files"]["skipped"]


def test_recollection_drops_a_stale_artifact(env, monkeypatch):
    seed = _seed(env["repo"] / "state" / "watch.json")
    deck = _session(env, reads=[seed])
    r, ev, man = _collect([deck / "app" / "pipeline.py"], cwd=str(deck))
    assert (ev / DF.ARTIFACT).exists()
    monkeypatch.setenv("JUDGE_DATA_FILES", "0")
    ev = collect.collect(r["id"], runner=FakeRunner(default=(255, "", "offline")), now=NOW)
    assert not (ev / DF.ARTIFACT).exists()
    assert DF.ARTIFACT not in json.loads((ev / "manifest.json").read_text())["artifacts"]


def test_reads_outside_the_window_do_not_count(env):
    seed = _seed(env["repo"] / "state" / "late.json")
    deck = _session(env, code="x = 1\n")
    _state_db(env["hermes"], [("call-late", "read_file", {"path": str(seed)})])
    snapshot.record_event(q.snapshot_dir(SESSION), "read_file", [], "ok",
                          now=datetime(2026, 10, 3, 3, 45, 0, tzinfo=timezone.utc), call_id="call-late")
    snapshot.record_event(q.snapshot_dir(SESSION), "read_file", [], "blocked", now=EV_T, call_id="call-late")
    r, ev, man = _collect([deck / "app" / "pipeline.py"], cwd=str(deck))
    assert man["data_files"]["included"] == []


# ---------------------------------------------------------------- parsing helpers
def test_read_targets_parsing(tmp_path):
    (tmp_path / "s").mkdir()
    for n in ("a.json", "b.json"):
        (tmp_path / "s" / n).write_text("{}")
    t = tmp_path.as_posix()
    assert DF.read_targets("cd s && jq -r '.seen.items | keys[]' a.json", t) == [f"{t}/s/a.json"]
    assert DF.read_targets("jq --arg k v -f prog.jq s/a.json", t) == [f"{t}/s/a.json"]
    assert DF.read_targets("head -n 20 s/a.json; tail -c 100 s/b.json", t) == [f"{t}/s/a.json", f"{t}/s/b.json"]
    assert DF.read_targets("cat s/*.json", t) == [f"{t}/s/a.json", f"{t}/s/b.json"]
    assert DF.read_targets("sudo cat /etc/app/conf.yaml", t) == ["/etc/app/conf.yaml"]
    assert DF.read_targets("cat $HOME/x.json `pwd`/y.json", t) == []
    assert DF.read_targets("python3 -c 'print(1)'; ls s", t) == []


def test_diff_literals_and_names():
    diff = ('+++ b/app/x.py\n+STATE = Path("state") / "watch.json"\n+cfg = load(\'conf/app.yaml\')\n'
            '-OLD = "gone.json"\n context "ctx.json"\n')
    lits = DF.diff_literals(diff)
    assert "watch.json" in lits and "conf/app.yaml" in lits and "gone.json" not in lits and "ctx.json" not in lits
    assert DF._named("/srv/deck/state/watch.json", lits)
    assert DF._named("/srv/deck/conf/app.yaml", lits)
    assert not DF._named("/srv/deck/state/other.json", lits)
