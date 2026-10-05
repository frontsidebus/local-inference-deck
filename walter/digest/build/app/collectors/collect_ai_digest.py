#!/usr/bin/env python3
"""Collect AI digest sources into one JSON file for digest building.

Usage:
    python3 collect_ai_digest.py <watch-slug> [output.json]

Watch slugs: ai-security, ai-research (see WATCHES below).
Default output: $BH_AGENT_WORKSPACE/ai_digest_<watch-slug>_raw.json.
Stdlib only (fetch/parse/window live in feedlib.py). Per-source failures are recorded in the
output, not fatal. Feed URLs drift: verify before adding; a 404 is a coverage gap, not "no news".
URLs below are the final ones (no redirects) as of 2026-10.

Per source the output has: ok, error, count (items emitted: in the collection window, after the
per-source cap), raw_count (items in the feed), in_window, older, undated, items, and an optional
note (e.g. arXiv's empty weekend listing, which is ok, not a gap).
"""
import json
import os
import sys

import feedlib

WATCHES = {
    # AI SECURITY: model/agent security, prompt injection, AI supply chain,
    # AI-related threat-actor and vuln news.
    "ai-security": {
        "arxiv_csCR": ("feed", "https://rss.arxiv.org/rss/cs.CR"),
        "SimonWillison": ("feed", "https://simonwillison.net/atom/everything/"),
        "GoogleSecurityBlog": ("feed", "https://feeds.feedburner.com/GoogleOnlineSecurityBlog"),
        "MicrosoftSecurityBlog": ("feed", "https://www.microsoft.com/en-us/security/blog/feed/"),
        "HuggingFaceBlog": ("feed", "https://huggingface.co/blog/feed.xml"),
        "OpenAINews": ("feed", "https://openai.com/news/rss.xml"),
        "TrailOfBits": ("feed", "https://blog.trailofbits.com/index.xml"),
        "Unit42": ("feed", "https://unit42.paloaltonetworks.com/feed/"),
        "WizBlog": ("feed", "https://www.wiz.io/feed/rss.xml"),
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
        "GoogleAIBlog": ("feed", "https://blog.google/innovation-and-ai/technology/ai/rss/"),
        "ResearchGoogle": ("feed", "https://research.google/blog/rss/"),
        "MicrosoftResearch": ("feed", "https://www.microsoft.com/en-us/research/feed/"),
        "NVIDIABlog": ("feed", "https://blogs.nvidia.com/feed/"),
        "HuggingFaceBlog": ("feed", "https://huggingface.co/blog/feed.xml"),
        "OpenAINews": ("feed", "https://openai.com/news/rss.xml"),
        "Interconnects": ("feed", "https://www.interconnects.ai/feed"),
        "ImportAI": ("feed", "https://importai.substack.com/feed"),
        "LatentSpace": ("feed", "https://www.latent.space/feed"),
        "LessWrong": ("feed", "https://www.lesswrong.com/feed.xml"),
        "FutureOfLife": ("feed", "https://futureoflife.org/feed/"),
        "SimonWillison": ("feed", "https://simonwillison.net/atom/everything/"),
        "ArsTechnicaAI": ("feed", "https://arstechnica.com/ai/feed/"),
        "TechCrunchAI": ("feed", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    },
}

# Max items emitted per source, newest first, after the date window. arXiv cs.LG alone lists
# 100+ papers a day; full-archive blog feeds (OpenAI, Hugging Face, Wiz) list hundreds to
# thousands of posts going back years.
FEED_ITEMS = 40


def collect_source(name, spec, cfg, since=None, fetcher=feedlib.fetch):
    kind, url = spec
    if kind != "feed":
        return {"ok": False, "error": f"unknown source kind {kind!r}", "count": 0, "items": []}
    return feedlib.collect_feed(name, url, since or feedlib.window_start(), FEED_ITEMS, cfg,
                                fetcher=fetcher)


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in WATCHES:
        print(f"usage: {sys.argv[0]} <{'|'.join(WATCHES)}> [output.json]")
        sys.exit(1)
    slug = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.environ.get("BH_AGENT_WORKSPACE", "."), f"ai_digest_{slug}_raw.json")
    since = feedlib.window_start()
    cache = feedlib.FeedCache.from_env()   # $STATE_DIR/feedcache: conditional GET, backoff
    fetcher = cache.fetch if cache else feedlib.fetch
    results = feedlib.collect(
        WATCHES[slug], lambda n, spec, cfg: collect_source(n, spec, cfg, since, fetcher=fetcher))
    d = os.path.dirname(os.path.abspath(out))
    os.makedirs(d, exist_ok=True)
    with open(out, "w") as f:
        json.dump({"watch": slug, "window_start": feedlib.iso(since), "sources": results}, f, indent=2)
    for name, v in results.items():
        print(feedlib.summary_line(name, v))


if __name__ == "__main__":
    main()
