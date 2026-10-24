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


@pytest.fixture(autouse=True)
def _no_live_unit_queries(monkeypatch):
    """bin/judge-findings asks `systemctl --user is-failed` about the live judge units: keep tests independent of
    the machine's unit state (the check is exercised with a stub systemctl where it is tested)."""
    monkeypatch.setenv("JUDGE_CHECK_UNITS", "0")


@pytest.fixture(autouse=True)
def _no_operator_config(monkeypatch, tmp_path_factory):
    """Step 0a: lib/config reads ~/.config/judge/judge.env ($JUDGE_ENV) and falls back to ~/.config/deck/site.env.
    Tests never read the operator's files: JUDGE_ENV points at a file that does not exist, and HOME at an empty
    temp dir (a test that needs its own HOME, judge.env or site env sets it)."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("JUDGE_ENV", str(home / "no-judge.env"))
