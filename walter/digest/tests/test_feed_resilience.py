"""Feed cache, 403/429 backoff and the CISA_Advisories fallback (CISA CSAF repository).

Local HTTP server and sanitized fixtures only (synthetic vendors; advisory day 999 cannot
exist). A fake clock drives the cache, so nothing sleeps."""
import json

import pytest

import collect_threat_intel
import csaf
import feedlib
import pipeline
from test_collectors import FAST, FIX, SINCE, XML, fx, srv  # noqa: F401  (srv is a fixture)

HOUR = 3600.0
CACHE_CFG = {"min_refetch": 15 * 60, "block_base": HOUR, "block_max": 24 * HOUR,
             "stale_max": 72 * HOUR}
CSAF_FILES = ("icsa-26-999-02", "icsa-26-999-03", "icsma-26-999-01")


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_cache(tmp_path, clock, **over):
    return feedlib.FeedCache(tmp_path / "feedcache", dict(CACHE_CFG, **over), clock=clock)


def csaf_routes(changes=None):
    routes = {"/csaf/changes.csv": [(200, {"Content-Type": "text/plain", "ETag": '"c1"'},
                                     changes or fx("csaf_changes.csv"))]}
    for f in CSAF_FILES:
        routes[f"/csaf/2026/{f}.json"] = [(200, {"Content-Type": "text/plain"}, fx(f"csaf_{f}.json"))]
    return routes


def fallback_run(s, cache, name="CISA_Advisories"):
    return feedlib.collect_with_fallbacks(
        name, ("feed", s.base + "/all.xml"),
        [("csaf_changes", s.base + "/csaf/changes.csv", "cisagov/CSAF (ICS advisories)")],
        SINCE, 15, FAST, desc_limit=220, fetcher=cache.fetch, cache=cache,
        handlers=collect_threat_intel.HANDLERS)


# --- conditional GET and minimum refetch interval ---------------------------------------------
def test_conditional_get_304_serves_cached_body(srv, tmp_path):
    clock = Clock()
    s = srv({"/f": [(200, dict(XML, ETag='"v1"', **{"Last-Modified": "Thu, 01 Oct 2026 10:00:00 GMT"}),
                     fx("atom_blog.xml")),
                    (304, {"ETag": '"v1"'}, b"")]})
    cache = make_cache(tmp_path, clock)
    raw1, info1 = cache.fetch(s.base + "/f", FAST)
    assert info1["cache"] == "fetched" and b"<feed" in raw1
    assert "If-None-Match" not in s.req_headers["/f"][0]
    clock.t += 16 * 60                                       # past the minimum interval
    res = feedlib.collect_feed("Blog", s.base + "/f", SINCE, 40, FAST, fetcher=cache.fetch)
    sent = s.req_headers["/f"][1]
    assert sent["If-None-Match"] == '"v1"'
    assert sent["If-Modified-Since"] == "Thu, 01 Oct 2026 10:00:00 GMT"
    assert s.hits["/f"] == 2
    assert res["ok"] and res["count"] == 2 and res["cache"] == "revalidated (304)"
    assert cache.meta(s.base + "/f")["fetched_at"] == clock.t      # the interval restarts


def test_min_refetch_interval_serves_cache_without_a_request(srv, tmp_path):
    clock = Clock()
    s = srv({"/f": [(200, XML, fx("atom_blog.xml"))]})
    cache = make_cache(tmp_path, clock)
    cache.fetch(s.base + "/f", FAST)
    for _ in range(3):                                       # RUN NOW pressed repeatedly
        clock.t += 60
        raw, info = cache.fetch(s.base + "/f", FAST)
        assert info["cache"].startswith("fresh") and b"<feed" in raw
    assert s.hits["/f"] == 1
    clock.t += 15 * 60
    _, info = cache.fetch(s.base + "/f", FAST)               # no validators: a plain refetch
    assert s.hits["/f"] == 2 and info["cache"] == "fetched"


def test_cache_body_mismatch_is_ignored(srv, tmp_path):
    clock = Clock()
    s = srv({"/f": [(200, dict(XML, ETag='"v1"'), fx("atom_blog.xml"))]})
    cache = make_cache(tmp_path, clock)
    cache.fetch(s.base + "/f", FAST)
    (tmp_path / "feedcache" / f"{cache._key(s.base + '/f')}.body").write_bytes(b"tampered")
    clock.t += 60
    _, info = cache.fetch(s.base + "/f", FAST)
    assert info["cache"] == "fetched" and "If-None-Match" not in s.req_headers["/f"][1]


# --- 403/429: persisted, growing backoff -----------------------------------------------------
def test_403_backoff_is_persisted_grows_and_expires(srv, tmp_path):
    clock = Clock()
    t0 = clock.t
    denied = (403, {"Content-Type": "text/html"}, b"<HTML><HEAD><TITLE>Access Denied</TITLE>")
    s = srv({"/f": [denied, denied, (200, XML, fx("atom_blog.xml"))]})
    url = s.base + "/f"
    with pytest.raises(feedlib.Blocked, match=r"HTTP 403 .*backing off until .*\(block #1\)"):
        make_cache(tmp_path, clock).fetch(url, FAST)
    assert s.hits["/f"] == 1                                 # 403 is not retried
    clock.t = t0 + 600
    with pytest.raises(feedlib.Blocked, match="no request sent"):
        make_cache(tmp_path, clock).fetch(url, FAST)         # a new process: state is on disk
    assert s.hits["/f"] == 1
    clock.t = t0 + HOUR + 1                                  # first backoff (1 h) expired
    with pytest.raises(feedlib.Blocked, match=r"block #2"):
        make_cache(tmp_path, clock).fetch(url, FAST)
    assert s.hits["/f"] == 2
    blk = make_cache(tmp_path, clock).meta(url)["block"]
    assert blk["count"] == 2 and blk["until"] == clock.t + 2 * HOUR and blk["since"] == t0
    clock.t += 2 * HOUR - 1
    with pytest.raises(feedlib.Blocked):
        make_cache(tmp_path, clock).fetch(url, FAST)
    assert s.hits["/f"] == 2
    clock.t += 2
    raw, info = make_cache(tmp_path, clock).fetch(url, FAST)
    assert info["cache"] == "fetched" and s.hits["/f"] == 3
    assert "block" not in make_cache(tmp_path, clock).meta(url)


def test_429_honours_retry_after_and_is_not_retried(srv, tmp_path):
    clock = Clock()
    s = srv({"/f": [(429, {"Retry-After": str(int(5 * HOUR))}, b"slow down")]})
    cache = make_cache(tmp_path, clock)
    with pytest.raises(feedlib.Blocked, match="HTTP 429"):
        cache.fetch(s.base + "/f", dict(FAST, retries=1))
    assert s.hits["/f"] == 1
    assert cache.meta(s.base + "/f")["block"]["until"] == clock.t + 5 * HOUR


def test_backoff_is_capped(tmp_path):
    clock = Clock()
    cache = make_cache(tmp_path, clock)
    cache._save_meta("u", {"block": {"status": 403, "count": 9, "since": 0, "last": 0, "until": 0}})

    def denied(url, cfg, headers=None, retry_status=None):
        raise feedlib.FetchError("HTTP 403 Forbidden", status=403)
    cache._fetch = denied
    with pytest.raises(feedlib.Blocked, match="block #10"):
        cache.fetch("u", FAST)
    assert cache.meta("u")["block"]["until"] == clock.t + 24 * HOUR


def test_cache_from_env(tmp_path):
    assert feedlib.FeedCache.from_env({}) is None
    assert feedlib.FeedCache.from_env({"STATE_DIR": str(tmp_path), "DIGEST_CACHE_DIR": "off"}) is None
    c = feedlib.FeedCache.from_env({"STATE_DIR": str(tmp_path), "DIGEST_MIN_REFETCH_MINUTES": "5"})
    assert c.root == str(tmp_path / "feedcache") and c.cfg["min_refetch"] == 300
    c = feedlib.FeedCache.from_env({"DIGEST_CACHE_DIR": str(tmp_path / "x")})
    assert c.root == str(tmp_path / "x") and c.cfg["block_base"] == HOUR


def test_unwritable_cache_never_fails_a_fetch(srv, tmp_path):
    (tmp_path / "file").write_text("x")
    s = srv({"/f": [(200, XML, fx("atom_blog.xml"))]})
    cache = feedlib.FeedCache(tmp_path / "file" / "sub", CACHE_CFG, clock=Clock())
    raw, info = cache.fetch(s.base + "/f", FAST)
    assert b"<feed" in raw and info["cache"] == "fetched"


# --- CSAF: the new source format ----------------------------------------------------------------
def test_csaf_changes_and_document_parsing():
    rows = csaf.parse_changes(fx("csaf_changes.csv"))
    assert rows[0] == ("2026/icsa-26-999-03.json", "2026-10-03T06:00:00.000000Z") and len(rows) == 4
    it = csaf.parse_doc(fx("csaf_icsa-26-999-03.json"), "https://raw.example/x.json", 220)
    assert it == {"title": "Samplesoft HMI Studio",
                  "link": "https://www.cisa.gov/news-events/ics-advisories/icsa-26-999-03",
                  "date": "2026-10-03T06:00:00Z",
                  "desc": "Synthetic: successful exploitation could crash the sample HMI.",
                  "arxiv_id": "", "advisory_id": "ICSA-26-999-03"}
    med = csaf.parse_doc(fx("csaf_icsma-26-999-01.json"))      # no web reference: built from id
    assert med["link"] == "https://www.cisa.gov/news-events/ics-medical-advisories/icsma-26-999-01"
    for bad in (b"<html>Access Denied</html>", b'{"a": 1}', b"", b"just,text\n"):
        with pytest.raises(ValueError):
            csaf.parse_changes(bad)


def test_csaf_collect_window_doc_cache_and_budget(srv, tmp_path):
    clock = Clock()
    s = srv(csaf_routes())
    cache = make_cache(tmp_path, clock)
    url = s.base + "/csaf/changes.csv"
    res = csaf.collect_changes("X", url, SINCE, 15, FAST, 220, cache.fetch, cache,
                               env={"DIGEST_CSAF_MAX_DOCS": "2"})
    assert res["ok"] and res["raw_count"] == 4 and res["in_window"] == 3 and res["older"] == 1
    assert [i["advisory_id"] for i in res["items"]] == ["ICSA-26-999-03", "ICSMA-26-999-01"]
    assert "1 advisory document(s) left for the next run" in res["note"]
    clock.t += HOUR
    res = csaf.collect_changes("X", url, SINCE, 15, FAST, 220, cache.fetch, cache,
                               env={"DIGEST_CSAF_MAX_DOCS": "2"})
    assert res["count"] == 3 and res["docs_fetched"] == 1 and "left for" not in res["note"]
    assert all(s.hits[f"/csaf/2026/{f}.json"] == 1 for f in CSAF_FILES)   # docs fetched once
    assert s.req_headers["/csaf/changes.csv"][1]["If-None-Match"] == '"c1"'


def test_csaf_all_documents_failing_is_a_failure(srv):
    s = srv({"/csaf/changes.csv": [(200, {}, fx("csaf_changes.csv"))]})      # docs 404
    s.routes.update({f"/csaf/2026/{f}.json": [(404, {}, b"nope")] for f in CSAF_FILES})
    res = csaf.collect_changes("X", s.base + "/csaf/changes.csv", SINCE, 15, FAST)
    assert not res["ok"] and "HTTP 404" in res["error"]


# --- fallback --------------------------------------------------------------------------------
def test_primary_ok_never_touches_the_fallback(srv, tmp_path):
    routes = csaf_routes()
    routes["/all.xml"] = [(200, XML, fx("cisa_advisories_rss.xml"))]
    s = srv(routes)
    res = fallback_run(s, make_cache(tmp_path, Clock()))
    assert res["ok"] and res["via"] == "primary" and res["count"] == 3
    assert [i["advisory_id"] for i in res["items"]] == ["ICSA-26-999-01", "ICSA-26-999-02", "AA26-999A"]
    assert set(s.hits) == {"/all.xml"}


def test_fallback_used_when_primary_blocked_then_backing_off(srv, tmp_path):
    clock = Clock()
    routes = csaf_routes()
    routes["/all.xml"] = [(403, {"Content-Type": "text/html"}, b"<HTML>Access Denied</HTML>")]
    s = srv(routes)
    res = fallback_run(s, make_cache(tmp_path, clock))
    assert res["ok"] and res["error"] is None and res["via"] == "fallback"
    assert [i["advisory_id"] for i in res["items"]] == ["ICSA-26-999-03", "ICSMA-26-999-01", "ICSA-26-999-02"]
    assert "HTTP 403" in res["note"] and "served via fallback cisagov/CSAF" in res["note"]
    assert "ICS and ICS medical advisories only" in res["note"]
    # RUN NOW again: the primary is backing off (no request), the fallback is served from cache
    clock.t += 300
    res2 = fallback_run(s, make_cache(tmp_path, clock))
    assert res2["ok"] and res2["via"] == "fallback" and "no request sent" in res2["note"]
    assert res2["items"] == res["items"]
    assert s.hits["/all.xml"] == 1 and s.hits["/csaf/changes.csv"] == 1
    assert all(s.hits[f"/csaf/2026/{f}.json"] == 1 for f in CSAF_FILES)


def test_primary_and_fallback_both_failing_is_one_gap(srv, tmp_path):
    s = srv({"/all.xml": [(403, {}, b"denied")], "/csaf/changes.csv": [(503, {}, b"down")]})
    res = fallback_run(s, make_cache(tmp_path, Clock()))
    assert not res["ok"] and res["items"] == []
    assert "primary" in res["error"] and "HTTP 403" in res["error"]
    assert "fallback cisagov/CSAF (ICS advisories): HTTP 503" in res["error"]


def test_dedupe_across_primary_and_fallback_and_pipeline_normalisation(srv, tmp_path):
    """Run 1: the primary works (its items are reported and recorded by the pipeline).
    Run 2: the primary is blocked; the fallback lists one advisory the primary already had
    (ICSA-26-999-02, under a different title) and two new ones. The result is deduped by
    advisory id, keeps the primary's title, and the pipeline reports only the new ones."""
    clock = Clock()
    routes = csaf_routes()
    routes["/all.xml"] = [(200, XML, fx("cisa_advisories_rss.xml")),
                          (403, {"Content-Type": "text/html"}, b"<HTML>Access Denied</HTML>")]
    s = srv(routes)
    run1 = fallback_run(s, make_cache(tmp_path, clock))
    state = {"seen": {"events": {}}, "sources": {}}
    first = pipeline._dedupe("default", {"CISA_Advisories": run1}, state)
    for it in first:
        state["seen"]["events"][it["key"]] = {"title": it["title"]}

    clock.t += 2 * HOUR
    run2 = fallback_run(s, make_cache(tmp_path, clock))
    assert run2["via"] == "fallback" and s.hits["/all.xml"] == 2
    ids = [i["advisory_id"] for i in run2["items"]]
    assert len(ids) == len(set(ids)) == 5          # 3 CSAF + the stale primary copy, by id
    assert set(ids) == {"ICSA-26-999-01", "ICSA-26-999-02", "ICSA-26-999-03",
                        "ICSMA-26-999-01", "AA26-999A"}
    dup = next(i for i in run2["items"] if i["advisory_id"] == "ICSA-26-999-02")
    assert dup["title"] == "Examplecorp FlowMeter Gateway"   # not "... (Update A)"
    assert "merged 3 item(s) from the primary copy" in run2["note"]

    new = pipeline._dedupe("default", {"CISA_Advisories": run2}, state)
    assert {i["title"] for i in new} == {"Samplesoft HMI Studio", "Example Medical InfusionHub"}
    hmi = next(i for i in new if i["title"] == "Samplesoft HMI Studio")
    assert hmi == {"kind": "news", "key": "samplesoft-hmi-studio", "title": "Samplesoft HMI Studio",
                   "source": "CISA_Advisories", "date": "2026-10-03T06:00:00Z",
                   "link": "https://www.cisa.gov/news-events/ics-advisories/icsa-26-999-03",
                   "desc": "Synthetic: successful exploitation could crash the sample HMI."}
    # the same advisory from the primary normalises to the same key and link
    prim = next(i for i in first if "999-02" in i["link"])
    assert (prim["key"], prim["link"]) == ("examplecorp-flowmeter-gateway",
                                           "https://www.cisa.gov/news-events/ics-advisories/icsa-26-999-02")


def test_merge_items_prefers_first_list_and_sorts():
    a = [{"title": "A", "link": "https://x.example/icsa-26-999-01", "date": "2026-10-01T00:00:00Z"}]
    b = [{"title": "A2", "link": "https://y.example/ICSA-26-999-01.json", "date": "2026-10-05T00:00:00Z"},
         {"title": "B", "link": "https://x.example/b", "date": ""},
         {"title": "C", "link": "https://x.example/c", "date": "2026-10-02T00:00:00Z"}]
    out = feedlib.merge_items(a, b, max_items=10)
    assert [i["title"] for i in out] == ["C", "A", "B"]
    assert feedlib.merge_items(a, b, max_items=1)[0]["title"] == "C"


def test_threat_intel_config_and_wiring(monkeypatch):
    fb = collect_threat_intel.FALLBACKS["CISA_Advisories"]
    assert fb[0][0] == "csaf_changes" and fb[0][1].startswith("https://raw.githubusercontent.com/cisagov/CSAF/")
    assert fb[0][1].endswith("/OT/white/changes.csv")
    assert set(collect_threat_intel.SOURCES) == {"CISA_KEV", "CISA_Advisories", "SANS_ISC",
                                                 "BleepingComputer", "TheHackerNews"}
    seen = {}

    def fake(name, primary, fallbacks, since, max_items, cfg, **kw):
        seen.update(name=name, primary=primary, fallbacks=fallbacks, max_items=max_items, **kw)
        return {"ok": True}
    monkeypatch.setattr(feedlib, "collect_with_fallbacks", fake)
    collect_threat_intel.collect_source("CISA_Advisories", collect_threat_intel.SOURCES["CISA_Advisories"],
                                        FAST, since=SINCE, cache="C")
    assert seen["primary"][1].endswith("/cybersecurity-advisories/all.xml")
    assert seen["max_items"] == 15 and seen["cache"] == "C" and seen["desc_limit"] == 220


def test_fallback_item_keeps_the_title_the_primary_gave_it(srv, tmp_path):
    """Without a usable primary copy (older than DIGEST_STALE_MAX_HOURS) the fallback items
    stand alone; a known advisory id still gets the primary's title, so the pipeline's title
    key matches what it recorded when the primary reported it."""
    clock = Clock()
    routes = csaf_routes()
    routes["/all.xml"] = [(200, XML, fx("cisa_advisories_rss.xml")), (403, {}, b"denied")]
    s = srv(routes)
    fallback_run(s, make_cache(tmp_path, clock))
    clock.t += 100 * HOUR                                   # the primary copy is too old to merge
    res = fallback_run(s, make_cache(tmp_path, clock))
    assert res["via"] == "fallback" and "merged" not in res["note"]
    assert [i["title"] for i in res["items"]] == [
        "Samplesoft HMI Studio", "Example Medical InfusionHub", "Examplecorp FlowMeter Gateway"]
    ids = json.loads((tmp_path / "feedcache" / "ids-CISA_Advisories.json").read_text())
    assert ids["AA26-999A"] == "Sample Actors Target Example Networks"
