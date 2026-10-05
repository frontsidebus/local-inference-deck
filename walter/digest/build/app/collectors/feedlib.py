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
- FeedCache:    per-URL cache in the digest state dir: conditional GET (ETag/Last-Modified), a
                minimum refetch interval (serve the cached copy), and a persisted, growing
                backoff after a 403/429 (no request until it expires).
- collect_with_fallbacks(): a source with a primary and fallback URLs (used when the primary
                fails or is backing off), items deduped by advisory id across them.
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
  DIGEST_CACHE_DIR            feed cache dir; default $STATE_DIR/feedcache; "off" disables
  DIGEST_MIN_REFETCH_MINUTES  serve the cached copy if fetched less than this ago (default 15)
  DIGEST_BLOCK_BACKOFF_MINUTES  first backoff after a 403/429, doubling      (default 60)
  DIGEST_BLOCK_BACKOFF_MAX_HOURS  backoff ceiling                            (default 24)
  DIGEST_STALE_MAX_HOURS      a primary copy this young is merged under a fallback (default 72)
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import socket
import threading
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
    """A per-source failure with a message that is safe to show (no body dumps).

    status is the HTTP status for HTTP errors (else None); retry_after the server's
    Retry-After in seconds when it sent a numeric one."""

    def __init__(self, msg: str = "", status: int | None = None, retry_after: float | None = None):
        super().__init__(msg)
        self.status = status
        self.retry_after = retry_after


class Blocked(FetchError):
    """The source answered 403/429 (now or earlier) and is in a persisted backoff."""


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


def _fetch_once(url: str, timeout: float, max_bytes: int,
                headers: dict | None = None) -> tuple[bytes, dict]:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": ACCEPT, "Accept-Encoding": ACCEPT_ENCODING,
        **(headers or {})})
    deadline = time.monotonic() + timeout
    with urllib.request.urlopen(req, timeout=timeout) as r:
        info = {"status": getattr(r, "status", 200),
                "content_type": r.headers.get("Content-Type", ""),
                "encoding": r.headers.get("Content-Encoding", ""),
                "final_url": r.geturl(),
                "etag": r.headers.get("ETag", ""),
                "last_modified": r.headers.get("Last-Modified", "")}
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


def _retry_after(headers) -> float | None:
    try:
        v = float((headers.get("Retry-After") or "").strip()) if headers else None
    except ValueError:
        return None
    return v if v is not None and v >= 0 else None


def fetch(url: str, cfg: dict | None = None, sleep=time.sleep, headers: dict | None = None,
          retry_status=RETRY_STATUS) -> tuple[bytes, dict]:
    """GET url -> (decoded body, info). Raises FetchError with a short, safe message.

    headers: extra request headers (FeedCache sends If-None-Match / If-Modified-Since).
    A 304 Not Modified is returned, not raised: (b"", info with status 304).
    retry_status: the statuses worth one more attempt (FeedCache leaves out 403/429)."""
    cfg = cfg or settings()
    attempts = 1 + cfg["retries"]
    last = None
    for attempt in range(attempts):
        if attempt:
            sleep(cfg["backoff"] * (2 ** (attempt - 1)))
        try:
            return _fetch_once(url, cfg["timeout"], cfg["max_bytes"], headers)
        except urllib.error.HTTPError as e:
            if e.code == 304:
                hdr = e.headers or {}
                return b"", {"status": 304, "content_type": "", "encoding": "", "final_url": url,
                             "etag": hdr.get("ETag", "") or "",
                             "last_modified": hdr.get("Last-Modified", "") or ""}
            try:
                body = e.read(512)
            except Exception:  # noqa: BLE001
                body = b""
            ctype = e.headers.get("Content-Type", "") if e.headers else ""
            last = FetchError(f"HTTP {e.code} {e.reason} ({_describe(ctype, body)})",
                              status=e.code, retry_after=_retry_after(e.headers))
            if e.code not in retry_status:
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
    raise FetchError(f"{last}{tries}", status=getattr(last, "status", None),
                     retry_after=getattr(last, "retry_after", None))


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
        if info.get("cache"):
            out["cache"] = info["cache"]
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


# ---------------------------------------------------------------------------------------------
# cache: conditional GET, minimum refetch interval, persisted 403/429 backoff
# ---------------------------------------------------------------------------------------------
BLOCK_STATUS = {403, 429}


def cache_settings(env=None) -> dict:
    env = os.environ if env is None else env
    return {
        "min_refetch": _env_num("DIGEST_MIN_REFETCH_MINUTES", 15, float, 0, 24 * 60, env) * 60,
        "block_base": _env_num("DIGEST_BLOCK_BACKOFF_MINUTES", 60, float, 1, 24 * 60, env) * 60,
        "block_max": _env_num("DIGEST_BLOCK_BACKOFF_MAX_HOURS", 24, float, 1, 24 * 14, env) * 3600,
        "stale_max": _env_num("DIGEST_STALE_MAX_HOURS", 72, float, 0, 24 * 30, env) * 3600,
    }


class FeedCache:
    """Per-URL cache under the digest state dir (default $STATE_DIR/feedcache).

    For every URL it keeps the last good body and its validators (ETag, Last-Modified), when it
    was fetched, and a 403/429 backoff record. fetch(url, cfg) has the feedlib.fetch signature:
    - within `min_refetch` seconds of the last good fetch it serves the cached body, no request
      (RUN NOW pressed repeatedly, or two watches sharing a feed);
    - otherwise it sends a conditional GET; a 304 serves the cached body;
    - a 403 or 429 starts (or extends) a backoff: block_base doubling per consecutive block, at
      most block_max, at least the server's Retry-After. Until it expires no request is sent and
      Blocked is raised; the first good answer clears it.
    Cache I/O errors never fail a fetch: the cache just stops helping."""

    def __init__(self, root, cfg: dict | None = None, clock=time.time, fetcher=None):
        self.root = os.fspath(root)
        self.cfg = cfg or cache_settings()
        self.clock = clock
        self._fetch = fetcher or fetch

    @classmethod
    def from_env(cls, env=None):
        """The configured cache, or None (DIGEST_CACHE_DIR=off, or neither it nor STATE_DIR)."""
        env = os.environ if env is None else env
        root = (env.get("DIGEST_CACHE_DIR") or "").strip()
        if root.lower() in ("off", "none", "0", "false"):
            return None
        if not root and env.get("STATE_DIR"):
            root = os.path.join(env["STATE_DIR"], "feedcache")
        return cls(root, cache_settings(env)) if root else None

    # -- storage ----------------------------------------------------------------------------
    def _key(self, url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()[:24]

    def _path(self, name: str) -> str:
        return os.path.join(self.root, name)

    def _read_json(self, name: str) -> dict:
        try:
            with open(self._path(name)) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, name: str, data: bytes) -> None:
        try:
            os.makedirs(self.root, exist_ok=True)
            tmp = self._path(f".{name}.{os.getpid()}.{threading.get_ident()}.tmp")
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, self._path(name))
        except OSError:
            pass

    def load_json(self, name: str) -> dict:
        return self._read_json(f"{name}.json")

    def save_json(self, name: str, data: dict) -> None:
        self._write(f"{name}.json", json.dumps(data, indent=1, sort_keys=True).encode())

    def meta(self, url: str) -> dict:
        m = self._read_json(f"{self._key(url)}.json")
        return m if m.get("url") == url else {}

    def _save_meta(self, url: str, m: dict) -> None:
        m["url"] = url
        self._write(f"{self._key(url)}.json", json.dumps(m, indent=1, sort_keys=True).encode())

    def body(self, url: str, m: dict | None = None):
        """The cached body, or None when absent or not the one the metadata describes."""
        m = self.meta(url) if m is None else m
        if not m.get("sha256"):
            return None
        try:
            with open(self._path(f"{self._key(url)}.body"), "rb") as f:
                raw = f.read()
        except OSError:
            return None
        return raw if hashlib.sha256(raw).hexdigest() == m["sha256"] else None

    def stale(self, url: str):
        """(body, fetched_at) of the last good copy if younger than stale_max, else None."""
        m = self.meta(url)
        at = m.get("fetched_at")
        if not at or self.clock() - at > self.cfg["stale_max"]:
            return None
        raw = self.body(url, m)
        return (raw, at) if raw is not None else None

    # -- fetch ------------------------------------------------------------------------------
    def fetch(self, url: str, cfg: dict | None = None) -> tuple[bytes, dict]:
        m = self.meta(url)
        now = self.clock()
        blk = m.get("block") or {}
        if blk and now < blk.get("until", 0):
            raise Blocked(f"blocked: HTTP {blk.get('status')} at {iso_ts(blk.get('last', now))}; "
                          f"backing off until {iso_ts(blk['until'])} "
                          f"(block #{blk.get('count', 1)} since {iso_ts(blk.get('since', now))}, "
                          f"no request sent)", status=blk.get("status"))
        base = {"content_type": m.get("content_type", ""), "final_url": m.get("final_url", url),
                "etag": m.get("etag", ""), "last_modified": m.get("last_modified", "")}
        at = m.get("fetched_at")
        cached = self.body(url, m) if at else None
        if cached is not None and now - at < self.cfg["min_refetch"]:
            return cached, dict(base, status=200, cache=f"fresh (fetched {iso_ts(at)}, no request)")
        headers = {}
        if cached is not None:
            if m.get("etag"):
                headers["If-None-Match"] = m["etag"]
            if m.get("last_modified"):
                headers["If-Modified-Since"] = m["last_modified"]
        try:
            raw, info = self._fetch(url, cfg, headers=headers, retry_status=RETRY_STATUS - BLOCK_STATUS)
        except FetchError as e:
            if e.status in BLOCK_STATUS:
                count = blk.get("count", 0) + 1
                delay = min(self.cfg["block_max"], self.cfg["block_base"] * 2 ** (count - 1))
                if e.retry_after:
                    delay = max(delay, min(e.retry_after, self.cfg["block_max"]))
                m["block"] = {"status": e.status, "count": count, "since": blk.get("since", now),
                              "last": now, "until": now + delay}
                self._save_meta(url, m)
                raise Blocked(f"{e}; backing off until {iso_ts(now + delay)} (block #{count})",
                              status=e.status, retry_after=e.retry_after) from None
            raise
        m.pop("block", None)
        if info.get("status") == 304:
            if cached is None:  # we never send validators without a body; be safe anyway
                raise FetchError("HTTP 304 Not Modified but no cached copy", status=304)
            m["fetched_at"] = now
            for k in ("etag", "last_modified"):
                if info.get(k):
                    m[k] = info[k]
            self._save_meta(url, m)
            return cached, dict(base, status=304, cache="revalidated (304)")
        self._write(f"{self._key(url)}.body", raw)
        m.update({"fetched_at": now, "sha256": hashlib.sha256(raw).hexdigest(),
                  "etag": info.get("etag", ""), "last_modified": info.get("last_modified", ""),
                  "content_type": info.get("content_type", ""),
                  "final_url": info.get("final_url", url)})
        self._save_meta(url, m)
        return raw, dict(info, cache="fetched")


def iso_ts(ts: float) -> str:
    return iso(datetime.fromtimestamp(ts, timezone.utc))


# ---------------------------------------------------------------------------------------------
# fallbacks: one source, a primary URL and fallback URLs, deduped by advisory id
# ---------------------------------------------------------------------------------------------
# CISA advisory ids: ICS (ICSA-26-274-02), ICS medical (ICSMA-..), joint cybersecurity advisories
# (AA25-212A), analysis reports (AR25-..A), vulnerability advisories (VA-26-..-01).
ADVISORY_ID_RE = re.compile(
    r"\b(ICSM?A-\d{2}-\d{3}-\d{2}[A-Z]?|AA\d{2}-\d{3}[A-Z]|AR\d{2}-\d{3}[A-Z]|VA-\d{2}-\d{3}-\d{2})\b",
    re.I)
IDS_KEEP = 2000


def advisory_id(*candidates: str) -> str:
    for c in candidates:
        m = ADVISORY_ID_RE.search(c or "")
        if m:
            return m.group(1).upper()
    return ""


def _item_key(it: dict) -> str:
    return (it.get("advisory_id") or advisory_id(it.get("link", ""), it.get("title", ""))
            or it.get("link") or clean(it.get("title", "")).lower())


def merge_items(*lists: list[dict], max_items: int) -> list[dict]:
    """Union of item lists, deduped by advisory id (else link, else title); the first list
    wins. Newest first (ISO dates sort as strings), undated last, at most max_items."""
    out, seen = [], set()
    for lst in lists:
        for it in lst:
            k = _item_key(it)
            if k in seen:
                continue
            seen.add(k)
            out.append(it)
    floor = datetime.min.replace(tzinfo=timezone.utc)
    out.sort(key=lambda it: parse_date(it.get("date", "")) or floor, reverse=True)
    return out[:max_items]


def _short_host(url: str) -> str:
    m = re.match(r"https?://([^/]+)(/[^?#]*)?", url or "")
    return f"{m.group(1)}{m.group(2) or ''}" if m else url


def collect_with_fallbacks(name: str, primary: tuple, fallbacks: list, since: datetime,
                           max_items: int, cfg: dict, desc_limit: int = 260, fetcher=fetch,
                           cache: FeedCache | None = None, handlers: dict | None = None) -> dict:
    """Collect `name` from its primary (kind, url); if that fails (blocked, backing off, or any
    other error), from the first fallback (kind, url, label) that works. Never raises.

    kinds: "feed" (RSS/Atom/RDF via collect_feed) or one of `handlers`
    (kind -> fn(name, url, since, max_items, cfg, desc_limit, fetcher, cache) -> result).
    Items carry advisory_id. With a cache, the id -> title map of what the primary published is
    kept, so a fallback item for a known advisory gets the primary's exact title (the pipeline
    dedupes news by normalized title); a stale primary copy (DIGEST_STALE_MAX_HOURS) is merged
    under the fallback items, deduped by advisory id with the primary winning."""
    handlers = handlers or {}

    def run(kind, url):
        try:
            if kind == "feed":
                return collect_feed(name, url, since, max_items, cfg, desc_limit, fetcher)
            if kind in handlers:
                return handlers[kind](name, url, since, max_items, cfg, desc_limit, fetcher, cache)
            return {"ok": False, "error": f"unknown source kind {kind!r}", "count": 0, "items": []}
        except FetchError as e:
            return {"ok": False, "error": str(e), "count": 0, "items": []}
        except Exception as e:  # noqa: BLE001 - one source must never kill the run
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "count": 0, "items": []}

    def tag(items):
        for it in items:
            it["advisory_id"] = advisory_id(it.get("link", ""), it.get("title", ""))
        return items

    pkind, purl = primary[0], primary[1]
    res = run(pkind, purl)
    ids = cache.load_json(f"ids-{name}") if cache else {}
    if res.get("ok"):
        tag(res["items"])
        if cache:
            fresh = {it["advisory_id"]: it["title"] for it in res["items"] if it["advisory_id"]}
            if any(ids.get(k) != v for k, v in fresh.items()):
                ids.update(fresh)
                cache.save_json(f"ids-{name}", dict(list(ids.items())[-IDS_KEEP:]))
        res["via"] = "primary"
        return res
    errors = [f"primary {_short_host(purl)}: {res.get('error')}"]
    for fb in fallbacks:
        fkind, furl = fb[0], fb[1]
        label = fb[2] if len(fb) > 2 else _short_host(furl)
        fres = run(fkind, furl)
        if not fres.get("ok"):
            errors.append(f"fallback {label}: {fres.get('error')}")
            continue
        items = tag(fres["items"])
        for it in items:  # a known advisory keeps the title the primary gave it
            if it["advisory_id"] and ids.get(it["advisory_id"]):
                it["title"] = ids[it["advisory_id"]]
        stale_items, stale_note = [], ""
        st = cache.stale(purl) if cache and pkind == "feed" else None
        if st:
            p_items, _, err = parse_feed(st[0], "", desc_limit)
            if not err:
                stale_items, _ = window(p_items, since, max_items, cfg["undated_cap"])
                stale_items = tag(stale_items)
                stale_note = f"; merged {len(stale_items)} item(s) from the primary copy of {iso_ts(st[1])}"
        merged = merge_items(stale_items, items, max_items=max_items)
        out = {k: v for k, v in fres.items() if k not in ("items", "note")}
        out.update({"ok": True, "error": None, "count": len(merged), "items": merged,
                    "via": "fallback", "fallback": label,
                    "note": f"{errors[0]}; served via fallback {label}"
                            + (f" ({fres['note']})" if fres.get("note") else "") + stale_note})
        return out
    return {"ok": False, "error": "; ".join(errors), "count": 0, "items": []}


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
        cache = f" cache={v['cache']}" if v.get("cache") else ""
        return (f"[OK] {name} catalog={v.get('catalog_version')} total={v.get('total')} "
                f"recent={len(v['recent'])}{cache}")
    extra = f" ({v['note']})" if v.get("note") else ""
    extra += f" via={v['via']}" if v.get("via") else ""
    extra += f" cache={v['cache']}" if v.get("cache") else ""
    return (f"[OK] {name} count={v.get('count')} raw={v.get('raw_count')} "
            f"in_window={v.get('in_window')} undated={v.get('undated')}{extra}")
