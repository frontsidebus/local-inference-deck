"""Shared pytest setup for the telemetry app tests.

build/app/main.py is a template (main.py.tmpl): its ${SITE_VARS} sit only inside string literals, so
the unrendered file is valid Python. `load_main()` imports it as a fresh module, with the given
environment, without starting the collector loop. No network, no Prometheus, no sysfs.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "build" / "app"
MAIN_TMPL = APP_DIR / "main.py.tmpl"


@pytest.fixture
def load_main(monkeypatch):
    pytest.importorskip("starlette")
    pytest.importorskip("httpx")
    pytest.importorskip("uvicorn")

    def _load(**env):
        monkeypatch.delenv("TELEMETRY_GPU_EXPECTED_WIDTH", raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        spec = importlib.util.spec_from_loader("telemetry_main", loader=None)
        mod = importlib.util.module_from_spec(spec)
        mod.__file__ = str(APP_DIR / "main.py")
        sys.modules["telemetry_main"] = mod
        exec(compile(MAIN_TMPL.read_text(), str(MAIN_TMPL), "exec"), mod.__dict__)
        return mod

    yield _load
    sys.modules.pop("telemetry_main", None)
