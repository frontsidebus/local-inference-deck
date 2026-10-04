"""Shared pytest setup for the digest app tests.

Puts build/app on sys.path so `import pipeline` / `import main` work without
installing the package. No network, no real keys: tests use fakes and a
local HTTP server only.
"""
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "build" / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
