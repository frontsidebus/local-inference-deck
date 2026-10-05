"""HTTP route tests (skipped when starlette/httpx are not installed locally)."""
import time

import pytest

pytest.importorskip("starlette")
pytest.importorskip("httpx")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    import importlib

    import main
    import pipeline

    main = importlib.reload(main)

    async def fake_run_watch(watch, state_dir, progress, run_id=None, curate_fn=None):
        await progress("collecting", watch=watch)
        await progress("done", watch=watch, run_id=run_id)

    monkeypatch.setattr(pipeline, "run_watch", fake_run_watch)
    from starlette.testclient import TestClient

    with TestClient(main.app) as c:
        yield c


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_unknown_watch_404(client):
    assert client.post("/api/runs/nope/now").status_code == 404


def test_post_starts_run_and_get_cannot(client):
    r = client.post("/api/runs/default/now")
    assert r.status_code == 202
    assert len(r.json()["run_id"]) == 16
    time.sleep(0.2)
    assert client.get("/api/runs/default/now").status_code != 202


@pytest.mark.parametrize("bad", ["..%2F..%2Fetc%2Fpasswd", "x", "..", "20261004T000000Z.json"])
def test_run_id_validation(client, bad):
    assert client.get(f"/api/runs/default/{bad}").status_code != 200


@pytest.mark.parametrize("site,want", [("cross-site", 403), ("same-site", 403), ("same-origin", 202), ("none", 202)])
def test_post_refuses_cross_site(client, site, want):
    r = client.post("/api/runs/ai-research/now", headers={"Sec-Fetch-Site": site})
    assert r.status_code == want
    time.sleep(0.2)   # let a started fake run finish before the next case


def test_post_without_fetch_metadata_is_allowed(client):
    # curl on Walter (the CLI trigger) sends no Sec-Fetch-Site
    assert client.post("/api/runs/ai-security/now").status_code == 202


def test_every_asset_referenced_by_the_page_is_served(client):
    """index.html and app.css load /static/... (CSS, JS, fonts); each must return 200 (the first live
    deploy served them only at /, so the page rendered unstyled with no JS)."""
    import re
    from pathlib import Path
    static = Path(__file__).resolve().parent.parent / "build" / "app" / "static"
    refs = set(re.findall(r'(?:href|src)="(/static/[^"]+)"', (static / "index.html").read_text()))
    refs |= set(re.findall(r'url\("(/static/[^"]+)"\)', (static / "app.css").read_text()))
    assert {"/static/app.css", "/static/app.js"} <= refs
    for ref in sorted(refs):
        r = client.get(ref)
        assert r.status_code == 200, ref
        assert r.headers.get("x-content-type-options") == "nosniff", ref
    assert client.get("/").status_code == 200
