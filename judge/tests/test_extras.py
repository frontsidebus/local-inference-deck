"""collector/extras.py: C3 results (windowed, final-state marking) and host-state probe selection."""
import json
from datetime import datetime, timezone

import pytest

from collector_testlib import FakeRunner, make_env

import extras
import probe
from lib import config

SESSION = "20261003_082327_abc123"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("JUDGE_HOST_PROBES", raising=False)
    return make_env(tmp_path, monkeypatch)


def utc(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def req(claims="", since="2026-10-03T13:34:00Z", created="2026-10-03T13:34:20Z", **kw):
    r = {"id": "20261003T133420Z-abc123-completion", "kind": "completion", "session": SESSION,
         "created": created, "since": since, "changed_paths": [], "claims": claims, "plan": None,
         "data_class": "infra", "source_event": "pre_verify", "detail": {}}
    r.update(kw)
    return r


# ---------------------------------------------------------------- c3_results
def write_c3(review, rows):
    d = review / "snapshots" / SESSION
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "c3-results.jsonl", "w") as fh:
        for r in rows:
            fh.write((r if isinstance(r, str) else json.dumps(r)) + "\n")


def row(t, path, check, ok, attempt=0, detail=""):
    return {"t": t, "attempt": attempt, "path": path, "check": check, "ok": ok, "detail": detail}


def test_c3_results_final_state_and_window(env):
    write_c3(env["review"], [
        row("2026-10-03T13:00:00Z", "/w/a.sh", "bash-n", False, detail="before window"),
        row("2026-10-03T13:34:05Z", "/w/a.sh", "bash-n", False, detail="line 18: syntax error"),
        "not json",
        {"t": "garbage", "path": "/w/a.sh", "check": "bash-n", "ok": True},
        row("2026-10-03T13:34:09Z", "/w/b.py", "py-compile", True),
        row("2026-10-03T13:34:12Z", "/w/a.sh", "bash-n", True, attempt=1, detail="syntax OK"),
        row("2026-10-03T13:34:12Z", "/w/a.sh", "shellcheck", True, attempt=1),
        row("2026-10-03T13:40:00Z", "/w/a.sh", "bash-n", False, detail="after window"),
    ])
    got = extras.c3_results(SESSION, utc("2026-10-03T13:34:00Z"), utc("2026-10-03T13:34:30Z"), env["review"])
    assert [(r["path"], r["check"], r["ok"], r["final"]) for r in got] == [
        ("/w/a.sh", "bash-n", False, False),
        ("/w/b.py", "py-compile", True, True),
        ("/w/a.sh", "bash-n", True, True),
        ("/w/a.sh", "shellcheck", True, True),
    ]
    assert got[2]["attempt"] == 1 and got[2]["detail"] == "syntax OK"


def test_c3_results_same_timestamp_later_line_wins(env):
    write_c3(env["review"], [row("2026-10-03T13:34:05Z", "/w/a.json", "json", False),
                             row("2026-10-03T13:34:05Z", "/w/a.json", "json", True)])
    got = extras.c3_results(SESSION, utc("2026-10-03T13:34:00Z"), utc("2026-10-03T13:35:00Z"), env["review"])
    assert [(r["ok"], r["final"]) for r in got] == [(False, False), (True, True)]


def test_c3_results_missing_file_is_empty(env):
    assert extras.c3_results("nope", utc("2026-10-03T00:00:00Z"), utc("2026-10-04T00:00:00Z"), env["review"]) == []


# ---------------------------------------------------------------- probe selection
def plan(r, gate_lines=()):
    return [(n, a) for n, a, _ in extras.plan_host_probes(r, config.load_config(), gate_lines)]


def test_nginx_reload_from_gate_excerpt(env):
    got = plan(req("Reloaded; nginx is active and the reload completed."),
               ["ssh edge-alias 'sudo systemctl reload nginx'"])
    assert got == [("unit_state", ["covenant", "nginx"]),
                   ("unit_journal", ["covenant", "nginx", "2026-10-03T13:34:00Z", "2026-10-03T13:34:30Z"])]


def test_nginx_claim_only_uses_known_unit_host(env):
    assert plan(req("nginx is `active`, and the reload completed successfully (exit 0)."))[0] == \
        ("unit_state", ["covenant", "nginx"])


def test_unknown_unit_needs_a_host(env):
    assert plan(req("I ran systemctl restart foo-bar.service and it is active.")) == []
    got = plan(req("On walter I ran systemctl restart foo-bar.service; it is active."))
    assert got[0] == ("unit_state", ["walter", "foo-bar.service"])


def test_user_units_skipped(env):
    assert plan(req("systemctl --user restart hermes-gateway on walter")) == []


def test_grafana_loopback_ports(env):
    got = plan(req("Grafana on Walter is up. Notes say `127.0.0.1:3002` (stale); it listens on "
                   "`127.0.0.1:3001`. `curl http://127.0.0.1:3001/api/health` -> 200."))
    assert got == [("port_listening", ["walter", "3002"]), ("port_listening", ["walter", "3001"])]


def test_no_host_claims_no_probes(env):
    assert plan(req("Fixed both syntax errors in deploy.sh; bash -n passes.")) == []
    runner = FakeRunner()
    assert extras.host_state_probes(req("Fixed the typo in README.md."), config.load_config(),
                                    runner=runner, root=env["review"]) == {}
    assert runner.calls == []


def test_cap_of_four(env):
    claims = ("On covenant: systemctl reload nginx, systemctl restart oauth2-proxy, systemctl restart fail2ban. "
              "Grafana is up on 127.0.0.1:3001.")
    got = plan(req(claims))
    assert len(got) == extras.MAX_HOST_PROBES == 4
    assert [n for n, _ in got] == ["unit_state", "unit_journal", "unit_state", "unit_journal"]


def test_injection_never_reaches_a_probe(env):
    got = plan(req("ran systemctl reload nginx; rm -rf / on covenant"))
    assert got[0] == ("unit_state", ["covenant", "nginx"])
    for _, args in got:
        assert all(";" not in a and " " not in a for a in args)


def test_host_state_probes_runs_and_labels(env):
    (env["review"]).mkdir(parents=True, exist_ok=True)
    (env["review"] / "gate.log").write_text("\n".join(json.dumps(x) for x in [
        {"ts": "2026-10-03T13:33:54Z", "session": SESSION, "excerpt": "ssh edge-alias 'sudo systemctl reload nginx'"},
        {"ts": "2026-10-03T13:34:05Z", "session": "other", "excerpt": "systemctl restart docker on walter"},
        {"ts": "2026-10-03T10:00:00Z", "session": SESSION, "excerpt": "systemctl restart llama-swap"},
    ]) + "\n")
    runner = FakeRunner([
        (r"systemctl show", (0, "Id=nginx.service\nActiveState=active\nSubState=running\n", "")),
        (r"journalctl", (0, "2026-10-03T13:34:13+0000 edge systemd[1]: Reloaded nginx.service\n"
                            "2026-10-03T13:34:13+0000 edge app[9]: " + "api_" + "key=" + "abcdef0123456789\n", "")),
    ])
    out = extras.host_state_probes(req("nginx reload completed", since="2026-10-03T13:33:50Z"),
                                   config.load_config(), runner=runner, root=env["review"])
    assert sorted(out) == ["unit_journal-covenant-nginx", "unit_state-covenant-nginx"]
    assert "POINT IN TIME" in out["unit_state-covenant-nginx"] and "ActiveState=active" in out["unit_state-covenant-nginx"]
    j = out["unit_journal-covenant-nginx"]
    assert "WINDOWED" in j and "Reloaded nginx.service" in j
    assert "abcdef0123456789" not in j and "1 secret-shaped line(s) withheld" in j
    remote = [c["argv"][-1] for c in runner.calls if "journalctl" in c["argv"][-1]][0]
    assert "--since '2026-10-03 13:33:50 UTC' --until '2026-10-03 13:34:30 UTC'" in remote
    assert all(c["argv"][0] == "ssh" and "edge-alias" in c["argv"] for c in runner.calls)


def test_disabled_flag(env, monkeypatch):
    monkeypatch.setenv("JUDGE_HOST_PROBES", "0")
    runner = FakeRunner()
    assert extras.host_state_probes(req("nginx is active"), config.load_config(), runner=runner,
                                    gate_lines=[]) == {}
    assert runner.calls == []


def test_refused_probe_dropped_and_bad_request_never_raises(env, monkeypatch):
    monkeypatch.setattr(extras, "plan_host_probes",
                        lambda r, c, g: [("unit_state", ["covenant", "nginx; rm -rf /"], "test")])
    runner = FakeRunner()
    assert extras.host_state_probes(req("x"), config.load_config(), runner=runner, gate_lines=[]) == {}
    assert runner.calls == []
    monkeypatch.undo()
    out = extras.host_state_probes({"claims": "nginx is active"}, config.load_config(), runner=runner)
    assert list(out) == ["host_state_probes-error"] and runner.calls == []
