"""A fake Hugging Face Hub for the spark-model pull tests (http.server on 127.0.0.1; "localhost" is the CDN).

Serves /api/models/<repo>/revision/<rev>?blobs=true and /<repo>/resolve/<sha>/<path> (HTTP Range, optional
redirect to the "CDN" host, optional dropped connection after N bytes, optional ignored Range). Records every
request (host, path, headers) so tests can check where the token went. No network.
"""
import hashlib
import json
import re
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "models" / "lib"))
import gguf  # noqa: E402

SHA = "c0ffee" + "0" * 34
TOKEN = "fake-token." + "y" * 12        # built at runtime: no secret-shaped literal
TEMPLATE = "{% for m in messages %}<|{{ m.role }}|>{{ m.content }}{% endfor %}<|assistant|>"


def model_gguf(arch="llama", template=TEMPLATE, split=None, n_tensors=2, ctx=8192, extra_kv=None):
    kv = {"general.architecture": arch, f"{arch}.context_length": ctx, f"{arch}.block_count": 2}
    if template is not None:
        kv["tokenizer.chat_template"] = template
    if split is not None:
        kv["split.no"], kv["split.count"] = split
    kv.update(extra_kv or {})
    tensors = [(f"blk.{i}.attn_q.weight", (64, 64), "F32") for i in range(n_tensors)]
    return gguf.build_test_gguf(kv, tensors, with_data=True)


class FakeHub:
    def __init__(self):
        self.repos = {}            # repo -> {"files": {path: bytes}, "card": {...}, "gated": ..., "nolfs": set()}
        self.log = []              # (host, path, headers dict)
        self.drop_after = {}       # path -> drop the connection after this many body bytes (once)
        self.ignore_range = False
        self.redirect = True       # resolve/ answers 302 to the CDN host
        self.listed_sha = {}       # (repo, path) -> sha256 to report instead of the real one
        self.server = None

    def add(self, repo, files: dict, license="apache-2.0", gated=False, nolfs=()):
        self.repos[repo] = {"files": dict(files), "card": {"license": license} if license else {},
                            "gated": gated, "nolfs": set(nolfs)}

    def info(self, repo):
        r = self.repos[repo]
        sib = []
        for p, data in r["files"].items():
            s = {"rfilename": p, "size": len(data)}
            if p not in r["nolfs"]:
                s["lfs"] = {"sha256": self.listed_sha.get((repo, p), hashlib.sha256(data).hexdigest()),
                            "size": len(data)}
            sib.append(s)
        return {"id": repo, "sha": SHA, "gated": r["gated"], "private": False, "siblings": sib,
                "cardData": r["card"], "tags": ["gguf"]}

    @property
    def endpoint(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self):
        hub = self

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

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                host = (self.headers.get("Host") or "").split(":")[0]
                hub.log.append((host, u.path, dict(self.headers)))
                m = re.match(r"^/api/models/([^/]+/[^/]+)/revision/([^/]+)$", u.path)
                if m:
                    if m.group(1) not in hub.repos:
                        return self.send(404, b"{}", {"X-Error-Code": "RepoNotFound"})
                    return self.send(200, json.dumps(hub.info(m.group(1))).encode(),
                                     {"Content-Type": "application/json"})
                m = re.match(r"^/(cdn/)?([^/]+/[^/]+)/resolve/([0-9a-f]{40})/(.+)$", u.path)
                if m:
                    repo, path = m.group(2), urllib.parse.unquote(m.group(4))
                    data = hub.repos.get(repo, {}).get("files", {}).get(path)
                    if data is None:
                        return self.send(404, b"", {"X-Error-Code": "EntryNotFound"})
                    if hub.redirect and not m.group(1):
                        port = hub.server.server_address[1]
                        return self.send(302, b"", {"Location": f"http://localhost:{port}/cdn/{repo}/resolve/{m.group(3)}/{m.group(4)}"})
                    return self.serve(path, data)
                return self.send(404, b"")

            def serve(self, path, data):
                rng = self.headers.get("Range")
                start, end, status = 0, len(data) - 1, 200
                if rng and not hub.ignore_range:
                    mm = re.match(r"bytes=(\d+)-(\d*)$", rng)
                    start = int(mm.group(1))
                    if mm.group(2):
                        end = min(int(mm.group(2)), len(data) - 1)
                    if start >= len(data):
                        return self.send(416, b"", {"Content-Range": f"bytes */{len(data)}"})
                    status = 206
                body = data[start:end + 1]
                self.send_response(status)
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                cut = hub.drop_after.pop(path, None)
                if cut is not None and cut < len(body):
                    self.wfile.write(body[:cut])
                    self.wfile.flush()
                    self.close_connection = True
                    return
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def downloads(self, path_part=""):
        return [(h, p, hd) for h, p, hd in self.log if "/resolve/" in p and path_part in p]
