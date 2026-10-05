#!/usr/bin/env python3
"""Collect threat-intel sources into one JSON file for digest building.

Usage:
    python3 collect_threat_intel.py [output.json]

Default output: $BH_AGENT_WORKSPACE/threat_intel_raw.json (or ./threat_intel_raw.json).
Stdlib only (fetch/parse/window live in feedlib.py). Per-source failures are recorded in the
output, not fatal. Feed sources report count (emitted, in window), raw_count, in_window, older,
undated, like the AI collector; CISA_KEV reports total and its `recent` entries.
"""
import json
import os
import sys

import feedlib

# name -> (type, url). type: "kev" (CISA KEV JSON) or "feed" (RSS/Atom).
SOURCES = {
    "CISA_KEV": ("kev", "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"),
    "CISA_Advisories": ("feed", "https://www.cisa.gov/cybersecurity-advisories/all.xml"),
    "SANS_ISC": ("feed", "https://isc.sans.edu/rssfeed.xml"),
    "BleepingComputer": ("feed", "https://www.bleepingcomputer.com/feed/"),
    # thehackernews.com/feed 404s; use the FeedBurner mirror.
    "TheHackerNews": ("feed", "https://feeds.feedburner.com/TheHackersNews"),
}

KEV_FIELDS = ("cveID", "vendorProject", "product", "vulnerabilityName",
              "dateAdded", "dueDate", "knownRansomwareCampaignUse", "requiredAction")

KEV_RECENT = 25
FEED_ITEMS = 15


def collect_source(name, spec, cfg, since=None, fetcher=feedlib.fetch):
    kind, url = spec
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
        return {
            "ok": True,
            "catalog_version": data.get("catalogVersion"),
            "total": len(vulns),
            "recent": [{k: v.get(k) for k in KEV_FIELDS} for v in vulns[:KEV_RECENT]],
        }
    except feedlib.FetchError as e:
        return {"ok": False, "error": str(e), "count": 0, "items": []}
    except Exception as e:  # noqa: BLE001 - one source must never kill the run
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "count": 0, "items": []}


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.environ.get("BH_AGENT_WORKSPACE", "."), "threat_intel_raw.json")
    since = feedlib.window_start()
    results = feedlib.collect(SOURCES, lambda n, spec, cfg: collect_source(n, spec, cfg, since))
    d = os.path.dirname(os.path.abspath(out))
    os.makedirs(d, exist_ok=True)
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    for name, v in results.items():
        print(feedlib.summary_line(name, v))


if __name__ == "__main__":
    main()
