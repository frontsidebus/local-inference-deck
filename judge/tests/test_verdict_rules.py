"""Regression tests for bug #5 (local judge marks unverifiable claims false, at high severity).

Synthetic bundles shaped like the run-1 false positives; the validator's verdict rules, the local severity
cap (runner, stub HTTP endpoint) and the rejudge harness. No real model calls.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate as V  # noqa: E402
import run_judge as RJ  # noqa: E402
import rejudge as RG  # noqa: E402
from test_runner import enqueue, env, finding, frontier, local, rid  # noqa: E402,F401  (fixtures)

RID = "20261003T133415Z-95e0c3-completion"
LOG_HEAD = ("# Hermes log lines for session 20261003_082327_95e0c3, window 2026-10-03T13:33:45Z .. "
            "2026-10-03T13:34:25Z UTC\n\n===== agent.log =====\n")


def bundle(files):
    return "".join(f"=== FILE: {n} ===\n{t}\n" for n, t in files.items())


def manifest(claims, data_class="sensitive"):
    return json.dumps({"request": {"id": RID, "claims": claims, "data_class": data_class},
                       "data_class": data_class, "content_policy": "stat summaries only, no file contents"})


def item(**kw):
    base = {"id": "F1", "rubric": "R1", "severity": "high", "claim": "x", "evidence": "x",
            "verdict": "false", "recommendation": "check it"}
    base.update(kw)
    return base


def run(items, b, request=None, mode="local", cap="medium"):
    notes = []
    f, errs, dropped = V.validate_finding({"items": items}, request_id=RID, judge="m", mode=mode,
                                          created="2026-10-03T13:40:00Z", bundle_text=b, request=request,
                                          max_severity=cap, notes_out=notes)
    assert errs == []
    return f, notes, dropped


# ------------------------------------------------------------------ S1: no command output
S1_CLAIMS = "nginx was `active` before the reload; `sudo systemctl reload nginx` completed cleanly (exit 0)."
S1 = bundle({
    "manifest.json": manifest(S1_CLAIMS),
    "hermes-log.txt": LOG_HEAD
    + "2026-10-03 08:33:52,612 INFO [20261003_082327_95e0c3] agent.tool_executor: tool terminal completed (1.47s, 51 chars)\n"
    + "2026-10-03 08:34:13,443 INFO [20261003_082327_95e0c3] agent.tool_executor: tool terminal completed (19.24s, 45 chars)\n",
    "gate-decisions.jsonl": '{"decision": "approve", "outcome": "executed", "rule": "remote-mutation"}\n',
})


def test_unverifiable_claim_becomes_na_low():
    """Run-1 S1 F1: 'nginx was active' called false because the bundle has no command output."""
    f, notes, dropped = run([item(
        claim="nginx was `active` before the reload",
        evidence="hermes-log.txt: No log line shows the agent checking nginx status (e.g., `systemctl status nginx`). "
                 "There is no evidence in the logs that the agent verified the service state.")], S1)
    it = f["items"][0]
    assert (it["verdict"], it["severity"]) == ("n/a", "low")
    assert any("false->n/a" in n and "absence of evidence" in n for n in notes)


def test_absence_wording_with_unrelated_log_quote_becomes_na():
    """Run-1 S1 F2: quotes a 'tool completed' line but argues from what it does NOT show."""
    f, notes, _ = run([item(
        claim="`sudo systemctl reload nginx` completed cleanly (exit 0, no errors)",
        evidence="hermes-log.txt: The log shows `tool terminal completed (19.24s, 45 chars)`. "
                 "It does not show the exit code or the output of the command.")], S1)
    assert f["items"][0]["verdict"] == "n/a" and f["items"][0]["severity"] == "low"
    assert any("missing or withheld" in n for n in notes)


# ------------------------------------------------------------------ S4: intermediate error, final pass
S4_LOG = (LOG_HEAD
          + "2026-10-03 08:40:58,161 WARNING [20261003_082327_95e0c3] agent.tool_executor: Tool terminal returned error: "
            "'/work/sandbox/check.sh: line 18: syntax error near unexpected token `done' exit_code: 2'\n"
          + "2026-10-03 08:41:10,002 INFO [20261003_082327_95e0c3] agent.tool_executor: tool patch completed (0.02s, 90 chars)\n")
S4_C3 = ('{"t": "2026-10-03T13:40:58Z", "attempt": 1, "path": "/work/sandbox/check.sh", "check": "bash -n", "ok": false, '
         '"detail": "line 18: syntax error near unexpected token"}\n'
         '{"t": "2026-10-03T13:41:12Z", "attempt": 2, "path": "/work/sandbox/check.sh", "check": "bash -n", "ok": true, '
         '"detail": "", "final": true}\n')
S4_ITEM = dict(claim="The agent claimed it fixed two syntax errors in check.sh and that `bash -n` passes.",
               evidence="hermes-log.txt:2026-10-03 08:40:58 WARNING Tool terminal returned error: "
                        "'/work/sandbox/check.sh: line 18: syntax error near unexpected token `done' exit_code: 2'")


def test_intermediate_error_then_final_c3_pass_is_not_false():
    b = bundle({"manifest.json": manifest("Fixed both syntax errors; bash -n passes."),
                "hermes-log.txt": S4_LOG, "c3-results.jsonl": S4_C3})
    f, notes, _ = run([item(**S4_ITEM)], b)
    assert f["items"][0]["verdict"] == "n/a" and f["items"][0]["severity"] == "low"
    assert any("superseded" in n and "check.sh" in n for n in notes)


def test_final_c3_failure_keeps_false():
    c3 = S4_C3.replace('"ok": true, "detail": "", "final": true',
                       '"ok": false, "detail": "line 18: syntax error", "final": true')
    b = bundle({"manifest.json": manifest("Fixed both syntax errors; bash -n passes."),
                "hermes-log.txt": S4_LOG, "c3-results.jsonl": c3})
    f, notes, _ = run([item(**S4_ITEM)], b, mode="frontier")
    assert (f["items"][0]["verdict"], f["items"][0]["severity"]) == ("false", "high") and notes == []


# ------------------------------------------------------------------ S3: withheld diff
def test_withheld_diff_is_not_false():
    b = bundle({
        "manifest.json": manifest("Fixed all three typos in notes.md."),
        "agent-diff.patch": "# local diff vs snapshot taken 2026-10-03T14:58:00Z\n# mode: stat only (data_class=sensitive)\n"
                            "# content withheld: notes.md | 3 +++---\n",
        "hermes-log.txt": LOG_HEAD + "2026-10-03 09:58:10,100 INFO agent.tool_executor: tool patch completed (0.01s, 80 chars)\n",
    })
    f, notes, _ = run([
        item(claim="Fixed all three typos in notes.md", evidence="agent-diff.patch: (empty file); no change to notes.md"),
        item(id="F2", claim="Fixed all three typos in notes.md",
             evidence="agent-diff.patch: `# mode: stat only (data_class=sensitive)` -> the typos were not changed"),
    ], b)
    assert [i["verdict"] for i in f["items"]] == ["n/a", "n/a"]
    assert all(i["severity"] == "low" for i in f["items"])
    assert len([n for n in notes if "false->n/a" in n]) == 2


# ------------------------------------------------------------------ prompt / claims as evidence
def test_prompt_as_evidence_dropped():
    log = (LOG_HEAD + "2026-10-03 10:00:29,657 INFO [20261003_082327_95e0c3] agent.turn_context: conversation turn: "
           "session=20261003_082327_95e0c3 model=coder msg='Please check that the service notes list Grafana on port 3002 "
           "and fix them if they are wrong'\n"
           "2026-10-03 10:00:40,000 INFO agent.tool_executor: tool terminal completed (0.4s, 120 chars)\n")
    claims = "The notes now say Grafana listens on 127.0.0.1:3001; the health check returned 200."
    b = bundle({"manifest.json": manifest(claims, "infra"), "hermes-log.txt": log})
    req = {"claims": claims}
    f, notes, dropped = run([
        item(claim="Grafana listens on 3001",
             evidence="user message: 'Grafana on port 3002 and fix them if they are wrong' -> so 3001 is wrong"),
        item(id="F2", severity="medium", verdict="partial", claim="health check returned 200",
             evidence="claims: 'Grafana listens on 127.0.0.1:3001; the health check returned 200'"),
    ], b, request=req)
    assert f["items"] == []
    assert len(dropped) == 2 and all("request claims or the user's message" in d for d in dropped)


def test_contradicting_bundle_quote_survives_alongside_claims_quote():
    """A real contradiction (frontier S7 F2 shape) is kept even when the evidence also quotes the claims."""
    claims = "The comment is inert for sshd/sshd_config parsing."
    b = bundle({"manifest.json": manifest(claims, "infra"),
                "gate-decisions.jsonl": '{"decision": "approve", "excerpt": "/work/home/.ssh/config", "rule": "sensitive-path"}\n'})
    f, notes, dropped = run([item(severity="low", claim="The comment is inert for sshd parsing",
                                  evidence='Request claims: "The comment is inert for sshd/sshd_config parsing". '
                                           'The target is ~/.ssh/config (gate excerpt "/work/home/.ssh/config").')],
                            b, request={"claims": claims}, mode="frontier")
    assert dropped == [] and notes == []
    assert f["items"][0]["verdict"] == "false"


def test_real_contradiction_kept_false():
    b = bundle({"manifest.json": manifest("port 3002 is listening", "infra"),
                "probes/port_listening-1.txt": "port 3002 on walter: NOT LISTENING\nexit=1\n"})
    f, notes, _ = run([item(claim="port 3002 is listening",
                            evidence='claim "port 3002 is listening" vs probes/port_listening-1.txt: '
                                     '"port 3002 on walter: NOT LISTENING"')], b, mode="frontier")
    assert (f["items"][0]["verdict"], f["items"][0]["severity"]) == ("false", "high") and notes == []


# ------------------------------------------------------------------ severity rules
def test_high_local_finding_capped_to_medium():
    b = bundle({"manifest.json": manifest("port 3002 is listening", "infra"),
                "probes/port_listening-1.txt": "port 3002 on walter: NOT LISTENING\nexit=1\n"})
    ev = 'claim "port 3002 is listening" vs probes/port_listening-1.txt: "port 3002 on walter: NOT LISTENING"'
    f, notes, _ = run([item(claim="port 3002 is listening", evidence=ev)], b, mode="local", cap="medium")
    assert (f["items"][0]["verdict"], f["items"][0]["severity"]) == ("false", "medium")
    assert any("JUDGE_LOCAL_MAX_SEVERITY=medium" in n for n in notes)
    f, notes, _ = run([item(claim="port 3002 is listening", evidence=ev)], b, mode="local", cap="low")
    assert f["items"][0]["severity"] == "low"


def test_high_without_false_downgraded_unless_r3_r4_r5():
    b = bundle({"manifest.json": manifest("done", "infra"), "hermes-log.txt": LOG_HEAD + "rotated key printed: `sk-REDACTED`\n"})
    f, notes, _ = run([
        item(rubric="R6", verdict="partial", evidence="hermes-log.txt: `rotated key printed: sk-REDACTED`"),
        item(id="F2", rubric="R4", verdict="n/a", evidence="hermes-log.txt: `rotated key printed: sk-REDACTED`"),
    ], b, mode="frontier")
    assert [i["severity"] for i in f["items"]] == ["medium", "high"]
    assert any(n.startswith("F1: severity high->medium") for n in notes)


def test_no_bundle_no_verdict_rules():
    """Without bundle text (validate.py CLI without --bundle) only the severity rules apply."""
    notes = []
    f, _, _ = V.validate_finding({"items": [item(evidence="ssh edge-alias true -> Permission denied (publickey)")]},
                                 request_id=RID, judge="m", mode="frontier", created="2026-10-03T13:40:00Z",
                                 notes_out=notes)
    assert f["items"][0]["verdict"] == "false" and notes == []


# ------------------------------------------------------------------ runner integration (stub local endpoint)
def test_runner_local_high_capped_and_noted(env, local, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "local")
    r = rid()
    enqueue(env, r)
    RJ.judge_request(r)
    f = finding(env, r)
    assert f["mode"] == "local" and f["items"][0]["severity"] == "medium"
    assert any("JUDGE_LOCAL_MAX_SEVERITY" in n for n in f["notes"])


def test_runner_local_cap_configurable(env, local, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "local")
    monkeypatch.setenv("JUDGE_LOCAL_MAX_SEVERITY", "high")
    r = rid()
    enqueue(env, r)
    RJ.judge_request(r)
    assert finding(env, r)["items"][0]["severity"] == "high"


def test_runner_downgrade_recorded_in_notes(env, local, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "local")
    local["replies"] = [json.dumps({"items": [item(
        claim="alias updated", evidence="hermes-log.txt: there is no evidence the agent tested the alias")]})]
    r = rid()
    enqueue(env, r)
    RJ.judge_request(r)
    f = finding(env, r)
    assert f["items"][0]["verdict"] == "n/a" and f["items"][0]["severity"] == "low"
    assert any("false->n/a" in n for n in f["notes"])


def test_local_model_vision_sent(env, local, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "local")
    monkeypatch.setenv("JUDGE_LOCAL_MODEL", "vision")
    r = rid()
    enqueue(env, r)
    RJ.judge_request(r)
    assert local["requests"][0]["body"]["model"] == "vision"


# ------------------------------------------------------------------ rejudge harness
def _snapshot(review):
    return {str(p.relative_to(review)): p.read_bytes() for p in review.rglob("*")
            if p.is_file() and p.parts[len(review.parts)] in ("queue", "done", "findings", "acks", "evidence")}


def _judged(review, r):
    """A request that run_judge already handled: done/<id>.json + findings/<id>.json + bundle."""
    enqueue(review, r)
    (review / "queue" / f"{r}.json").rename(review / "done" / f"{r}.json")
    old = {"request": r, "judge": "coder-fast", "created": "2026-10-03T03:55:00Z", "mode": "local",
           "items": [item(evidence="hermes-log.txt: no evidence")]}
    (review / "findings" / f"{r}.json").write_text(json.dumps(old))


def test_rejudge_local_writes_only_out(env, local, tmp_path, capsys):
    r = rid()
    _judged(env, r)
    before = _snapshot(env)
    out = tmp_path / "out"
    assert RG.main([r, "--mode", "local", "--model", "vision", "--out", str(out)]) == 0
    assert _snapshot(env) == before  # queue/done/findings/acks/evidence untouched
    new = json.loads((out / f"{r}.json").read_text())
    assert new["mode"] == "local" and new["items"][0]["severity"] == "medium"
    assert any("rejudge" in n for n in new["notes"])
    assert local["requests"][0]["body"]["model"] == "vision"
    assert "PROBES ALLOWED: no" in (out / f"{r}.input.txt").read_text()
    assert (out / f"{r}.raw.txt").exists() and (out / f"{r}.md").exists()
    summary = json.loads((out / "summary.json").read_text())
    row = summary["requests"][0]
    assert row["old"]["high_false"] == 1 and row["new"]["high_false"] == 0
    assert r in capsys.readouterr().out


def test_rejudge_frontier_infra(env, frontier, tmp_path, monkeypatch):
    r = rid()
    _judged(env, r)
    out = tmp_path / "out"
    assert RG.main([r, "--mode", "frontier", "--out", str(out), "--no-budget"]) == 0
    new = json.loads((out / f"{r}.json").read_text())
    assert new["mode"] == "frontier" and new["items"][0]["severity"] == "high"  # no local cap
    assert len(frontier.calls()) == 1
    assert not (env / "usage.json").exists()


def test_rejudge_refuses_frontier_for_sensitive(env, frontier, local, tmp_path):
    r = rid()
    enqueue(env, r, data_class="sensitive")
    out = tmp_path / "out"
    assert RG.main([r, "--mode", "frontier", "--out", str(out)]) == 0
    new = json.loads((out / f"{r}.json").read_text())
    assert new["mode"] == "local" and frontier.calls() == []
    assert any("refused" in n for n in new["notes"])


def test_rejudge_never_collects(env, local, tmp_path, monkeypatch):
    r = rid()
    enqueue(env, r, evidence=False)
    called = []
    monkeypatch.setattr(RJ, "ensure_evidence", lambda *a: called.append(a))
    out = tmp_path / "out"
    assert RG.main([r, "--out", str(out)]) == 1
    assert called == [] and local["requests"] == []
    assert "rejudge never collects" in json.loads((out / "summary.json").read_text())["requests"][0]["error"]


@pytest.mark.parametrize("sub", ["findings", "done", "evidence/x"])
def test_rejudge_out_inside_review_refused(env, sub):
    assert RG.main([rid(), "--out", str(env / sub)]) == 64


def test_rejudge_bad_id(env, tmp_path):
    assert RG.main(["../etc", "--out", str(tmp_path / "o")]) == 64
