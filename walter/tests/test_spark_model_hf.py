"""walter/models/lib/hf.py against a local fake Hub (http.server on 127.0.0.1; "localhost" plays the CDN host).

Covers: revision pinning, file lists (siblings with LFS sha256, tree fallback with Link pagination), card data,
search params, error mapping (401/GatedRepo/RevisionNotFound), retries, redirect following with the token sent
only to the Hub host, Range reads that double until gguf.parse_header succeeds and stop at the cap.
No network: everything is served from this process.
Run: python3 -B -m pytest -q walter/tests/test_spark_model_hf.py
"""
import json
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "models" / "lib"))
import gguf  # noqa: E402
import hf  # noqa: E402

SHA = "a" * 40
LFS = "b" * 64
TOKEN = "fake-token." + "x" * 12        # built at runtime: no secret-shaped literal
REPO = "someorg/Some-Model-GGUF"
GG = gguf.build_test_gguf(
    {"general.architecture": "llama", "tokenizer.ggml.tokens": [f"tok{i}" for i in range(20000)],
     "tokenizer.chat_template": "{{ messages }}"},
    [("blk.0.attn_q.weight", (256, 256), "Q4_K")], with_data=True)


class Hub:
    def __init__(self):
        self.log = []          # (host, path, headers)
        self.fail_next = 0
        self.ignore_range = False
        self.tree_only = False

    def info(self):
        sib = [{"rfilename": "README.md", "size": 10},
               {"rfilename": "Q4_K_M/m.gguf", "size": len(GG), "lfs": {"sha256": LFS, "size": len(GG)}}]
        if self.tree_only:
            sib = [{"rfilename": s["rfilename"]} for s in sib]
        return {"id": REPO, "sha": SHA, "gated": "manual", "private": False, "author": "someorg",
                "tags": ["gguf", "license:apache-2.0"], "siblings": sib,
                "cardData": {"license": "other", "license_name": "x-community", "base_model": "base/Model",
                             "extra_gated_prompt": "agree"},
                "gguf": {"architecture": "llama", "total": 8_000_000_000, "context_length": 8192,
                         "chat_template": "{{ x }}"}}


def make_handler(hub: Hub, port_box):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, status, body=b"", headers=None):
            self.send_response(status)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: C901
            host = self.headers.get("Host", "").split(":")[0]
            u = urllib.parse.urlsplit(self.path)
            q = urllib.parse.parse_qs(u.query)
            hub.log.append((host, self.path, dict(self.headers)))
            if hub.fail_next:
                hub.fail_next -= 1
                return self.send(503, b"busy")
            p = u.path
            if p == f"/api/models/{REPO}/revision/main" or p == f"/api/models/{REPO}/revision/{SHA}":
                return self.send(200, json.dumps(hub.info()).encode())
            if p.startswith(f"/api/models/{REPO}/revision/"):
                return self.send(404, b"{}", {"X-Error-Code": "RevisionNotFound", "X-Error-Message": "Invalid rev"})
            if p == f"/api/models/{REPO}/tree/{SHA}":
                port = port_box[0]
                if "cursor" not in q:
                    items = [{"type": "directory", "path": "Q4_K_M", "size": 0},
                             {"type": "file", "path": "README.md", "size": 10}]
                    nxt = f"<http://127.0.0.1:{port}/api/models/{REPO}/tree/{SHA}?recursive=true&cursor=2>; rel=\"next\""
                    return self.send(200, json.dumps(items).encode(), {"Link": nxt})
                items = [{"type": "file", "path": "Q4_K_M/m.gguf", "size": len(GG), "lfs": {"oid": LFS}}]
                return self.send(200, json.dumps(items).encode())
            if p == "/api/models":
                body = [{"id": REPO, "author": "someorg", "downloads": 5, "likes": 1, "gated": False,
                         "lastModified": "2026-01-01T00:00:00.000Z", "tags": ["license:mit"],
                         "gguf": {"architecture": "llama", "total": 8e9, "context_length": 8192}},
                        {"bogus": 1}]
                return self.send(200, json.dumps(body).encode())
            if p.startswith("/gated/"):
                return self.send(401, b"", {"X-Error-Code": "GatedRepo", "X-Error-Message": "restricted"})
            if p.startswith(f"/{REPO}/resolve/{SHA}/"):
                loc = f"http://localhost:{port_box[0]}/cdn/blob?sig=secret"
                return self.send(302, b"", {"Location": loc})
            if p == "/cdn/blob":
                rng = self.headers.get("Range")
                if rng and not hub.ignore_range:
                    a, b = rng.split("=")[1].split("-")
                    a, b = int(a), min(int(b), len(GG) - 1)
                    if a >= len(GG):
                        return self.send(416, b"")
                    return self.send(206, GG[a:b + 1], {"Content-Range": f"bytes {a}-{b}/{len(GG)}"})
                return self.send(200, GG)
            return self.send(401, b"", {"X-Error-Message": "Invalid username or password."})
    return H


@pytest.fixture()
def hub(monkeypatch):
    h = Hub()
    box = [0]
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(h, box))
    box[0] = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    t.start()
    h.endpoint = f"http://127.0.0.1:{box[0]}"
    hf._INFO.clear()
    monkeypatch.setattr(hf, "_sleep", lambda s: None)
    yield h
    srv.shutdown()
    srv.server_close()


@pytest.fixture()
def token_file(tmp_path):
    p = tmp_path / "token"
    p.write_text(TOKEN + "\n")
    p.chmod(0o400)
    return p


def test_resolve_and_files_and_card(hub):
    assert hf.resolve_revision(REPO, "main", endpoint=hub.endpoint) == SHA
    files = hf.list_files(REPO, SHA, endpoint=hub.endpoint)
    assert files == [{"path": "README.md", "size": 10, "sha256": None},
                     {"path": "Q4_K_M/m.gguf", "size": len(GG), "sha256": LFS}]
    card = hf.model_card(REPO, SHA, endpoint=hub.endpoint)
    assert card["license"] == "other" and card["license_name"] == "x-community"
    assert card["gated"] == "manual" and card["base_model"] == ["base/Model"]
    assert card["gguf"]["architecture"] == "llama" and card["gguf"]["chat_template_bytes"] == 7
    assert card["extra_gated_prompt"] == "agree"
    paths = [p for _h, p, _hd in hub.log]
    assert all("blobs=true" in p for p in paths)
    assert len(paths) == 1      # the revision info is cached per sha


def test_tree_fallback_paginates(hub):
    hub.tree_only = True
    files = hf.list_files(REPO, SHA, endpoint=hub.endpoint)
    assert files == [{"path": "README.md", "size": 10, "sha256": None},
                     {"path": "Q4_K_M/m.gguf", "size": len(GG), "sha256": LFS}]


def test_revision_not_found_and_missing_repo(hub):
    with pytest.raises(hf.HFError) as e:
        hf.resolve_revision(REPO, "nope/branch", endpoint=hub.endpoint)
    assert e.value.code == "RevisionNotFound" and e.value.status == 404
    assert "nope%2Fbranch" in hub.log[-1][1]
    with pytest.raises(hf.HFError, match="not found, private"):
        hf.resolve_revision("other/missing", endpoint=hub.endpoint)


def test_gated_error(hub):
    with pytest.raises(hf.HFError, match="gated") as e:
        hf._Client(hub.endpoint).get(hub.endpoint + "/gated/x")
    assert e.value.code == "GatedRepo"


def test_search(hub):
    res = hf.search("model", author="someorg", limit=5, endpoint=hub.endpoint)
    assert res == [{"id": REPO, "author": "someorg", "downloads": 5, "likes": 1, "gated": False, "license": "mit",
                    "last_modified": "2026-01-01T00:00:00.000Z", "tags": ["license:mit"],
                    "gguf": {"architecture": "llama", "total": 8e9, "context_length": 8192}}]
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(hub.log[-1][1]).query)
    assert q["filter"] == ["gguf"] and q["author"] == ["someorg"] and q["search"] == ["model"]
    assert "gguf" in q["expand[]"] and q["limit"] == ["5"]


def test_retry_on_503(hub):
    hub.fail_next = 2
    assert hf.resolve_revision(REPO, endpoint=hub.endpoint) == SHA
    hub.fail_next = 5
    hf._INFO.clear()
    with pytest.raises(hf.HFError, match="503"):
        hf.resolve_revision(REPO, endpoint=hub.endpoint)


def test_token_only_to_hub_and_never_in_urls(hub, token_file, capsys):
    hf.resolve_revision(REPO, endpoint=hub.endpoint, token_file=token_file)
    read = hf.range_reader(REPO, SHA, "Q4_K_M/m.gguf", endpoint=hub.endpoint, token_file=token_file)
    gguf.parse_header(read)
    for host, path, headers in hub.log:
        assert TOKEN not in path
        auth = {k.lower(): v for k, v in headers.items()}.get("authorization")
        if host == "127.0.0.1":
            assert auth == f"Bearer {TOKEN}"
        else:
            assert host == "localhost" and auth is None, "token leaked to the CDN host"
    assert any(h == "localhost" for h, _p, _hd in hub.log)
    assert TOKEN not in repr(hf._Client(hub.endpoint, token_file))
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err


def test_error_messages_hide_signed_query(hub):
    hub.ignore_range = True
    read = hf.range_reader(REPO, SHA, "Q4_K_M/m.gguf", endpoint=hub.endpoint, initial=1024)
    read(0, 10)                                   # start 0: a 200 is accepted (body capped)
    with pytest.raises(hf.HFError) as e:
        read(5000, 10)                            # a later tail fetch must be a 206
    assert "sig=secret" not in str(e.value)


def test_range_reader_doubles_until_parsed(hub):
    read = hf.range_reader(REPO, SHA, "Q4_K_M/m.gguf", endpoint=hub.endpoint, initial=4096)
    h = gguf.parse_header(read)
    assert len(h.kv["tokenizer.ggml.tokens"]) == 20000 and h.tensors[0].name == "blk.0.attn_q.weight"
    st = read.stats
    assert st["size"] == len(GG) and st["bytes"] < len(GG) + 1
    ranges = [hd.get("Range") for host, _p, hd in hub.log if host == "localhost"]
    starts_ends = [tuple(int(x) for x in r.split("=")[1].split("-")) for r in ranges]
    assert starts_ends[0][0] == 0
    for (a0, b0), (a1, b1) in zip(starts_ends, starts_ends[1:]):
        assert a1 == b0 + 1 and (b1 + 1) >= 2 * (b0 + 1) or b1 + 1 == len(GG)   # tail only, size doubles
    # reads beyond EOF come back short, no extra request
    n = st["requests"]
    assert read(len(GG) - 4, 100) == GG[-4:]
    assert read.stats["requests"] == n


def test_range_reader_caps(hub):
    read = hf.range_reader(REPO, SHA, "Q4_K_M/m.gguf", endpoint=hub.endpoint, initial=1024, max_bytes=8192)
    assert len(read(0, 100000)) == 8192
    with pytest.raises(gguf.GGUFError, match="limit"):
        gguf.parse_header(read, max_bytes=8192)


def test_input_validation(tmp_path):
    for bad in ["noslash", "a/b/c", "../x", "a/..", "", "a b/c"]:
        with pytest.raises(hf.HFError):
            hf.resolve_revision(bad, fetch=lambda *a: None)
    with pytest.raises(hf.HFError):
        hf.list_files(REPO, "main", fetch=lambda *a: None)
    for bad in ["../x.gguf", "/abs.gguf", "a//b.gguf", "a/./b.gguf", "a\\b.gguf", "x\n.gguf"]:
        with pytest.raises(hf.HFError):
            hf.range_reader(REPO, SHA, bad, fetch=lambda *a: None)
    p = tmp_path / "t"
    p.write_text("not a token with spaces\n")
    with pytest.raises(hf.HFError) as e:
        hf.read_token(p)
    assert "spaces" not in str(e.value)
    assert hf.read_token(None) is None


def test_redirect_rules():
    calls = []

    def fetch(url, headers, max_body, timeout):
        calls.append(url)
        if url.startswith("https://huggingface.co/x"):
            return hf.Response(302, {"location": "http://evil.example/downgrade"}, b"")
        if url.startswith("https://huggingface.co/loop"):
            return hf.Response(302, {"location": "/loop"}, b"")
        return hf.Response(302, {"location": "file:///etc/passwd"}, b"")
    c = hf._Client(fetch=fetch)
    with pytest.raises(hf.HFError, match="https -> http"):
        c.get("https://huggingface.co/x")
    with pytest.raises(hf.HFError, match="too many redirects"):
        c.get("https://huggingface.co/loop")
    with pytest.raises(hf.HFError, match="refusing redirect"):
        c.get("https://huggingface.co/file")
