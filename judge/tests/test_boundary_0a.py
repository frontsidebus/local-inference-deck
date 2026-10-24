"""Step 0a: fail-closed boundaries (judge.env loader, no checkout-location fallbacks, frontier data boundary).

Everything runs under tmp_path with example values: a temp HOME, no ~/.config/judge/judge.env, no
~/.config/deck/site.env unless a test creates one. No network; the frontier CLI is a stub that records calls.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from collector_testlib import JUDGE_DIR

from lib import config



@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    for k in ("SITE_ENV", "JUDGE_ENV", "JUDGE_REPO_DIR", "JUDGE_MODE", "JUDGE_SSH_ALIASES", "JUDGE_INFRA_REPOS",
              "HERMES_HOME", "JUDGE_REVIEW_DIR", "JUDGE_FRONTIER_DAILY_MAX", "SPARK_DOMAIN"):
        monkeypatch.delenv(k, raising=False)
    return h


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


# ------------------------------------------------------------------ precedence
def test_precedence_environment_judge_env_site_env_defaults(home, tmp_path, monkeypatch):
    site = _write(tmp_path / "site.env", "JUDGE_MODE=local\nJUDGE_STALL_MINUTES=7\nJUDGE_LOCAL_MODEL=site-model\n"
                                         "SPARK_DOMAIN=example.com\n")
    je = _write(home / ".config" / "judge" / "judge.env",
                f"SITE_ENV={site}\nJUDGE_STALL_MINUTES=9\nJUDGE_LOCAL_MODEL=\n")
    cfg = config.load_config()
    assert cfg["SITE_ENV"] == str(site) and cfg["JUDGE_ENV"] == str(je)
    assert cfg["JUDGE_MODE"] == "local"                    # site.env > DEFAULTS
    assert cfg["JUDGE_STALL_MINUTES"] == "9"               # judge.env > site.env
    assert cfg["JUDGE_LOCAL_MODEL"] == "site-model"        # an empty judge.env value does not override
    assert cfg["JUDGE_RUNAWAY_TOKENS"] == config.DEFAULTS["JUDGE_RUNAWAY_TOKENS"]
    monkeypatch.setenv("JUDGE_STALL_MINUTES", "11")
    monkeypatch.setenv("JUDGE_MODE", "frontier")
    cfg = config.load_config()
    assert cfg["JUDGE_STALL_MINUTES"] == "11" and cfg["JUDGE_MODE"] == "frontier"   # environment > judge.env
    # a key only judge.env knows is overridable from the environment too (as in the units)
    _write(je, f"SITE_ENV={site}\nJUDGE_REFUSAL_NEXT_CALLS=4\n")
    assert config.load_config()["JUDGE_REFUSAL_NEXT_CALLS"] == "4"
    monkeypatch.setenv("JUDGE_REFUSAL_NEXT_CALLS", "5")
    assert config.load_config()["JUDGE_REFUSAL_NEXT_CALLS"] == "5"


def test_site_env_lookup_order(home, tmp_path, monkeypatch):
    deck = _write(home / ".config" / "deck" / "site.env", "SPARK_DOMAIN=deck.example.com\n")
    assert config.site_env_path() == deck                  # the last fallback
    other = _write(tmp_path / "other.env", "SPARK_DOMAIN=other.example.com\n")
    je = _write(home / ".config" / "judge" / "judge.env", f"SITE_ENV={other}\n")
    assert config.site_env_path() == other                 # judge.env > ~/.config/deck/site.env
    env_site = _write(tmp_path / "env.env", "SPARK_DOMAIN=env.example.com\n")
    monkeypatch.setenv("SITE_ENV", str(env_site))
    assert config.site_env_path() == env_site              # environment > judge.env
    assert config.load_config()["SPARK_DOMAIN"] == "env.example.com"
    # an explicit SITE_ENV that is missing is unresolved: no fallback to judge.env or the deck file
    monkeypatch.setenv("SITE_ENV", str(tmp_path / "missing.env"))
    assert config.site_env_path() is None and config.load_config()["SITE_ENV"] == ""
    monkeypatch.delenv("SITE_ENV")
    _write(je, f"SITE_ENV={tmp_path / 'missing.env'}\n")
    assert config.site_env_path() is None
    # relative SITE_ENV in judge.env: relative to judge.env's directory; ~ uses HOME
    _write(home / ".config" / "judge" / "rel.env", "SPARK_DOMAIN=rel.example.com\n")
    _write(je, "SITE_ENV=rel.env\n")
    assert config.site_env_path() == home / ".config" / "judge" / "rel.env"
    _write(je, "SITE_ENV=~/.config/deck/site.env\n")
    assert config.site_env_path() == deck


def test_judge_env_override_path(home, tmp_path, monkeypatch):
    _write(home / ".config" / "judge" / "judge.env", "JUDGE_STALL_MINUTES=1\n")
    alt = _write(tmp_path / "alt" / "judge.env", "JUDGE_STALL_MINUTES=2\n")
    monkeypatch.setenv("JUDGE_ENV", str(alt))
    assert config.load_config()["JUDGE_STALL_MINUTES"] == "2"
    monkeypatch.setenv("JUDGE_ENV", str(tmp_path / "absent.env"))   # an absent $JUDGE_ENV: no judge.env at all
    cfg = config.load_config()
    assert cfg["JUDGE_STALL_MINUTES"] == config.DEFAULTS["JUDGE_STALL_MINUTES"] and cfg["JUDGE_ENV"] == ""


def test_no_repo_root_fallback_for_site_env(home):
    """The checkout's own directory is never searched: no site env resolves without judge.env/SITE_ENV/deck file."""
    assert not hasattr(config, "REPO_ROOT")
    assert config.site_env_path() is None
    cfg = config.load_config()
    assert cfg["SITE_ENV"] == "" and cfg["JUDGE_REPO_DIR"] == ""


# ------------------------------------------------------------------ empty JUDGE_REPO_DIR stays empty
def test_empty_judge_repo_dir_stays_empty(home, tmp_path, monkeypatch):
    site = _write(tmp_path / "site.env", "SPARK_DOMAIN=example.com\nJUDGE_REPO_DIR=\nJUDGE_INFRA_REPOS=\n")
    monkeypatch.setenv("SITE_ENV", str(site))
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    _write(work / "walter" / "deploy.sh", "echo hi\n")
    monkeypatch.chdir(work)
    cfg = config.load_config()
    assert cfg["JUDGE_REPO_DIR"] == ""                     # never abspath("") (= the cwd)
    rules = config.infra_rules(cfg)
    assert not any(r.startswith(str(work)) for r in rules)
    assert config.infra_git_dirs(cfg) == set()
    assert config.classify([str(work / "walter" / "deploy.sh")], cfg) == "sensitive"
    assert config.classify(["walter/deploy.sh"], cfg, cwd=str(work)) == "sensitive"
    assert config.classify([], cfg, cwd=str(work)) == "sensitive"
    monkeypatch.setenv("JUDGE_REPO_DIR", "")             # an empty environment value does not change that
    assert config.load_config()["JUDGE_REPO_DIR"] == ""
    monkeypatch.setenv("JUDGE_REPO_DIR", "  ")
    assert config.load_config()["JUDGE_REPO_DIR"] == ""
    monkeypatch.setenv("JUDGE_REPO_DIR", str(work))       # a configured repo still works
    assert config.classify([str(work / "walter" / "deploy.sh")]) == "infra"


# ------------------------------------------------------------------ frontier_refusal
def test_frontier_refusal(home, tmp_path, monkeypatch):
    assert "site env unresolved" in config.frontier_refusal(config.load_config())
    generic = _write(tmp_path / "generic.env", "EDGE_SSH_USER=ubuntu\nJUDGE_MODE=frontier\n")
    monkeypatch.setenv("SITE_ENV", str(generic))
    assert "site mask is empty" in config.frontier_refusal(config.load_config())
    _write(generic, "SPARK_DOMAIN=example.com\n")
    assert config.frontier_refusal(config.load_config()) == ""


# ------------------------------------------------------------------ relocated checkout, empty environment
FAKE_CLAUDE = """#!{py}
import json, os, sys
rec = {rec!r}
n = len(os.listdir(rec)) + 1
open(os.path.join(rec, "call-%d.json" % n), "w").write(json.dumps({{"stdin": sys.stdin.read()}}))
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": '{{"items": []}}'}}))
"""


@pytest.fixture
def relocated(tmp_path):
    """A copy of judge/ at <tmp>/checkout/agent-judge/judge, with what the old fallbacks would have picked up next
    to it: <checkout>/site.env (example values) and a sibling <checkout>/walter/deploy.sh. HOME is empty (no
    judge.env, no ~/.config/deck/site.env), the environment holds only PATH and HOME, and `claude` on PATH is a
    recording stub."""
    root = tmp_path / "checkout" / "agent-judge"
    judge = root / "judge"
    shutil.copytree(JUDGE_DIR, judge, ignore=shutil.ignore_patterns("__pycache__", "tests", ".pytest_cache"))
    _write(root / "site.env", "SPARK_DOMAIN=example.com\nSPARK_API_HOST=api.example.com\nJUDGE_MODE=frontier\n")
    sibling = _write(root / "walter" / "deploy.sh", "echo deploy\n")
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    (home / ".hermes" / "config.yaml").write_text("model: example\n")
    rec = tmp_path / "frontier-calls"
    rec.mkdir()
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "claude").write_text(FAKE_CLAUDE.format(py=sys.executable, rec=str(rec)))
    (stub / "claude").chmod(0o755)
    env = {"PATH": f"{stub}:/usr/bin:/bin", "HOME": str(home)}

    class R:
        pass
    r = R()
    r.root, r.judge, r.sibling, r.home, r.rec, r.env = root, judge, sibling, home, rec, env
    r.review = home / ".hermes" / "review"

    def run(rel, *args, stdin="", cwd=None):
        return subprocess.run([sys.executable, str(judge / rel), *args], input=stdin, capture_output=True,
                              text=True, env=env, cwd=str(cwd or root), timeout=60)
    r.run = run
    r.calls = lambda: sorted(rec.iterdir())
    return r


def _payload(event, cwd, **kw):
    p = {"hook_event_name": event, "tool_name": None, "tool_input": None, "session_id": "20261003_031000_a1b2c3",
         "cwd": str(cwd), "profile": "default", "extra": {}}
    p.update(kw)
    return json.dumps(p)


def _requests(review: Path):
    out = []
    for sub in ("queue", "queue/deferred", "done"):
        d = review / sub
        if d.is_dir():
            out += [json.loads(p.read_text()) for p in d.glob("*.json")]
    return out


def test_relocated_config_resolves_nothing(relocated):
    p = relocated.run("lib/config.py", "--site-env-path")
    assert p.returncode == 0 and p.stdout.strip() == ""
    assert relocated.run("lib/config.py", "--get", "JUDGE_REPO_DIR").stdout.strip() == ""
    assert relocated.run("lib/config.py", "--classify", str(relocated.sibling)).stdout.strip() == "sensitive"
    explain = json.loads(relocated.run("lib/config.py", "--explain", cwd=relocated.root).stdout)
    assert explain["class"] == "sensitive"                 # the checkout itself (the cwd) is not infra


def test_relocated_hooks_send_nothing_and_classify_nothing_infra(relocated):
    r = relocated
    # enqueue: session start (a clear message in hook-errors.log), then edits next to the checkout, session end
    p = r.run("hooks/enqueue.py", stdin=_payload("on_session_start", r.root))
    assert p.returncode == 0 and p.stdout.strip() == "{}"
    log = (r.review / "hook-errors.log").read_text()
    assert "frontier judge refused for this session" in log and "site env unresolved" in log
    assert "~/.config/deck/site.env" in log
    for tool, ti in (("write_file", {"path": str(r.sibling), "content": "x"}),
                     ("patch", {"path": "walter/deploy.sh", "old_string": "a", "new_string": "b"}),
                     ("terminal", {"command": f"sed -i s/a/b/ {r.sibling}"})):
        p = r.run("hooks/enqueue.py", stdin=_payload("post_tool_call", r.root, tool_name=tool, tool_input=ti,
                                                      extra={"status": "ok"}))
        assert p.returncode == 0 and p.stdout.strip() == "{}"
    p = r.run("hooks/enqueue.py", stdin=_payload("on_session_end", r.root, extra={"final_response": "Done."}))
    assert p.returncode == 0
    # gate: a write and a command on the sibling dir; must answer with valid JSON and never call anything
    for tool, ti in (("write_file", {"path": str(r.sibling), "content": "echo x\n"}),
                     ("terminal", {"command": f"bash {r.sibling}"}),
                     ("read_file", {"path": str(r.root / "site.env")})):
        p = r.run("hooks/gate.py", stdin=_payload("pre_tool_call", r.root, tool_name=tool, tool_input=ti))
        assert p.returncode in (0, 2), p.stderr
        json.loads(p.stdout or "{}")
    # verify and inject: exit cleanly
    p = r.run("hooks/verify.py", stdin=_payload("pre_verify", r.root, extra={"paths": [str(r.sibling)]}))
    assert p.returncode == 0, p.stderr
    json.loads(p.stdout or "{}")
    p = r.run("hooks/inject.py", stdin=_payload("pre_llm_call", r.root))
    assert p.returncode == 0, p.stderr
    json.loads(p.stdout or "{}")
    assert [q["data_class"] for q in _requests(r.review) if q.get("data_class") == "infra"] == []
    assert r.calls() == []


def test_relocated_runner_refuses_frontier(relocated):
    """Even a request labelled infra (an older hook, a forged request) is never sent without a site env."""
    r = relocated
    rid = "20261003T035210Z-a1b2c3-completion"
    req = {"id": rid, "kind": "completion", "session": "20261003_031000_a1b2c3", "created": "2026-10-03T03:52:10Z",
           "since": "2026-10-03T03:20:00Z", "changed_paths": [str(r.sibling)], "claims": "deploy.sh updated",
           "plan": None, "data_class": "infra", "source_event": "on_session_end", "detail": {}}
    for d in ("queue", "evidence", "findings", "acks", "done"):
        (r.review / d).mkdir(parents=True, exist_ok=True)
    (r.review / "queue" / f"{rid}.json").write_text(json.dumps(req))
    ev = r.review / "evidence" / rid
    ev.mkdir()
    (ev / "manifest.json").write_text(json.dumps({"request": req, "data_class": "infra", "artifacts": []}))
    (ev / "hermes-log.txt").write_text("2026-10-03T03:30:00Z tool_call write_file walter/deploy.sh\n")
    p = r.run("runner/run_judge.py", rid)
    assert r.calls() == []                                  # nothing reached the frontier CLI
    log = (r.review / "runner.log").read_text()
    assert "frontier refused (nothing sent): site env unresolved" in log
    # the local judge cannot run either (no SPARK_API_HOST: no site env): the request stays queued for later
    assert p.returncode == 1 and (r.review / "queue" / f"{rid}.json").exists()
    # with JUDGE_MAX_ATTEMPTS=1 the placeholder finding records why, and still nothing is sent
    r.env["JUDGE_MAX_ATTEMPTS"] = "1"
    p = r.run("runner/run_judge.py", rid)
    f = json.loads((r.review / "findings" / f"{rid}.json").read_text())
    assert f["mode"] == "local"
    assert any("frontier refused (nothing sent): site env unresolved" in n for n in f.get("notes", []))
    assert r.calls() == []


def test_relocated_rejudge_refuses_explicit_frontier(relocated, tmp_path):
    r = relocated
    rid = "20261003T035210Z-a1b2c3-completion"
    req = {"id": rid, "kind": "completion", "session": "20261003_031000_a1b2c3", "created": "2026-10-03T03:52:10Z",
           "changed_paths": [str(r.sibling)], "claims": "Done. deploy.sh updated and verified.", "plan": None,
           "data_class": "infra", "detail": {}}
    for d in ("queue", "evidence", "findings", "acks", "done"):
        (r.review / d).mkdir(parents=True, exist_ok=True)
    (r.review / "done" / f"{rid}.json").write_text(json.dumps(req))
    ev = r.review / "evidence" / rid
    ev.mkdir()
    (ev / "manifest.json").write_text(json.dumps({"request": req, "data_class": "infra", "artifacts": []}))
    for mode in ("frontier", "frontier-claims"):
        out = tmp_path / f"out-{mode}"
        p = r.run("runner/rejudge.py", rid, "--mode", mode, "--out", str(out), "--no-budget")
        assert p.returncode == 1
        row = json.loads((out / "summary.json").read_text())["requests"][0]
        assert row.get("refused") and "site env unresolved" in row["error"] and "nothing sent" in row["error"]
    assert r.calls() == []


# ------------------------------------------------------------------ in-process guards (defence in depth)
def test_empty_mask_is_never_a_pass_through(home, monkeypatch):
    sys.path.insert(0, str(JUDGE_DIR / "runner"))
    import run_judge as RJ
    from lib import sitemask as SM
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    with pytest.raises(RJ.JudgeError, match="nothing sent"):
        RJ.frontier_messages(msgs, SM.SiteMask([]))
    called = []
    monkeypatch.setattr(RJ.subprocess, "run", lambda *a, **k: called.append(a))
    with pytest.raises(RJ.JudgeError, match="refused"):
        RJ.call_frontier(msgs)
    assert called == []
    assert RJ.choose_mode("infra")[0] == "local"
    assert RJ.claims_stage_enabled("sensitive", {"kind": "completion", "claims": "Done."}).startswith(
        "frontier refused")
