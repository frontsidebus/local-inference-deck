"""Shared test helpers for the collector-side modules (config, queue, enqueue, collector, probes).

Everything lives under tmp_path: HOME, HERMES_HOME, JUDGE_REVIEW_DIR, SITE_ENV, JUDGE_REPO_DIR. No network,
no real ssh: tests pass a FakeRunner to anything that would run a command.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

JUDGE_DIR = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
for p in (str(JUDGE_DIR), str(JUDGE_DIR / "probes"), str(JUDGE_DIR / "collector"), str(JUDGE_DIR / "hooks")):
    if p not in sys.path:
        sys.path.insert(0, p)

SITE_ENV_TEXT = """# synthetic site.env for tests
SPARK_DOMAIN=example.com
SPARK_API_HOST=api.example.com
EDGE_PUBLIC_IP=203.0.113.10
BACKEND_WG_IP=10.100.0.2
BACKEND_LAN_IP=192.168.122.10     # comment after value
BACKEND_SSH_USER=operator
JUDGE_SSH_ALIASES="edge-alias other-alias"
SPARK_USERS="alice bob"
export QUOTED='single # not a comment'
"""


def make_env(tmp_path: Path, monkeypatch, site_text: str = SITE_ENV_TEXT) -> dict:
    home = tmp_path / "home"
    hermes = home / ".hermes"
    review = tmp_path / "review"
    repo = tmp_path / "repo"
    for d in (home / ".ssh", hermes / "logs", hermes / "skills" / "demo", hermes / "memories", repo):
        d.mkdir(parents=True, exist_ok=True)
    (home / ".ssh" / "config").write_text("Host edge-alias\n  User ubuntu\n\nHost lab-*\n  User x\n")
    (hermes / "config.yaml").write_text("model: coder\napprovals:\n  mode: manual\n")
    (hermes / "skills" / "demo" / "SKILL.md").write_text("# demo skill\nstep one\n")
    (hermes / "memories" / "MEMORY.md").write_text("fact one\n")
    site = tmp_path / "site.env"
    site.write_text(site_text)
    for k in ("JUDGE_MODE", "EDGE_SSH_KEY", "EDGE_SSH_USER", "JUDGE_SSH_ALIASES", "JUDGE_INFRA_REPOS",
              "JUDGE_ENQUEUE_ALWAYS", "BACKEND_LAN_IP", "EDGE_PUBLIC_IP", "SPARK_DOMAIN",
              "JUDGE_MIXED_MAX_SENSITIVE", "JUDGE_SCRATCH_GLOBS", "JUDGE_SECRET_GLOBS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    monkeypatch.setenv("JUDGE_REVIEW_DIR", str(review))
    monkeypatch.setenv("SITE_ENV", str(site))
    monkeypatch.setenv("JUDGE_REPO_DIR", str(repo))
    monkeypatch.setenv("JUDGE_LOG_TZ", "-05:00")
    return {"home": home, "hermes": hermes, "review": review, "repo": repo, "site": site}


def install_logs(hermes: Path) -> None:
    for n in ("agent.log", "errors.log"):
        shutil.copyfile(FIXTURES / n, hermes / "logs" / n)


class FakeRunner:
    """Records argv; answers from a list of (regex over the joined argv, (rc, out, err)) rules."""

    def __init__(self, rules=None, default=(0, "", "")):
        self.rules = list(rules or [])
        self.default = default
        self.calls = []

    def __call__(self, argv, timeout, cwd=None):
        self.calls.append({"argv": list(argv), "timeout": timeout, "cwd": cwd})
        joined = " ".join(argv)
        for rx, res in self.rules:
            if re.search(rx, joined):
                return res(argv) if callable(res) else res
        return self.default
