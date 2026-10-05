"""Acks (#10): JSON ack format + legacy reading, the closure matrix, forged-ack downgrade, judge-ack
--agent and agent-context detection, judge-findings grouping and --needs-human. Temp review dir only."""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from collector_testlib import make_env

from lib import queue as q

JUDGE = Path(__file__).resolve().parent.parent
ACK = JUDGE / "bin" / "judge-ack"
FINDINGS = JUDGE / "bin" / "judge-findings"
RID = "20261003T035210Z-fdc8ec-completion"
NOW = datetime(2026, 10, 3, 4, 0, 0, tzinfo=timezone.utc)
AGENT_MARKERS = ("AI_AGENT", "HERMES_AGENT", "HERMES_SESSION_ID", "HERMES_SESSION_KEY", "JUDGE_ACK_AGENT_ENV")


def item(iid, sev):
    return {"id": iid, "rubric": "R1", "severity": sev, "claim": f"claim {iid}", "evidence": f"evidence {iid}",
            "verdict": "false", "recommendation": "check"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = make_env(tmp_path, monkeypatch)
    for k in AGENT_MARKERS:
        monkeypatch.delenv(k, raising=False)
    q.write_finding({"request": RID, "judge": "m", "created": q.utc_now_iso(), "mode": "frontier",
                     "items": [item("H1", "high"), item("M1", "medium"), item("L1", "low"), item("H2", "high")]})
    return e


def cli(env, tool, *args, extra_env=None):
    e = {k: v for k, v in os.environ.items() if k not in AGENT_MARKERS}
    e.update({"HERMES_HOME": str(env["hermes"]), "JUDGE_REVIEW_DIR": str(env["review"]),
              "SITE_ENV": str(env["site"])})
    e.update(extra_env or {})
    return subprocess.run([sys.executable, str(tool), *args], env=e, capture_output=True, text=True, timeout=60)


def load_ack_module():
    loader = importlib.machinery.SourceFileLoader("judge_ack_cli", str(ACK))
    spec = importlib.util.spec_from_loader("judge_ack_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def human_shell():
    """judge-ack records actor=agent when a Hermes process is an ancestor (correctly); there is no bypass, so
    the tests that expect a human ack skip when the suite itself runs inside a Hermes session."""
    if [r for r in load_ack_module().agent_context({}) if r.startswith("proc:")]:
        pytest.skip("test suite runs under a Hermes process; judge-ack would (correctly) record actor=agent")


# ------------------------------------------------------------------ format
def test_ack_json_format(env):
    p = q.ack(RID, "H1", "looked at it\nignored", actor="agent", via={"tool": "judge-ack"}, now=NOW)
    assert json.loads(p.read_text()) == {"actor": "agent", "reason": "looked at it", "ts": "2026-10-03T04:00:00Z",
                                         "via": {"tool": "judge-ack"}}
    assert (p.stat().st_mode & 0o777) == 0o600
    a = q.read_ack(RID, "H1")
    assert a["actor"] == "agent" and a["reason"] == "looked at it" and a["legacy"] is False
    with pytest.raises(ValueError):
        q.ack(RID, "H1", "x", actor="root")
    assert q.read_ack(RID, "M1") is None and q.ack_actor(RID, "M1") is None


@pytest.mark.parametrize("content", ["", "fixed by hand\n", "plain reason without newline"])
def test_legacy_plain_text_ack_reads_as_human(env, content):
    (env["review"] / "acks" / f"{RID}.H1").write_text(content)
    a = q.read_ack(RID, "H1")
    assert a["actor"] == "human" and a["legacy"] is True and a["reason"] == content.strip()
    assert a["ts"].endswith("Z")
    assert q.is_acked(RID, "H1") and q.item_status(RID, item("H1", "high")) == "closed"


def test_json_ack_without_valid_actor_is_not_trusted(env):
    (env["review"] / "acks" / f"{RID}.H1").write_text(json.dumps({"actor": "root", "reason": "x"}))
    assert q.read_ack(RID, "H1")["actor"] == "agent"
    (env["review"] / "acks" / f"{RID}.H2").write_text(json.dumps({"reason": "x"}))
    assert q.item_status(RID, item("H2", "high")) == "agent-acked"


# ------------------------------------------------------------------ closure matrix
@pytest.mark.parametrize("actor,sev,status", [
    (None, "high", "open"), (None, "medium", "open"), (None, "low", "open"),
    ("human", "high", "closed"), ("human", "medium", "closed"), ("human", "low", "closed"),
    ("agent", "high", "agent-acked"), ("agent", "medium", "closed"), ("agent", "low", "closed"),
])
def test_closure_matrix(env, actor, sev, status):
    iid = {"high": "H1", "medium": "M1", "low": "L1"}[sev]
    if actor:
        q.ack(RID, iid, "reason", actor=actor)
    it = item(iid, sev)
    assert q.closure(actor, sev) == status
    assert q.item_status(RID, it) == status
    assert q.is_closed(RID, it) is (status == "closed")
    assert q.is_acked(RID, iid) is (actor is not None)  # any ack stops C5 re-injection
    assert q.needs_human(status, it) is (sev == "high" and status != "closed")


def test_items_by_status(env):
    q.ack(RID, "H1", "agent says false", actor="agent")
    q.ack(RID, "M1", "agent says fixed", actor="agent")
    q.ack(RID, "L1", "human ok")
    groups = {s: sorted(i["id"] for _, i in v) for s, v in q.items_by_status().items()}
    assert groups == {"open": ["H2"], "agent-acked": ["H1"], "closed": ["L1", "M1"]}


# ------------------------------------------------------------------ forgery
def test_forged_human_ack_written_by_agent_tool_call_is_agent(env):
    ack_file = env["review"] / "acks" / f"{RID}.H1"
    ack_file.write_text(json.dumps({"actor": "human", "reason": "trust me", "ts": "2026-10-03T04:00:00Z"}))
    assert q.ack_actor(RID, "H1") == "human"
    snap = env["review"] / "snapshots" / "20261003_031000_a1b2c3"
    snap.mkdir(parents=True)
    (snap / "events.jsonl").write_text(
        json.dumps({"t": "2026-10-03T04:00:00Z", "tool": "terminal", "paths": ["/usr/bin/echo"]}) + "\n"
        + json.dumps({"t": "2026-10-03T04:00:01Z", "tool": "terminal", "paths": [str(ack_file)]}) + "\n")
    assert q.agent_wrote_ack(RID, "H1") and not q.agent_wrote_ack(RID, "H2")
    assert q.ack_actor(RID, "H1") == "agent"
    assert q.item_status(RID, item("H1", "high")) == "agent-acked"
    out = json.loads(cli(env, FINDINGS, RID, "--json").stdout)
    h1 = next(i for i in out[0]["items"] if i["id"] == "H1")
    assert h1["status"] == "agent-acked" and h1["ack"]["actor"] == "agent" and h1["ack"]["claimed_actor"] == "human"


# ------------------------------------------------------------------ judge-ack CLI
def test_judge_ack_default_human_and_agent_flag(env, human_shell):
    cp = cli(env, ACK, RID, "M1", "fixed  the\talias")
    assert cp.returncode == 0, cp.stderr
    a = json.loads((env["review"] / "acks" / f"{RID}.M1").read_text())
    assert a["actor"] == "human" and a["reason"] == "fixed the alias" and a["via"]["tool"] == "judge-ack"
    assert a["via"]["flag_agent"] is False and a["via"]["agent_context"] == []
    cp = cli(env, ACK, "--agent", RID, "H1", "the reviewer misread the log")
    assert cp.returncode == 0 and "stays open until a human" in cp.stdout
    a = json.loads((env["review"] / "acks" / f"{RID}.H1").read_text())
    assert a["actor"] == "agent" and a["via"]["flag_agent"] is True
    assert q.item_status(RID, item("H1", "high")) == "agent-acked"
    # flag position does not matter
    assert cli(env, ACK, RID, "L1", "--agent", "fine").returncode == 0
    assert q.read_ack(RID, "L1")["actor"] == "agent"


def test_judge_ack_usage_and_errors(env):
    assert cli(env, ACK, RID, "H1").returncode == 64
    assert cli(env, ACK, "--bogus", RID, "H1", "x").returncode == 64
    assert cli(env, ACK, RID, "H1", "   ").returncode == 64
    assert cli(env, ACK, "../../x", "H1", "x").returncode == 64
    assert cli(env, ACK, RID, "Z9", "x").returncode == 2
    assert cli(env, ACK, "--help").returncode == 0


@pytest.mark.parametrize("marker", ["HERMES_AGENT", "HERMES_SESSION_ID", "AI_AGENT"])
def test_judge_ack_inside_agent_session_records_agent(env, marker):
    cp = cli(env, ACK, RID, "H1", "not a real problem", extra_env={marker: "x"})
    assert cp.returncode == 0 and "recorded as actor=agent" in cp.stderr
    a = json.loads((env["review"] / "acks" / f"{RID}.H1").read_text())
    assert a["actor"] == "agent" and a["via"]["agent_context"] == [f"env:{marker}"]


def test_judge_ack_extra_marker_env(env):
    cp = cli(env, ACK, RID, "H1", "x", extra_env={"JUDGE_ACK_AGENT_ENV": "MY_HARNESS", "MY_HARNESS": "1"})
    assert cp.returncode == 0 and q.read_ack(RID, "H1")["actor"] == "agent"


def test_agent_ack_never_replaces_human_ack(env, human_shell):
    assert cli(env, ACK, RID, "H1", "human checked: false positive").returncode == 0
    cp = cli(env, ACK, "--agent", RID, "H1", "agent opinion")
    assert cp.returncode == 0 and "already closed by a human" in cp.stdout
    assert q.read_ack(RID, "H1")["actor"] == "human"
    # a human ack replaces an agent ack
    assert cli(env, ACK, "--agent", RID, "H2", "agent opinion").returncode == 0
    assert cli(env, ACK, RID, "H2", "human confirms").returncode == 0
    assert q.item_status(RID, item("H2", "high")) == "closed"


def test_agent_context_process_ancestry(tmp_path):
    mod = load_ack_module()
    proc = tmp_path / "proc"

    def mk(pid, ppid, argv, comm="python3"):
        d = proc / str(pid)
        d.mkdir(parents=True)
        (d / "stat").write_text(f"{pid} ({comm}) S {ppid} 1 1 0 -1\n")
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")

    mk(300, 200, ["/bin/bash", "-c", "judge-ack x"], comm="bash (weird) name")
    mk(200, 100, ["/home/u/.hermes/hermes-agent/venv/bin/python3", "/home/u/.local/bin/hermes", "chat"])
    mk(100, 1, ["/sbin/init"])
    mk(400, 100, ["/usr/bin/bash"])
    assert mod.agent_context({}, proc_root=str(proc), start_pid=300) == ["proc:200:hermes"]
    assert mod.agent_context({}, proc_root=str(proc), start_pid=400) == []
    assert mod.agent_context({"HERMES_AGENT": "true"}, proc_root=str(proc), start_pid=400) == ["env:HERMES_AGENT"]
    assert mod.agent_context({}, proc_root=str(proc / "missing"), start_pid=300) == []


# ------------------------------------------------------------------ judge-findings
def test_judge_findings_groups_and_needs_human(env):
    q.ack(RID, "H1", "agent says false", actor="agent")
    q.ack(RID, "M1", "agent says fixed", actor="agent")
    q.ack(RID, "L1", "human ok")
    out = cli(env, FINDINGS, "--items", extra_env={"NO_COLOR": "1"}).stdout
    i_open, i_agent, i_closed = (out.index("== open (1) =="), out.index("== agent-acked: awaiting a human (1) =="),
                                 out.index("== closed (2) =="))
    assert i_open < i_agent < i_closed
    assert f"{RID} H2" in out[i_open:i_agent] and f"{RID} H1" in out[i_agent:i_closed]
    assert f"{RID} M1" in out[i_closed:] and f"{RID} L1" in out[i_closed:]
    assert "ack: agent" in out[i_agent:i_closed] and "ack: human" in out[i_closed:]

    data = json.loads(cli(env, FINDINGS, "--json").stdout)
    st = {i["id"]: (i["status"], i["acked"]) for i in data[0]["items"]}
    assert st == {"H1": ("agent-acked", True), "M1": ("closed", True), "L1": ("closed", True), "H2": ("open", False)}

    nh = json.loads(cli(env, FINDINGS, "--needs-human", "--json").stdout)
    assert sorted(i["id"] for i in nh[0]["items"]) == ["H1", "H2"]
    un = json.loads(cli(env, FINDINGS, "--unacked", "--json").stdout)
    assert [i["id"] for i in un[0]["items"]] == ["H2"]

    summary = cli(env, FINDINGS, extra_env={"NO_COLOR": "1"}).stdout
    assert "open:1 agent-acked:1 closed:2" in summary
    one = cli(env, FINDINGS, RID, extra_env={"NO_COLOR": "1"}).stdout
    assert "== agent-acked: awaiting a human (1) ==" in one

    q.ack(RID, "H1", "human agrees")
    q.ack(RID, "H2", "human fixed it")
    nh = json.loads(cli(env, FINDINGS, "--needs-human", "--json").stdout)
    assert nh[0]["items"] == []
    assert "(no items)" in cli(env, FINDINGS, "--needs-human").stdout


# ------------------------------------------------------------------ R8 code-defect items
R8RID = "20261003T040000Z-fdc8ec-completion"
SCN = "POST /run returns id A; the files are written as id B; GET /runs/A then returns 404."


def r8_item(iid="D1", sev="medium"):
    return {"id": iid, "rubric": "R8", "severity": sev, "claim": "run id differs between main and pipeline",
            "evidence": "app/main.py:178: `run_id = make_id(now())`", "verdict": "defect",
            "failure_scenario": SCN, "recommendation": "pass the id through"}


@pytest.fixture
def r8env(env):
    q.write_finding({"request": R8RID, "judge": "m", "created": q.utc_now_iso(), "mode": "frontier",
                     "items": [r8_item("D1"), r8_item("D2", "high"), item("M9", "medium")]})
    return env


def test_finding_md_shows_failure_scenario(r8env):
    md = (r8env["review"] / "findings" / f"{R8RID}.md").read_text()
    assert "verdict defect" in md and f"**Failure scenario:** {SCN}" in md
    assert md.index("**Failure scenario:**") < md.index("**Recommendation:** pass the id through")
    assert md.count("**Failure scenario:**") == 2  # not for the R1 item


def test_judge_findings_r8_item(r8env):
    out = cli(r8env, FINDINGS, R8RID, extra_env={"NO_COLOR": "1"}).stdout
    d1 = out[out.index(f"{R8RID} D1"):]
    d1 = d1[:d1.index("recommendation:")]
    assert "R8 verdict=DEFECT (code defect)" in d1
    assert "defect: run id differs" in d1 and "code: app/main.py:178: `run_id = make_id(now())`" in d1
    assert f"failure scenario: {SCN}" in d1 and "claim:" not in d1
    m9 = out[out.index(f"{R8RID} M9"):]
    assert "verdict=false" in m9 and "claim: claim M9" in m9 and "failure scenario" not in m9
    # compact: 5 lines per R8 item (header, defect, code, scenario, recommendation)
    assert len(out[out.index(f"{R8RID} D1"):out.index(f"{R8RID} M9")].strip().splitlines()) in (5, 6)
    summary = cli(r8env, FINDINGS, extra_env={"NO_COLOR": "1"}).stdout
    line = next(ln for ln in summary.splitlines() if ln.startswith(R8RID))
    assert line.rstrip().endswith("defect:2")
    assert "defect:" not in next(ln for ln in summary.splitlines() if ln.startswith(RID))
    data = json.loads(cli(r8env, FINDINGS, R8RID, "--json").stdout)
    assert {i["id"]: i.get("failure_scenario") for i in data[0]["items"]} == {"D1": SCN, "D2": SCN, "M9": None}


def test_judge_findings_colors_defect_on_tty_only(r8env):
    out = cli(r8env, FINDINGS, R8RID).stdout  # stdout is a pipe: no colors
    assert "\033[" not in out and "verdict=DEFECT" in out


def test_judge_ack_defect_items(r8env, human_shell):
    r = cli(r8env, ACK, "--agent", R8RID, "D1", "fixed the run id")
    assert r.returncode == 0 and "acknowledged" in r.stdout
    r = cli(r8env, ACK, "--agent", R8RID, "D2", "fixed too")
    assert r.returncode == 0 and "stays open until a human" in r.stdout
    data = json.loads(cli(r8env, FINDINGS, R8RID, "--json").stdout)
    st = {i["id"]: i["status"] for i in data[0]["items"]}
    assert st == {"D1": "closed", "D2": "agent-acked", "M9": "open"}
    assert cli(r8env, ACK, R8RID, "D2", "human checked").returncode == 0
    data = json.loads(cli(r8env, FINDINGS, R8RID, "--json").stdout)
    assert {i["id"]: i["status"] for i in data[0]["items"]}["D2"] == "closed"
    assert cli(r8env, ACK, R8RID, "D9", "x").returncode == 2
