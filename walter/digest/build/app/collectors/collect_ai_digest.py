#!/usr/bin/env python3
"""Collect AI digest sources into one JSON file for digest building.

Usage:
    python3 collect_ai_digest.py <watch-slug> [output.json]

Watch slugs: ai-security, ai-research (see WATCHES below).
Default output: $BH_AGENT_WORKSPACE/ai_digest_<watch-slug>_raw.json.
Stdlib only. Per-source failures are recorded in the output, not fatal.
Feed URLs drift — verify before adding; a 404 is a coverage gap, not "no news".
"""
import html
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET

UA = {"User-Agent": "Mozilla/5.0 (hermes-ai-digest; research)"}

WATCHES = {
    # AI SECURITY: model/agent security, prompt injection, AI supply chain,
    # AI-related threat-actor and vuln news.
    "ai-security": {
        "arxiv_csCR": ("feed", "https://rss.arxiv.org/rss/cs.CR"),
        "SimonWillison": ("feed", "https://simonwillison.net/atom/everything/"),
        "GoogleSecurityBlog": ("feed", "https://security.googleblog.com/feeds/posts/default"),
        "MicrosoftSecurityBlog": ("feed", "https://www.microsoft.com/en-us/security/blog/feed/"),
        "HuggingFaceBlog": ("feed", "https://huggingface.co/blog/feed.xml"),
        "OpenAINews": ("feed", "https://openai.com/news/rss.xml"),
        "TrailOfBits": ("feed", "https://blog.trailofbits.com/feed/"),
        "Unit42": ("feed", "https://unit42.paloaltonetworks.com/feed/"),
        "WizBlog": ("feed", "https://www.wiz.io/blog/rss"),
        "KrebsOnSecurity": ("feed", "https://krebsonsecurity.com/feed/"),
        "Schneier": ("feed", "https://www.schneier.com/feed/"),
    },
    # AI RESEARCH: frontier labs, arXiv, AI commentary, policy, industry.
    "ai-research": {
        "arxiv_csAI": ("feed", "https://rss.arxiv.org/rss/cs.AI"),
        "arxiv_csLG": ("feed", "https://rss.arxiv.org/rss/cs.LG"),
        "arxiv_csCL": ("feed", "https://rss.arxiv.org/rss/cs.CL"),
        "arxiv_csMA": ("feed", "https://rss.arxiv.org/rss/cs.MA"),
        "DeepMindBlog": ("feed", "https://deepmind.google/blog/rss.xml"),
        "GoogleAIBlog": ("feed", "https://blog.google/technology/ai/rss/"),
        "ResearchGoogle": ("feed", "https://research.google/blog/rss/"),
        "MicrosoftResearch": ("feed", "https://www.microsoft.com/en-us/research/feed/"),
        "NVIDIABlog": ("feed", "https://blogs.nvidia.com/feed/"),
        "HuggingFaceBlog": ("feed", "https://huggingface.co/blog/feed.xml"),
        "OpenAINews": ("feed", "https://openai.com/news/rss.xml"),
        "Interconnects": ("feed", "https://www.interconnects.ai/feed"),
        "ImportAI": ("feed", "https://importai.substack.com/feed"),
        "LatentSpace": ("feed", "https://www.latent.space/feed"),
        "LessWrong": ("feed", "https://www.lesswrong.com/rss"),
        "FutureOfLife": ("feed", "https://futureoflife.org/feed/"),
        "SimonWillison": ("feed", "https://simonwillison.net/atom/everything/"),
        "ArsTechnicaAI": ("feed", "https://arstechnica.com/ai/feed/"),
        "TechCrunchAI": ("feed", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    },
}

FEED_ITEMS = 40          # cap per feed (arXiv feeds can carry 100+)
ATOM = "{http://www.w3.org/2005/Atom}"


def get(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def clean(text, limit=260):
    text = re.sub(r"<[^>]+>", "", text or "")
    return html.unescape(text).strip()[:limit]


def arxiv_id(link):
    m = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(v\d+)?", link or "")
    return m.group(1) if m else ""


def parse_feed(raw):
    """Return (items, error). Handles RSS 2.0 and Atom."""
    try:
        root = ET.fromstring(raw)
    except Exception as e:
        return [], f"xml parse: {e}"
    items = []
    for it in root.iter("item"):  # RSS
        link = (it.findtext("link") or "").strip()
        items.append({
            "title": html.unescape((it.findtext("title") or "").strip()),
            "link": link,
            "date": (it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date") or "").strip(),
            "desc": clean(it.findtext("description")),
            "arxiv_id": arxiv_id(link),
        })
    if items:
        return items, None
    for it in root.iter(f"{ATOM}entry"):  # Atom
        le = it.find(f"{ATOM}link")
        link = le.get("href") if le is not None else ""
        items.append({
            "title": html.unescape((it.findtext(f"{ATOM}title") or "").strip()),
            "link": link,
            "date": (it.findtext(f"{ATOM}published") or it.findtext(f"{ATOM}updated") or "").strip(),
            "desc": clean(it.findtext(f"{ATOM}summary") or it.findtext(f"{ATOM}content")),
            "arxiv_id": arxiv_id(link),
        })
    if items:
        return items, None
    return [], "no items found (unknown feed format)"


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in WATCHES:
        print(f"usage: {sys.argv[0]} <{'|'.join(WATCHES)}> [output.json]")
        sys.exit(1)
    slug = sys.argv[1]
    sources = WATCHES[slug]
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.environ.get("BH_AGENT_WORKSPACE", "."), f"ai_digest_{slug}_raw.json")
    results = {}
    for name, (kind, url) in sources.items():
        try:
            items, err = parse_feed(get(url))
            results[name] = {"ok": err is None, "error": err,
                             "count": len(items), "items": items[:FEED_ITEMS]}
        except Exception as e:
            results[name] = {"ok": False, "error": f"{type(e).__name__}: {e}",
                             "count": 0, "items": []}
    d = os.path.dirname(os.path.abspath(out))
    os.makedirs(d, exist_ok=True)
    with open(out, "w") as f:
        json.dump({"watch": slug, "sources": results}, f, indent=2)
    for name, v in results.items():
        if v.get("ok"):
            print(f"[OK] {name} count={v.get('count')}")
        else:
            print(f"[FAIL] {name}: {v.get('error')}")


if __name__ == "__main__":
    main()
