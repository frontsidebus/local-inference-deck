"""Curation tests: schema validation, repair retry, json_schema fallback, chunking/merge,
finish_reason=length, empty input and rendering. No network: a local fake LiteLLM."""
import asyncio
import http.server
import json
import re
import threading

import pytest

import pipeline
from test_pipeline import KEY, load_state, run, seed  # noqa: F401  (fixtures/helpers)
from test_pipeline import env  # noqa: F401  (pytest fixture)


# --------------------------------------------------------------------------------------------
# fake LiteLLM: answers from a script of callables (request body -> (status, response dict))
# --------------------------------------------------------------------------------------------
class FakeLLM:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def __enter__(self):
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                fn = outer.script.pop(0) if len(outer.script) > 1 else outer.script[0]
                status, resp = fn(body)
                data = json.dumps(resp).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/v1"
        return self

    def __exit__(self, *a):
        self.srv.shutdown()


def reply(content, finish="stop"):
    if not isinstance(content, str):
        content = json.dumps(content)
    return lambda body: (200, {"choices": [{"message": {"content": content}, "finish_reason": finish}],
                               "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


def ids_in(body):
    """Item ids present in the prompt of a request."""
    return re.findall(r'^\{"id": "(i\d+)"', body["messages"][1]["content"], re.M)


def tier_all(tier=1):
    """A valid tiered answer that reports every item of the request in one tier."""
    def fn(body):
        items = [{"id": i, "also": [], "confidence": "HIGH", "why": "w", "follow_up": ""}
                 for i in ids_in(body)]
        return reply({"tiers": [{"tier": tier, "items": items}]})(body)
    return fn


def news(n, source="S", day=1):
    return [{"kind": "news", "key": f"k{source}{i}", "title": f"{source} story {i}", "source": source,
             "date": f"2026-10-{day:02d}T{i % 24:02d}:00:00Z", "link": f"https://e.example/{source}/{i}",
             "desc": "d"} for i in range(n)]


@pytest.fixture
def llm_env(tmp_path, monkeypatch):
    keyf = tmp_path / "key"
    keyf.write_text(KEY + "\n")
    monkeypatch.setenv("LITELLM_KEY_FILE", str(keyf))
    monkeypatch.delenv("DIGEST_MAX_TOKENS", raising=False)

    def go(fake, watch, items, gaps=()):
        monkeypatch.setenv("LITELLM_URL", fake.url)
        return asyncio.run(pipeline.curate_with_llm(watch, items, list(gaps)))
    return go


# --------------------------------------------------------------------------------------------
# validator
# --------------------------------------------------------------------------------------------
SCHEMA = pipeline._tiered_schema("default", ["i1", "i2"])
GOOD = {"tiers": [{"tier": 1, "items": [
    {"id": "i1", "also": [], "confidence": "HIGH", "why": "w", "follow_up": ""}]}]}


def mutate(fn):
    d = json.loads(json.dumps(GOOD))
    fn(d)
    return d


@pytest.mark.parametrize("fn,msg", [
    (lambda d: d["tiers"][0]["items"][0].update(confidence="high"),
     "$.tiers[0].items[0].confidence: 'high' is not one of ['HIGH', 'MEDIUM', 'LOW']"),
    (lambda d: d["tiers"][0]["items"][0].pop("why"), "$.tiers[0].items[0]: missing required field 'why'"),
    (lambda d: d["tiers"][0]["items"][0].update(title="x"), "$.tiers[0].items[0]: unexpected field 'title'"),
    (lambda d: d["tiers"][0].update(tier=9), "$.tiers[0].tier: 9 is not one of [1, 2, 3, 4]"),
    (lambda d: d["tiers"][0].update(tier="1"), "$.tiers[0].tier: expected integer, got str"),
    (lambda d: d["tiers"][0]["items"][0].update(id="i7"), "$.tiers[0].items[0].id: 'i7' is not one of"),
    (lambda d: d["tiers"][0]["items"][0].update(why="x" * 400), "$.tiers[0].items[0].why: 400 chars, at most 300"),
    (lambda d: d["tiers"][0]["items"][0].update(also=["i1", "i2", "i1", "i2"]), "also: 4 items, at most 3"),
    (lambda d: d.update(tiers={}), "$.tiers: expected array, got dict"),
])
def test_validator_names_the_field(fn, msg):
    with pytest.raises(pipeline.SchemaMismatch) as e:
        pipeline._check(SCHEMA, mutate(fn))
    assert msg in str(e.value)


def test_validator_accepts_good_and_empty_tiers():
    pipeline._check(SCHEMA, GOOD)
    pipeline._check(SCHEMA, {"tiers": []})  # nothing worth reporting is a valid answer


def test_tolerant_coercion_fixes_harmless_drift():
    raw = {"tiers": [{"tier": "Tier 1", "items": [
        {"id": "i1", "confidence": "medium", "why": "x" * 500, "title": "dropped"}]}]}
    notes = []
    out = pipeline._coerce(SCHEMA, raw, notes=notes)
    pipeline._check(SCHEMA, out)
    e = out["tiers"][0]["items"][0]
    assert out["tiers"][0]["tier"] == 1 and e["confidence"] == "MEDIUM"
    assert e["also"] == [] and e["follow_up"] == "" and "title" not in e and len(e["why"]) == 300
    assert len(notes) >= 5


def test_topics_schema_without_papers_allows_only_empty_papers():
    s = pipeline._topics_schema([], ["i1"])
    ok = {"topics": [{"topic": "Security", "papers": [], "news": [{"id": "i1", "blurb": "b"}]}],
          "worth_a_closer_look": [{"id": "i1", "why": "y"}]}
    pipeline._check(s, ok)
    bad = json.loads(json.dumps(ok))
    bad["topics"][0]["papers"] = [{"id": "i1", "blurb": "b"}]
    with pytest.raises(pipeline.SchemaMismatch, match=r"papers: 1 items, at most 0"):
        pipeline._check(s, bad)


# --------------------------------------------------------------------------------------------
# request shape, repair, fallback
# --------------------------------------------------------------------------------------------
def test_request_is_grammar_constrained_low_temperature_seeded(llm_env):
    with FakeLLM([tier_all()]) as f:
        out = llm_env(f, "default", news(3))
    b = f.requests[0]
    assert b["response_format"]["type"] == "json_schema"
    schema = b["response_format"]["json_schema"]["schema"]
    entry = schema["properties"]["tiers"]["items"]["properties"]["items"]["items"]
    assert entry["properties"]["id"]["enum"] == ["i1", "i2", "i3"]
    assert b["temperature"] == pipeline.LLM_TEMPERATURE and b["seed"] == pipeline.LLM_SEED
    assert b["chat_template_kwargs"] == {"enable_thinking": False}
    assert b["max_tokens"] == pipeline._chunk_max_tokens(3, pipeline.DEFAULT_MAX_TOKENS)
    assert len(out["tiers"][0]["items"]) == 3
    it = out["tiers"][0]["items"][0]
    assert it["link"].startswith("https://e.example/") and it["evidence"] == [it["link"]]
    assert out["curation"]["mode"] == "json_schema" and out["curation"]["repairs"] == 0


def test_repair_retry_sends_exact_error_at_lower_temperature(llm_env):
    bad = {"tiers": [{"tier": 7, "items": []}]}
    with FakeLLM([reply(bad), tier_all(2)]) as f:
        out = llm_env(f, "default", news(2))
    assert len(f.requests) == 2
    repair = f.requests[1]
    assert repair["temperature"] == pipeline.LLM_REPAIR_TEMPERATURE < f.requests[0]["temperature"]
    assert repair["messages"][-2]["role"] == "assistant"
    assert "$.tiers[0].tier: 7 is not one of [1, 2, 3, 4]" in repair["messages"][-1]["content"]
    assert out["tiers"][0]["tier"] == 2 and out["curation"]["repairs"] == 1


def test_invalid_json_gets_a_repair_too(llm_env):
    with FakeLLM([reply('{"tiers": [ {"tier": 1 "items": []} ]}'), tier_all()]) as f:
        out = llm_env(f, "default", news(1))
    assert "invalid JSON" in f.requests[1]["messages"][-1]["content"]
    assert out["curation"]["repairs"] == 1


def test_repair_failure_raises_with_both_errors(llm_env):
    bad1 = {"tiers": [{"tier": 7, "items": []}]}
    bad2 = {"tiers": [{"tier": 1, "items": [{"id": "i1"}]}], "extra": 1}  # extra: coerced away
    with FakeLLM([reply(bad1), reply(bad2)]) as f:
        with pytest.raises(pipeline.CurationError) as e:
            llm_env(f, "default", news(1))
    msg = str(e.value)
    assert "after one repair retry" in msg and "missing required field 'confidence'" in msg and "tier: 7" in msg


def test_schema_rejected_falls_back_to_json_object(llm_env):
    rejected = lambda body: (500, {"error": {"message": "litellm.InternalServerError: JSON schema "  # noqa: E731
                                                         "error at #: unrecognized type"}})
    # fallback output is not grammar-constrained: fenced and with drift the tolerant pass fixes
    drift = ('```json\n{"tiers": [{"tier": "1", "items": [{"id": "i1", "confidence": "high", '
             '"why": "w"}]}]}\n```')
    with FakeLLM([rejected, reply(drift)]) as f:
        out = llm_env(f, "default", news(1))
    assert f.requests[0]["response_format"]["type"] == "json_schema"
    assert f.requests[1]["response_format"] == {"type": "json_object"}
    assert out["curation"]["mode"] == "json_object"
    assert out["tiers"][0]["items"][0]["confidence"] == "HIGH"


def test_other_http_errors_do_not_fall_back(llm_env):
    with FakeLLM([lambda body: (503, {"error": "upstream down"})]) as f:
        with pytest.raises(pipeline.CurationError, match="HTTP 503"):
            llm_env(f, "default", news(1))
    assert len(f.requests) == 1


# --------------------------------------------------------------------------------------------
# finish_reason=length, chunking, selection
# --------------------------------------------------------------------------------------------
def test_finish_length_splits_the_batch(llm_env):
    def fn(body):
        if len(ids_in(body)) > 2:
            return reply('{"tiers": [{"tier": 1, "items": [{"id": "i1", "al', finish="length")(body)
        return tier_all()(body)
    with FakeLLM([fn]) as f:
        out = llm_env(f, "default", news(4))
    assert [len(ids_in(b)) for b in f.requests] == [4, 2, 2]
    assert sum(len(t["items"]) for t in out["tiers"]) == 4 and out["curation"]["splits"] == 1


def test_finish_length_on_a_single_item_fails_cleanly(llm_env):
    with FakeLLM([reply("{", finish="length")]) as f:
        with pytest.raises(pipeline.CurationError, match="hit max_tokens"):
            llm_env(f, "default", news(1))


def test_chunking_merges_batches_and_sizes_max_tokens(llm_env, monkeypatch):
    monkeypatch.setenv("DIGEST_MAX_TOKENS", "4096")
    limit = pipeline._chunk_items_limit(4096)
    items = news(25, "A") + news(25, "B")
    with FakeLLM([tier_all()]) as f:
        out = llm_env(f, "ai-security", items)
    sizes = [len(ids_in(b)) for b in f.requests]
    assert len(sizes) > 1 and max(sizes) <= limit and sum(sizes) == 50
    for b, n in zip(f.requests, sizes):
        assert b["max_tokens"] == pipeline._chunk_max_tokens(n, 4096) <= 4096
        assert len(b["messages"][1]["content"]) < pipeline.CHUNK_MAX_CHARS + 6000
    merged = out["tiers"][0]["items"]
    assert len(merged) == 50 and len({i["id"] for i in merged}) == 50
    assert out["curation"]["batches"] == len(sizes)


def test_worst_case_output_fits_the_budget():
    # A full answer for a batch (every item reported, every string at its cap) fits max_tokens
    # with a pessimistic 3 chars/token.
    for ceiling in (1024, 4096, 8192, 16384):
        n = pipeline._chunk_items_limit(ceiling)
        ids = [f"i{k}" for k in range(1, n + 1)]
        e = {"id": ids[-1], "also": ids[-3:], "confidence": "MEDIUM",
             "why": "x" * pipeline.WHY_MAX, "follow_up": "x" * pipeline.FOLLOW_UP_MAX}
        worst = json.dumps({"tiers": [{"tier": 1, "items": [e] * n}]}, indent=2)
        assert len(worst) / 3 <= pipeline._chunk_max_tokens(n, ceiling)


def test_select_items_is_fair_and_newest_first():
    items = news(200, "Big", day=3) + news(5, "Small", day=1)
    sel = pipeline._select_items(items, limit=20)
    assert len(sel) == 20
    assert sum(1 for i in sel if i["source"] == "Small") == 5    # not crowded out
    ts = [pipeline._item_ts(i) for i in sel]
    assert ts == sorted(ts, reverse=True)


def test_over_budget_items_are_counted(llm_env, monkeypatch):
    monkeypatch.setattr(pipeline, "MAX_TOTAL_ITEMS", 10)
    with FakeLLM([tier_all()]) as f:
        out = llm_env(f, "default", news(30))
    assert out["curation"]["sent"] == 10 and out["curation"]["not_sent"] == 20


def test_topics_merge_caps_and_picks(llm_env, monkeypatch):
    monkeypatch.setenv("DIGEST_MAX_TOKENS", "2048")   # small batches -> several of them
    items = news(12, "N")

    def fn(body):
        ids = ids_in(body)
        return reply({"topics": [{"topic": "Security", "papers": [],
                                  "news": [{"id": i, "blurb": "b"} for i in ids[:5]]},
                                 {"topic": "Security", "papers": [], "news": []}],
                      "worth_a_closer_look": [{"id": i, "why": "y"} for i in ids[:3]]})(body)
    with FakeLLM([fn]) as f:
        out = llm_env(f, "ai-research", items)
    assert len(f.requests) > 1
    assert [t["topic"] for t in out["topics"]] == ["Security"]
    assert len(out["topics"][0]["news"]) == pipeline.MAX_NEWS_PER_TOPIC
    assert len(out["worth_a_closer_look"]) == 3 and len(set(out["worth_a_closer_look"])) == 3


# --------------------------------------------------------------------------------------------
# run_watch integration: empty input, failure banner, rendering
# --------------------------------------------------------------------------------------------
def test_no_new_items_skips_llm_and_renders_clean_run(env):  # noqa: F811
    calls = []

    async def curate(watch, deduped, gaps):
        calls.append(len(deduped))
        return {"tiers": []}
    run(env, curate)                                 # first run sees everything
    ev = run(env, curate, run_id="20261004T000001Z")  # second: nothing new
    assert calls == [3] and ev[-1][0] == "done"
    out = json.loads((env / "runs" / "default" / "20261004T000001Z.json").read_text())
    assert out["uncurated"] is False and out["curation_error"] is None and out["items"] == []
    assert out["curation"] == {"skipped": "no new items"}
    assert "Nothing new since 20" in out["markdown"] and "Uncurated" not in out["markdown"]


def test_failure_shows_banner_and_error_field(env):  # noqa: F811
    async def curate(watch, deduped, gaps):
        raise pipeline.CurationError("LLM output does not match the default schema: $.tiers: boom")
    run(env, curate)
    out = json.loads((env / "runs" / "default" / "20261004T000000Z.json").read_text())
    assert out["uncurated"] is True and "$.tiers: boom" in out["curation_error"]
    md = (env / "runs" / "default" / "20261004T000000Z.md").read_text()
    assert md.startswith("# Threat Intel Digest")
    assert "> **Curation failed:** LLM output does not match" in md
    assert "CVE-2026-0001" in md and "Story A" in md   # every item still listed


def test_structured_result_is_rendered_to_markdown(env):  # noqa: F811
    async def curate(watch, deduped, gaps):
        return {"tiers": [{"tier": 1, "items": [{
            "title": "Bad [title] *x*", "link": "https://e.example/a (b)", "source": "SANS_ISC",
            "date": "2026-10-03", "cve": "CVE-2026-0001", "confidence": "HIGH", "why": "Because.",
            "follow_up": "Patch.", "evidence": []}]}]}
    run(env, curate)
    out = json.loads((env / "runs" / "default" / "20261004T000000Z.json").read_text())
    md = out["markdown"]
    assert out["uncurated"] is False and out["curation_error"] is None
    assert "## Tier 1: actively exploited" in md
    assert "**[Bad (title) x](https://e.example/a%20%28b%29)** · SANS_ISC · 2026-10-03 · CVE-2026-0001" in md
    assert "*Next:* Patch." in md
