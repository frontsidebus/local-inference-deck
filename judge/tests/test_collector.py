import json
import os
import subprocess
from datetime import datetime, timezone

import pytest

from collector_testlib import FIXTURES, FakeRunner, install_logs, make_env

import collect
from lib import queue as q
from lib import snapshot
from lib.redact import redact
from lib import config

SESSION = "20261003_031000_a1b2c3"
NOW = datetime(2026, 10, 3, 3, 30, 0, tzinfo=timezone.utc)
SLOTS = (FIXTURES / "slots-walter.json").read_text()


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = make_env(tmp_path, monkeypatch)
    install_logs(e["hermes"])
    return e


def runner():
    return FakeRunner([
        (r"operator@192\.168\.122\.10 .*find /etc /srv /usr/local",
         (0, "# 1 path(s)\nroot 644 2026-10-03T03:25:00.0000000000 /etc/llama-swap/config.yaml\n\n# systemctl --failed\n", "")),
        (r"edge-alias .*find", (255, "", "ssh: connect to host edge port 22: Connection timed out")),
        (r"docker ps", (0, "llama-coder\t127.0.0.1:5801->8080/tcp\n", "")),
        (r"/slots", (0, f"=== 5801\n{SLOTS}\n", "")),
    ])


def _request(env, data_class="infra", paths=None):
    paths = paths if paths is not None else [str(env["hermes"] / "config.yaml")]
    r = q.make_request("completion", SESSION, "2026-10-03T03:20:00Z", source_event="on_session_end",
                       changed_paths=paths, claims="All done.", data_class=data_class,
                       created="2026-10-03T03:30:00Z")
    q.write_request(r)
    return r


IN_WINDOW = datetime(2026, 10, 3, 3, 25, 0, tzinfo=timezone.utc).timestamp()


def _in_window(*paths):
    """Tests use fixed request times; give real files an mtime inside the request window."""
    for p in paths:
        os.utime(p, (IN_WINDOW, IN_WINDOW))


EV_T = datetime(2026, 10, 3, 3, 25, 0, tzinfo=timezone.utc)


def _agent_wrote(*paths):
    """The post_tool_call hook saw the agent patch these paths (request paths need a backing event)."""
    snapshot.record_event(q.snapshot_dir(SESSION), "patch", [str(p) for p in paths], "ok", now=EV_T)


def _snapshot_and_change(env):
    snapshot.take(SESSION, None, config.load_config())
    (env["hermes"] / "config.yaml").write_text("model: coder\napprovals:\n  mode: off\napi_key: s3cr3tvalue99\n")
    _agent_wrote(env["hermes"] / "config.yaml")


def test_bundle_infra(env):
    _snapshot_and_change(env)
    r = _request(env)
    ev = collect.collect(r["id"], runner=runner(), now=NOW)
    man = json.loads((ev / "manifest.json").read_text())
    assert man["data_class"] == "infra" and man["request"]["id"] == r["id"] and man["collector_version"]
    extras = {"c3-results.jsonl"} | {a for a in man["artifacts"] if a.startswith("probes/host-")}
    assert set(man["artifacts"]) - extras == {"hermes-log.txt", "gate-decisions.jsonl", "tool-calls.jsonl", "refusals.jsonl",
                                     "agent-diff.patch", "others-changed.txt", "host-walter.txt", "host-covenant.txt", "slots.json"}
    assert man["window"] == {"since": "2026-10-03T03:20:00Z", "until": "2026-10-03T03:30:10Z", "grace_seconds": 10,
                             "until_basis": "grace"}
    assert man["point_in_time"]["slots.json"]["observed_at"] == "2026-10-03T03:30:00Z"
    assert man["notes"]["gate-decisions.jsonl"].startswith("no gate decisions in window")
    assert (ev / "gate-decisions.jsonl").read_text() == ""
    log = (ev / "hermes-log.txt").read_text()
    assert "API call #2" in log and "tool terminal completed (0.21s" in log
    assert "Unrepairable tool_call arguments" in log            # untagged, in window
    assert "latency=1843.0s" in log
    assert "API call #1:" not in log                            # before window
    assert "othersession" not in log                            # other session
    assert "23:59:00" not in log                                # after window
    assert "401 Authorization Required" in log                  # continuation line kept
    assert "Sup3rS3cretPw" not in log and "<redacted>" in log
    diff = (ev / "agent-diff.patch").read_text()
    assert "+  mode: off" in diff and "-  mode: manual" in diff
    assert "s3cr3tvalue99" not in diff
    assert "/etc/llama-swap/config.yaml" in (ev / "host-walter.txt").read_text()
    assert "UNREACHABLE" in (ev / "host-covenant.txt").read_text()
    slots = json.loads((ev / "slots.json").read_text())
    assert slots["llama-coder"][0]["n_decoded"] == 48213
    for p in ev.rglob("*"):
        assert (p.stat().st_mode & 0o777) == (0o700 if p.is_dir() else 0o600)


def test_bundle_sensitive_has_no_content(env):
    _snapshot_and_change(env)
    r = _request(env, data_class="infra", paths=[str(env["hermes"] / "config.yaml"), "/home/x/company/app.py"])
    ev = collect.collect(r["id"], runner=runner(), now=NOW)
    man = json.loads((ev / "manifest.json").read_text())
    assert man["data_class"] == "sensitive" and man["request_data_class"] == "infra"
    diff = (ev / "agent-diff.patch").read_text()
    assert "mode: off" not in diff
    assert (f"# content withheld (data_class=sensitive): {env['hermes'] / 'config.yaml'} \u2014 3 lines changed "
            "(+2/-1) [modified]") in diff
    # the unbacked request path is not the agent's, but still makes the bundle sensitive
    assert man["attribution"]["rejected_request_paths"] == ["/home/x/company/app.py"]


def test_repo_diff_since_session_head(env, tmp_path):
    repo = env["repo"]
    g = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run(g + ["init", "-q"], check=True)
    (repo / "a.txt").write_text("one\n")
    subprocess.run(g + ["add", "."], check=True)
    subprocess.run(g + ["commit", "-qm", "init"], check=True)
    snapshot.take(SESSION, str(repo), config.load_config())
    (repo / "a.txt").write_text("one\ntwo\n")
    (repo / "new.txt").write_text("fresh\n")
    _in_window(repo / "a.txt", repo / "new.txt")
    _agent_wrote(repo / "a.txt")
    r = _request(env, paths=[str(repo / "a.txt")])
    ev = collect.collect(r["id"], runner=runner(), now=NOW)
    diff = (ev / "agent-diff.patch").read_text()
    assert "+two" in diff and "fresh" not in diff           # new.txt: nobody's tool call touched it
    others = (ev / "others-changed.txt").read_text()
    assert f"A {repo / 'new.txt'} | +1 (untracked)" in others and "fresh" not in others
    changed = [p for _, p in snapshot.changed_files(q.snapshot_dir(SESSION), config.load_config())]
    assert str(repo / "a.txt") in changed and str(repo / "new.txt") in changed


def test_no_snapshot_and_bad_since_are_safe(env):
    r = _request(env)
    ev = collect.collect(r["id"], runner=runner(), now=NOW)
    assert "no snapshot" in (ev / "agent-diff.patch").read_text()
    with pytest.raises(ValueError):
        collect.host_command("2026-10-03T03:20:00Z'; rm -rf /", "2026-10-03T03:30:10Z")
    with pytest.raises(ValueError):
        collect.host_command("2026-10-03T03:20:00Z", "2026-10-03T03:30:10Z'; rm -rf /")


def test_add_probe_and_cli_errors(env):
    r = _request(env)
    collect.collect(r["id"], runner=runner(), now=NOW)
    fr = FakeRunner([(r"ss -ltnH", (0, "LISTEN 0 1 *:22\n", ""))])
    rc, p = collect.add_probe(r["id"], "port_listening", ["walter", "22"], runner=fr)
    assert rc == 0 and p.name == "port_listening-1.txt"
    rc, p = collect.add_probe(r["id"], "port_listening", ["walter", "22"], runner=fr)
    assert p.name == "port_listening-2.txt"
    assert "probes/port_listening-2.txt" in json.loads((p.parent.parent / "manifest.json").read_text())["artifacts"]
    rc, p = collect.add_probe(r["id"], "evil", [], runner=fr)
    assert rc == 64 and p is None
    assert collect.main(["../../x"]) == 2
    assert collect.main(["20200101T000000Z-aaaaaa-plan"]) == 2


def test_redact_shapes():
    tok = "sk-" + "Zx9" * 8
    s = redact(f"url=https://u:p@ss@h.example.com/x key={tok} Authorization: Bearer abcdefghijklmnop "
               "PrivateKey = " + "A" * 43 + "= max_tokens=4096 tokens=~3,545 password: \"hunter2hunter2\"")
    assert tok not in s and "p@ss" not in s and "abcdefghijklmnop" not in s and "A" * 43 not in s
    assert "hunter2hunter2" not in s
    assert "max_tokens=4096" in s and "tokens=~3,545" in s


# --- effective_class with no changed paths: only host-rule gate requests keep the hook's label ---

@pytest.mark.parametrize("req_class,expected", [("infra", "infra"), ("sensitive", "sensitive"), (None, "sensitive")])
def test_effective_class_host_gate_keeps_hook_decision(env, req_class, expected):
    req = {"kind": "gate", "changed_paths": [], "data_class": req_class,
           "detail": {"rules": ["remote-mutation", "remote-opaque"]}}
    if req_class is None:
        del req["data_class"]
    assert collect.effective_class(req, config.load_config()) == expected


def test_effective_class_paths_still_rechecked(env):
    req = {"changed_paths": ["/home/x/company/app.py"], "data_class": "infra", "detail": {}}
    assert collect.effective_class(req, config.load_config()) == "sensitive"
