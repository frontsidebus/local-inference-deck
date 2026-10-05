"""Walter digest service.

On-demand digest runner: the three watches (default / ai-security / ai-research)
are triggered via POST /api/runs/{watch}/now, run in the background through
pipeline.run_watch(), and their artifacts (markdown + JSON) are persisted under
STATE_DIR/runs/{watch}/<run_id>.*. Progress is pushed over SSE.

All site values come from the environment (see compose.yaml): BIND, STATE_DIR.
The pipeline itself reads LITELLM_URL / DIGEST_MODEL / LITELLM_KEY_FILE.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

log = logging.getLogger("digest")

BIND = os.environ.get("BIND", "127.0.0.1:3300")
STATE_DIR = Path(os.environ.get("STATE_DIR", "state"))
STATIC = Path(__file__).parent / "static"
HEARTBEAT_S = 15

# slug -> display name. Slugs are the only accepted watch names (no client input
# beyond this fixed set ever reaches the filesystem).
WATCHES: dict[str, str] = {
    "default": "Threat Intel",
    "ai-security": "AI Security",
    "ai-research": "AI Research",
}

# run ids are UTC timestamps shaped 20261004T000101Z; anything else is rejected
RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z$")


def _new_run_id(watch: str) -> str:
    """Timestamp-shaped run id; waits out a same-second collision."""
    while True:
        rid = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        if not (STATE_DIR / "runs" / watch / f"{rid}.json").exists():
            return rid
        time.sleep(1.1)


class Run:
    """One in-flight (or finished, still in memory) run."""

    def __init__(self, watch: str, run_id: str) -> None:
        self.watch = watch
        self.run_id = run_id
        self.status = "collecting"  # collecting | curating | done | error
        self.error: str | None = None
        self.events: list[dict[str, Any]] = []
        self.cond = asyncio.Condition()
        self.task: asyncio.Task | None = None

    async def progress(self, stage: str, **detail: Any) -> None:
        ev = {"stage": stage, "ts": time.time(), **detail}
        async with self.cond:
            self.events.append(ev)
            if stage == "error":
                self.status = "error"
                self.error = str(detail.get("error", "run failed"))
            elif stage in ("collecting", "curating", "done"):
                self.status = stage
            self.cond.notify_all()

    def artifacts(self) -> tuple[Path, Path]:
        d = STATE_DIR / "runs" / self.watch
        return d / f"{self.run_id}.md", d / f"{self.run_id}.json"


RUNS: dict[str, Run] = {}          # run_id -> Run
INFLIGHT: dict[str, Run] = {}      # watch -> Run currently running


async def _execute(run: Run) -> None:
    import pipeline

    try:
        await pipeline.run_watch(run.watch, STATE_DIR, run.progress, run_id=run.run_id)
        if run.status not in ("done", "error"):  # pipeline finished without a terminal stage
            await run.progress("done")
    except Exception as e:  # noqa: BLE001
        log.exception("run %s/%s failed", run.watch, run.run_id)
        try:
            await run.progress("error", error=f"{type(e).__name__}: {e}")
        except Exception:  # noqa: BLE001
            log.exception("failed to record run error")
    finally:
        INFLIGHT.pop(run.watch, None)


def _run_dir(watch: str) -> Path:
    return STATE_DIR / "runs" / watch


# ---------------------------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------------------------
SEC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
}


async def healthz(request) -> Response:
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store", **SEC_HEADERS})


async def watches(request) -> Response:
    out = []
    for slug, name in WATCHES.items():
        latest = None
        for f in sorted(_run_dir(slug).glob("*.json"), reverse=True):
            try:
                d = json.loads(f.read_text())
                latest = {
                    "run_id": d.get("run_id", f.stem),
                    "generated_at": d.get("generated_at"),
                    "items": len(d.get("items", [])),
                }
                break
            except Exception:  # noqa: BLE001
                continue
        out.append(
            {
                "slug": slug,
                "name": name,
                "running": slug in INFLIGHT,
                "latest": latest,
            }
        )
    return JSONResponse({"watches": out}, headers={"Cache-Control": "no-store", **SEC_HEADERS})


def _validate(watch: str, run_id: str | None) -> JSONResponse | None:
    if watch not in WATCHES:
        return JSONResponse({"error": "unknown watch"}, status_code=404, headers=SEC_HEADERS)
    if run_id is not None and not RUN_ID_RE.match(run_id):
        return JSONResponse({"error": "invalid run id"}, status_code=400, headers=SEC_HEADERS)
    return None


# Browsers label every request with Sec-Fetch-Site. Starting a run is the only state-changing
# route: refuse it from another site (CSRF, defence in depth behind the SameSite=Lax session
# cookie at the edge). Clients without the header (curl on Walter, the CLI path) are unaffected.
ALLOWED_FETCH_SITES = {"same-origin", "none"}


async def run_now(request) -> Response:
    watch = request.path_params["watch"]
    if err := _validate(watch, None):
        return err
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in ALLOWED_FETCH_SITES:
        return JSONResponse({"error": "cross-site request refused"}, status_code=403,
                            headers={"Cache-Control": "no-store", **SEC_HEADERS})
    if watch in INFLIGHT:
        return JSONResponse(
            {"error": "a run of this watch is already in progress", "run_id": INFLIGHT[watch].run_id},
            status_code=409,
        )
    run = Run(watch, _new_run_id(watch))
    RUNS[run.run_id] = run
    INFLIGHT[watch] = run
    run.task = asyncio.create_task(_execute(run))
    log.info("started run %s/%s", watch, run.run_id)
    return JSONResponse({"run_id": run.run_id}, status_code=202, headers={"Cache-Control": "no-store", **SEC_HEADERS})


async def run_history(request) -> Response:
    watch = request.path_params["watch"]
    if err := _validate(watch, None):
        return err
    out = []
    d = _run_dir(watch)
    for f in sorted(d.glob("*.json"), reverse=True):  # timestamp ids sort newest-first
        try:
            data = json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            continue
        out.append(
            {
                "run_id": data.get("run_id", f.stem),
                "generated_at": data.get("generated_at"),
                "items": len(data.get("items", [])),
                "stub": bool(data.get("stub", False)),
            }
        )
    return JSONResponse({"runs": out}, headers={"Cache-Control": "no-store", **SEC_HEADERS})


async def run_get(request) -> Response:
    watch = request.path_params["watch"]
    run_id = request.path_params["run_id"]
    if err := _validate(watch, run_id):
        return err
    md, js = _run_dir(watch) / f"{run_id}.md", _run_dir(watch) / f"{run_id}.json"
    if not md.exists() or not js.exists():
        return JSONResponse({"error": "run not found"}, status_code=404)
    return JSONResponse(
        {"run_id": run_id, "watch": watch, "markdown": md.read_text(), "json": json.loads(js.read_text())},
        headers={"Cache-Control": "no-store", **SEC_HEADERS},
    )


async def run_stream(request) -> Response:
    watch = request.path_params["watch"]
    run_id = request.path_params["run_id"]
    if err := _validate(watch, run_id):
        return err
    run = RUNS.get(run_id)
    if run is None:
        # finished long ago: replay from the artifacts if they exist
        md, js = _run_dir(watch) / f"{run_id}.md", _run_dir(watch) / f"{run_id}.json"
        if not js.exists():
            return JSONResponse({"error": "run not found"}, status_code=404)
        run = Run(watch, run_id)
        run.status = "done"
        run.events = [{"stage": "done", "ts": time.time(), "run_id": run_id, "replayed": True}]

    async def gen():
        yield b"retry: 3000\n\n"
        sent = 0
        last_hb = time.monotonic()
        while True:
            async with run.cond:
                while sent < len(run.events) and run.status not in ("done", "error"):
                    ev = run.events[sent]
                    sent += 1
                    yield b"event: " + ev["stage"].encode() + b"\ndata: " + json.dumps(ev).encode() + b"\n\n"
                    last_hb = time.monotonic()
                if run.status in ("done", "error"):
                    while sent < len(run.events):
                        ev = run.events[sent]
                        sent += 1
                        yield b"event: " + ev["stage"].encode() + b"\ndata: " + json.dumps(ev).encode() + b"\n\n"
                    return
                try:
                    await asyncio.wait_for(run.cond.wait(), timeout=max(0.1, HEARTBEAT_S - (time.monotonic() - last_hb)))
                except asyncio.TimeoutError:
                    pass
            if time.monotonic() - last_hb >= HEARTBEAT_S:
                last_hb = time.monotonic()
                yield b": hb\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "X-Content-Type-Options": "nosniff",
        },
    )


class StaticWithHeaders(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers.update(SEC_HEADERS)
        if path.startswith("fonts/"):
            resp.headers["Cache-Control"] = "public, max-age=604800, immutable"
        else:
            resp.headers["Cache-Control"] = "no-cache"
        return resp


@asynccontextmanager
async def lifespan(app):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    yield


app = Starlette(
    routes=[
        Route("/healthz", healthz),
        Route("/api/watches", watches),
        Route("/api/runs/{watch}", run_history),
        Route("/api/runs/{watch}/now", run_now, methods=["POST"]),
        Route("/api/runs/{watch}/{run_id}", run_get),
        Route("/api/runs/{watch}/{run_id}/stream", run_stream),
        # index.html and app.css reference assets as /static/...; "/" serves index.html for the SPA.
        Mount("/static", StaticWithHeaders(directory=STATIC), name="assets"),
        Mount("/", StaticWithHeaders(directory=STATIC, html=True), name="static"),
    ],
    lifespan=lifespan,
)


def make_socket(addr: str) -> socket.socket:
    host, port = addr.rsplit(":", 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # IP_FREEBIND: bind the WireGuard IP even if wg0 isn't up yet at boot
    s.setsockopt(socket.IPPROTO_IP, getattr(socket, "IP_FREEBIND", 15), 1)
    s.bind((host, int(port)))
    s.set_inheritable(True)
    return s


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    socks = [make_socket(a.strip()) for a in BIND.split(",") if a.strip()]
    config = uvicorn.Config(
        app,
        log_level="warning",
        access_log=False,
        proxy_headers=False,
        server_header=False,
        timeout_graceful_shutdown=3,
    )
    server = uvicorn.Server(config)
    asyncio.run(server.serve(sockets=socks))


if __name__ == "__main__":
    main()
