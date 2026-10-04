"""Pipeline tests: no network, no real key. A fake collector script and a local fake LiteLLM."""
import asyncio
import http.server
import json
import sys
import threading

import pytest

import pipeline

KEY = "testonly-fake-digest-key"

FAKE_COLLECTOR = r'''
import json, sys
out = sys.argv[-1]
json.dump({
    "CISA_KEV": {"ok": True, "recent": [
        {"cveID": "CVE-2026-0001", "vendorProject": "V", "product": "P", "vulnerabilityName": "n",
         "dateAdded": "2026-10-03", "dueDate": "2026-10-24"},
        {"cveID": "CVE-2026-0002", "vendorProject": "V", "product": "P", "vulnerabilityName": "m",
         "dateAdded": "2026-10-03", "dueDate": "2026-10-24"}]},
    "SANS_ISC": {"ok": True, "items": [{"title": "Story A", "link": "https://e.example/a", "date": "2026-10-03"}]},
    "BleepingComputer": {"ok": False, "error": "HTTP 500", "items": []},
}, open(out, "w"))
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    col = tmp_path / "fakecol.py"
    col.write_text(FAKE_COLLECTOR)
    monkeypatch.setattr(pipeline, "_collector_cmd", lambda watch, out: [sys.executable, str(col), out])
    keyf = tmp_path / "key"
    keyf.write_text(KEY + "\n")
    monkeypatch.setenv("LITELLM_KEY_FILE", str(keyf))
    state = tmp_path / "state"
    state.mkdir()
    return state


def run(state, curate_fn=None, run_id="20261004T000000Z"):
    events = []

    async def progress(stage, **kw):
        events.append((stage, kw))

    asyncio.run(pipeline.run_watch("default", state, progress, run_id=run_id, curate_fn=curate_fn))
    return events


async def fake_curate(watch, deduped, gaps):
    return {"tiers": [], "markdown": "# curated"}


async def failing_curate(watch, deduped, gaps):
    raise RuntimeError("boom")


def load_state(state):
    return json.loads((state / "state" / "default.json").read_text())


def seed(state, cutoff="2026-09-25T00:00:00Z", **seen):
    (state / "state").mkdir(exist_ok=True)
    (state / "state" / "default.json").write_text(json.dumps(
        {"cutoff": cutoff, "sources": {}, "seen": {"cves": seen.get("cves", {}), "events": {}}}))


def test_failed_source_keeps_cutoff_ok_sources_advance(env):
    seed(env)
    ev = run(env, fake_curate)
    assert ev[-1][0] == "done"
    s = load_state(env)
    assert s["cutoff"] != "2026-09-25T00:00:00Z"
    assert s["sources"]["CISA_KEV"]["cutoff"] == s["cutoff"]
    assert s["sources"]["SANS_ISC"]["cutoff"] == s["cutoff"]
    # first run after seeding: the failed source is pinned to the OLD watch-level cutoff
    assert s["sources"]["BleepingComputer"]["cutoff"] == "2026-09-25T00:00:00Z"
    assert s["sources"]["BleepingComputer"]["ok"] is False


def test_dedupe_skips_seen_ids(env):
    seed(env, cves={"CVE-2026-0001": {}})
    run(env, fake_curate)
    out = json.loads((env / "runs" / "default" / "20261004T000000Z.json").read_text())
    cves = [i.get("cve") for i in out["items"] if i.get("cve")]
    assert cves == ["CVE-2026-0002"]
    # a second run reports nothing new
    run(env, fake_curate, run_id="20261004T000001Z")
    out2 = json.loads((env / "runs" / "default" / "20261004T000001Z.json").read_text())
    assert out2["items"] == []


def test_curation_failure_falls_back_to_uncurated(env):
    ev = run(env, failing_curate)
    assert ev[-1][0] == "done"
    out = json.loads((env / "runs" / "default" / "20261004T000000Z.json").read_text())
    assert out["uncurated"] is True
    assert (env / "runs" / "default" / "20261004T000000Z.md").read_text().strip()


def test_llm_call_uses_key_file_and_bounded_output(env, monkeypatch):
    seen = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen["auth"] = self.headers.get("Authorization")
            seen["max_tokens"] = body.get("max_tokens")
            content = json.dumps({"tiers": [{"tier": 1, "items": [
                {"title": "x", "why": "y", "confidence": "HIGH", "evidence": ["https://e.example/a"]}]}],
                "markdown": "# ok"})
            data = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("LITELLM_URL", f"http://127.0.0.1:{srv.server_port}/v1")
        ev = run(env)  # default curate_fn = curate_with_llm
    finally:
        srv.shutdown()
    assert seen["auth"] == f"Bearer {KEY}"
    assert isinstance(seen["max_tokens"], int) and 0 < seen["max_tokens"] <= 32768
    out = json.loads((env / "runs" / "default" / "20261004T000000Z.json").read_text())
    assert out["uncurated"] is False
    for p in env.rglob("*"):
        if p.is_file():
            assert KEY not in p.read_text()
    assert KEY not in repr(ev)


def test_missing_key_file_does_not_leak_path_and_falls_back(env, monkeypatch):
    monkeypatch.setenv("LITELLM_KEY_FILE", "/nonexistent/digest-key")
    ev = run(env)
    assert ev[-1][0] == "done"
    out = json.loads((env / "runs" / "default" / "20261004T000000Z.json").read_text())
    assert out["uncurated"] is True
