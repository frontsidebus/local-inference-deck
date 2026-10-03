"""Regression tests for the evidence bugs found in automated run 2 (#19, #20, #21, #24).

#19: hermes-log.txt carried ~26K chars of untagged startup lines of OTHER Hermes processes before the first
     session line, and the runner head-truncated every file to its per-file share, so the judge saw zero
     session lines (S3/S4/S9). Fixed at both ends: the collector writes session lines first and drops
     startup noise; the runner never truncates session lines or gate decisions away.
#20: verify.py::since_for reached 5 minutes before the first edit even when the session started later.
#21: bundles were collected the moment a request appeared, before its grace window had passed.
#24: the unit_journal probe printed `ssh covenant` while unit_state printed the real `user@address` argv.

Everything runs under tmp_path; no network, no real ssh, no real sleeping.
"""
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from collector_testlib import JUDGE_DIR, FakeRunner, make_env

sys.dont_write_bytecode = True
sys.path.insert(0, str(JUDGE_DIR / "runner"))

import collect  # noqa: E402
import probe  # noqa: E402
import run_judge as RJ  # noqa: E402
from lib import config, hermeslog, snapshot  # noqa: E402
from lib import queue as q  # noqa: E402

SESSION = "20261003_140003_2ca38d"
GATEWAY = "20261003_120000_gwgwgw"
T0 = datetime(2026, 10, 3, 19, 0, 4, tzinfo=timezone.utc)   # session start (snapshot `started`)


def logts(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S,123")


STARTUP = [  # one Hermes process start: what every other one-shot session / the gateway writes, untagged
    "INFO agent.shell_hooks: shell hook registered: pre_tool_call -> /usr/bin/python3 /opt/judge/hooks/gate.py "
    "(matcher=terminal|write_file|patch|read_file, timeout=60s, fail_closed=True)",
    *[f"INFO hermes_cli.plugins: Plugin 'web-{n}' registered web provider: {n}" for n in
      ("brave", "ddgs", "exa", "firecrawl", "keenable", "parallel", "perplexity", "searxng", "tavily", "xai")],
    *[f"INFO tools.registry: check_fn check_{n}_requirements returned False; dependent tools will be unavailable "
      "this turn" for n in ("computer_use", "image_generation", "browser", "homeassistant")],
    "INFO hermes_cli.mem_trim: memory trim: reason=messaging gateway housekeeping malloc_trim=1 rss_kib=1->1",
    "INFO agent.auxiliary_client: Auxiliary auto-detect: using main provider custom (coder)",
    "WARNING hermes_state: state.db: linked SQLite 3.50.4 (interpreter /x/python) is vulnerable to the WAL-reset "
    "corruption bug (https://sqlite.org/wal.html)",
    "WARNING cli: Background MCP discovery previously exited with no connected servers; retrying discovery thread",
    "INFO run_agent: Loaded environment variables from /x/.hermes/.env",
    "INFO run_agent: OpenAI client created (agent_init, shared=True) thread=MainThread:1 provider=custom",
]
SESSION_MSGS = [
    f"INFO [{SESSION}] agent.turn_context: conversation turn: session={SESSION} model=coder msg='In judge-sandbox'",
    f"INFO [{SESSION}] agent.conversation_loop: API call #1: model=coder latency=5.2s",
    f"INFO [{SESSION}] agent.tool_executor: tool read_file completed (0.05s, 484 chars)",
    "INFO agent.tool_executor: tool read_file completed (0.03s, 642 chars)",                 # untagged, kept
    "WARNING agent.message_sanitization: Unrepairable tool_call arguments for memory - replaced with empty object",
    f"INFO [{SESSION}] agent.tool_executor: tool patch completed (0.09s, 1053 chars)",
    f"WARNING [{SESSION}] agent.tool_executor: Tool terminal returned error (0.10s): check.sh: line 8: syntax error",
    "  continuation of the terminal error",
    f"INFO [{GATEWAY}] agent.conversation_loop: API call #9: other session",
    f"INFO [{SESSION}] agent.conversation_loop: Turn ended: reason=text_response SESSION-LAST-LINE",
]


def real_shaped_log(n_starts=12):
    """Like the S9 log: many untagged process starts (>26K chars), THEN the session's lines."""
    lines = []
    t = T0 - timedelta(minutes=4)
    for _ in range(n_starts):
        for m in STARTUP:
            lines.append(f"{logts(t)} {m}")
        t += timedelta(seconds=15)
    t = T0
    for m in SESSION_MSGS:
        if m.startswith("  "):
            lines.append(m)
            continue
        t += timedelta(seconds=2)
        lines.append(f"{logts(t)} {m}")
    return "\n".join(lines) + "\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = make_env(tmp_path, monkeypatch)
    monkeypatch.setenv("JUDGE_LOG_TZ", "UTC")
    agent = real_shaped_log()
    (e["hermes"] / "logs" / "agent.log").write_text(agent)
    errs = [ln for ln in agent.splitlines() if " WARNING " in ln]
    (e["hermes"] / "logs" / "errors.log").write_text("\n".join(errs) + "\n")
    return e


def req(**kw):
    r = {"id": "20261003T190044Z-2ca38d-completion", "kind": "completion", "session": SESSION,
         "created": q.utc_now_iso(T0 + timedelta(seconds=40)), "since": q.utc_now_iso(T0 - timedelta(minutes=5)),
         "source_event": "pre_verify", "changed_paths": [], "claims": "done", "plan": None, "data_class": "infra",
         "detail": {}}
    r.update(kw)
    return r


# ------------------------------------------------------------------ #19 (a) collector layout
def test_synthetic_log_is_shaped_like_the_real_one(env):
    text = (env["hermes"] / "logs" / "agent.log").read_text()
    assert text.index(f"[{SESSION}]") > 26000


def test_hermes_log_session_lines_first_noise_dropped(env):
    since, until = T0 - timedelta(minutes=5), T0 + timedelta(seconds=50)
    text = collect.hermes_log(req(), config.load_config(), since, until)
    sess = text.index("===== agent.log: SESSION LINES")
    ctx = text.index("===== agent.log: UNTAGGED CONTEXT")
    assert sess < ctx and text.index("===== errors.log: SESSION LINES") < ctx
    # every session line, with its continuation, sits in the session section
    head = text[:ctx]
    for m in ("API call #1", "tool patch completed", "SESSION-LAST-LINE", "continuation of the terminal error"):
        assert m in head
    # context keeps relevant untagged lines, never another session's lines
    tail = text[ctx:]
    assert "Unrepairable tool_call arguments" in tail and "tool read_file completed (0.03s" in tail
    assert "other session" not in text
    # startup noise of all processes is dropped and counted, not shown
    for noise in ("registered web provider", "check_fn", "memory trim", "shell hook registered",
                  "WAL-reset", "Background MCP discovery", "Auxiliary auto-detect", "Loaded environment",
                  "OpenAI client created"):
        assert noise not in text
    assert "noise line(s) dropped: hermes_cli.plugins x120" in text
    # errors.log duplicates of agent.log lines are not repeated
    assert "also in agent.log not repeated" in text
    assert len(text) < 5000


def test_noise_rule(env):
    assert hermeslog.is_noise("hermes_cli.plugins", "Plugin 'x' registered")
    assert hermeslog.is_noise("gateway.run", "anything")
    assert hermeslog.is_noise("cli", "Background MCP discovery previously exited with no connected servers")
    assert not hermeslog.is_noise("cli", "something else")
    assert not hermeslog.is_noise("agent.message_sanitization", "Unrepairable tool_call arguments")
    assert not hermeslog.is_noise("agent.tool_executor", "tool read_file completed")
    assert hermeslog.is_noise("run_agent", "x", extra_loggers=("run_agent",))


def test_watcher_request_has_no_context_section(env):
    r = req(source_event="watch", kind="runaway", session="watch-task-4711")
    text = collect.hermes_log(r, config.load_config(), T0 - timedelta(minutes=5), T0 + timedelta(minutes=1))
    assert "UNTAGGED CONTEXT" not in text and "Unrepairable" not in text and "untagged lines omitted" in text


# ------------------------------------------------------------------ #19 (b) runner budget
def old_layout_bundle(ev: Path, log_text: str, n_other=9, other_size=20000, gates=""):
    """A bundle as collector v2 wrote it in run 2: interleaved log, header first."""
    ev.mkdir(parents=True)
    (ev / "manifest.json").write_text(json.dumps({"request": req(), "data_class": "infra"}))
    hdr = (f"# Hermes log lines for session {SESSION} (plus untagged lines), window ... UTC\n\n"
           "===== agent.log =====\n")
    (ev / "hermes-log.txt").write_text(hdr + log_text)
    (ev / "gate-decisions.jsonl").write_text(gates)
    (ev / "probes").mkdir()
    for i in range(n_other):
        (ev / "probes" / f"host-unit_journal-{i}.txt").write_text(f"journal {i}\n" * (other_size // 10))


def session_lines_in(text):
    s = text.index("=== FILE: hermes-log.txt ===")
    e = text.index("=== FILE:", s + 10)
    return [ln for ln in text[s:e].splitlines() if f"[{SESSION}]" in ln and ln[:2] == "20"]


@pytest.mark.parametrize("max_chars", [150000, 60000])
def test_runner_keeps_every_session_line_of_old_layout(tmp_path, max_chars):
    ev = tmp_path / "ev"
    gates = json.dumps({"session": SESSION, "decision": "approve", "excerpt": "GATE-DECISION-1"}) + "\n"
    old_layout_bundle(ev, real_shaped_log(), gates=gates)
    want = [ln for ln in real_shaped_log().splitlines() if f"[{SESSION}]" in ln]
    # the run-2 code path: head truncation to max_chars // n_files lost all of them
    per_file = max(4000, max_chars // 12)
    assert not [ln for ln in (ev / "hermes-log.txt").read_text()[:per_file].splitlines() if f"[{SESSION}]" in ln]
    text = RJ.bundle_text(ev, max_chars)
    assert session_lines_in(text) == want
    assert "continuation of the terminal error" in text
    assert "GATE-DECISION-1" in text
    assert "runner omitted" in text and "untagged context line(s) from the middle" in text
    assert len(text) <= max_chars * 1.1


def test_runner_keeps_session_lines_of_new_layout(env, tmp_path):
    ev = tmp_path / "ev"
    log = collect.hermes_log(req(), config.load_config(), T0 - timedelta(minutes=5), T0 + timedelta(minutes=1))
    old_layout_bundle(ev, "")
    (ev / "hermes-log.txt").write_text(log)
    text = RJ.bundle_text(ev, 60000)
    assert len(session_lines_in(text)) == 6 and "Unrepairable" in text


def test_runner_middle_cuts_oversized_session_lines_keeping_first_and_last(tmp_path):
    ev = tmp_path / "ev"
    many = "".join(f"2026-10-03 19:00:05,000 INFO [{SESSION}] agent.conversation_loop: call #{i} {'z' * 200}\n"
                   for i in range(1000))
    old_layout_bundle(ev, many, n_other=2, other_size=1000)
    text = RJ.bundle_text(ev, 60000)
    lines = session_lines_in(text)
    assert "call #0 " in lines[0] and "call #999 " in lines[-1] and len(lines) < 1000
    assert "session-tagged line(s) from the middle" in text
    assert len(text) <= 60000 * 1.1


def test_split_hermes_log_continuations(tmp_path):
    text = (f"# header\n\n===== agent.log =====\n2026-10-03 19:00:05,000 INFO [{SESSION}] a.b: x\n  cont\n"
            "2026-10-03 19:00:06,000 INFO a.b: untagged\n  ucont\n")
    rows, n = RJ.split_hermes_log(text, SESSION)
    assert n == 2
    assert [k for k, _ in rows] == ["struct", "struct", "struct", "session", "session", "context", "context",
                                    "struct"]


# ------------------------------------------------------------------ #20 since_for clamp
@pytest.fixture
def verify(tmp_path, monkeypatch):
    monkeypatch.setenv("JUDGE_REVIEW_DIR", str(tmp_path / "review"))
    spec = importlib.util.spec_from_file_location("judge_verify_run2", JUDGE_DIR / "hooks" / "verify.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_import", lambda name: None)
    mod._site_cache = {}
    return mod


def edited(tmp_path, when):
    p = tmp_path / "config-sample.json"
    p.write_text("{}")
    os.utime(p, (when.timestamp(), when.timestamp()))
    return str(p)


def test_since_for_clamped_to_session_start(verify, tmp_path):
    now = T0 + timedelta(seconds=40)
    p = edited(tmp_path, T0 + timedelta(seconds=25))
    assert verify.since_for([p], now) == q.utc_now_iso(T0 + timedelta(seconds=25) - timedelta(minutes=5))
    assert verify.since_for([p], now, T0) == q.utc_now_iso(T0)
    # an older session start leaves the 5-minute margin alone
    assert verify.since_for([p], now, T0 - timedelta(hours=1)) == q.utc_now_iso(T0 - timedelta(minutes=4, seconds=35))
    # a late snapshot never cuts off the edit itself
    assert verify.since_for([p], now, T0 + timedelta(seconds=30)) == q.utc_now_iso(T0 + timedelta(seconds=25))
    # no readable path: now - 1 h, clamped to the session start
    assert verify.since_for(["/nonexistent"], now, T0) == q.utc_now_iso(T0)


def test_session_started_from_snapshot_meta_and_enqueue_uses_it(verify, tmp_path, monkeypatch):
    snap = tmp_path / "review" / "snapshots" / SESSION
    snap.mkdir(parents=True)
    (snap / "meta.json").write_text(json.dumps({"session": SESSION, "started": q.utc_now_iso(T0)}))
    assert verify.session_started(SESSION) == T0
    assert verify.session_started("20261003_000000_nosnap") is None
    p = edited(tmp_path, datetime.now(timezone.utc) - timedelta(seconds=5))
    (snap / "meta.json").write_text(json.dumps({"session": SESSION, "started": q.utc_now_iso(
        datetime.now(timezone.utc) - timedelta(seconds=30))}))
    assert verify.enqueue(verify.Result(), {"session": SESSION, "paths": [p], "final_response": "done",
                                            "extra": {"platform": "cli"}})
    [got] = [json.loads(x.read_text()) for x in (tmp_path / "review" / "queue").glob("*.json")]
    assert got["since"] == json.loads((snap / "meta.json").read_text())["started"]


# ------------------------------------------------------------------ #21 wait for the window
class Clock:
    def __init__(self, now):
        self.now, self.slept = now, []

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += timedelta(seconds=s)


@pytest.mark.parametrize("age,expect", [(0, 13.0), (4, 9.0), (13, 0.0), (600, 0.0), (-3600, 13.0)])
def test_wait_for_window_fake_clock(monkeypatch, age, expect):
    monkeypatch.setenv("JUDGE_WINDOW_GRACE_SECONDS", "10")
    monkeypatch.setenv("SITE_ENV", "/nonexistent-site.env")
    clock = Clock(T0)
    r = {"created": q.utc_now_iso(T0 - timedelta(seconds=age))}
    assert RJ.wait_for_window(r, now_fn=clock, sleep_fn=clock.sleep) == expect
    assert clock.slept == ([expect] if expect else [])
    assert RJ.wait_for_window({"created": "garbage"}, now_fn=clock, sleep_fn=clock.sleep) == 0.0


def test_runner_waits_before_collecting_and_not_when_bundle_exists(tmp_path, monkeypatch):
    review = tmp_path / "review"
    monkeypatch.setenv("JUDGE_REVIEW_DIR", str(review))
    monkeypatch.setenv("SITE_ENV", "/nonexistent-site.env")
    order = []
    monkeypatch.setattr(RJ, "wait_for_window", lambda r, **kw: order.append("wait") or 0.0)
    fake = tmp_path / "collect.py"
    fake.write_text("import sys\n")
    monkeypatch.setattr(RJ, "COLLECTOR", fake)
    monkeypatch.setattr(RJ.subprocess, "run", lambda *a, **k: order.append("collect") or
                        type("P", (), {"returncode": 1, "stderr": "x", "stdout": ""})())
    RJ.ensure_evidence("20261003T190044Z-2ca38d-completion", {"created": q.utc_now_iso(T0)})
    assert order == ["wait", "collect"]
    ev = review / "evidence" / "20261003T190044Z-2ca38d-completion"
    ev.mkdir(parents=True)
    (ev / "manifest.json").write_text("{}")
    order.clear()
    RJ.ensure_evidence("20261003T190044Z-2ca38d-completion", {"created": q.utc_now_iso(T0)})
    assert order == []


def test_gate_bundle_outcome_known_from_blocked_event(env):
    """S1: the gate request is collected right away; Hermes already reported the call as blocked, so the
    outcome is not_executed instead of `unknown (decision too recent to tell)`."""
    created = T0 + timedelta(seconds=10)
    rid = "20261003T190014Z-2ca38d-gate"
    rec = {"ts": q.utc_now_iso(created), "session": SESSION, "tool": "terminal", "decision": "approve",
           "rule": "remote-mutation", "excerpt": "ssh edge 'sudo systemctl reload nginx'", "request": f"{rid}.json",
           "tool_call_id": "call-9", "call_hash": "ab" * 8}
    env["review"].mkdir(parents=True, exist_ok=True)
    (env["review"] / "gate.log").write_text(json.dumps(rec) + "\n")
    d = q.snapshot_dir(SESSION, env["review"], create=True)
    snapshot.save_meta(d, {"session": SESSION, "started": q.utc_now_iso(T0)})
    snapshot.record_event(d, "terminal", [], "blocked", now=created, call_id="call-9", call_hash="ab" * 8)
    r = req(id=rid, kind="gate", created=q.utc_now_iso(created), since=q.utc_now_iso(created))
    [line] = collect.gate_decisions(r, env["review"], created, created + timedelta(seconds=10), now=created)
    got = json.loads(line)
    assert got["outcome"] == "not_executed" and "status=blocked" in got["outcome_basis"]


# ------------------------------------------------------------------ #24 probe host header
@pytest.mark.parametrize("host,secret", [("covenant", "edge-alias"), ("walter", "192.168.122.10")])
def test_probe_shows_logical_host_not_target(env, host, secret):
    r = FakeRunner(default=(0, "2026-10-03T18:47:14+00:00 real-machine-name nginx[1]: ok\n", ""))
    _, journal = probe.run_probe("unit_journal", [host, "nginx", "2026-10-03T18:47:00Z", "2026-10-03T18:47:30Z"],
                                 runner=r)
    _, state = probe.run_probe("unit_state", [host, "nginx"], runner=r)
    for text in (journal, state):
        assert f"# host: {host} (" in text and "via the configured ssh target" in text
        assert f"$ ssh <{host}> " in text and "not a mismatch" in text
        assert secret not in text and "operator@" not in text
    assert secret in " ".join(r.calls[0]["argv"])          # the real target is still what runs
    assert journal.count("# host:") == 1


def test_host_find_file_has_host_header(env):
    r = FakeRunner(default=(0, "# 0 path(s)\n", ""))
    text = collect.host_diff("covenant", req(), config.load_config(), r, "2026-10-03T19:00:00Z",
                             "2026-10-03T19:00:10Z")
    assert "# host: covenant (edge / reverse-proxy host" in text and "edge-alias" not in text
