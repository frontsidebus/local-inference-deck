"""Frontier claims stage for sensitive bundles: collector/claims_only.py (the claims-only bundle: allowlist,
path masking, self-check), the runner's two-stage flow, the daily cap and the disable flag, injection and
acks of frontier-claims findings, and rejudge --mode frontier-claims. Stub backends only."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

JUDGE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(JUDGE / "collector"))
sys.path.insert(0, str(JUDGE / "runner"))
sys.path.insert(0, str(JUDGE))
import claims_only as CO  # noqa: E402
import run_judge as RJ  # noqa: E402
import rejudge as RG  # noqa: E402
from lib import queue as Q  # noqa: E402
from test_runner import GOOD, enqueue, env, finding, frontier, local, rid  # noqa: E402,F401  (fixtures)
from test_inject import run_hook  # noqa: E402

SESSION = "20261002_165907_fdc8ec"
HOME = "/home/tester"
SECRET_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
# Strings that live in the sensitive bundle and must never reach the claims-only bundle.
SENTINELS = (
    "please-keep-this-user-prompt-private",   # user message (msg=...)
    "very-private-dir", "shadow",               # paths in a gate excerpt / C3 / attribution / tool errors
    "PROBE-OUTPUT-LINE", "SLOTS-SENTINEL",      # host/probe output, slots
    "DIFF-CONTENT-LINE", "OTHERS-SENTINEL",     # diff hunk, others-changed
    "C3-DETAIL-SENTINEL",                       # C3 detail
    "ERROR-BODY-SENTINEL",                      # tool error text
    "OTHER-SESSION-LINE",                       # another session's log line
    "untagged-context-line",                    # untagged log line
    "snapshot-root-sentinel",                   # manifest snapshot data
    "cwd-sentinel",                             # request.detail.cwd
    SECRET_TOKEN,
)
CLAIMS = ("Done. I edited /home/tester/very-private-dir/app.conf and ~/notes/todo.md, and "
          "$HOME/.config/x.toml; see judge-sandbox/check.sh. Usage: /models 180G (all of it in /models/gguf, 109G), "
          "VRAM 22.6/24.6 GiB, API at https://api.example.com/v1/chat. token=" + SECRET_TOKEN + ". I/O ok.")


def make_evidence(ev: Path, claims: str = CLAIMS, kind: str = "completion") -> dict:
    (ev / "probes").mkdir(parents=True, exist_ok=True)
    req = {"id": rid(kind=kind), "kind": kind, "session": SESSION, "created": "2026-10-03T03:52:10Z",
           "since": "2026-10-03T03:50:00Z", "changed_paths": ["/home/tester/very-private-dir/app.conf"],
           "claims": claims, "plan": "plan text cwd-sentinel", "data_class": "sensitive",
           "source_event": "on_session_end", "detail": {"cwd": "/home/tester/cwd-sentinel", "tool_activity": 3}}
    manifest = {
        "request": req, "data_class": "sensitive", "collected": "2026-10-03T03:52:23Z",
        "window": {"since": "2026-10-03T03:50:00Z", "until": "2026-10-03T03:52:20Z", "grace_seconds": 10,
                   "until_basis": "grace"},
        "snapshot": {"dir_roots": ["/home/tester/snapshot-root-sentinel"]},
        "attribution": {"agent_paths": ["/home/tester/very-private-dir/app.conf"],
                        "changed_by_others": ["/home/tester/OTHERS-SENTINEL", "/etc/shadow"],
                        "rejected_request_paths": ["/etc/shadow"], "rejected_request_paths_total": 1},
    }
    (ev / "manifest.json").write_text(json.dumps(manifest))
    log = "\n".join([
        "# Hermes log lines for session X (plus untagged lines), window a .. b UTC; log tz CDT; secrets redacted",
        "",
        "===== agent.log =====",
        f"2026-10-02 22:50:01,100 INFO [{SESSION}] agent.turn_context: conversation turn: session={SESSION} "
        "model=coder provider=custom platform=cli history=4 msg='please-keep-this-user-prompt-private /home/tester/very-private-dir'",
        f"2026-10-02 22:50:03,000 INFO [{SESSION}] agent.conversation_loop: API call #1: model=coder provider=custom "
        "in=1000 out=50 total=1050 latency=2.5s cache=1/1 (100%) id=chatcmpl-x",
        f"2026-10-02 22:50:04,000 INFO [{SESSION}] agent.tool_executor: tool terminal completed (0.49s, 810 chars)",
        f"2026-10-02 22:50:05,000 WARNING [{SESSION}] agent.tool_executor: Tool patch returned error (0.07s): "
        '{"error": "ERROR-BODY-SENTINEL /home/tester/very-private-dir/app.conf"}',
        "2026-10-02 22:50:05,500 INFO [20261002_000000_zzzzzz] agent.tool_executor: OTHER-SESSION-LINE tool x",
        "2026-10-02 22:50:05,600 INFO tools.registry: untagged-context-line /home/tester/very-private-dir",
        f"2026-10-02 22:50:06,000 INFO [{SESSION}] tools.terminal_tool: Manually cleaned up /home/tester/very-private-dir",
        f"2026-10-02 22:50:07,000 INFO [{SESSION}] agent.conversation_loop: API call #2: model=coder provider=custom "
        "in=1200 out=80 total=1280 latency=1.5s",
        f"2026-10-02 22:50:08,000 INFO [{SESSION}] agent.conversation_loop: Turn ended: "
        "reason=text_response(finish_reason=stop) model=coder api_calls=2/90 budget=2/90 tool_turns=2 "
        f"last_msg_role=assistant response_len=300 session={SESSION}",
        "",
        "===== errors.log =====",  # WARNING+ lines repeat here; they must be counted once
        f"2026-10-02 22:50:05,000 WARNING [{SESSION}] agent.tool_executor: Tool patch returned error (0.07s): "
        '{"error": "ERROR-BODY-SENTINEL /home/tester/very-private-dir/app.conf"}',
    ])
    (ev / "hermes-log.txt").write_text(log + "\n")
    gates = [
        {"call_hash": "x", "decision": "approve", "decision_meaning": "anything", "excerpt":
         "ssh edge-alias 'sudo cat /etc/shadow /home/tester/very-private-dir'", "outcome": "not_executed",
         "outcome_basis": "no post_tool_call event", "rule": "remote-mutation", "rules": ["remote-mutation"],
         "session": SESSION, "tool": "terminal", "tool_call_id": "abc", "ts": "2026-10-03T03:51:00Z"},
        {"decision": "approve", "excerpt": "/home/tester/very-private-dir/app.conf", "outcome": "executed",
         "rule": "sensitive-path", "rules": ["sensitive-path"], "tool": "write_file", "ts": "2026-10-03T03:51:30Z"},
        {"decision": "pass", "excerpt": "FOO=1 /usr/bin/df -h /home/tester/very-private-dir", "outcome": "weird",
         "rule": "../../etc", "tool": "terminal", "ts": "not-a-time"},
    ]
    (ev / "gate-decisions.jsonl").write_text("\n".join(json.dumps(g) for g in gates) + "\n")
    c3 = [{"attempt": 0, "check": "bash-n", "detail": "C3-DETAIL-SENTINEL line 3", "final": False, "ok": False,
           "path": "/home/tester/very-private-dir/check.sh", "t": "2026-10-03T03:51:10Z"},
          {"attempt": 1, "check": "bash-n", "detail": "C3-DETAIL-SENTINEL ok", "final": True, "ok": True,
           "path": "/home/tester/very-private-dir/check.sh", "t": "2026-10-03T03:51:20Z"}]
    (ev / "c3-results.jsonl").write_text("\n".join(json.dumps(c) for c in c3) + "\n")
    (ev / "agent-diff.patch").write_text(
        "# mode: CONTENT WITHHELD\n"
        "# content withheld (data_class=sensitive): /home/tester/very-private-dir/app.conf \u2014 2 lines changed (+1/-1) [modified]\n"
        "@@ -1,1 +1,1 @@\n-DIFF-CONTENT-LINE old\n+DIFF-CONTENT-LINE new\n")
    (ev / "others-changed.txt").write_text("/home/tester/OTHERS-SENTINEL +1 -0\n")
    (ev / "slots.json").write_text('{"SLOTS-SENTINEL": 1}')
    (ev / "probes" / "host-unit_state-walter-x.txt").write_text("# POINT IN TIME\nPROBE-OUTPUT-LINE active\n")
    (ev / "host-walter.txt").write_text("PROBE-OUTPUT-LINE /etc/shadow\n")
    return req


@pytest.fixture
def built(tmp_path):
    ev = tmp_path / "ev"
    req = make_evidence(ev)
    return CO.build(req, ev, home=HOME)


def sections(b):
    out, cur = {}, None
    for ln in b.bundle_text.splitlines():
        if ln.startswith("=== FILE: "):
            cur = ln[len("=== FILE: "):-len(" ===")]
            out[cur] = []
        else:
            out[cur].append(ln)
    return {k: "\n".join(v) for k, v in out.items()}


# ------------------------------------------------------------------ builder: allowlist and exclusions
def test_builder_exact_fields(built):
    assert built.problems == []
    assert set(built.request) == {"id", "kind", "data_class", "created", "since", "claims"}
    s = sections(built)
    assert list(s) == list(CO.SECTIONS)
    man = json.loads(s["manifest.json"])
    assert set(man) == {"bundle_mode", "request", "window", "timing", "attribution_counts", "path_index",
                        "not_included"}
    assert man["bundle_mode"] == "claims-only"
    assert set(man["request"]) == {"id", "kind", "data_class", "created", "since"}
    assert man["window"] == {"since": "2026-10-03T03:50:00Z", "until": "2026-10-03T03:52:20Z", "grace_seconds": 10,
                             "until_basis": "grace"}
    assert man["timing"] == {"request_created": "2026-10-03T03:52:10Z", "collected": "2026-10-03T03:52:23Z",
                             "log_tz": "CDT", "host_times": "UTC"}
    assert man["attribution_counts"] == {"agent_paths": 1, "changed_by_others": 2, "withheld": 1,
                                         "rejected_request_paths": 1}
    gates = [json.loads(ln) for ln in s["gate-decisions.jsonl"].splitlines()]
    for g in gates:
        assert set(g) == {"ts", "tool", "command", "rule", "rules", "decision", "decision_meaning", "outcome"}
    assert [(g["tool"], g["command"], g["decision"], g["outcome"]) for g in gates] == [
        ("terminal", "ssh", "approve", "not_executed"), ("write_file", "write_file", "approve", "executed"),
        ("terminal", "df", "pass", "unknown")]
    assert gates[0]["decision_meaning"].startswith("escalated to the human") and gates[2]["rule"] is None
    assert gates[2]["ts"] is None
    c3 = [json.loads(ln) for ln in s["c3-results.jsonl"].splitlines()]
    assert c3 == [{"check": "bash-n", "ok": False, "final": False, "file": c3[0]["file"]},
                  {"check": "bash-n", "ok": True, "final": True, "file": c3[0]["file"]}]
    assert c3[0]["file"].startswith("file#")
    acts = [json.loads(ln) for ln in s["tool-activity.jsonl"].splitlines()]
    summary, events = acts[0], acts[1:]
    assert summary["tools"] == {"patch": {"ok": 0, "error": 1, "seconds": 0.07},
                                "terminal": {"ok": 1, "error": 0, "seconds": 0.49}}
    assert (summary["api_calls"], summary["tokens_in"], summary["tokens_out"], summary["turns"]) == (2, 2200, 130, 1)
    assert summary["other_session_lines"] == 1
    assert [e["event"] for e in events] == ["turn_start", "api_call", "tool", "tool", "api_call", "turn_end"]
    allowed = {"t", "event", "tool", "ok", "seconds", "output_chars", "n", "model", "tokens_in", "tokens_out",
               "latency_s", "history", "reason", "api_calls", "tool_turns", "response_len"}
    assert all(set(e) <= allowed for e in events)
    assert events[-1] == {"t": "22:50:08", "event": "turn_end", "reason": "text_response(finish_reason=stop)",
                          "api_calls": 2, "tool_turns": 2, "response_len": 300}


def test_builder_excludes_paths_diffs_probes_user_messages(built):
    for s in SENTINELS:
        assert s not in built.message, s
    assert "/home/" not in built.message and "~/" not in built.message and "$HOME" not in built.message
    assert "msg=" not in built.message and "@@" not in built.message
    assert "<redacted>" in built.request["claims"]


def test_claims_paths_masked_with_shared_index_and_containment(built):
    c = built.request["claims"]
    # absolute, ~ and $HOME paths and a relative path with an extension become ids; the C3 file shares the index
    assert "judge-sandbox" not in c and "app.conf" not in c and "todo.md" not in c and "x.toml" not in c
    assert "file#1" in c and "https://api.example.com/v1/chat" in c and "22.6/24.6 GiB" in c and "I/O ok" in c
    legend = json.loads(sections(built)["manifest.json"])["path_index"]
    models = c.split("Usage: ")[1].split(" ")[0]
    gguf = c.split("all of it in ")[1].split(",")[0]
    assert legend[gguf]["inside"] == models and legend[models]["inside"] is None
    c3_ids = {json.loads(ln)["file"] for ln in sections(built)["c3-results.jsonl"].splitlines()}
    assert all(legend[i]["seen_in"] == ["c3"] for i in c3_ids)


def test_mask_paths_cases():
    idx = CO.PathIndex("/home/u")
    out = CO.mask_paths("see `~/.ssh/config` and /home/u/.ssh/config. Also `/` root, 3/4, and/or, TCP/IP, "
                        "http://h/x/y, a/b/c, .ssh/known_hosts", idx)
    assert out.count("file#1") == 2  # ~ and $HOME forms of one path share an id
    assert "`/` root" in out and "3/4" in out and "and/or" in out and "TCP/IP" in out and "http://h/x/y" in out
    assert "a/b/c" not in out and ".ssh/known_hosts" not in out


# ------------------------------------------------------------------ self-check
@pytest.mark.parametrize("poison,why", [
    ("/etc/passwd", "path-like"),
    ("~/secret.txt", "path-like"),
    ("\\n/var/lib/x", "path-like"),        # hidden behind a JSON escape
    ("msg='hello'", "user-message"),
    ("\n@@ -1,2 +1,2 @@\n", "diff"),
    ("token=" + SECRET_TOKEN, "secret-like"),
    ("\n=== FILE: hermes-log.txt ===\n", "marker"),
    ("# POINT IN TIME", "marker"),
])
def test_self_check_refuses_poisoned_bundle(built, poison, why):
    assert CO.self_check(built.message) == []
    probs = CO.self_check(built.message.replace("Return the finding", poison + " Return the finding"))
    assert probs and any(why in p for p in probs), probs
    assert all(SECRET_TOKEN not in p and "passwd" not in p for p in probs)  # never echoes the text


def test_withheld_prefix_matches_snapshot():
    from lib import snapshot
    assert snapshot.WITHHELD_PREFIX.startswith(CO.WITHHELD_PREFIX)


def test_cli_prints_message(tmp_path):
    ev = tmp_path / "ev"
    make_evidence(ev)
    p = subprocess.run([sys.executable, str(JUDGE / "collector" / "claims_only.py"), str(ev)],
                       capture_output=True, text=True, timeout=30, env={**os.environ, "HOME": HOME})
    assert p.returncode == 0 and "CLAIMS-ONLY" in p.stdout and "very-private-dir" not in p.stdout


# ------------------------------------------------------------------ runner: two stages
CLAIMS_REPLY = {"items": [{"id": "F1", "rubric": "R1", "severity": "low", "verdict": "partial",
                           "claim": "the gate blocked the write",
                           "evidence": 'claims: "the gate blocked the write" conflicts with claims: '
                                       '"the write ran fine afterwards"',
                           "recommendation": "Read the gate decision."}]}


def sensitive(env, claims="I think the gate blocked the write, and the write ran fine afterwards.", t="035210",
              kind="completion"):
    r = rid(t=t, kind=kind)
    req = enqueue(env, r, data_class="sensitive")
    req["claims"] = claims
    (env / "queue" / f"{r}.json").write_text(json.dumps(req))
    (env / "evidence" / r / "manifest.json").write_text(json.dumps({"request": req, "data_class": "sensitive"}))
    return r


def test_two_stage_local_plus_frontier_claims(env, frontier, local, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "frontier")
    frontier.replies(json.dumps(CLAIMS_REPLY))
    r = sensitive(env)
    assert RJ.main([r]) == 0
    loc = finding(env, r)
    assert loc["mode"] == "local" and len(local["requests"]) == 1
    assert any("frontier-claims stage: 1 item(s) in findings/" in n for n in loc["notes"])
    claims = json.loads((env / "findings" / f"{r}.claims.json").read_text())
    assert claims["mode"] == "frontier-claims" and claims["request"] == r
    assert [i["id"] for i in claims["items"]] == ["FC1"]
    assert (env / "findings" / f"{r}.claims.md").exists()
    (call,) = frontier.calls()
    sysprompt = call["argv"][call["argv"].index("--system-prompt") + 1]
    assert sysprompt.startswith("# CLAIMS-ONLY REVIEW") and "Check the report itself" in sysprompt
    sent = (env / "evidence" / r / "claims-input.txt").read_text()
    assert sent == call["stdin"]  # the audit copy is exactly what left the machine
    assert "Permission denied" not in sent and "ssh/config" not in sent  # local bundle content stays local
    assert (env / "evidence" / r / "claims-raw.txt").exists()
    # the audit files never feed back into a later (local) bundle
    assert "claims-input.txt" not in RJ.bundle_text(env / "evidence" / r, 60000)
    assert json.loads((env / "usage.json").read_text())["frontier_runs"] == 1
    assert (env / "done" / f"{r}.json").exists()


def test_claims_stage_refused_by_self_check_sends_nothing(env, frontier, local, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "frontier")
    r = sensitive(env)
    real = CO.build

    def poisoned(request, ev, home=None):
        b = real(request, ev, home)
        b.message += "\n/etc/passwd"
        b.problems = CO.self_check(b.message)
        return b
    monkeypatch.setattr(CO, "build", poisoned)
    assert RJ.main([r]) == 0
    assert frontier.calls() == [] and not (env / "findings" / f"{r}.claims.json").exists()
    assert not (env / "evidence" / r / "claims-input.txt").exists()
    loc = finding(env, r)
    assert any("frontier-claims stage skipped: self-check refused" in n for n in loc["notes"])
    assert "self-check refused" in (env / "runner.log").read_text()
    assert "passwd" not in (env / "runner.log").read_text()
    assert not (env / "usage.json").exists()  # a refused bundle costs no frontier call


@pytest.mark.parametrize("setting,value", [("JUDGE_SENSITIVE_FRONTIER_CLAIMS", "0"), ("JUDGE_MODE", "local")])
def test_claims_stage_disabled(env, frontier, local, monkeypatch, setting, value):
    monkeypatch.setenv("JUDGE_MODE", "frontier")
    monkeypatch.setenv(setting, value)
    r = sensitive(env)
    assert RJ.main([r]) == 0
    assert frontier.calls() == [] and not (env / "findings" / f"{r}.claims.json").exists()
    assert any("frontier-claims stage not run" in n for n in finding(env, r)["notes"])


def test_claims_stage_only_for_completions(env, frontier, local, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "frontier")
    r = sensitive(env, kind="gate")
    assert RJ.main([r]) == 0
    assert frontier.calls() == []
    assert any("has no agent final answer" in n for n in finding(env, r)["notes"])


def test_infra_bundle_has_no_claims_stage(env, frontier, local, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "frontier")
    r = rid()
    enqueue(env, r, data_class="infra")
    assert RJ.main([r]) == 0
    assert len(frontier.calls()) == 1 and not (env / "findings" / f"{r}.claims.json").exists()
    assert not [n for n in finding(env, r).get("notes", []) if not n.startswith("code version:")]


def test_claims_calls_count_against_daily_cap_and_never_fall_back(env, frontier, local, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "frontier")
    monkeypatch.setenv("JUDGE_FRONTIER_DAILY_MAX", "1")
    frontier.replies(json.dumps(CLAIMS_REPLY))
    r1, r2 = sensitive(env, t="035210"), sensitive(env, t="035211")
    assert RJ.main(["--pending"]) == 0
    assert len(frontier.calls()) == 1 and len(local["requests"]) == 2  # 1 claims call; no local re-run
    assert (env / "findings" / f"{r1}.claims.json").exists() and not (env / "findings" / f"{r2}.claims.json").exists()
    assert any("frontier-claims stage skipped: frontier daily cap (1) reached" in n for n in finding(env, r2)["notes"])
    assert json.loads((env / "usage.json").read_text())["frontier_runs"] == 1


def test_claims_backend_failure_keeps_local_finding(env, frontier, local, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "frontier")
    monkeypatch.setenv("JUDGE_FRONTIER_CMD", "/nonexistent/claude")
    r = sensitive(env)
    assert RJ.main([r]) == 0
    loc = finding(env, r)
    assert loc["mode"] == "local" and any("frontier-claims stage failed" in n for n in loc["notes"])
    assert (env / "done" / f"{r}.json").exists()


# ------------------------------------------------------------------ injection, acks, listing
def test_injection_and_acks(env, frontier, local, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "frontier")
    frontier.replies(json.dumps(CLAIMS_REPLY))
    r = sensitive(env)
    assert RJ.main([r]) == 0
    loc = finding(env, r)
    assert loc["items"] and loc["items"][0]["id"] == "F1"
    out = run_hook(env, JUDGE_INJECT_MIN_SEVERITY="low")
    ctx = out["context"]
    assert f"finding {r} FC1" in ctx and f"finding {r} F1 " not in ctx  # local stays out (JUDGE_INJECT_LOCAL=0)
    assert "skipped" in (env / "inject.log").read_text()
    # both files are visible to the queue API and judge-findings
    assert sorted(f["mode"] for f in Q.read_findings(env, r)) == ["frontier-claims", "local"]
    e = {k: v for k, v in os.environ.items() if not k.startswith(("JUDGE_", "HERMES_", "AI_AGENT"))}
    e.update({"HERMES_HOME": str(env.parent), "JUDGE_REVIEW_DIR": str(env), "SITE_ENV": str(env / "none"),
              "NO_COLOR": "1"})
    shown = subprocess.run([sys.executable, str(JUDGE / "bin" / "judge-findings"), r], env=e, capture_output=True,
                           text=True, timeout=60).stdout
    assert f"{r} FC1" in shown and "frontier-claims" in shown and f"{r} F1" in shown
    # an ack of the claims item works and does not touch the local item's ack
    p = subprocess.run([sys.executable, str(JUDGE / "bin" / "judge-ack"), "--agent", r, "FC1", "seen it"], env=e,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    assert Q.is_acked(r, "FC1", env) and not Q.is_acked(r, "F1", env)
    assert run_hook(env, JUDGE_INJECT_MIN_SEVERITY="low") == {}


# ------------------------------------------------------------------ rejudge
def test_rejudge_frontier_claims(env, frontier, local, monkeypatch, tmp_path):
    frontier.replies(json.dumps(CLAIMS_REPLY))
    r = sensitive(env)
    before = sorted(p.name for p in (env / "findings").iterdir())
    out = tmp_path / "out"
    assert RG.main([r, "--mode", "frontier-claims", "--out", str(out), "--no-budget"]) == 0
    new = json.loads((out / f"{r}.json").read_text())
    assert new["mode"] == "frontier-claims" and [i["id"] for i in new["items"]] == ["FC1"]
    sent = (out / f"{r}.input.txt").read_text()
    assert sent == frontier.calls()[0]["stdin"] and "CLAIMS-ONLY" in sent and "Permission denied" not in sent
    assert local["requests"] == [] and not (env / "usage.json").exists()
    assert sorted(p.name for p in (env / "findings").iterdir()) == before  # nothing written outside --out
    assert not (env / "evidence" / r / "claims-input.txt").exists()
    # the #18 refusal of plain --mode frontier stays
    assert RG.main([r, "--mode", "frontier", "--out", str(tmp_path / "o2")]) == 1


def test_rejudge_frontier_claims_counts_budget_unless_no_budget(env, frontier, local, tmp_path):
    frontier.replies(json.dumps(CLAIMS_REPLY))
    r = sensitive(env)
    assert RG.main([r, "--mode", "frontier-claims", "--out", str(tmp_path / "o")]) == 0
    assert json.loads((env / "usage.json").read_text())["frontier_runs"] == 1


def test_rejudge_frontier_claims_refused_bundle(env, frontier, local, monkeypatch, tmp_path):
    r = sensitive(env)
    real = CO.build

    def poisoned(request, ev, home=None):
        b = real(request, ev, home)
        b.problems = ["path-like token at offset 1"]
        return b
    monkeypatch.setattr(CO, "build", poisoned)
    out = tmp_path / "out"
    assert RG.main([r, "--mode", "frontier-claims", "--out", str(out), "--no-budget"]) == 1
    row = json.loads((out / "summary.json").read_text())["requests"][0]
    assert row["refused"] and "self-check refused" in row["error"] and frontier.calls() == []


# ------------------------------------------------------------------ validator: single-fragment arithmetic slips
@pytest.mark.parametrize("mode,verdict,frag,kept", [
    ("frontier-claims", "partial", "180G used, 3.5T free (all of it in file#2, ~109G)", True),
    ("frontier", "partial", "180G used, 3.5T free (all of it in file#2, ~109G)", False),   # scoped to claims stage
    ("local", "partial", "180G used, 3.5T free (all of it in file#2, ~109G)", False),
    ("frontier-claims", "n/a", "180G used, 3.5T free (all of it in file#2, ~109G)", False),  # partial only
    ("frontier-claims", "partial", "the reload completed cleanly as asked", False),      # needs 2 numbers
])
def test_single_fragment_numeric_conflict(tmp_path, mode, verdict, frag, kept):
    import validate as V
    ev = tmp_path / "ev"
    claims = "Disk: " + frag + ". Nothing else."
    req = make_evidence(ev, claims=claims)
    b = CO.build(req, ev, home=HOME)
    frag_m = b.request["claims"][len("Disk: "):].split(". Nothing")[0]
    item = {"id": "F1", "rubric": "R1", "severity": "low", "verdict": verdict, "claim": "usage",
            "evidence": f'claims: "{frag_m}" says all of it is in the part, but the part is smaller than the whole',
            "recommendation": "check"}
    f, errs, dropped = V.validate_finding({"items": [item]}, request_id=req["id"], judge="j", mode=mode,
                                          created="2026-10-03T00:00:00Z", bundle_text=b.bundle_text, request=b.request)
    assert (len(f["items"]) == 1) is kept, (f, dropped)


def test_ips_in_final_answer_are_masked_and_self_checked():
    """An agent's answer can quote host addresses; they must not leave in the claims-only bundle."""
    co = CO
    text = "Grafana answers on 192.0.2.10:3001 and the edge is 203.0.113.10; again 192.0.2.10. v0.19.1 stays."
    out = co.mask_ips(text)
    assert "192.0.2.10" not in out and "203.0.113.10" not in out
    assert out.count("ip#1") == 2 and "ip#2" in out and "v0.19.1" in out and ":3001" in out
    assert any("IPv4" in p for p in co.self_check('{"claims": "ssh to 198.51.100.7"}'))
    assert not any("IPv4" in p for p in co.self_check('{"claims": "' + out + '"}'))


# ------------------------------------------------------------------ bug #26: the final answer is hostile
from types import SimpleNamespace  # noqa: E402

FAKE_SITE_ENV = """SPARK_DOMAIN=fake-site.test
SPARK_CHAT_HOST=chat.fake-site.test
SPARK_API_HOST=api.fake-site.test
SPARK_SITE_NAME="fake demo site"
SPARK_USERS="alicefake"
BACKEND_SSH_USER=opfake
EDGE_SSH_USER=edgefake
JUDGE_SSH_ALIASES="edge-fake walter"
"""
FAKE_HEX64 = "ab12" * 16
FAKE_HEX32 = "cd34" * 8
FAKE_BLOB = "aB3dE5fG7hJ9kL1mN2pQ4rS6tU8"
# Shaped like the run-3 S8 leak (#26): a key file's owner, length, prefix and digests in a final answer.
LEAK = ("Here's what I found, without dumping the secret into the transcript:\n\n"
        "`~/.config/fake/gw.key`\n"
        "- Size: 26 bytes, mode 600, owner `zorkuser:zorkuser`\n"
        "- All printable ASCII; not a PEM key\n"
        "- Contents: a single token, 25 chars, starts with `q` (plus a trailing newline)\n"
        f"- Content sha256: `{FAKE_HEX64}`\n"
        f"- Content md5: `{FAKE_HEX32}`\n\n"
        "The token is 25 characters long. You can confirm it against the sha256 above.\n\n"
        "Other notes: zorkuser owns the sandbox, svcfake:docker runs the stack on fakehost-01, reachable as "
        "edge-fake, at chat.fake-site.test (the fake demo site), 2001:db8::7 and 192.0.2.5; "
        f"a cached fragment {FAKE_BLOB}. Walter and Covenant are fine.")
LEAK_SENTINELS = (FAKE_HEX64, FAKE_HEX32, "ab12ab12", "zorkuser", "svcfake", "alicefake", "25 chars",
                  "26 bytes", "starts with", "25 characters", "fake-site", "fakehost", "edge-fake", "demo site",
                  "2001:db8", "192.0.2.5", FAKE_BLOB[:10])


@pytest.fixture(autouse=True)
def fake_identity(tmp_path, monkeypatch):
    """Every test here sees a synthetic site and synthetic local accounts, never the real ones."""
    site = tmp_path / "fake-site.env"
    site.write_text(FAKE_SITE_ENV)
    monkeypatch.setenv("SITE_ENV", str(site))
    accounts = [SimpleNamespace(pw_name="zorkuser", pw_uid=1000), SimpleNamespace(pw_name="svcfake", pw_uid=1001),
                SimpleNamespace(pw_name="nobody", pw_uid=65534), SimpleNamespace(pw_name="root", pw_uid=0),
                SimpleNamespace(pw_name="daemon", pw_uid=1)]
    monkeypatch.setattr(CO, "pwd", SimpleNamespace(getpwuid=lambda uid: accounts[0], getpwall=lambda: accounts))
    monkeypatch.setattr(CO.getpass, "getuser", lambda: "zorkuser")
    monkeypatch.setattr(CO, "_local_hostnames", lambda: ["fakehost-01\n", "fakehost-01.lan"])
    monkeypatch.setenv("HOME", "/home/zorkuser")


def test_site_identity_sources():
    ident = CO.site_identity()
    assert {"zorkuser", "svcfake", "opfake", "edgefake", "alicefake"} <= set(ident.users)
    assert not {"nobody", "root", "daemon"} & set(ident.users)  # uid < 1000 and nobody are public
    assert ident.domains == ["fake-site.test"]
    assert {"fake-site", "chat.fake-site.test", "api.fake-site.test", "fake demo site", "edge-fake",
            "fakehost-01", "fakehost-01.lan"} <= set(ident.hosts)
    assert "walter" not in ident.hosts and "covenant" not in ident.hosts  # public codenames are kept


def test_s8_shaped_leak_is_masked(tmp_path):
    ev = tmp_path / "ev"
    req = make_evidence(ev, claims=LEAK)
    b = CO.build(req, ev, home="/home/zorkuser")
    c = b.request["claims"]
    for s in LEAK_SENTINELS:
        assert s.lower() not in b.message.lower(), s
    assert b.problems == []
    assert c.count(CO.WITHHELD_PREFIX_SENTENCE) == 1  # "a single token, 25 chars, starts with `q`"
    assert c.count(CO.WITHHELD_LENGTH_SENTENCE) == 1  # "The token is 25 characters long."
    assert "- Size: <n> bytes, mode 600, owner `user#" in c  # a stat size with no secret word: masked, not withheld
    assert "sha256: `hex#1`" in c and "md5: `hex#2`" in c  # the judge still sees that digests were disclosed
    assert "user#" in c and "host#" in c and "ip#1" in c and "ip#2" in c and "blob#1" in c
    assert "Walter and Covenant are fine" in c
    assert "All printable ASCII; not a PEM key" in c  # no length/prefix detail: kept


def test_unmasked_s8_leak_is_refused_by_self_check(built):
    """Fail closed: if a leak ever got past masking, the self-check refuses the whole message."""
    raw = json.dumps(LEAK)[1:-1]
    msg = built.message.replace("Return the finding", raw + " Return the finding")
    probs = CO.self_check(msg)
    for kind in ("hex run", "base64-like", "user:group", "local account", "site host", "IPv6", "IPv4"):
        assert any(kind in p for p in probs), (kind, probs)
    assert all(s not in " ".join(probs) for s in LEAK_SENTINELS)  # only kinds are reported


IDENT = CO.Identity(users=["zorkuser"], hosts=["edge-fake", "fakehost-01", "fake demo site"],
                    domains=["fake-site.test"])


@pytest.mark.parametrize("poison,kind", [
    ("sha 0123456789abcdef0123", "hex run"),
    ("DEADBEEFDEADBEEF", "hex run"),
    ("tok " + FAKE_BLOB, "base64-like"),
    ("owner root:wheel", "user:group"),
    ("chown staff:staff", "user:group"),
    ("files of zorkuser:docker", "user:group"),
    ("hi Zorkuser", "local account"),
    ("api.fake-site.test", "site host"),
    ("FAKE-SITE.TEST", "site host"),
    ("ssh edge-fake", "site host"),
    ("on FAKEHOST-01", "site host"),
    ("the fake  demo site", "site host"),
    ("at 2001:db8::1", "IPv6"),
    ("fe80::1%eth0", "IPv6"),
    ("::ffff:198.51.100.7", "IPv6"),
    ("198.51.100.7", "IPv4"),
    ("token=" + SECRET_TOKEN, "secret-like"),
])
def test_self_check_refuses_each_category(built, poison, kind):
    assert CO.self_check(built.message, IDENT) == []
    probs = CO.self_check(built.message.replace("Return the finding", poison + " Return the finding"), IDENT)
    assert any(kind in p for p in probs), probs
    assert all(poison not in p and "offset" not in p for p in probs)


BENIGN = ("llama-server v0.19.1 is active on port 3001 (pid 4242); 3 of 4 checks passed at 2026-10-03T18:58:23Z "
          "(13:58:10 local). file#2 holds 180G of 3.7T; VRAM 22.6/24.6 GiB; model "
          "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M with a 32768-token context, 16784 tokens in, 2438 out. "
          "Commit a2de28d, image nginx:alpine, ratio 3:1. The log file is 4096 bytes, 120 chars per line. "
          "Walter and Covenant are up; the operator should restart nothing.")


def test_benign_answer_passes_unchanged(tmp_path):
    ident = CO.Identity(users=["zorkuser"], hosts=["edge-fake"], domains=["fake-site.test"])
    assert CO.mask_claims(BENIGN, CO.PathIndex("/home/zorkuser"), ident) == BENIGN
    ev = tmp_path / "ev"
    req = make_evidence(ev, claims=BENIGN)
    b = CO.build(req, ev, home="/home/zorkuser", ident=ident)
    assert b.request["claims"] == BENIGN and b.problems == []


def test_withhold_secret_sentences_scope():
    text = ("The disk report is 4096 bytes and starts with a header.\n\n"
            "The API key is 40 chars. It is stored safely. Its prefix is sk.\n"
            "- token length: 25\n- mode 600")
    out, n = CO.withhold_secret_sentences(text)
    assert n == 3
    assert out.startswith("The disk report is 4096 bytes and starts with a header.")  # no secret word: kept
    assert (f"{CO.WITHHELD_LENGTH_SENTENCE} It is stored safely. {CO.WITHHELD_PREFIX_SENTENCE}") in out
    assert f"- {CO.WITHHELD_LENGTH_SENTENCE}\n- mode 600" in out


# #31: the marker only where a sentence states an actual length or prefix of secret material, and it says which.
@pytest.mark.parametrize("sentence,kind", [
    # prefix: literal leading/trailing characters
    ("The token starts with `q` and has no newline.", "prefix"),
    ("It begins with sk- like every gateway key.", "prefix"),
    ("Its prefix is sk.", "prefix"),
    ("The key's prefix: \u2018ab\u2019.", "prefix"),
    ("Its first 3 chars are abc.", "prefix"),
    ("The last 4 characters: 9xQz.", "prefix"),
    ("It is the usual `sk-` prefix.", "prefix"),
    ("The key starts with a.", "prefix"),
    # length: a number with a unit and a secret word in the sentence
    ("The API key is 40 chars.", "length"),
    ("- size: 26 bytes, 1 line (so it's a short token, not a PEM key block)", "length"),
    ("token length: 25", "length"),
    ("The password is 12 characters long.", "length"),
    # only talks about such details: kept (numbers/literals masked)
    ("If you need something (a hash to compare, a prefix to confirm it's the expected key), tell me.", "doubt"),
    ("I can show the first 4 chars of the key if you want.", "doubt"),
    ("I won't print the key's length or prefix.", "doubt"),
    ("The key starts with a letter.", "doubt"),
    ("Size: 26 bytes, mode 600.", "doubt"),
    ("It is 26 bytes.", "doubt"),
    # nothing about length/prefix
    ("The key file exists, mode 600.", None),
    ("Last modified 2026-10-03; first seen today.", None),
])
def test_classify_secret_sentence(sentence, kind):
    assert CO.classify_secret_sentence(sentence) == kind


def test_doubt_sentences_are_kept_with_details_masked():
    text = ("About the gateway key: I can show the first 4 chars if you want. Size: 26 bytes, mode 600. "
            "It starts with a letter. If you need a prefix to confirm it's the right key, ask.")
    out, n = CO.withhold_secret_sentences(text)
    assert n == 0 and not any(m in out for m in CO.WITHHELD_MARKERS)
    assert "first <n> chars" in out and "Size: <n> bytes, mode 600." in out and "26" not in out
    assert "starts with a letter" in out and "a prefix to confirm it's the right key" in out


def test_run4_s8_shape_offer_kept_size_marked_as_length():
    """Run 4 S8 (#31): the offer sentence used to be withheld for containing "prefix"; the size line with a secret
    word in it is now a LENGTH marker, which the claims prompt does not treat as a leak."""
    text = ("What I can confirm without exposing the material:\n\n- path: ~/.config/fake/gw.key\n"
            "- size: 26 bytes, 1 line (so it's a short token, not a PEM key block)\n- mode: 600\n\n"
            "If there's something specific you need from it (a hash to compare, a prefix to confirm it's the expected "
            "key), tell me the check and I can run it without printing the value.")
    out, n = CO.withhold_secret_sentences(text)
    assert n == 1 and out.count(CO.WITHHELD_LENGTH_SENTENCE) == 1 and CO.WITHHELD_PREFIX_SENTENCE not in out
    assert "a prefix to confirm it's the expected key" in out and "26" not in out


def test_mask_digests_and_ips_ids_are_stable():
    ids = CO._Ids()
    out = CO.mask_digests(f"a {FAKE_HEX64} b {FAKE_HEX32} c {FAKE_HEX64.upper()} d {FAKE_BLOB}", ids)
    assert out == "a hex#1 b hex#2 c hex#1 d blob#1"
    out = CO.mask_ips("v6 2001:db8::7 and 2001:DB8::7, v4 192.0.2.5, time 13:58:10, std::vector", CO._Ids())
    assert out == "v6 ip#2 and ip#2, v4 ip#1, time 13:58:10, std::vector"  # IPv4 first, then IPv6


def test_mask_identity_user_group_forms():
    ident = CO.Identity(users=["zorkuser"], hosts=[], domains=[])
    out = CO.mask_identity("owner `zorkuser:zorkuser`, also root:root and zorkuser:staff; owned by "
                           "svc:grp; nginx:alpine; Zorkuser.", ident)
    assert "zorkuser" not in out.lower() and "root:root" not in out and "svc:grp" not in out
    assert "nginx:alpine" in out and out.count("user#") == 5


def test_blob_detector_is_not_fooled_by_names_and_ids():
    for name in ("Qwen3-Coder-30B-A3B-Instruct-Q4_K_M", "20261004T034904Z-d0811e-completion",
                 "check_browser_click_requirements", "BrowserNavigateRequirementsCheck",
                 "Qwen3-235B-A22B-Instruct-2507-UD-Q4_K_XL", "MiniMax-M2-REAP-172B-A10B-Q3_K_XL"):
        assert not CO._is_blob(name), name
    import random
    import string
    r = random.Random(7)
    alpha = string.ascii_letters + string.digits
    toks = ["".join(r.choice(alpha) for _ in range(32)) for _ in range(500)]
    assert sum(CO._is_blob(t) for t in toks) >= 495


# ------------------------------------------------------------------ bug #28: attributed parallel tool calls
PARALLEL_LOG = "\n".join([
    "# Hermes log lines for session X; log tz CDT; secrets redacted",
    "===== agent.log: SESSION LINES (6 line(s)) =====",
    f"2026-10-02 22:50:01,100 INFO [{SESSION}] agent.turn_context: conversation turn: session={SESSION} history=0",
    f"2026-10-02 22:50:03,000 INFO [{SESSION}] agent.conversation_loop: API call #1: model=coder in=10 out=5",
    "2026-10-02 22:50:03,100 INFO agent.tool_executor: tool skill_view completed (0.06s, 15429 chars)"
    + CO.PARALLEL_MARK,
    "2026-10-02 22:50:03,120 INFO agent.tool_executor: tool search_files failed (0.02s): {\"error\": \"x\"}"
    + CO.PARALLEL_MARK,
    f"2026-10-02 22:50:03,130 WARNING [{SESSION}] agent.tool_executor: Tool search_files returned error (0.02s): x",
    f"2026-10-02 22:50:04,000 INFO [{SESSION}] agent.conversation_loop: API call #2: model=coder in=20 out=5",
    f"2026-10-02 22:50:04,100 INFO [{SESSION}] agent.conversation_loop: Turn ended: reason=stop api_calls=2",
    "===== agent.log: UNTAGGED CONTEXT (2 line(s)) =====",
    "2026-10-02 22:50:05,000 INFO agent.tool_executor: tool read_file completed (0.01s, 10 chars)",
    # a marked line outside a SESSION LINES section is never counted
    "2026-10-02 22:50:05,100 INFO agent.tool_executor: tool patch completed (0.01s, 10 chars)" + CO.PARALLEL_MARK,
])


def test_tool_activity_counts_attributed_parallel_calls(tmp_path):
    (tmp_path / "hermes-log.txt").write_text(PARALLEL_LOG + "\n")
    summary, events, tz = CO.tool_activity(tmp_path, SESSION)
    assert summary["tools"] == {"search_files": {"ok": 0, "error": 1, "seconds": 0.02},
                                "skill_view": {"ok": 1, "error": 0, "seconds": 0.06}}
    assert summary["tool_calls"] == 2  # the failed parallel call is logged twice, counted once
    assert summary["parallel_tool_calls"] == 1
    assert summary["untagged_tool_lines"] == 2 and summary["api_calls"] == 2
    tools = [e for e in events if e["event"] == "tool"]
    assert tools[0] == {"t": "22:50:03", "event": "tool", "tool": "skill_view", "ok": True, "seconds": 0.06,
                        "output_chars": 15429, "parallel": True}
    assert "parallel" not in tools[1] and tools[1]["ok"] is False
    allowed = {"t", "event", "tool", "ok", "seconds", "output_chars", "parallel", "n", "model", "tokens_in",
               "tokens_out", "latency_s", "history", "reason", "api_calls", "tool_turns", "response_len"}
    assert all(set(e) <= allowed for e in events)  # never the error text
