"""Shared pytest setup for judge/tests.

Tests must never reach the owner's desktop: judge/watch/runaway.py runs ``notify-send`` when DISPLAY is set,
and an end-to-end test drives the real watcher. Removing the display and session-bus variables for every
test (subprocesses inherit them) keeps alerts out of the desktop while a test suite runs.
"""
import pytest


@pytest.fixture(autouse=True)
def _no_desktop_notifications(monkeypatch):
    for var in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS"):
        monkeypatch.delenv(var, raising=False)
