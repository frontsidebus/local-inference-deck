"""The image's entrypoint: Maya's server (serve/server.py) with only the inference API reachable.

llama-swap passes ANY path to a model through /upstream/<model>/..., so every llama-swap key holder could reach
Maya's admin routes (POST /api/context restarts the engine, /settings changes defaults, /api/update, the web app,
/mcp). Maya guards them only with its API key. This wrapper keeps Maya's own handler and auth and answers 404 to
every route outside ALLOW before Maya sees the request, and to every method but GET and POST (v1.1.21 added DELETE
/v1/responses/<id>). Maya's source is unchanged: serve() looks up make_handler when it is called, so replacing it
here is enough.

Output cap: Maya has none per request (an absent, 0 or -1 max_tokens means "the rest of the context"), and maya-ds4
ignores the thinking budget. If the run config has "lab_max_tokens", every chat and messages request is clamped to
it: a missing, non-positive or larger max_tokens / max_completion_tokens becomes that cap.
"""
import json
import sys

sys.path.insert(0, "/opt/maya")
import serve.server as S  # noqa: E402

ALLOW = {
    "GET": {"/health", "/v1/models", "/models", "/props", "/slots"},
    "POST": {"/v1/chat/completions", "/v1/messages"},
}


def _path(raw: str) -> str:
    return raw.split("?")[0].rstrip("/")


def _config_cap(argv) -> int:
    """lab_max_tokens from the --config run config, 0 for none."""
    path = next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--config"), None)
    if not path:
        return 0
    with open(path, encoding="utf-8-sig") as f:
        cap = json.load(f).get("lab_max_tokens") or 0
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:
        raise SystemExit(f"lab_entry: lab_max_tokens must be a positive integer, not {cap!r}")
    return cap


def clamp_body(body: bytes, path: str, cap: int) -> bytes:
    """The request body with its output length clamped to cap (unchanged if cap is 0 or it is not a JSON object;
    Maya then answers the malformed body itself)."""
    if not cap or path not in ALLOW["POST"]:
        return body
    try:
        req = json.loads(body or b"{}")
    except ValueError:
        return body
    if not isinstance(req, dict):
        return body
    want = req.get("max_completion_tokens") or req.get("max_tokens")
    if isinstance(want, int) and not isinstance(want, bool) and 0 < want <= cap:
        return body
    req.pop("max_completion_tokens", None)
    req["max_tokens"] = cap
    return json.dumps(req).encode()


CAP = 0


def make_handler(svc):
    base = _make_handler(svc)

    class LabHandler(base):
        def _lab_allowed(self, method: str) -> bool:
            if _path(self.path) in ALLOW.get(method, ()):
                return True
            self._json(404, {"error": {"message": "not found"}})
            return False

        def do_GET(self):
            if self._lab_allowed("GET"):
                super().do_GET()

        def do_POST(self):
            if self._lab_allowed("POST"):
                super().do_POST()

        def _body(self) -> bytes:
            return clamp_body(super()._body(), _path(self.path), CAP)

        def _lab_refuse(self):
            self._json(404, {"error": {"message": "not found"}})

        do_OPTIONS = do_HEAD = do_DELETE = do_PUT = do_PATCH = do_TRACE = do_CONNECT = _lab_refuse

    return LabHandler


_make_handler = S.make_handler
S.make_handler = make_handler

if __name__ == "__main__":
    CAP = _config_cap(sys.argv)
    sys.argv[0] = "/opt/maya/serve/server.py"
    sys.exit(S.main())
