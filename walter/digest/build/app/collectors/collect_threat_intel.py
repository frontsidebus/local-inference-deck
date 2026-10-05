#!/usr/bin/env python3
"""Collect threat-intel sources into one JSON file for digest building.

Usage:
    python3 collect_threat_intel.py [output.json]

Default output: $BH_AGENT_WORKSPACE/threat_intel_raw.json (or ./threat_intel_raw.json).
Stdlib only (fetch/parse/window live in feedlib.py). Per-source failures are recorded in the
output, not fatal. Fetches go through
feedlib.FeedCache (conditional GET, minimum refetch interval, 403/429 backoff); CISA_Advisories
falls back to CISA's CSAF repository when its primary feed is blocked (see FALLBACKS). Feed sources report count (emitted, in window), raw_count, in_window, older,
undated, like the AI collector; CISA_KEV reports total and its `recent` entries.
"""
import json
import os
import sys

import csaf
import feedlib

# name -> (type, url). type: "kev" (CISA KEV JSON) or "feed" (RSS/Atom).
SOURCES = {
    "CISA_KEV": ("kev", "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"),
    # All CISA advisory types (ICS, ICS medical, alerts, joint advisories, analysis reports).
    # Behind Akamai, which can 403 a client IP for hours; see FALLBACKS.
    "CISA_Advisories": ("feed", "https://www.cisa.gov/cybersecurity-advisories/all.xml"),
    "SANS_ISC": ("feed", "https://isc.sans.edu/rssfeed.xml"),
    "BleepingComputer": ("feed", "https://www.bleepingcomputer.com/feed/"),
    # thehackernews.com/feed 404s; use the FeedBurner mirror.
    "TheHackerNews": ("feed", "https://feeds.feedburner.com/TheHackersNews"),
}

# name -> [(kind, url, label)], tried in order when the primary fails or is backing off after a
# 403/429. Items are deduped by advisory id (ICSA-.., ICSMA-.., AA..) across primary and fallback.
FALLBACKS = {
    # Official CISA CSAF repository (cisagov org on GitHub), served by raw.githubusercontent.com
    # (no REST API quota). ICS and ICS medical advisories only; alerts and joint advisories are
    # not published as CSAF.
    "CISA_Advisories": [
        ("csaf_changes", "https://raw.githubusercontent.com/cisagov/CSAF/develop/csaf_files/OT/white/changes.csv",
         "cisagov/CSAF (ICS advisories)"),
    ],
}
HANDLERS = {"csaf_changes": csaf.collect_changes}

KEV_FIELDS = ("cveID", "vendorProject", "product", "vulnerabilityName",
              "dateAdded", "dueDate", "knownRansomwareCampaignUse", "requiredAction")

KEV_RECENT = 25
FEED_ITEMS = 15


def collect_source(name, spec, cfg, since=None, fetcher=feedlib.fetch, cache=None):
    kind, url = spec
    if kind == "feed" and name in FALLBACKS:
        return feedlib.collect_with_fallbacks(
            name, spec, FALLBACKS[name], since or feedlib.window_start(), FEED_ITEMS, cfg,
            desc_limit=220, fetcher=fetcher, cache=cache, handlers=HANDLERS)
    if kind == "feed":
        return feedlib.collect_feed(name, url, since or feedlib.window_start(), FEED_ITEMS, cfg,
                                    desc_limit=220, fetcher=fetcher)
    if kind != "kev":
        return {"ok": False, "error": f"unknown source kind {kind!r}", "count": 0, "items": []}
    try:
        raw, info = fetcher(url, cfg)
        try:
            data = json.loads(raw)
        except ValueError as e:
            return {"ok": False, "error": f"not JSON: {e} (content-type "
                    f"{info.get('content_type') or 'unknown'}, starts {feedlib.snippet(raw)})",
                    "count": 0, "items": []}
        vulns = sorted(data.get("vulnerabilities", []),
                       key=lambda x: x.get("dateAdded", ""), reverse=True)
        out = {
            "ok": True,
            "catalog_version": data.get("catalogVersion"),
            "total": len(vulns),
            "recent": [{k: v.get(k) for k in KEV_FIELDS} for v in vulns[:KEV_RECENT]],
        }
        if info.get("cache"):
            out["cache"] = info["cache"]
        return out
    except feedlib.FetchError as e:
        return {"ok": False, "error": str(e), "count": 0, "items": []}
    except Exception as e:  # noqa: BLE001 - one source must never kill the run
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "count": 0, "items": []}


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.environ.get("BH_AGENT_WORKSPACE", "."), "threat_intel_raw.json")
    since = feedlib.window_start()
    cache = feedlib.FeedCache.from_env()   # $STATE_DIR/feedcache: conditional GET, backoff
    fetcher = cache.fetch if cache else feedlib.fetch
    results = feedlib.collect(SOURCES, lambda n, spec, cfg: collect_source(
        n, spec, cfg, since, fetcher=fetcher, cache=cache))
    d = os.path.dirname(os.path.abspath(out))
    os.makedirs(d, exist_ok=True)
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    for name, v in results.items():
        print(feedlib.summary_line(name, v))


if __name__ == "__main__":
    main()
