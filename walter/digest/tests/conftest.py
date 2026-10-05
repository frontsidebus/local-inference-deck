"""Shared pytest setup for the digest app tests.

Puts build/app (and build/app/collectors) on sys.path so `import pipeline`, `import main` and
`import feedlib` work without installing the package. No network, no real keys: tests use
fakes, fixture feeds (tests/fixtures) and a local HTTP server only.
"""
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "build" / "app"
# collectors/ too: the collectors run as scripts and `import feedlib` from their own directory.
for d in (APP_DIR, APP_DIR / "collectors"):
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))
