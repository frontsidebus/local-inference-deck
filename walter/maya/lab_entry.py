"""The image's entrypoint: Maya's server (serve/server.py) with only the inference API reachable.

llama-swap passes ANY path to a model through /upstream/<model>/..., so every llama-swap key holder could reach
Maya's admin routes (POST /api/context restarts the engine, /settings changes defaults, /api/update, the web app,
/mcp). Maya guards them only with its API key. This wrapper keeps Maya's own handler and auth and answers 404 to
every route outside ALLOW before Maya sees the request. Maya's source is unchanged: serve() looks up
make_handler when it is called, so replacing it here is enough.
"""
import sys

sys.path.insert(0, "/opt/maya")
import serve.server as S  # noqa: E402

ALLOW = {
    "GET": {"/health", "/v1/models", "/models", "/props", "/slots"},
    "POST": {"/v1/chat/completions", "/v1/messages"},
}


def _path(raw: str) -> str:
    return raw.split("?")[0].rstrip("/")


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

        def do_OPTIONS(self):
            self._json(404, {"error": {"message": "not found"}})

        def do_HEAD(self):
            self._json(404, {"error": {"message": "not found"}})

    return LabHandler


_make_handler = S.make_handler
S.make_handler = make_handler

if __name__ == "__main__":
    sys.argv[0] = "/opt/maya/serve/server.py"
    sys.exit(S.main())
