"""Shared feed fetching, parsing and windowing for the digest collectors (stdlib only).

Used by collect_ai_digest.py and collect_threat_intel.py, which run as subprocesses of the
pipeline (their directory is on sys.path, so `import feedlib` works there and in the tests).

- fetch():      one URL, a total timeout per attempt, one retry with backoff on transient errors,
                a size cap on the downloaded AND the decoded body, gzip/deflate decoding (some
                servers send gzip even when it was not asked for), and errors that say what came
                back (HTTP status, content-type, a short printable snippet of the first bytes).
- parse_feed(): RSS 2.0, Atom and RDF/RSS 1.0, namespace-agnostic. A well-formed feed with no
                items is valid ("no new items"), not an error. Non-feeds (HTML, JSON, binary) get
                an error naming the content-type and the first bytes.
- window():     drops dated items older than the collection window, keeps the newest
                `max_items` in-window items, and caps undated items at `undated_cap`.
- collect():    runs all sources of a collector in parallel; one source can never kill the run.

Configuration (environment; the pipeline passes DIGEST_SINCE, the rest are optional knobs):

  DIGEST_SINCE                ISO 8601 window start (the pipeline's oldest per-source cutoff)
  DIGEST_LOOKBACK_DAYS        window when DIGEST_SINCE is absent             (default 14)
  DIGEST_MAX_LOOKBACK_DAYS    the window never reaches further back than this (default 30)
  DIGEST_WINDOW_SLACK_HOURS   subtracted from DIGEST_SINCE; the pipeline's per-source cutoffs
                              do the exact filtering, this only bounds the volume (default 48)
  DIGEST_UNDATED_CAP          newest N undated items kept per source         (default 30)
  DIGEST_FETCH_TIMEOUT        seconds per attempt, whole download            (default 20)
  DIGEST_FETCH_RETRIES        retries after the first attempt                (default 1)
  DIGEST_RETRY_BACKOFF        seconds before the first retry, doubling       (default 2)
  DIGEST_MAX_BODY_BYTES       cap on the downloaded and on the decoded body  (default 8 MiB)
  DIGEST_COLLECT_WORKERS      sources fetched in parallel                    (default 6)
"""
from __future__ import annotations

import html
import os
import re
import socket
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

USER_AGENT = "Mozilla/5.0 (compatible; walter-digest/1.0; feed reader)"
ACCEPT = ("application/rss+xml, application/atom+xml, application/rdf+xml;q=0.9, "
          "application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5")
# Only what we can decode with the stdlib: never advertise br/zstd.
ACCEPT_ENCODING = "gzip, deflate"

ARXIV_ID_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(v\d+)?|oai:arXiv\.org:(\d{4}\.\d{4,5})")
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


def _env_num(name: str, default, cast=float, lo=None, hi=None, env=None):
    env = os.environ if env is None else env
    try:
        v = cast(env.get(name, default))
    except (TypeError, ValueError):
        v = cast(default)
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def settings() -> dict:
    return {
        "timeout": _env_num("DIGEST_FETCH_TIMEOUT", 20, float, 1, 120),
        "retries": _env_num("DIGEST_FETCH_RETRIES", 1, int, 0, 3),
        "backoff": _env_num("DIGEST_RETRY_BACKOFF", 2, float, 0, 30),
        "max_bytes": _env_num("DIGEST_MAX_BODY_BYTES", 8 * 1024 * 1024, int, 64 * 1024, 64 * 1024 * 1024),
        "undated_cap": _env_num("DIGEST_UNDATED_CAP", 30, int, 0, 500),
        "workers": _env_num("DIGEST_COLLECT_WORKERS", 6, int, 1, 16),
    }


# ---------------------------------------------------------------------------------------------
# errors and snippets
# ---------------------------------------------------------------------------------------------
class FetchError(Exception):
    """A per-source failure with a message that is safe to show (no body dumps)."""


def snippet(raw: bytes, n: int = 60) -> str:
    """First bytes as a short, printable, single-line string (for error messages)."""
    s = (raw or b"")[:n].decode("utf-8", errors="replace")
    s = "".join(c if c.isprintable() and c != "�" else "." for c in s)
    s = re.sub(r"\s+", " ", s).strip()
    return repr(s)


def _describe(content_type: str, raw: bytes) -> str:
    return f"content-type {content_type or 'unknown'}, {len(raw or b'')} bytes, starts {snippet(raw)}"


# ---------------------------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------------------------
def _decode_body(raw: bytes, encoding: str, max_bytes: int) -> bytes:
    enc = (encoding or "").strip().lower()
    # Sniff gzip magic too: some servers compress without saying so (or say it twice).
    if enc in ("gzip", "x-gzip") or (enc in ("", "identity") and raw[:2] == b"\x1f\x8b"):
        wbits = 16 + zlib.MAX_WBITS
    elif enc == "deflate":
        wbits = zlib.MAX_WBITS if raw[:1] == b"\x78" else -zlib.MAX_WBITS
    elif enc in ("", "identity"):
        return raw
    else:
        raise FetchError(f"unsupported content-encoding {enc!r} (not requested)")
    d = zlib.decompressobj(wbits)
    try:
        out = d.decompress(raw, max_bytes + 1)
    except zlib.error as e:
        raise FetchError(f"content-encoding {enc or 'gzip (sniffed)'}: corrupt body: {e}") from None
    if len(out) > max_bytes or d.unconsumed_tail:
        raise FetchError(f"decoded body exceeds {max_bytes} bytes")
    return out


def _fetch_once(url: str, timeout: float, max_bytes: int) -> tuple[bytes, dict]:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": ACCEPT, "Accept-Encoding": ACCEPT_ENCODING})
    deadline = time.monotonic() + timeout
    with urllib.request.urlopen(req, timeout=timeout) as r:
        info = {"status": getattr(r, "status", 200),
                "content_type": r.headers.get("Content-Type", ""),
                "encoding": r.headers.get("Content-Encoding", ""),
                "final_url": r.geturl()}
        chunks, total = [], 0
        while True:
            if time.monotonic() > deadline:
                raise FetchError(f"timed out after {timeout:g}s (slow body)")
            chunk = r.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise FetchError(f"body exceeds {max_bytes} bytes")
            chunks.append(chunk)
    raw = b"".join(chunks)
    return _decode_body(raw, info["encoding"], max_bytes), info


def fetch(url: str, cfg: dict | None = None, sleep=time.sleep) -> tuple[bytes, dict]:
    """GET url -> (decoded body, info). Raises FetchError with a short, safe message."""
    cfg = cfg or settings()
    attempts = 1 + cfg["retries"]
    last = None
    for attempt in range(attempts):
        if attempt:
            sleep(cfg["backoff"] * (2 ** (attempt - 1)))
        try:
            return _fetch_once(url, cfg["timeout"], cfg["max_bytes"])
        except urllib.error.HTTPError as e:
            try:
                body = e.read(512)
            except Exception:  # noqa: BLE001
                body = b""
            ctype = e.headers.get("Content-Type", "") if e.headers else ""
            last = FetchError(f"HTTP {e.code} {e.reason} ({_describe(ctype, body)})")
            if e.code not in RETRY_STATUS:
                break
        except FetchError as e:
            last = e
            if "exceeds" in str(e) or "content-encoding" in str(e):
                break  # deterministic: a retry would get the same answer
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            reason = getattr(e, "reason", None) or e
            last = FetchError(f"network: {type(reason).__name__}: {reason}")
        except Exception as e:  # noqa: BLE001 - http.client oddities (IncompleteRead, BadStatusLine)
            last = FetchError(f"{type(e).__name__}: {e}")
    tries = f" (after {attempt + 1} attempts)" if attempt else ""
    raise FetchError(f"{last}{tries}")


# ---------------------------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------------------------
def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _children(el, *names):
    for c in el:
        if _local(c.tag) in names:
            yield c


def _text(el, *names) -> str:
    """Text of the first direct child whose local name is in names (in names order)."""
    for n in names:
        for c in _children(el, n):
            t = "".join(c.itertext()).strip()
            if t:
                return t
    return ""


def clean(text: str, limit: int = 260) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text[:limit]


def arxiv_id(*candidates: str) -> str:
    for c in candidates:
        m = ARXIV_ID_RE.search(c or "")
        if m:
            return m.group(1) or m.group(3)
    return ""


def _atom_link(el) -> str:
    best = ""
    for c in _children(el, "link"):
        href = (c.get("href") or "").strip()
        rel = c.get("rel") or "alternate"
        if href and rel == "alternate":
            return href
        best = best or href
    return best


def _item(el, desc_limit: int) -> dict:
    # Atom links carry href (pick rel=alternate); RSS 2.0 and RDF links are element text.
    if any(c.get("href") is not None for c in _children(el, "link")):
        link = _atom_link(el)
    else:
        link = _text(el, "link")
    guid = _text(el, "guid", "id")
    if not link and guid.startswith(("http://", "https://")):
        link = guid
    if not link:
        link = (el.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about") or "").strip()
    return {
        "title": clean(_text(el, "title"), 400),
        "link": link,
        "date": _text(el, "pubDate", "date", "published", "updated", "issued", "modified"),
        "desc": clean(_text(el, "description", "summary", "encoded", "content"), desc_limit),
        "arxiv_id": arxiv_id(link, guid),
        "announce_type": _text(el, "announce_type"),
    }


def _strip_prolog(raw: bytes) -> bytes:
    """Drop a UTF-8 BOM and leading whitespace: both make expat reject the XML declaration."""
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return raw.lstrip()


def parse_feed(raw: bytes, content_type: str = "", desc_limit: int = 260):
    """Return (items, meta, error). meta: {"format": rss|atom|rdf, "empty": bool}.

    error is None for any well-formed RSS/Atom/RDF document, including one with no items."""
    raw = raw or b""
    body = _strip_prolog(raw)
    head = body[:512].lower()
    if not body:
        return [], {}, f"not a feed: empty body ({_describe(content_type, raw)})"
    if body[:1] != b"<":
        return [], {}, f"not a feed ({_describe(content_type, raw)})"
    if b"<!doctype html" in head or b"<html" in head:
        return [], {}, f"not a feed: got HTML ({_describe(content_type, raw)})"
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        return [], {}, f"xml parse: {e} ({_describe(content_type, raw)})"
    kind = _local(root.tag).lower()
    if kind == "rss":
        fmt, els = "rss", [i for ch in _children(root, "channel") for i in _children(ch, "item")]
    elif kind == "feed":
        fmt, els = "atom", list(_children(root, "entry"))
    elif kind == "rdf":
        fmt, els = "rdf", list(_children(root, "item"))
    else:
        return [], {}, f"not a feed: root element <{_local(root.tag)}> ({_describe(content_type, raw)})"
    items = [_item(e, desc_limit) for e in els]
    items = [i for i in items if i["title"] or i["link"]]
    return items, {"format": fmt, "empty": not items}, None


# ---------------------------------------------------------------------------------------------
# window
# ---------------------------------------------------------------------------------------------
def parse_date(s: str):
    """RFC 822 or ISO 8601 -> aware UTC datetime, or None."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
        if dt is not None:
            return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except ValueError:
        return None


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def window_start(now: datetime | None = None, env=None) -> datetime:
    """Start of the collection window: DIGEST_SINCE minus slack, else now - lookback,
    never further back than DIGEST_MAX_LOOKBACK_DAYS."""
    env = os.environ if env is None else env
    now = now or datetime.now(timezone.utc)
    max_back = timedelta(days=_env_num("DIGEST_MAX_LOOKBACK_DAYS", 30, float, 1, 365, env))
    since = parse_date(env.get("DIGEST_SINCE", ""))
    if since is not None:
        since -= timedelta(hours=_env_num("DIGEST_WINDOW_SLACK_HOURS", 48, float, 0, 24 * 14, env))
    else:
        since = now - timedelta(days=_env_num("DIGEST_LOOKBACK_DAYS", 14, float, 1, 365, env))
    return max(since, now - max_back)


def window(items: list[dict], since: datetime, max_items: int, undated_cap: int,
           skip_announce: tuple = ()) -> tuple[list[dict], dict]:
    """Bound a parsed feed. Returns (kept items, stats).

    stats: raw_count (items in the feed), in_window (dated items at/after `since`, before the
    cap), older (dated items before `since`), undated, skipped (arXiv replacements), and
    count (= len(kept): what the pipeline actually receives)."""
    dated, undated = [], []
    older = skipped = 0
    for it in items:
        if skip_announce and it.get("announce_type", "") in skip_announce:
            skipped += 1
            continue
        dt = parse_date(it.get("date", ""))
        if dt is None:
            undated.append(it)
        elif dt < since:
            older += 1
        else:
            it = dict(it, date=iso(dt))
            dated.append((dt, it))
    dated.sort(key=lambda p: p[0], reverse=True)   # some feeds are not newest-first
    kept = [it for _, it in dated[:max_items]]
    room = max(0, max_items - len(kept))
    kept += undated[:min(undated_cap, room)]      # document order: feeds list newest first
    stats = {"raw_count": len(items), "in_window": len(dated), "older": older,
             "undated": len(undated), "count": len(kept)}
    if skipped:
        stats["skipped"] = skipped
    return kept, stats


# ---------------------------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------------------------
def collect_feed(name: str, url: str, since: datetime, max_items: int, cfg: dict,
                 desc_limit: int = 260, fetcher=fetch) -> dict:
    """Fetch, parse and window one feed. Never raises."""
    is_arxiv = name.startswith("arxiv_")
    try:
        raw, info = fetcher(url, cfg)
        items, meta, err = parse_feed(raw, info.get("content_type", ""), desc_limit)
        if err:
            return {"ok": False, "error": err, "count": 0, "items": []}
        kept, stats = window(items, since, max_items, cfg["undated_cap"],
                             skip_announce=("replace", "replace-cross") if is_arxiv else ())
        out = {"ok": True, "error": None, **stats, "items": kept}
        if info.get("final_url") and info["final_url"] != url:
            out["final_url"] = info["final_url"]
        if meta.get("empty"):
            out["note"] = ("no new arXiv listing: announcements are Sun-Thu evenings US Eastern, "
                           "the feed is empty on weekends and holidays"
                           if is_arxiv else "feed has no items")
        elif not kept:
            out["note"] = "no items in the collection window"
        for it in kept:
            if not it.get("announce_type"):
                it.pop("announce_type", None)
        return out
    except FetchError as e:
        return {"ok": False, "error": str(e), "count": 0, "items": []}
    except Exception as e:  # noqa: BLE001 - one source must never kill the run
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "count": 0, "items": []}


def collect(sources: dict, worker, cfg: dict | None = None) -> dict:
    """Run worker(name, spec, cfg) for every source in parallel; preserve the source order.

    A worker that raises is recorded as a failed source; the others are unaffected."""
    cfg = cfg or settings()
    names = list(sources)
    results = {}
    with ThreadPoolExecutor(max_workers=min(cfg["workers"], max(1, len(names)))) as ex:
        futs = {n: ex.submit(worker, n, sources[n], cfg) for n in names}
        for n in names:
            try:
                results[n] = futs[n].result()
            except Exception as e:  # noqa: BLE001
                results[n] = {"ok": False, "error": f"{type(e).__name__}: {e}", "count": 0, "items": []}
    return results


def summary_line(name: str, v: dict) -> str:
    if not v.get("ok"):
        return f"[FAIL] {name}: {v.get('error')}"
    if "recent" in v:
        return f"[OK] {name} catalog={v.get('catalog_version')} total={v.get('total')} recent={len(v['recent'])}"
    extra = f" ({v['note']})" if v.get("note") else ""
    return (f"[OK] {name} count={v.get('count')} raw={v.get('raw_count')} "
            f"in_window={v.get('in_window')} undated={v.get('undated')}{extra}")
