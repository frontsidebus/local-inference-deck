"""CISA CSAF advisories as a feed (stdlib only): the fallback for CISA_Advisories.

CISA publishes its ICS and ICS medical advisories (ICSA-/ICSMA-) as CSAF 2.0 JSON in the
official `cisagov/CSAF` GitHub repository, normally the same day as the web advisory. Each
distribution directory has a `changes.csv` (CSAF 2.0 section 7.1.16): one line per document,
`"<year>/<file>.json","<last change, ISO 8601>"`, newest first. It is fetched from
raw.githubusercontent.com, which is not the GitHub REST API (no 60/hour unauthenticated quota)
and answers conditional GETs with 304.

One run costs: one conditional GET of changes.csv (none within the minimum refetch interval),
plus one GET per advisory that is in the collection window and not already in the document
cache (`csaf-docs` in the feed cache: path + change date -> normalized item). At most
DIGEST_CSAF_MAX_DOCS documents (default 15) and DIGEST_CSAF_BUDGET seconds (default 60) per run;
anything left over is picked up by the next run and reported in the source note.

Items are normalized like the CISA RSS items: the advisory title (CSAF document.title is the
same text the web feed uses), the web advisory URL, the release date and the summary note.
"""
from __future__ import annotations

import csv
import io
import json
import os
import time

import feedlib

DOCS_KEEP = 400
WEB = {"ICSA": "https://www.cisa.gov/news-events/ics-advisories/",
       "ICSMA": "https://www.cisa.gov/news-events/ics-medical-advisories/"}


def _settings(env=None) -> dict:
    env = os.environ if env is None else env
    return {"max_docs": feedlib._env_num("DIGEST_CSAF_MAX_DOCS", 15, int, 0, 100, env),
            "budget": feedlib._env_num("DIGEST_CSAF_BUDGET", 60, float, 5, 240, env)}


def parse_changes(raw: bytes) -> list[tuple[str, str]]:
    """changes.csv -> [(relative path, change date string)] for .json rows; raises ValueError
    when the body is not a CSAF changes.csv."""
    text = feedlib._strip_prolog(raw or b"").decode("utf-8", errors="replace")
    if not text or text[:1] == "<" or text[:1] == "{":
        raise ValueError("not a CSAF changes.csv")
    rows = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 2 and row[0].strip().endswith(".json"):
            rows.append((row[0].strip(), row[1].strip()))
    if not rows:
        raise ValueError("not a CSAF changes.csv (no .json rows)")
    return rows


def web_link(adv_id: str) -> str:
    prefix = adv_id.split("-", 1)[0].upper()
    return WEB[prefix] + adv_id.lower() if prefix in WEB else ""


def parse_doc(raw: bytes, doc_url: str = "", desc_limit: int = 260) -> dict:
    """One CSAF document -> a feed item (title, link, date, desc, arxiv_id, advisory_id)."""
    d = json.loads(raw)["document"]
    tracking = d.get("tracking") or {}
    adv_id = (tracking.get("id") or "").strip().upper() or feedlib.advisory_id(doc_url)
    link = ""
    for ref in d.get("references") or []:
        u = (ref.get("url") or "").strip()
        if ref.get("category") == "self" and u.startswith("https://www.cisa.gov/"):
            link = u
            break
    link = link or web_link(adv_id) or doc_url
    summary = next((n.get("text") or "" for n in d.get("notes") or []
                    if n.get("category") == "summary"), "")
    date = feedlib.parse_date(tracking.get("current_release_date") or
                              tracking.get("initial_release_date") or "")
    title = feedlib.clean(d.get("title") or "", 400)
    if not title:
        raise ValueError("CSAF document has no title")
    return {"title": title, "link": link, "date": feedlib.iso(date) if date else "",
            "desc": feedlib.clean(summary, desc_limit), "arxiv_id": "", "advisory_id": adv_id}


def collect_changes(name: str, url: str, since, max_items: int, cfg: dict, desc_limit: int = 260,
                    fetcher=feedlib.fetch, cache=None, doc_fetcher=feedlib.fetch, env=None,
                    clock=time.monotonic) -> dict:
    """The collect_with_fallbacks handler for kind "csaf_changes" (url = a changes.csv)."""
    st = _settings(env)
    try:
        raw, info = fetcher(url, cfg)
    except feedlib.FetchError as e:
        return {"ok": False, "error": str(e), "count": 0, "items": []}
    try:
        rows = parse_changes(raw)
    except ValueError as e:
        return {"ok": False, "error": f"{e} ({feedlib._describe(info.get('content_type', ''), raw)})",
                "count": 0, "items": []}
    base = url.rsplit("/", 1)[0] + "/"
    dated, older, undated = [], 0, 0
    for path, when in rows:
        dt = feedlib.parse_date(when)
        if dt is None:
            undated += 1
        elif dt < since:
            older += 1
        else:
            dated.append((dt, path))
    dated.sort(reverse=True)
    wanted = dated[:max_items]

    docs = cache.load_json("csaf-docs") if cache else {}
    items, fetched, pending, errors = [], 0, 0, []
    t0 = clock()
    doc_cfg = dict(cfg, retries=0)
    for dt, path in wanted:
        stamp = feedlib.iso(dt)
        hit = docs.get(path)
        if hit and hit.get("changed") == stamp and isinstance(hit.get("item"), dict):
            items.append(dict(hit["item"]))
            continue
        if fetched >= st["max_docs"] or clock() - t0 > st["budget"]:
            pending += 1
            continue
        fetched += 1
        try:
            draw, _ = doc_fetcher(base + path, doc_cfg)
            it = parse_doc(draw, base + path, desc_limit)
        except (feedlib.FetchError, ValueError, KeyError, TypeError) as e:
            errors.append(f"{path}: {e}")
            continue
        it["date"] = it["date"] or stamp
        docs[path] = {"changed": stamp, "item": it}
        items.append(dict(it))
    if cache and fetched:
        keep = sorted(docs.items(), key=lambda kv: kv[1].get("changed", ""))[-DOCS_KEEP:]
        cache.save_json("csaf-docs", dict(keep))
    items.sort(key=lambda it: it.get("date", ""), reverse=True)
    if wanted and not items and errors:
        return {"ok": False, "error": f"all {len(errors)} advisory document(s) failed: "
                f"{errors[0][:200]}", "count": 0, "items": []}
    notes = ["ICS and ICS medical advisories only (CSAF)"]
    if pending:
        notes.append(f"{pending} advisory document(s) left for the next run")
    if errors:
        notes.append(f"{len(errors)} document(s) failed: {errors[0][:160]}")
    if not wanted:
        notes.append("no advisories in the collection window")
    out = {"ok": True, "error": None, "raw_count": len(rows), "in_window": len(dated),
           "older": older, "undated": undated, "count": len(items), "items": items,
           "docs_fetched": fetched, "note": "; ".join(notes)}
    if info.get("cache"):
        out["cache"] = info["cache"]
    return out
