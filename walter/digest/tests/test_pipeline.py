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
            seen["thinking"] = body.get("chat_template_kwargs", {}).get("enable_thinking")
            content = json.dumps({"tiers": [{"tier": 1, "items": [
                {"id": "i1", "also": [], "why": "y", "confidence": "HIGH", "follow_up": ""}]}]})
            data = json.dumps({"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}).encode()
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
    assert seen["thinking"] is False
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


# --- per-source cutoffs on a first run with no seeded state ------------------------------------
# Run 1: BleepingComputer fails. Run 2: it recovers and returns an item dated before run 1.
# That item is inside its window (the source never delivered it) and must be reported.
PHASED_COLLECTOR = r'''
import json, os, sys
out = sys.argv[-1]
old = "2020-01-01T00:00:00Z"   # older than any run, i.e. before every cutoff the pipeline writes
if os.environ["FAKE_PHASE"] == "1":
    bc = {"ok": False, "error": "HTTP 500", "items": []}
else:
    bc = {"ok": True, "items": [{"title": "Late story", "link": "https://e.example/late", "date": old}]}
json.dump({
    "SANS_ISC": {"ok": True, "items": [{"title": "Old SANS " + os.environ["FAKE_PHASE"],
                                        "link": "https://e.example/s", "date": old}]},
    "BleepingComputer": bc,
}, open(out, "w"))
'''


@pytest.fixture
def phased(tmp_path, monkeypatch):
    col = tmp_path / "phasedcol.py"
    col.write_text(PHASED_COLLECTOR)
    monkeypatch.setattr(pipeline, "_collector_cmd", lambda watch, out: [sys.executable, str(col), out])
    keyf = tmp_path / "key"
    keyf.write_text(KEY + "\n")
    monkeypatch.setenv("LITELLM_KEY_FILE", str(keyf))
    state = tmp_path / "state"
    state.mkdir()
    return state


def test_first_run_no_seed_failed_source_keeps_its_window(phased, monkeypatch):
    monkeypatch.setenv("FAKE_PHASE", "1")
    run(phased, fake_curate, run_id="20261004T000000Z")
    s = load_state(phased)
    assert s["cutoff"] is not None                       # the watch advanced (SANS_ISC succeeded)
    assert s["sources"]["SANS_ISC"]["cutoff"] == s["cutoff"]
    # the failed source records an explicit "no cutoff yet", not the new watch-level one
    assert "cutoff" in s["sources"]["BleepingComputer"]
    assert s["sources"]["BleepingComputer"]["cutoff"] is None

    monkeypatch.setenv("FAKE_PHASE", "2")
    run(phased, fake_curate, run_id="20261004T000001Z")
    out = json.loads((phased / "runs" / "default" / "20261004T000001Z.json").read_text())
    titles = [i["title"] for i in out["items"]]
    assert "Late story" in titles                        # recovered source: old item kept
    assert "Old SANS 2" not in titles                    # healthy source: old item cut off
    s = load_state(phased)
    assert s["sources"]["BleepingComputer"]["cutoff"] == s["cutoff"]   # now it advances


def test_failed_source_twice_keeps_pinned_cutoff(env):
    seed(env)
    run(env, fake_curate, run_id="20261004T000000Z")
    run(env, fake_curate, run_id="20261004T000001Z")    # BleepingComputer fails again
    s = load_state(env)
    assert s["sources"]["BleepingComputer"]["cutoff"] == "2026-09-25T00:00:00Z"


def test_dedupe_cutoff_resolution():
    item = {"title": "T", "link": "https://e.example/t", "date": "2026-09-30T00:00:00Z"}
    sources = {"A": {"ok": True, "items": [dict(item, title="A item")]},
               "B": {"ok": True, "items": [dict(item, title="B item")]},
               "C": {"ok": True, "items": [dict(item, title="C item")]},
               "D": {"ok": True, "items": [dict(item, title="D item")]}}
    state = {"cutoff": "2026-10-01T00:00:00Z", "seen": {}, "sources": {
        "A": {"ok": False, "cutoff": None},                     # never succeeded: no cutoff
        "B": {"ok": True, "cutoff": "2026-09-29T00:00:00Z"},   # own, older cutoff
        "C": {"ok": True},                                      # seeded entry without one: watch cutoff
    }}                                                          # D: unknown source: watch cutoff
    got = {i["title"] for i in pipeline._dedupe("default", sources, state)}
    assert got == {"A item", "B item"}


@pytest.mark.parametrize("val,want", [(None, 8192), ("4096", 4096), ("0", 1024), ("-1", 1024),
                                      ("1000000", 16384), ("lots", 8192)])
def test_max_tokens_always_bounded(monkeypatch, val, want):
    if val is None:
        monkeypatch.delenv("DIGEST_MAX_TOKENS", raising=False)
    else:
        monkeypatch.setenv("DIGEST_MAX_TOKENS", val)
    assert pipeline._max_tokens() == want


FALLBACK_COLLECTOR = r'''
import json, sys
out = sys.argv[1]
json.dump({
    "SANS_ISC": {"ok": True, "items": [{"title": "Story A", "link": "https://e.example/a", "date": "2026-10-03"}]},
    "CISA_Advisories": {"ok": True, "via": "fallback", "fallback": "csaf",
                        "note": "HTTP 403; served via fallback csaf",
                        "items": [{"title": "ICSA-26-999-01 Example", "link": "https://e.example/icsa",
                                   "date": "2026-10-03"}]},
}, open(out, "w"))
'''


def test_fallback_served_source_is_a_gap_and_keeps_its_cutoff(env, tmp_path, monkeypatch):
    """A source served only by its fallback (partial coverage, e.g. CSAF = ICS advisories only) is
    reported as a coverage gap and does not advance its own cutoff, so items the primary carries
    from the blocked window are still reported once it recovers."""
    col = tmp_path / "fallbackcol.py"
    col.write_text(FALLBACK_COLLECTOR)
    monkeypatch.setattr(pipeline, "_collector_cmd", lambda watch, out: [sys.executable, str(col), out])
    seed(env)
    ev = run(env, fake_curate)
    assert ev[-1][0] == "done"
    out = json.loads((env / "runs" / "default" / "20261004T000000Z.json").read_text())
    assert any(g.startswith("CISA_Advisories:") and "fallback" in g for g in out["coverage_gaps"])
    assert any("ICSA-26-999-01" in (i.get("title") or "") for i in out["items"])
    s = load_state(env)
    assert s["sources"]["SANS_ISC"]["cutoff"] == s["cutoff"] != "2026-09-25T00:00:00Z"
    assert s["sources"]["CISA_Advisories"]["cutoff"] == "2026-09-25T00:00:00Z"
