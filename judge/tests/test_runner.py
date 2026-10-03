"""Tests for judge/runner/run_judge.py. No network, no real `claude`: a stub CLI and a stub HTTP server."""
import http.server
import json
import os
import stat
import sys
import textwrap
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parent.parent / "runner"
sys.path.insert(0, str(RUNNER))
import run_judge as RJ  # noqa: E402

KEY = "test-local-key-not-a-secret"
GOOD = {"items": [{"id": "F1", "rubric": "R1", "severity": "high", "claim": "alias fixed",
                   "evidence": "probes/ssh_alias_test-1.txt: Permission denied (publickey)",
                   "verdict": "false", "recommendation": "check the alias"}]}


def rid(kind="completion", short="fdc8ec", t="035210"):
    return f"20261003T{t}Z-{short}-{kind}"


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    review = home / "review"
    for d in ("queue", "evidence", "findings", "acks", "done"):
        (review / d).mkdir(parents=True)
    keyf = tmp_path / "key"
    keyf.write_text(KEY + "\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("JUDGE_REVIEW_DIR", str(review))
    monkeypatch.setenv("SITE_ENV", str(tmp_path / "no-site.env"))  # never read the real site.env
    monkeypatch.setenv("JUDGE_LOCAL_KEY_FILE", str(keyf))
    monkeypatch.setenv("JUDGE_LOCAL_MODEL", "big")
    monkeypatch.setenv("JUDGE_PROBES", "0")
    for k in ("JUDGE_MODE", "JUDGE_FRONTIER_CMD", "JUDGE_FRONTIER_DAILY_MAX", "JUDGE_LOCAL_URL",
              "JUDGE_FRONTIER_MODEL", "SPARK_API_HOST"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(RJ, "COLLECTOR", tmp_path / "no-collector.py")
    return review


def enqueue(review, request_id, data_class="infra", evidence=True, session="20261002_165907_fdc8ec"):
    req = {"id": request_id, "kind": request_id.rsplit("-", 1)[1], "session": session,
           "created": "2026-10-03T03:52:10Z", "since": "2026-10-03T03:20:00Z",
           "changed_paths": ["~/.ssh/config"], "claims": "alias updated", "plan": None,
           "data_class": data_class, "source_event": "on_session_end", "detail": {}}
    (review / "queue" / f"{request_id}.json").write_text(json.dumps(req))
    if evidence:
        ev = review / "evidence" / request_id
        (ev / "probes").mkdir(parents=True)
        (ev / "manifest.json").write_text(json.dumps({"request": req, "data_class": data_class, "artifacts": []}))
        (ev / "hermes-log.txt").write_text("2026-10-03T03:30:00Z tool_call terminal ssh edge-alias true\n")
        (ev / "probes" / "ssh_alias_test-1.txt").write_text("Permission denied (publickey)\nexit=255\n")
    return req


# ------------------------------------------------------------------ frontier stub CLI
@pytest.fixture
def frontier(tmp_path, monkeypatch):
    """A fake `claude`: records argv/env/stdin per call, replies from a list of canned results."""
    rec = tmp_path / "frontier"
    rec.mkdir()
    script = tmp_path / "fake-claude"
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, os, sys
        rec = {str(rec)!r}
        n = len([p for p in os.listdir(rec) if p.startswith("call-")]) + 1
        replies = json.load(open(os.path.join(rec, "replies.json")))
        json.dump({{"argv": sys.argv[1:], "env": dict(os.environ), "stdin": sys.stdin.read(), "cwd": os.getcwd()}},
                  open(os.path.join(rec, f"call-{{n}}.json"), "w"))
        reply = replies[min(n, len(replies)) - 1]
        print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": reply,
                          "modelUsage": {{"claude-test-model": {{"outputTokens": 10}}}}}}))
        """))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("JUDGE_FRONTIER_CMD", str(script))

    class F:
        def replies(self, *texts):
            (rec / "replies.json").write_text(json.dumps(list(texts)))

        def calls(self):
            return [json.loads(p.read_text()) for p in sorted(rec.glob("call-*.json"))]
    f = F()
    f.replies(json.dumps(GOOD))
    return f


# ------------------------------------------------------------------ local stub server
@pytest.fixture
def local(monkeypatch):
    state = {"replies": [json.dumps(GOOD)], "requests": [], "reject_response_format": False}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
            if state["reject_response_format"] and "response_format" in body:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error": "response_format is not supported"}')
                return
            i = min(len(state["requests"]), len(state["replies"])) - 1
            out = {"model": "big-served", "choices": [{"message": {"role": "assistant", "content": state["replies"][i]},
                                                       "finish_reason": "stop"}]}
            data = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    monkeypatch.setenv("JUDGE_LOCAL_URL", f"http://127.0.0.1:{srv.server_address[1]}/v1")
    yield state
    srv.shutdown()


def finding(review, request_id):
    return json.loads((review / "findings" / f"{request_id}.json").read_text())


# ------------------------------------------------------------------ tests
def test_frontier_mode(env, frontier, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "frontier")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example.com")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "gateway-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "gateway-key")
    r = rid()
    enqueue(env, r)
    assert RJ.main([r]) == 0
    f = finding(env, r)
    assert f["mode"] == "frontier" and f["judge"] == "claude-test-model" and f["request"] == r
    assert f["items"][0]["verdict"] == "false"
    assert (env / "done" / f"{r}.json").exists() and not (env / "queue" / f"{r}.json").exists()
    assert (env / "findings" / f"{r}.md").exists()
    (call,) = frontier.calls()
    argv = call["argv"]
    assert argv[0] == "-p" and "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--tools") + 1] == ""
    assert "--system-prompt" in argv and "independent reviewer" in argv[argv.index("--system-prompt") + 1]
    assert "Permission denied (publickey)" in call["stdin"] and "untrusted data" in call["stdin"]
    # env stripping: the frontier judge must use the default Anthropic API, never the gateway
    for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        assert k not in call["env"]
    assert call["cwd"] != os.getcwd()
    usage = json.loads((env / "usage.json").read_text())
    assert usage["frontier_runs"] == 1 and usage["date"] == datetime.now(timezone.utc).strftime("%Y-%m-%d")


def test_frontier_env_keeps_api_key_without_endpoint_override(monkeypatch):
    base = {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "k", "HOME": "/tmp"}
    out = RJ.frontier_env(base)
    assert out["ANTHROPIC_API_KEY"] == "k" and out["PATH"] == "/usr/bin"
    out = RJ.frontier_env({**base, "ANTHROPIC_BASE_URL": "https://gw"})
    assert "ANTHROPIC_BASE_URL" not in out and "ANTHROPIC_API_KEY" not in out


def test_frontier_env_drops_anything_aimed_at_gateway(monkeypatch):
    monkeypatch.setenv("SPARK_API_HOST", "api.example.com")
    out = RJ.frontier_env({"SOME_PROXY_URL": "https://api.example.com/v1", "OK": "1"})
    assert out == {"OK": "1"}


def test_local_mode(env, local, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "local")
    r = rid()
    enqueue(env, r)
    assert RJ.main([r]) == 0
    f = finding(env, r)
    assert f["mode"] == "local" and f["judge"] == "big-served"
    (req,) = local["requests"]
    assert req["path"] == "/v1/chat/completions"
    assert req["auth"] == f"Bearer {KEY}"
    body = req["body"]
    assert body["model"] == "big" and isinstance(body["max_tokens"], int) and body["max_tokens"] > 0
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["role"] == "system" and "R7" in body["messages"][0]["content"]
    # the key never lands in logs or outputs
    for p in env.rglob("*"):
        if p.is_file():
            assert KEY not in p.read_text(errors="replace"), p


def test_local_max_tokens_override(env, local, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "local")
    monkeypatch.setenv("JUDGE_LOCAL_MAX_TOKENS", "1234")
    r = rid()
    enqueue(env, r)
    RJ.main([r])
    assert local["requests"][0]["body"]["max_tokens"] == 1234


def test_local_response_format_rejected_retries_without(env, local, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "local")
    local["reject_response_format"] = True
    r = rid()
    enqueue(env, r)
    assert RJ.main([r]) == 0
    assert "response_format" in local["requests"][0]["body"]
    assert "response_format" not in local["requests"][1]["body"]
    assert finding(env, r)["items"]


def test_sensitive_forces_local(env, frontier, local, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "frontier")
    r = rid()
    enqueue(env, r, data_class="sensitive")
    assert RJ.main([r]) == 0
    assert frontier.calls() == []
    assert len(local["requests"]) == 1
    f = finding(env, r)
    assert f["mode"] == "local" and any("local judge enforced" in n for n in f["notes"])


def test_missing_data_class_counts_as_sensitive(env, frontier, local, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "frontier")
    r = rid()
    req = enqueue(env, r)
    req.pop("data_class")
    (env / "queue" / f"{r}.json").write_text(json.dumps(req))
    (env / "evidence" / r / "manifest.json").write_text(json.dumps({"request": req}))
    RJ.main([r])
    assert frontier.calls() == [] and finding(env, r)["mode"] == "local"


def test_manifest_sensitive_wins_over_request(env, frontier, local, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "frontier")
    r = rid()
    enqueue(env, r, data_class="infra")
    (env / "evidence" / r / "manifest.json").write_text(json.dumps({"data_class": "sensitive"}))
    RJ.main([r])
    assert frontier.calls() == [] and finding(env, r)["mode"] == "local"


def test_invalid_json_retry_then_success(env, local, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "local")
    local["replies"] = ["I think everything looks fine!", json.dumps(GOOD)]
    r = rid()
    enqueue(env, r)
    assert RJ.main([r]) == 0
    assert len(local["requests"]) == 2
    msgs = local["requests"][1]["body"]["messages"]
    assert msgs[-2] == {"role": "assistant", "content": "I think everything looks fine!"}
    assert "invalid" in msgs[-1]["content"] and "JSON object" in msgs[-1]["content"]
    assert finding(env, r)["items"][0]["id"] == "F1"
    raw = (env / "evidence" / r / "judge-raw.txt").read_text()
    assert "everything looks fine" in raw


def test_invalid_json_twice_writes_na_item(env, local, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "local")
    local["replies"] = ["garbage one", "garbage two"]
    r = rid()
    enqueue(env, r)
    assert RJ.main([r]) == 0
    f = finding(env, r)
    assert len(f["items"]) == 1
    it = f["items"][0]
    assert it["verdict"] == "n/a" and "invalid" in it["claim"] and "judge-raw.txt" in it["evidence"]
    raw = (env / "evidence" / r / "judge-raw.txt").read_text()
    assert "garbage one" in raw and "garbage two" in raw
    assert (env / "done" / f"{r}.json").exists()


def test_frontier_invalid_retry_sends_error_back(env, frontier, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "frontier")
    frontier.replies("nope", json.dumps(GOOD))
    r = rid()
    enqueue(env, r)
    RJ.main([r])
    calls = frontier.calls()
    assert len(calls) == 2
    assert "YOUR PREVIOUS REPLY" in calls[1]["stdin"] and "Your reply was invalid" in calls[1]["stdin"]
    assert finding(env, r)["mode"] == "frontier"


def test_daily_cap_falls_back_to_local(env, frontier, local, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "frontier")
    monkeypatch.setenv("JUDGE_FRONTIER_DAILY_MAX", "1")
    r1, r2 = rid(t="035210"), rid(t="035211")
    enqueue(env, r1)
    enqueue(env, r2)
    assert RJ.main(["--pending"]) == 0
    assert len(frontier.calls()) == 1 and len(local["requests"]) == 1
    f1, f2 = finding(env, r1), finding(env, r2)
    assert f1["mode"] == "frontier" and "notes" not in f1
    assert f2["mode"] == "local" and any("daily cap" in n for n in f2["notes"])
    assert json.loads((env / "usage.json").read_text())["frontier_runs"] == 1


def test_daily_cap_default_20_and_resets_next_day(env, frontier, local, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "frontier")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    (env / "usage.json").write_text(json.dumps({"date": today, "frontier_runs": 20}))
    r = rid()
    enqueue(env, r)
    RJ.main([r])
    assert frontier.calls() == [] and finding(env, r)["mode"] == "local"
    (env / "usage.json").write_text(json.dumps({"date": "2000-01-01", "frontier_runs": 99}))
    r2 = rid(t="040000")
    enqueue(env, r2)
    RJ.main([r2])
    assert len(frontier.calls()) == 1 and finding(env, r2)["mode"] == "frontier"


def test_validator_drops_unreferenced_items_and_notes_it(env, local, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "local")
    bad = {"items": [GOOD["items"][0], {**GOOD["items"][0], "id": "F2", "evidence": "trust me"}]}
    local["replies"] = [json.dumps(bad)]
    r = rid()
    enqueue(env, r)
    RJ.main([r])
    f = finding(env, r)
    assert [i["id"] for i in f["items"]] == ["F1"]
    assert any("dropped" in n for n in f["notes"])


def test_backend_failure_stays_queued_then_placeholder(env, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "local")
    monkeypatch.setenv("JUDGE_LOCAL_URL", "http://127.0.0.1:9/v1")  # nothing listens on port 9
    monkeypatch.setenv("JUDGE_LOCAL_TIMEOUT", "2")
    r = rid()
    enqueue(env, r)
    assert RJ.main([r]) == 1 and (env / "queue" / f"{r}.json").exists()
    assert RJ.main([r]) == 1
    assert RJ.main([r]) == 0
    f = finding(env, r)
    assert f["items"][0]["verdict"] == "n/a" and "failed 3 times" in f["items"][0]["claim"]
    assert (env / "done" / f"{r}.json").exists()
    assert KEY not in (env / "runner.log").read_text()


def test_missing_key_file_is_backend_error(env, local, monkeypatch, tmp_path):
    monkeypatch.setenv("JUDGE_MODE", "local")
    monkeypatch.setenv("JUDGE_LOCAL_KEY_FILE", str(tmp_path / "missing.key"))
    r = rid()
    enqueue(env, r)
    assert RJ.main([r]) == 1 and local["requests"] == []


def test_collector_called_when_evidence_missing(env, local, monkeypatch, tmp_path):
    monkeypatch.setenv("JUDGE_MODE", "local")
    stub = tmp_path / "collect.py"
    stub.write_text(textwrap.dedent("""\
        import json, os, sys
        rid = sys.argv[1]
        ev = os.path.join(os.environ["JUDGE_REVIEW_DIR"], "evidence", rid)
        os.makedirs(ev, exist_ok=True)
        json.dump({"data_class": "infra"}, open(os.path.join(ev, "manifest.json"), "w"))
        open(os.path.join(ev, "hermes-log.txt"), "w").write("collected by stub\\n")
        """))
    monkeypatch.setattr(RJ, "COLLECTOR", stub)
    r = rid()
    enqueue(env, r, evidence=False)
    assert RJ.main([r]) == 0
    assert "collected by stub" in local["requests"][0]["body"]["messages"][1]["content"]


def test_collector_missing_writes_placeholder(env, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "local")
    r = rid()
    enqueue(env, r, evidence=False)
    assert RJ.main([r]) == 0
    f = finding(env, r)
    assert f["items"][0]["verdict"] == "n/a" and "collector" in f["items"][0]["evidence"]


def test_probe_round(env, local, monkeypatch, tmp_path):
    monkeypatch.setenv("JUDGE_MODE", "local")
    monkeypatch.setenv("JUDGE_PROBES", "1")
    probe = tmp_path / "probe.py"
    probe.write_text("import sys\nprint('probe-output', *sys.argv[1:])\n")
    monkeypatch.setattr(RJ, "PROBE", probe)
    local["replies"] = [json.dumps({"probe_requests": [{"name": "ssh_alias_test", "args": ["edge-alias"]}]}),
                        json.dumps(GOOD)]
    r = rid()
    enqueue(env, r)
    assert RJ.main([r]) == 0
    assert "PROBES ALLOWED: yes" in local["requests"][0]["body"]["messages"][1]["content"]
    follow = local["requests"][1]["body"]["messages"][-1]["content"]
    assert "probe-output ssh_alias_test edge-alias" in follow and "untrusted" in follow
    assert (env / "evidence" / r / "probes" / "judge-ssh_alias_test-1.txt").exists()
    assert finding(env, r)["items"]


def test_bad_request_id_rejected(env):
    assert RJ.main(["../../etc/passwd"]) == 1
    assert RJ.main([]) == 64


def test_bundle_cap(tmp_path):
    ev = tmp_path / "ev"
    ev.mkdir()
    (ev / "manifest.json").write_text("{}")
    (ev / "hermes-log.txt").write_text("x" * 50000)
    (ev / "judge-raw.txt").write_text("previous judge output")
    text = RJ.bundle_text(ev, 10000)
    assert text.startswith("=== FILE: manifest.json ===")
    assert "truncated by runner" in text and "previous judge output" not in text
    assert len(text) < 12000
