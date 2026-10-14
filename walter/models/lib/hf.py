"""Hugging Face Hub over plain urllib (stdlib only): search, revision pinning, file lists, card data, Range reads.

Endpoints (checked 2026-10-14 against huggingface_hub's hf_api.py / file_download.py / utils/_http.py and live
read-only calls on public repos; see walter/models/README.md "Fit calculator" for what is verified vs assumed):
  GET /api/models?search=&author=&filter=gguf&sort=downloads&direction=-1&limit=&expand[]=...   search
  GET /api/models/<repo>/revision/<rev>?blobs=true    sha, siblings[{rfilename,size,lfs{sha256,size}}], cardData,
                                                      gated (false|"auto"|"manual"), gguf{architecture,total,...}
  GET /api/models/<repo>/tree/<sha>?recursive=true    fallback file list (paginated by Link: rel="next")
  GET /<repo>/resolve/<sha>/<path>  + Range           302 to a CDN/Xet bridge that answers 206 with Content-Range
Errors carry X-Error-Code (RepoNotFound, RevisionNotFound, EntryNotFound, GatedRepo); a missing or private repo
answers 401 without a token, the same as bad credentials.

Token: optional, read from a file whose PATH is passed in (never argv, env dumps or logs). It is sent only as an
Authorization header to Hub hosts; redirects off the Hub (CDN, Xet bridge) never get it, as huggingface_hub does.
Every function takes `fetch=` (a transport, for tests) and `endpoint=` (default https://huggingface.co).
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

__all__ = [
    "HFError", "Response", "search", "resolve_revision", "list_files", "model_card", "range_reader",
    "read_token", "urllib_fetch", "DEFAULT_ENDPOINT",
]

DEFAULT_ENDPOINT = "https://huggingface.co"
HUB_HOSTS = {"huggingface.co", "hf.co"}
USER_AGENT = "spark-model/1 (urllib)"
TIMEOUT = 30
MAX_REDIRECTS = 10
MAX_JSON = 64 << 20               # cap on any API body read into memory
RANGE_INITIAL = 8 << 20
RANGE_MAX = 256 << 20

REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_.\-]{8,512}$")

_sleep = time.sleep               # tests patch this


class HFError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None, code: str | None = None):
        super().__init__(msg)
        self.status, self.code = status, code


@dataclass
class Response:
    status: int
    headers: dict          # lower-case names
    body: bytes


Fetch = Callable[[str, dict, int, float], Response]   # (url, headers, max_body, timeout) -> Response


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def urllib_fetch(url: str, headers: dict, max_body: int, timeout: float) -> Response:
    """One GET without following redirects; reads at most max_body bytes of the body."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        resp = _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:      # 3xx (not followed), 4xx, 5xx
        resp = e
    try:
        status = resp.status if hasattr(resp, "status") else resp.code
        hdrs = {k.lower(): v for k, v in resp.headers.items()} if resp.headers else {}
        body = resp.read(max_body) if 200 <= status < 300 else resp.read(64 << 10)
    finally:
        resp.close()
    return Response(status, hdrs, body or b"")


def read_token(token_file) -> str | None:
    """The token from a file (first line, stripped). None if no path is given. Never echoes the value."""
    if not token_file:
        return None
    with open(token_file, "rb") as f:
        raw = f.read(4097)
    if len(raw) > 4096:
        raise HFError(f"token file {token_file} is too large to be a token")
    tok = raw.decode("ascii", errors="replace").strip()
    if not TOKEN_RE.match(tok):
        raise HFError(f"token file {token_file} does not hold a plausible token (one line, [A-Za-z0-9_.-])")
    return tok


def _check_repo(repo: str) -> str:
    if not isinstance(repo, str) or not REPO_RE.match(repo) or ".." in repo:
        raise HFError(f"invalid repo id {repo!r} (expected org/name)")
    return repo


def _check_sha(sha: str) -> str:
    if not isinstance(sha, str) or not SHA_RE.match(sha):
        raise HFError(f"not a 40-hex commit sha: {sha!r}")
    return sha


def _check_path(path: str) -> str:
    parts = path.split("/") if isinstance(path, str) else []
    if not parts or path.startswith("/") or any(p in ("", ".", "..") for p in parts) or "\\" in path \
            or any(ord(ch) < 32 for ch in path):
        raise HFError(f"invalid repo file path {path!r}")
    return path


class _Client:
    def __init__(self, endpoint=None, token_file=None, fetch: Fetch | None = None, timeout=TIMEOUT, retries=2):
        self.endpoint = (endpoint or DEFAULT_ENDPOINT).rstrip("/")
        self.hub_hosts = HUB_HOSTS | {urllib.parse.urlsplit(self.endpoint).hostname or ""}
        self._token = read_token(token_file)
        self.fetch = fetch or urllib_fetch
        self.timeout, self.retries = timeout, retries

    def __repr__(self):
        return f"<hf client {self.endpoint} token={'set' if self._token else 'none'}>"

    def get(self, url: str, *, headers=None, max_body=MAX_JSON, ok=(200,)) -> Response:
        """GET with redirects (auth only to Hub hosts), retries on 429/5xx/network errors, Hub error mapping."""
        hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
        attempt = 0
        while True:
            try:
                resp, final = self._follow(url, hdrs, max_body)
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                if attempt >= self.retries:
                    raise HFError(f"network error for {_safe(url)}: {e}") from None
                resp, final = None, url
            if resp is not None and resp.status in ok:
                return resp
            if resp is not None and resp.status not in (429, 500, 502, 503, 504) or attempt >= self.retries:
                raise _error(resp, final)
            attempt += 1
            _sleep(min(2 ** attempt, 10))

    def _follow(self, url, hdrs, max_body):
        for _ in range(MAX_REDIRECTS + 1):
            send = dict(hdrs)
            host = urllib.parse.urlsplit(url).hostname or ""
            if self._token and host in self.hub_hosts:
                send["Authorization"] = f"Bearer {self._token}"
            resp = self.fetch(url, send, max_body, self.timeout)
            if resp.status in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                nxt = urllib.parse.urljoin(url, resp.headers["location"])
                if urllib.parse.urlsplit(nxt).scheme not in ("https", "http"):
                    raise HFError(f"refusing redirect to {_safe(nxt)}")
                if urllib.parse.urlsplit(url).scheme == "https" and nxt.startswith("http:"):
                    raise HFError(f"refusing https -> http redirect to {_safe(nxt)}")
                url = nxt
                continue
            return resp, url
        raise HFError(f"too many redirects for {_safe(url)}")

    def json(self, path: str, params=None):
        url = self.endpoint + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        resp = self.get(url)
        try:
            return json.loads(resp.body), resp
        except ValueError:
            raise HFError(f"invalid JSON from {_safe(url)}") from None


def _safe(url: str) -> str:
    """URL without its query (signed CDN URLs carry credentials in the query)."""
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def _error(resp: Response | None, url: str) -> HFError:
    if resp is None:
        return HFError(f"no response from {_safe(url)}")
    code = resp.headers.get("x-error-code")
    msg = resp.headers.get("x-error-message") or ""
    s = resp.status
    if code == "GatedRepo":
        why = "gated repo: accept its licence on the Hub and use a token (--token-file)"
    elif code == "RepoNotFound" or (s == 401 and not code):
        why = "repo not found, private, or the token is invalid (the Hub answers 401 for all three)"
    elif code in ("RevisionNotFound", "EntryNotFound"):
        why = code
    elif s == 416:
        why = "range not satisfiable"
    else:
        why = f"HTTP {s}"
    detail = f" ({msg[:200]})" if msg and code != "GatedRepo" else ""
    return HFError(f"{why}: {_safe(url)}{detail}", status=s, code=code)


# --- revision info cache: (endpoint, repo, sha) -> /revision/<sha>?blobs=true JSON. A sha is immutable. --------
_INFO: dict[tuple, dict] = {}


def _info(c: _Client, repo: str, sha: str) -> dict:
    key = (c.endpoint, repo, sha)
    if key not in _INFO:
        data, _r = c.json(f"/api/models/{repo}/revision/{sha}", {"blobs": "true"})
        if not isinstance(data, dict):
            raise HFError("unexpected revision info shape")
        if len(_INFO) > 64:
            _INFO.clear()
        _INFO[key] = data
    return _INFO[key]


# --- public API ------------------------------------------------------------------------------------------------
SEARCH_EXPAND = ["author", "downloads", "likes", "gated", "lastModified", "tags", "cardData", "gguf"]


def search(query: str = "", *, author: str | None = None, limit: int = 20, sort: str = "downloads",
           gguf_only: bool = True, endpoint=None, token_file=None, fetch=None) -> list[dict]:
    """Model search, newest/most downloaded first. Each item: {id, author, downloads, likes, gated, license,
    last_modified, tags, gguf: {architecture, total(params), context_length} | None}."""
    c = _Client(endpoint, token_file, fetch)
    params = {"search": query, "sort": sort, "direction": "-1", "limit": str(max(1, min(int(limit), 100))),
              "expand[]": SEARCH_EXPAND}
    if gguf_only:
        params["filter"] = "gguf"
    if author:
        params["author"] = author
    if not query:
        del params["search"]
    data, _r = c.json("/api/models", params)
    if not isinstance(data, list):
        raise HFError("unexpected search response shape")
    out = []
    for m in data[: int(limit)]:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str):
            continue
        card = m.get("cardData") if isinstance(m.get("cardData"), dict) else {}
        g = m.get("gguf") if isinstance(m.get("gguf"), dict) else None
        out.append({
            "id": m["id"],
            "author": m.get("author") or m["id"].split("/")[0],
            "downloads": m.get("downloads"),
            "likes": m.get("likes"),
            "gated": m.get("gated", False),
            "license": card.get("license") or _tag_value(m.get("tags"), "license"),
            "last_modified": m.get("lastModified"),
            "tags": m.get("tags") or [],
            "gguf": None if g is None else {k: g.get(k) for k in ("architecture", "total", "context_length")},
        })
    return out


def _tag_value(tags, prefix):
    for t in tags or []:
        if isinstance(t, str) and t.startswith(prefix + ":"):
            return t.split(":", 1)[1]
    return None


def resolve_revision(repo: str, rev: str = "main", *, endpoint=None, token_file=None, fetch=None) -> str:
    """Branch/tag/sha -> the 40-hex commit sha (asks the Hub even for a sha, so a typo fails here)."""
    _check_repo(repo)
    c = _Client(endpoint, token_file, fetch)
    data, _r = c.json(f"/api/models/{repo}/revision/{urllib.parse.quote(rev or 'main', safe='')}",
                      {"blobs": "true"})
    sha = data.get("sha") if isinstance(data, dict) else None
    _check_sha(sha)
    if isinstance(data, dict):
        _INFO[(c.endpoint, repo, sha)] = data
    return sha


def list_files(repo: str, sha: str, *, endpoint=None, token_file=None, fetch=None) -> list[dict]:
    """Every file at the commit: [{path, size, sha256}] (sha256 = the LFS sha256, None for non-LFS files)."""
    _check_repo(repo), _check_sha(sha)
    c = _Client(endpoint, token_file, fetch)
    sib = _info(c, repo, sha).get("siblings")
    if isinstance(sib, list) and sib and all(isinstance(s, dict) and isinstance(s.get("size"), int) for s in sib):
        return [_file(s.get("rfilename"), s.get("size"), (s.get("lfs") or {}).get("sha256")) for s in sib]
    return _tree(c, repo, sha)


def _file(path, size, sha256):
    if not isinstance(path, str):
        raise HFError("file entry without a path")
    if not isinstance(size, int) or size < 0:
        raise HFError(f"{path}: bad size {size!r}")
    if sha256 is not None and not (isinstance(sha256, str) and SHA256_RE.match(sha256)):
        sha256 = None
    return {"path": path, "size": size, "sha256": sha256}


def _tree(c: _Client, repo, sha):
    url = f"{c.endpoint}/api/models/{repo}/tree/{sha}?recursive=true"
    out, pages = [], 0
    while url and pages < 100:
        resp = c.get(url)
        pages += 1
        try:
            items = json.loads(resp.body)
        except ValueError:
            raise HFError("invalid JSON from the tree API") from None
        for it in items if isinstance(items, list) else []:
            if isinstance(it, dict) and it.get("type") == "file":
                out.append(_file(it.get("path"), it.get("size"), (it.get("lfs") or {}).get("oid")))
        url = _next_link(resp.headers.get("link", ""))
        if url and (urllib.parse.urlsplit(url).hostname or "") not in c.hub_hosts:
            raise HFError("tree pagination left the Hub")
    return out


def _next_link(link: str) -> str | None:
    m = re.search(r'<([^>]+)>\s*;\s*rel="next"', link or "")
    return m.group(1) if m else None


def model_card(repo: str, sha: str, *, endpoint=None, token_file=None, fetch=None) -> dict:
    """Card metadata at the commit: {license, license_name, license_link, gated, base_model, tags, pipeline_tag,
    extra_gated_prompt, private, disabled, author, gguf: {architecture, total, context_length, chat_template_bytes}}."""
    _check_repo(repo), _check_sha(sha)
    c = _Client(endpoint, token_file, fetch)
    d = _info(c, repo, sha)
    card = d.get("cardData") if isinstance(d.get("cardData"), dict) else {}
    base = card.get("base_model")
    base = [base] if isinstance(base, str) else [b for b in base or [] if isinstance(b, str)]
    g = d.get("gguf") if isinstance(d.get("gguf"), dict) else None
    tmpl = g.get("chat_template") if g else None
    return {
        "license": card.get("license") or _tag_value(d.get("tags"), "license"),
        "license_name": card.get("license_name"),
        "license_link": card.get("license_link"),
        "gated": d.get("gated", False),
        "base_model": base,
        "tags": [t for t in d.get("tags") or [] if isinstance(t, str)],
        "pipeline_tag": card.get("pipeline_tag") or d.get("pipeline_tag"),
        "extra_gated_prompt": card.get("extra_gated_prompt"),
        "private": bool(d.get("private", False)),
        "disabled": bool(d.get("disabled", False)),
        "author": d.get("author"),
        "gguf": None if g is None else {
            "architecture": g.get("architecture"), "total": g.get("total"),
            "context_length": g.get("context_length"),
            "chat_template_bytes": len(tmpl.encode()) if isinstance(tmpl, str) else None,
        },
    }


def range_reader(repo: str, sha: str, path: str, *, initial: int = RANGE_INITIAL, max_bytes: int = RANGE_MAX,
                 endpoint=None, token_file=None, fetch=None) -> Callable[[int, int], bytes]:
    """read(offset, n) over HTTP Range on resolve/<sha>/<path>, for gguf.parse_header.

    Keeps one buffer from byte 0 and grows it by doubling (initial, 2x, 4x, ... capped at max_bytes) until it covers
    the request; only the new tail is fetched each time. Reads past max_bytes return short (parse_header then fails
    with GGUFError). read.stats = {"requests": n, "bytes": fetched, "size": file size or None}."""
    _check_repo(repo), _check_sha(sha), _check_path(path)
    c = _Client(endpoint, token_file, fetch)
    url = f"{c.endpoint}/{repo}/resolve/{sha}/{urllib.parse.quote(path, safe='/')}"
    buf = bytearray()
    stats = {"requests": 0, "bytes": 0, "size": None}

    def grow(target: int):
        size = max(initial, len(buf))
        while size < target:
            size *= 2
        size = min(size, max_bytes)
        if stats["size"] is not None:
            size = min(size, stats["size"])
        start = len(buf)
        if size <= start:
            return
        resp = c.get(url, headers={"Range": f"bytes={start}-{size - 1}"}, max_body=size - start, ok=(200, 206))
        stats["requests"] += 1
        body = resp.body
        if resp.status == 206:
            m = re.match(r"bytes (\d+)-(\d+)/(\d+|\*)", resp.headers.get("content-range", ""))
            if not m or int(m.group(1)) != start:
                raise HFError(f"bad Content-Range {resp.headers.get('content-range')!r} for {path}")
            if m.group(3) != "*":
                stats["size"] = int(m.group(3))
        elif start == 0:                      # server ignored Range: keep what was asked for (body is capped)
            body = body[: size]
            if "content-length" in resp.headers and resp.headers["content-length"].isdigit():
                stats["size"] = int(resp.headers["content-length"])
        else:
            raise HFError(f"server ignored the Range header for {path}")
        body = body[: size - start]
        stats["bytes"] += len(body)
        buf.extend(body)
        if len(body) < size - start:          # EOF
            stats["size"] = len(buf)

    def read(offset: int, n: int) -> bytes:
        if offset < 0 or n < 0:
            raise ValueError("negative offset or length")
        end = min(offset + n, max_bytes)
        if end > len(buf) and (stats["size"] is None or len(buf) < stats["size"]):
            grow(end)
        return bytes(buf[offset:end])

    read.stats = stats
    return read
