"""Deferred-item tests: items over the LLM prompt budget stay out of `seen` and pin their
source's cutoff, so the next run re-sends them (pipeline: `sent_keys` / deferred cutoff pin).

No network: `curate_with_llm` runs against the local FakeLLM; collectors are faked with a
fixed feed (as test_state_sample.run_with does); state starts from the sanitized samples.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

import pipeline
from test_curation import FakeLLM, ids_in, news, reply, tier_all  # noqa: F401  (helpers)
from test_pipeline import KEY  # noqa: F401  (fake key constant)
from test_state_sample import SAMPLE_DIR, FEED_COLLECTOR, run_with, seeded_dir  # noqa: F401  (helpers)

BUDGET = 10   # pipeline.MAX_TOTAL_ITEMS, monkeypatched to force deferrals


def llm_env(tmp_path, monkeypatch, fake):
    keyf = tmp_path / "key"
    keyf.write_text(KEY + "\n")
    monkeypatch.setenv("LITELLM_KEY_FILE", str(keyf))
    monkeypatch.delenv("DIGEST_MAX_TOKENS", raising=False)
    monkeypatch.setenv("LITELLM_URL", fake.url)
    monkeypatch.setattr(pipeline, "MAX_TOTAL_ITEMS", BUDGET)


def run_llm(tmp_path, monkeypatch, watch, state_dir, feed, run_id="20260106T000000Z"):
    """As test_state_sample.run_with, but with the real curate_with_llm (against the FakeLLM)."""
    feed_file = tmp_path / f"feed-{run_id}.json"
    feed_file.write_text(json.dumps(feed))
    col = tmp_path / "feedcol.py"
    col.write_text(FEED_COLLECTOR)
    monkeypatch.setattr(pipeline, "_collector_cmd",
               lambda w, out: [sys.executable, str(col), str(feed_file), out])
    events = []

    async def progress(stage, **kw):
        events.append((stage, kw))

    asyncio.run(pipeline.run_watch(watch, state_dir, progress, run_id=run_id,
                                   curate_fn=pipeline.curate_with_llm))
    assert events[-1][0] == "done", events
    out = json.loads((state_dir / "runs" / watch / f"{run_id}.json").read_text())
    saved = json.loads((state_dir / "state" / f"{watch}.json").read_text())
    return out, saved


def kev(n, day=6):
    return [{"cveID": f"CVE-2099-{100 + i}", "vendorProject": "Sample Vendor",
             "product": f"Sample Product {i}", "vulnerabilityName": f"Sample vulnerability {i}",
             "dateAdded": f"2026-01-{day:02d}", "dueDate": f"2026-02-{day:02d}",
             "knownRansomwareCampaignUse": "Unknown"} for i in range(n)]


def feed_default():
    """More new news + KEV items than BUDGET, each dated after every sample cutoff."""
    return {"CISA_KEV": {"ok": True, "recent": kev(8)},
            "SANS_ISC": {"ok": True, "items": news(8, "S", day=7)},
            "TheHackerNews": {"ok": True, "items": news(8, "T", day=8)}}


def papers(n, day=6):
    return [{"title": f"Sample paper {i}", "link": f"https://e.example/abs/9999.10{i:02d}",
             "arxiv_id": f"9999.10{i:02d}", "date": f"2026-01-{day:02d}T00:00:00Z",
             "desc": "d"} for i in range(n)]


def feed_research():
    return {"arxiv_csAI": {"ok": True, "items": papers(6)},
            "OpenAINews": {"ok": True, "items": news(8, "O", day=7)}}


def topics_all():
    """A valid ai-research answer: every id of the request in one topic, 3 distinct picks."""
    def fn(body):
        ids = ids_in(body)
        return reply({"topics": [{"topic": "Security",
                                  "papers": [{"id": i, "blurb": "b"} for i in ids],
                                  "news": []}],
                      "worth_a_closer_look": [{"id": i, "why": "w"} for i in ids[:3]]})(body)
    return fn


def state_file(d: Path, watch: str) -> dict:
    return json.loads((d / "state" / f"{watch}.json").read_text())


# ---------------------------------------------------------------------------------------------
# 1 + 3. default: over-budget run records only the sent items; deferred sources pin their cutoff
# ---------------------------------------------------------------------------------------------
def test_default_deferred_stay_unseen_and_pin_cutoff(tmp_path, monkeypatch):
    d = seeded_dir(tmp_path, "default")
    before = state_file(d, "default")
    with FakeLLM([tier_all()]) as f:
        llm_env(tmp_path, monkeypatch, f)
        out, saved = run_llm(tmp_path, monkeypatch, "default", d, feed_default())
    cur = out["curation"]
    assert cur["sent"] == BUDGET and cur["not_sent"] == 24 - BUDGET

    # the sample's already-seen entries survive, untouched
    assert saved["seen"]["cves"]["CVE-2099-0001"] == before["seen"]["cves"]["CVE-2099-0001"]
    assert saved["seen"]["events"]["sample-advisory-1"] == before["seen"]["events"]["sample-advisory-1"]

    # only the sent items are new in seen; the deferred ones are absent
    sent_ids = set(cur["sent_keys"])
    new_cves = set(saved["seen"]["cves"]) - set(before["seen"]["cves"])
    new_events = set(saved["seen"]["events"]) - set(before["seen"]["events"])
    assert new_cves | new_events == sent_ids
    assert len(new_cves) + len(new_events) == BUDGET
    for it in out["items"]:
        if it["kind"] == "cve":
            assert (it["cve"] in new_cves) == (it["cve"] in sent_ids)
        else:
            assert (it["key"] in new_events) == (it["key"] in sent_ids)

    # every new item's source had deferred items, so every ok source's cutoff is pinned to its
    # oldest deferred item's date and stays at or before the run time
    assert saved["cutoff"] > "2026-01-05T12:00:00Z"
    for name in ("CISA_KEV", "SANS_ISC", "TheHackerNews"):
        oldest = min(pipeline._parse_date(it.get("date") or it.get("date_added"))
                     for it in out["items"] if it["source"] == name and _item_id(it) not in sent_ids)
        assert saved["sources"][name]["cutoff"] == oldest.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert saved["sources"][name]["cutoff"] <= saved["cutoff"]
    # the failed source keeps its old cutoff
    assert saved["sources"]["CISA_Advisories"]["cutoff"] == "2026-01-03T12:00:00Z"


def test_default_deferred_come_back_and_run_dry(tmp_path, monkeypatch):
    """Same state dir, same feed: deferred items are new again; after enough runs every item is
    in seen exactly once and a further run has no new items."""
    d = seeded_dir(tmp_path, "default")
    feed = feed_default()
    all_ids = {it["cve"] if it["kind"] == "cve" else it["key"] for it in pipeline._dedupe(
        "default", feed, state_file(d, "default"))}
    assert len(all_ids) == 24

    with FakeLLM([tier_all()]) as f:
        llm_env(tmp_path, monkeypatch, f)
        out1, saved1 = run_llm(tmp_path, monkeypatch, "default", d, feed,
                               run_id="20260106T000000Z")
        # run 2: the deferred items are new again and are now sent
        out2, saved2 = run_llm(tmp_path, monkeypatch, "default", d, feed,
                               run_id="20260106T010000Z")
    deferred = [it for it in out1["items"] if _item_id(it) not in set(out1["curation"]["sent_keys"])]
    assert set(_item_id(it) for it in deferred) == set(out2["curation"]["sent_keys"])
    assert out2["curation"]["not_sent"] == 0

    # run 3: nothing new left; no LLM call, no new seen entries
    out3, saved3 = run_with(tmp_path, monkeypatch, "default", d, feed,
                            run_id="20260106T020000Z")
    assert out3["curation"] == {"skipped": "no new items"}
    assert len(f.requests) == 2
    assert saved3["seen"] == saved2["seen"]

    # every item in seen exactly once: all 24 new ids present, none recorded twice
    seen_ids = set(saved3["seen"]["cves"]) | set(saved3["seen"]["events"])
    assert all_ids <= seen_ids
    assert len(all_ids) == 24


# ---------------------------------------------------------------------------------------------
# 3b. an ok source with nothing deferred gets the run time
# ---------------------------------------------------------------------------------------------
def test_source_without_deferrals_gets_run_time(tmp_path, monkeypatch):
    d = seeded_dir(tmp_path, "default")
    feed = feed_default()
    feed["BleepingComputer"] = {"ok": True, "items": news(2, "B", day=7)}
    with FakeLLM([tier_all()]) as f:
        llm_env(tmp_path, monkeypatch, f)
        out, saved = run_llm(tmp_path, monkeypatch, "default", d, feed)
    sent = set(out["curation"]["sent_keys"])
    assert not any(it["source"] == "BleepingComputer" and _item_id(it) not in sent for it in out["items"])
    assert saved["sources"]["BleepingComputer"]["cutoff"] == saved["cutoff"]  # the run time
    # a source with deferred items stays pinned, earlier than the run time
    assert saved["sources"]["CISA_KEV"]["cutoff"] < saved["cutoff"]


# ---------------------------------------------------------------------------------------------
# 4. curation failure: every item is recorded (the fallback digest lists them)
# ---------------------------------------------------------------------------------------------
def test_curation_failure_records_everything(tmp_path, monkeypatch):
    d = seeded_dir(tmp_path, "default")
    before = state_file(d, "default")
    feed = feed_default()
    all_ids = {_item_id(it) for it in pipeline._dedupe("default", feed, before)}

    def boom(body):
        return (500, {"error": "internal error"})
    with FakeLLM([boom]) as f:
        llm_env(tmp_path, monkeypatch, f)
        out, saved = run_llm(tmp_path, monkeypatch, "default", d, feed)
    assert out["uncurated"] is True and out["curation"] == {"failed": True}
    new_cves = set(saved["seen"]["cves"]) - set(before["seen"]["cves"])
    new_events = set(saved["seen"]["events"]) - set(before["seen"]["events"])
    assert all_ids <= new_cves | new_events


# ---------------------------------------------------------------------------------------------
# 5. a curate_fn that reports no sent_keys: every item is recorded (old behaviour)
# ---------------------------------------------------------------------------------------------
def test_curate_fn_without_sent_keys_records_everything(tmp_path, monkeypatch):
    d = seeded_dir(tmp_path, "default")
    feed = feed_default()
    out, saved = run_with(tmp_path, monkeypatch, "default", d, feed)   # run_with's _curate
    all_ids = {_item_id(it) for it in out["items"]}
    seen_ids = set(saved["seen"]["cves"]) | set(saved["seen"]["events"])
    assert all_ids <= seen_ids


# ---------------------------------------------------------------------------------------------
# 6. ai-research: deferred papers and news come back on run 2
# ---------------------------------------------------------------------------------------------
def test_research_deferred_papers_and_news_come_back(tmp_path, monkeypatch):
    d = seeded_dir(tmp_path, "ai-research")
    before = state_file(d, "ai-research")
    feed = feed_research()
    all_ids = {_item_id(it) for it in pipeline._dedupe("ai-research", feed, before)}
    assert len(all_ids) == 14

    with FakeLLM([topics_all()]) as f:
        llm_env(tmp_path, monkeypatch, f)
        out1, saved1 = run_llm(tmp_path, monkeypatch, "ai-research", d, feed,
                               run_id="20260106T000000Z")
        out2, saved2 = run_llm(tmp_path, monkeypatch, "ai-research", d, feed,
                               run_id="20260106T010000Z")
    sent1 = set(out1["curation"]["sent_keys"])
    deferred = [it for it in out1["items"] if _item_id(it) not in sent1]
    assert deferred and out1["curation"]["not_sent"] == len(deferred)

    # run 1: only the sent ids are new in seen.papers / seen.items; the sample's entries survive
    new_papers = set(saved1["seen"]["papers"]) - set(before["seen"]["papers"])
    new_items = set(saved1["seen"]["items"]) - set(before["seen"]["items"])
    assert new_papers | new_items == sent1
    assert saved1["seen"]["papers"]["9999.00011"] == before["seen"]["papers"]["9999.00011"]

    # run 2: the deferred papers and news are sent again
    assert set(_item_id(it) for it in deferred) == set(out2["curation"]["sent_keys"])
    assert out2["curation"]["not_sent"] == 0

    # after enough runs every item is in seen exactly once and a further run has no new items
    out3, saved3 = run_with(tmp_path, monkeypatch, "ai-research", d, feed,
                            run_id="20260106T020000Z")
    assert out3["curation"] == {"skipped": "no new items"}
    assert saved3["seen"] == saved2["seen"]
    assert all_ids <= set(saved3["seen"]["papers"]) | set(saved3["seen"]["items"])


def _item_id(it: dict) -> str:
    """The state `seen` key of an item (matches pipeline._item_id)."""
    if it["kind"] == "cve":
        return it["cve"]
    if it["kind"] == "paper":
        return it["arxiv_id"]
    return it["key"]
