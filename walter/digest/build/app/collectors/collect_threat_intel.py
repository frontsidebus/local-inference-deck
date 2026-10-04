#!/usr/bin/env python3
"""Collect threat-intel sources into one JSON file for digest building.

Usage:
    python3 collect_threat_intel.py [output.json]

Default output: $BH_AGENT_WORKSPACE/threat_intel_raw.json (or ./threat_intel_raw.json).
Stdlib only. Per-source failures are recorded in the output, not fatal.
"""
import html
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET

UA = {"User-Agent": "Mozilla/5.0 (hermes-threat-intel; research)"}

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


def get(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def clean(text, limit=220):
    text = re.sub(r"<[^>]+>", "", text or "")
    return html.unescape(text).strip()[:limit]


def parse_feed(raw):
    """Return (items, error). Handles RSS 2.0 and Atom."""
    try:
        root = ET.fromstring(raw)
    except Exception as e:
        return [], f"xml parse: {e}"
    items = []
    for it in root.iter("item"):  # RSS
        items.append({
            "title": html.unescape((it.findtext("title") or "").strip()),
            "link": (it.findtext("link") or "").strip(),
            "date": (it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date") or "").strip(),
            "desc": clean(it.findtext("description")),
        })
    if items:
        return items, None
    ns = "{http://www.w3.org/2005/Atom}"
    for it in root.iter(f"{ns}entry"):
        le = it.find(f"{ns}link")
        items.append({
            "title": html.unescape((it.findtext(f"{ns}title") or "").strip()),
            "link": (le.get("href") if le is not None else ""),
            "date": (it.findtext(f"{ns}published") or it.findtext(f"{ns}updated") or "").strip(),
            "desc": clean(it.findtext(f"{ns}summary") or it.findtext(f"{ns}content")),
        })
    if items:
        return items, None
    return [], "no items found (unknown feed format)"


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.environ.get("BH_AGENT_WORKSPACE", "."), "threat_intel_raw.json")
    results = {}
    for name, (kind, url) in SOURCES.items():
        try:
            raw = get(url)
            if kind == "kev":
                data = json.loads(raw)
                vulns = sorted(data.get("vulnerabilities", []),
                               key=lambda x: x.get("dateAdded", ""), reverse=True)
                results[name] = {
                    "ok": True,
                    "catalog_version": data.get("catalogVersion"),
                    "total": len(vulns),
                    "recent": [{k: v.get(k) for k in KEV_FIELDS} for v in vulns[:KEV_RECENT]],
                }
            else:
                items, err = parse_feed(raw)
                results[name] = {"ok": err is None, "error": err,
                                 "count": len(items), "items": items[:FEED_ITEMS]}
        except Exception as e:
            results[name] = {"ok": False, "error": f"{type(e).__name__}: {e}",
                             "count": 0, "items": []}
    d = os.path.dirname(os.path.abspath(out))
    os.makedirs(d, exist_ok=True)
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    for name, v in results.items():
        if v.get("ok"):
            extra = (f" catalog={v.get('catalog_version')} total={v.get('total')}"
                     if "recent" in v else f" count={v.get('count')}")
            print(f"[OK] {name}{extra}")
        else:
            print(f"[FAIL] {name}: {v.get('error')}")


if __name__ == "__main__":
    main()
