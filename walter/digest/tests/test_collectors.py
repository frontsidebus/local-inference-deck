"""Collector tests: fixture feeds (tests/fixtures, sanitized samples of the real formats) and a
local HTTP server. No network."""
import asyncio
import gzip
import http.server
import json
import sys
import threading
import time
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import collect_ai_digest
import collect_threat_intel
import feedlib
import pipeline

FIX = Path(__file__).parent / "fixtures"
UTC = timezone.utc
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
SINCE = datetime(2026, 9, 28, tzinfo=UTC)
FAST = {"timeout": 2, "retries": 1, "backoff": 0, "max_bytes": 1024 * 1024,
        "undated_cap": 30, "workers": 4}


def fx(name: str) -> bytes:
    return (FIX / name).read_bytes()


# --- parsing: every format we meet ------------------------------------------------------------
def test_arxiv_rss_weekday_namespaces_and_ids():
    items, meta, err = feedlib.parse_feed(fx("arxiv_rss_weekday.xml"))
    assert err is None and meta["format"] == "rss"
    assert [i["arxiv_id"] for i in items] == ["2610.01001", "2610.01002", "2509.09999"]
    assert [i["announce_type"] for i in items] == ["new", "cross", "replace"]
    assert items[0]["date"] == "Tue, 06 Oct 2026 00:00:00 -0400"
    assert "<b>" not in items[0]["desc"] and "markup" in items[0]["desc"]


def test_arxiv_weekend_empty_listing_is_ok_not_a_gap():
    items, meta, err = feedlib.parse_feed(fx("arxiv_rss_weekend_empty.xml"))
    assert err is None and items == [] and meta["empty"] is True
    res = feedlib.collect_feed("arxiv_csAI", "https://feed.invalid/x", SINCE, 40, FAST,
                               fetcher=lambda u, c: (fx("arxiv_rss_weekend_empty.xml"), {}))
    assert res["ok"] is True and res["error"] is None and res["count"] == 0
    assert "weekend" in res["note"]


def test_arxiv_replacements_skipped_new_and_cross_kept():
    res = feedlib.collect_feed("arxiv_csAI", "u", SINCE, 40, FAST,
                               fetcher=lambda u, c: (fx("arxiv_rss_weekday.xml"), {}))
    assert res["ok"] and res["raw_count"] == 3 and res["count"] == 2 and res["skipped"] == 1
    assert {i["arxiv_id"] for i in res["items"]} == {"2610.01001", "2610.01002"}
    assert res["items"][0]["date"] == "2026-10-06T04:00:00Z"   # normalised to UTC ISO


def test_arxiv_api_atom_prefers_alternate_link():
    items, meta, err = feedlib.parse_feed(fx("arxiv_api_atom.xml"))
    assert err is None and meta["format"] == "atom"
    assert items[0]["link"] == "https://arxiv.org/abs/2610.03001v1"
    assert items[0]["arxiv_id"] == "2610.03001"
    assert items[0]["title"] == "Sample Edge Inference for Multi-Agent Systems"
    assert items[0]["date"] == "2026-10-02T14:43:42Z"          # published wins over updated


def test_atom_blog_summary_or_content_and_updated_fallback():
    items, _, err = feedlib.parse_feed(fx("atom_blog.xml"))
    assert err is None and len(items) == 2
    assert items[0]["desc"] == "Synthetic summary ."
    assert items[1]["desc"] == "Synthetic quote."
    assert items[1]["date"] == "2026-10-03T08:00:00+00:00"
    assert items[1]["link"] == "https://weblog.example/2026/Oct/3/quote/"   # link without rel


def test_rdf_rss1():
    items, meta, err = feedlib.parse_feed(fx("rdf_rss1.xml"))
    assert err is None and meta["format"] == "rdf"
    assert [i["link"] for i in items] == ["https://rdf.example/a", "https://rdf.example/b"]
    assert feedlib.parse_date(items[1]["date"]) == datetime(2026, 10, 2, 10, 0, tzinfo=UTC)


def test_bom_and_leading_whitespace():
    items, _, err = feedlib.parse_feed(b"\xef\xbb\xbf\n  " + fx("atom_blog.xml"))
    assert err is None and len(items) == 2


def test_html_is_not_a_feed_and_says_what_came_back():
    _, _, err = feedlib.parse_feed(fx("html_not_a_feed.html"), "text/html; charset=utf-8")
    assert err.startswith("not a feed: got HTML")
    assert "text/html" in err and "<!DOCTYPE html>" in err


def test_binary_garbage_snippet_is_short_and_printable():
    raw = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x02\xff" + bytes(range(256)) * 4
    _, _, err = feedlib.parse_feed(raw, "text/xml")
    assert err.startswith("not a feed (content-type text/xml")
    tail = err.split("starts ", 1)[1]
    assert len(tail) < 80 and all(c.isprintable() for c in tail)


def test_unknown_root_element():
    _, _, err = feedlib.parse_feed(b"<?xml version='1.0'?><sitemap><url/></sitemap>", "application/xml")
    assert "root element <sitemap>" in err


# --- windowing and counts ---------------------------------------------------------------------
def test_full_archive_feed_windowed_sorted_and_counted():
    res = feedlib.collect_feed("News", "u", SINCE, 40, FAST,
                               fetcher=lambda u, c: (fx("rss2_full_archive_unsorted.xml"), {}))
    assert res["raw_count"] == 6 and res["older"] == 2 and res["in_window"] == 4
    assert res["count"] == len(res["items"]) == 4
    dates = [i["date"] for i in res["items"]]
    assert dates == sorted(dates, reverse=True)                     # newest first after sort
    assert res["items"][1]["title"] == "Out-of-order post, newer than the one above"


def test_per_source_cap_keeps_newest():
    res = feedlib.collect_feed("News", "u", SINCE, 2, FAST,
                               fetcher=lambda u, c: (fx("rss2_full_archive_unsorted.xml"), {}))
    assert res["in_window"] == 4 and res["count"] == 2
    assert [i["title"] for i in res["items"]] == ["Newest sample post",
                                                   "Out-of-order post, newer than the one above"]


def test_undated_items_capped_at_newest_n():
    cfg = dict(FAST, undated_cap=10)
    res = feedlib.collect_feed("Undated", "u", SINCE, 40, cfg,
                               fetcher=lambda u, c: (fx("rss2_undated.xml"), {}))
    assert res["raw_count"] == 45 and res["undated"] == 45 and res["count"] == 10
    assert res["items"][0]["title"] == "Undated post 0"             # document order = newest


def test_window_with_nothing_in_range_notes_it():
    res = feedlib.collect_feed("Lab", "u", datetime(2026, 10, 5, tzinfo=UTC), 40, FAST,
                               fetcher=lambda u, c: (fx("rss2_media_gzip_source.xml"), {}))
    assert res["ok"] and res["count"] == 0 and res["raw_count"] == 3
    assert res["note"] == "no items in the collection window"


def test_window_start_since_slack_default_and_clamp():
    assert feedlib.window_start(NOW, {}) == NOW - timedelta(days=14)
    assert feedlib.window_start(NOW, {"DIGEST_LOOKBACK_DAYS": "3"}) == NOW - timedelta(days=3)
    since = {"DIGEST_SINCE": "2026-10-05T00:00:00Z"}
    assert feedlib.window_start(NOW, since) == datetime(2026, 10, 3, tzinfo=UTC)   # 48 h slack
    assert feedlib.window_start(NOW, dict(since, DIGEST_WINDOW_SLACK_HOURS="0")) == \
        datetime(2026, 10, 5, tzinfo=UTC)
    old = {"DIGEST_SINCE": "2020-01-01T00:00:00Z"}
    assert feedlib.window_start(NOW, old) == NOW - timedelta(days=30)               # clamp
    assert feedlib.window_start(NOW, {"DIGEST_SINCE": "garbage"}) == NOW - timedelta(days=14)


# --- fetching: encoding, retry, timeout, size cap ---------------------------------------------
class Srv:
    """Tiny local HTTP server; routes map path -> list of responses, consumed in order (the
    last one repeats). A response is (status, headers, body) or ("sleep", seconds)."""

    def __init__(self, routes):
        self.routes = routes
        self.hits = {}
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits[self.path] = outer.hits.get(self.path, 0) + 1
                seq = outer.routes[self.path]
                resp = seq[min(outer.hits[self.path] - 1, len(seq) - 1)]
                if resp[0] == "sleep":
                    time.sleep(resp[1])
                    resp = (200, {"Content-Type": "text/xml"}, fx("atom_blog.xml"))
                status, headers, body = resp
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def srv():
    servers = []

    def make(routes):
        s = Srv(routes)
        servers.append(s)
        return s
    yield make
    for s in servers:
        s.close()


XML = {"Content-Type": "text/xml"}


def test_gzip_sent_unasked_is_decoded(srv):
    body = gzip.compress(fx("rss2_media_gzip_source.xml"))
    s = srv({"/gz": [(200, {"Content-Type": "text/xml", "Content-Encoding": "gzip"}, body)],
             "/sniff": [(200, XML, body)],                                    # no header
             "/deflate": [(200, {"Content-Encoding": "deflate"},
                           zlib.compress(fx("rss2_media_gzip_source.xml")))]})
    for path in ("/gz", "/sniff", "/deflate"):
        raw, info = feedlib.fetch(s.base + path, FAST)
        items, _, err = feedlib.parse_feed(raw, info["content_type"])
        assert err is None and len(items) == 3, path


def test_unrequested_brotli_is_a_clear_error(srv):
    s = srv({"/br": [(200, {"Content-Encoding": "br"}, b"\x0b\x02\x80")]})
    with pytest.raises(feedlib.FetchError, match="unsupported content-encoding 'br'"):
        feedlib.fetch(s.base + "/br", FAST)
    assert s.hits["/br"] == 1                                                 # no pointless retry


def test_retry_once_on_503_then_ok(srv):
    sleeps = []
    s = srv({"/flaky": [(503, {"Content-Type": "text/html"}, b"busy"),
                        (200, XML, fx("atom_blog.xml"))]})
    raw, _ = feedlib.fetch(s.base + "/flaky", dict(FAST, backoff=1.5), sleep=sleeps.append)
    assert s.hits["/flaky"] == 2 and sleeps == [1.5] and b"<feed" in raw


def test_404_not_retried_and_error_shows_type_and_snippet(srv):
    s = srv({"/gone": [(404, {"Content-Type": "text/html"}, b"<html>Not Found</html>")]})
    with pytest.raises(feedlib.FetchError) as ei:
        feedlib.fetch(s.base + "/gone", FAST, sleep=lambda x: None)
    assert s.hits["/gone"] == 1
    msg = str(ei.value)
    assert msg.startswith("HTTP 404") and "text/html" in msg and "Not Found" in msg


def test_persistent_503_reports_attempts(srv):
    s = srv({"/down": [(503, {}, b"")]})
    with pytest.raises(feedlib.FetchError, match=r"HTTP 503 .*after 2 attempts"):
        feedlib.fetch(s.base + "/down", FAST, sleep=lambda x: None)
    assert s.hits["/down"] == 2


def test_timeout_is_bounded(srv):
    s = srv({"/slow": [("sleep", 3)]})
    t0 = time.monotonic()
    with pytest.raises(feedlib.FetchError, match="network|timed out"):
        feedlib.fetch(s.base + "/slow", dict(FAST, timeout=0.5, retries=1), sleep=lambda x: None)
    assert time.monotonic() - t0 < 2.5


def test_size_cap_on_raw_and_decoded_body(srv):
    big = b"<rss><channel>" + b"x" * 200_000 + b"</channel></rss>"
    s = srv({"/big": [(200, XML, big)],
             "/bomb": [(200, {"Content-Encoding": "gzip"}, gzip.compress(big))]})
    cfg = dict(FAST, max_bytes=100_000)
    with pytest.raises(feedlib.FetchError, match="body exceeds 100000 bytes"):
        feedlib.fetch(s.base + "/big", cfg)
    with pytest.raises(feedlib.FetchError, match="decoded body exceeds"):
        feedlib.fetch(s.base + "/bomb", cfg)
    assert s.hits == {"/big": 1, "/bomb": 1}


# --- one source never kills the run ---------------------------------------------------------
def test_collect_isolates_failures_and_keeps_order(srv):
    s = srv({"/ok": [(200, XML, fx("atom_blog.xml"))],
             "/html": [(200, {"Content-Type": "text/html"}, fx("html_not_a_feed.html"))],
             "/slow": [("sleep", 3)]})
    sources = {"A": ("feed", s.base + "/ok"), "B": ("feed", s.base + "/html"),
               "C": ("feed", s.base + "/slow"), "D": ("bogus", "x"), "E": ("feed", s.base + "/ok")}

    def worker(name, spec, cfg):
        if name == "E":
            raise RuntimeError("worker blew up")
        return collect_ai_digest.collect_source(name, spec, cfg, since=SINCE)

    res = feedlib.collect(sources, worker, dict(FAST, timeout=0.5, retries=0))
    assert list(res) == ["A", "B", "C", "D", "E"]
    assert res["A"]["ok"] and res["A"]["count"] == 2
    assert not res["B"]["ok"] and "got HTML" in res["B"]["error"]
    assert not res["C"]["ok"]
    assert not res["D"]["ok"] and "unknown source kind" in res["D"]["error"]
    assert res["E"] == {"ok": False, "error": "RuntimeError: worker blew up", "count": 0, "items": []}


def test_threat_intel_kev_and_bad_json():
    kev = json.dumps({"catalogVersion": "2026.10.05", "vulnerabilities": [
        {"cveID": f"CVE-2026-{i:04d}", "dateAdded": f"2026-09-{i:02d}"} for i in range(1, 30)]}).encode()
    ok = collect_threat_intel.collect_source("CISA_KEV", ("kev", "u"), FAST,
                                             fetcher=lambda u, c: (kev, {}))
    assert ok["ok"] and ok["total"] == 29 and len(ok["recent"]) == 25
    assert ok["recent"][0]["cveID"] == "CVE-2026-0029"
    bad = collect_threat_intel.collect_source(
        "CISA_KEV", ("kev", "u"), FAST,
        fetcher=lambda u, c: (b"<html>blocked</html>", {"content_type": "text/html"}))
    assert not bad["ok"] and bad["error"].startswith("not JSON") and "text/html" in bad["error"]


def test_feed_lists_have_no_known_redirecting_urls():
    urls = [u for w in collect_ai_digest.WATCHES.values() for _, u in w.values()]
    urls += [u for _, u in collect_threat_intel.SOURCES.values()]
    assert all(u.startswith("https://") for u in urls)
    for stale in ("wiz.io/blog/rss", "lesswrong.com/rss", "trailofbits.com/feed/",
                  "security.googleblog.com/feeds", "blog.google/technology/ai/rss/"):
        assert not any(stale in u for u in urls), stale


# --- pipeline wiring (collection/dedupe path) -------------------------------------------------
def test_collect_since_is_oldest_cutoff_or_none():
    st = {"cutoff": "2026-10-05T00:00:00Z", "sources": {
        "A": {"cutoff": "2026-10-05T00:00:00Z"}, "B": {"cutoff": "2026-09-25T00:00:00Z"}, "C": {}}}
    assert pipeline._collect_since(st) == "2026-09-25T00:00:00Z"
    st["sources"]["D"] = {"cutoff": None}           # never succeeded: no bound
    assert pipeline._collect_since(st) is None
    assert pipeline._collect_since({"cutoff": None, "sources": {}}) is None


ENV_COLLECTOR = r'''
import json, os, sys
json.dump({"S": {"ok": True, "items": [], "since": os.environ.get("DIGEST_SINCE")}},
          open(sys.argv[-1], "w"))
'''


def test_collect_passes_since_to_collector(tmp_path, monkeypatch):
    col = tmp_path / "envcol.py"
    col.write_text(ENV_COLLECTOR)
    monkeypatch.setattr(pipeline, "_collector_cmd", lambda w, out: [sys.executable, str(col), out])
    monkeypatch.setenv("DIGEST_SINCE", "1999-01-01T00:00:00Z")    # stale value never leaks through

    async def progress(*a, **k):
        pass
    got = asyncio.run(pipeline._collect("ai-research", progress, since="2026-10-01T00:00:00Z"))
    assert got["sources"]["S"]["since"] == "2026-10-01T00:00:00Z"
    got = asyncio.run(pipeline._collect("ai-research", progress))
    assert got["sources"]["S"]["since"] is None


def test_papers_deduped_by_id_not_dropped_by_batch_date():
    # arXiv stamps a batch with its announcement date; a run between that date and the batch
    # reaching the feed must not make the batch fall behind the cutoff.
    sources = {
        "arxiv_csAI": {"ok": True, "items": [
            {"title": "P1", "link": "https://arxiv.org/abs/2610.01001", "arxiv_id": "2610.01001",
             "date": "2026-10-06T04:00:00Z"},
            {"title": "P2", "link": "https://arxiv.org/abs/2610.01002", "date": "2026-10-06T04:00:00Z"}]},
        "arxiv_csLG": {"ok": True, "items": [
            {"title": "P1 again", "link": "https://arxiv.org/abs/2610.01001v2", "date": "2026-10-06T04:00:00Z"}]},
        "Blog": {"ok": True, "items": [{"title": "Old news", "link": "https://b.example/o",
                                        "date": "2026-10-06T04:00:00Z"}]},
    }
    state = {"cutoff": "2026-10-06T04:30:00Z", "sources": {},
             "seen": {"papers": {"2610.01002": {}}}}
    out = pipeline._dedupe("ai-research", sources, state)
    assert [(i["kind"], i.get("arxiv_id")) for i in out] == [("paper", "2610.01001")]
