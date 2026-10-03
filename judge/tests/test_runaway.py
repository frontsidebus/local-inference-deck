"""Tests for judge/watch/runaway.py (C6). Synthetic slot data only; no network, no probe execution."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

sys.dont_write_bytecode = True  # keep __pycache__ out of the repo tree

JUDGE = Path(__file__).resolve().parent.parent
WATCH = JUDGE / "watch" / "runaway.py"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("JUDGE_REVIEW_DIR", str(tmp_path / "review"))
    monkeypatch.setenv("JUDGE_RUNAWAY_TOKENS", "24000")
    monkeypatch.setenv("JUDGE_RUNAWAY_MINUTES", "10")
    monkeypatch.setenv("BACKEND_SSH_USER", "operator")
    monkeypatch.setenv("BACKEND_LAN_IP", "192.168.122.10")
    monkeypatch.delenv("DISPLAY", raising=False)
    return tmp_path / "review"


@pytest.fixture
def w(env, monkeypatch):
    spec = importlib.util.spec_from_file_location("judge_runaway_under_test", WATCH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_import", lambda name: None)
    mod._site_cache = {}

    def no_probe(*a, **k):
        raise AssertionError("probe must not run in tests")

    monkeypatch.setattr(mod, "fetch_slots", no_probe)
    return mod


def slot(id_task=101, n_decoded=1500, n_predict=-1, processing=True, sid=0, n_ctx=131072):
    """llama-server /slots entry (recent layout: params.n_predict, next_token[0].n_decoded)."""
    return {"id": sid, "id_task": id_task, "n_ctx": n_ctx, "is_processing": processing,
            "params": {"n_predict": n_predict, "temperature": 0.6},
            "next_token": [{"has_next_token": processing, "n_remain": -1, "n_decoded": n_decoded}]}


def doc(*slots, model="test-model-a"):
    return {model: list(slots)}


def run(w, data, now):
    out = io.StringIO()
    alerts = w.run_once(data, out, now=now)
    return alerts, out.getvalue()


def watch_log(env):
    p = env / "watch.log"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


# ---------------------------------------------------------------- parsing
def test_parse_layouts(w):
    flat_summary = {"model": "m1", "slots": [{"id": 0, "id_task": 5, "is_processing": True,
                                              "n_predict": -1, "n_decoded": 30000, "n_ctx": 8192}]}
    legacy = [{"id": 1, "id_task": 7, "state": 1, "n_predict": 512,
               "next_token": {"n_decoded": 12}}]
    a = list(w.iter_slots(flat_summary))
    b = list(w.iter_slots(legacy))
    c = list(w.iter_slots(doc(slot(n_decoded=42))))
    assert a[0]["model"] == "m1" and a[0]["n_decoded"] == 30000 and a[0]["n_predict"] == -1
    assert b[0]["n_decoded"] == 12 and b[0]["n_predict"] == 512 and b[0]["is_processing"]
    assert c[0]["model"] == "test-model-a" and c[0]["n_decoded"] == 42 and c[0]["n_ctx"] == 131072


def test_parse_probe_output_with_preamble(w):
    assert w.parse_probe_output('slots for walter:\n[{"id": 0, "id_task": 1}]\n') == [{"id": 0, "id_task": 1}]


# ---------------------------------------------------------------- thresholds
def test_token_trigger_uncapped_long_generation(w, env):
    """No output cap (n_predict=-1: the cap was lost or bypassed) still fires, and the reason says so."""
    t0 = 1_000_000.0
    alerts, _ = run(w, doc(slot(n_decoded=18000)), t0)
    assert alerts == []
    alerts, text = run(w, doc(slot(n_decoded=24000)), t0 + 60)
    assert len(alerts) == 1
    a = alerts[0]
    assert a["n_predict"] == -1 and a["n_decoded"] == 24000 and a["tok_per_s"] == 100.0
    assert "NO output cap (n_predict=-1)" in a["reasons"][0]
    assert "docker rm -f test-model-a" in text and "Not cancelled" in text
    [rec] = watch_log(env)
    assert rec["event"] == "runaway" and rec["id_task"] == 101 and "unload_cmd" in rec
    [req] = [json.loads(p.read_text()) for p in (env / "queue").glob("*-runaway.json")]
    assert req["kind"] == "runaway" and req["source_event"] == "watch" and req["data_class"] == "infra"
    assert req["changed_paths"] == [] and req["session"] == "watch-task101"


def test_token_trigger_fires_with_output_cap(w, env):
    """The output cap sets n_predict on every slot; a long capped generation must still alert, with progress."""
    t0 = 1_500_000.0
    assert run(w, doc(slot(id_task=31, n_decoded=20000, n_predict=32768)), t0)[0] == []
    alerts, text = run(w, doc(slot(id_task=31, n_decoded=24576, n_predict=32768)), t0 + 60)
    assert len(alerts) == 1
    r = alerts[0]["reasons"][0]
    assert "n_decoded=24576 >= 24000" in r and "24576/32768" in r and "75%" in r
    assert "RUNAWAY test-model-a slot 0 task 31" in text and "Not cancelled" in text
    assert "docker rm -f test-model-a" in text
    [rec] = watch_log(env)
    assert rec["n_predict"] == 32768 and rec["unload_cmd"].endswith("docker rm -f test-model-a")


def test_token_trigger_unknown_n_predict(w, env):
    s = slot(id_task=32, n_decoded=30000)
    del s["params"]["n_predict"]
    alerts, _ = run(w, doc(s), 1_600_000.0)
    assert len(alerts) == 1 and "n_predict unknown" in alerts[0]["reasons"][0]


def test_default_gateway_limit_never_trips_tokens(w, env):
    """A request with no limit gets the gateway default 16384, below the 24000 default threshold."""
    alerts, _ = run(w, doc(slot(id_task=33, n_decoded=16384, n_predict=16384)), 1_700_000.0)
    assert alerts == []


def test_default_token_threshold(w, env, monkeypatch):
    monkeypatch.delenv("JUDGE_RUNAWAY_TOKENS")
    assert w.DEFAULT_TOKENS == 24000
    assert run(w, doc(slot(id_task=34, n_decoded=23999, n_predict=32768)), 1_800_000.0)[0] == []
    assert len(run(w, doc(slot(id_task=34, n_decoded=24000, n_predict=32768)), 1_800_030.0)[0]) == 1


def test_default_matches_lib_config():
    sys.path.insert(0, str(JUDGE))
    from lib import config  # type: ignore
    src = WATCH.read_text()
    assert config.DEFAULTS["JUDGE_RUNAWAY_TOKENS"] == "24000" and "DEFAULT_TOKENS = 24000" in src


def test_minutes_trigger_even_with_cap(w, env):
    t0 = 2_000_000.0
    assert run(w, doc(slot(id_task=7, n_decoded=100, n_predict=4096)), t0)[0] == []
    assert run(w, doc(slot(id_task=7, n_decoded=3000, n_predict=4096)), t0 + 9 * 60)[0] == []
    alerts, _ = run(w, doc(slot(id_task=7, n_decoded=3900, n_predict=4096)), t0 + 10 * 60 + 1)
    assert len(alerts) == 1 and alerts[0]["reasons"] == ["task 7 processing for 10.0 min >= 10"]


def test_new_task_resets_minutes_clock(w, env):
    t0 = 3_000_000.0
    run(w, doc(slot(id_task=1, n_predict=256)), t0)
    run(w, doc(slot(id_task=2, n_predict=256)), t0 + 8 * 60)  # task 1 finished, task 2 started
    assert run(w, doc(slot(id_task=2, n_predict=256)), t0 + 12 * 60)[0] == []


def test_dedupe_one_alert_per_task(w, env):
    t0 = 4_000_000.0
    assert len(run(w, doc(slot(id_task=9, n_decoded=24500, n_predict=32768)), t0)[0]) == 1
    for i in range(1, 5):  # still running, and later past the minutes threshold too: no second alert
        assert run(w, doc(slot(id_task=9, n_decoded=24500 + i * 1500, n_predict=32768)), t0 + i * 200)[0] == []
    assert len(watch_log(env)) == 1
    assert len(list((env / "queue").glob("*-runaway.json"))) == 1
    # a different task on the same slot is a new incident
    assert len(run(w, doc(slot(id_task=10, n_decoded=25000, n_predict=32768)), t0 + 1000)[0]) == 1


def test_dedupe_survives_lost_state_file(w, env):
    t0 = 5_000_000.0
    run(w, doc(slot(id_task=11, n_decoded=30000)), t0)
    (env / "watch-state.json").unlink()
    assert run(w, doc(slot(id_task=11, n_decoded=31000)), t0 + 30)[0] == []


@pytest.mark.parametrize("n_predict", [-1, 2048, 16384, 32768])
def test_no_false_alarm_normal_completion(w, env, n_predict):
    """A normal ~2k-token completion finishing in under a minute, with or without a cap."""
    t0 = 6_000_000.0
    for i, n in enumerate([300, 1200, 2000]):
        assert run(w, doc(slot(id_task=55, n_decoded=n, n_predict=n_predict)), t0 + i * 20)[0] == []
    assert run(w, doc(slot(id_task=55, n_decoded=2000, n_predict=n_predict, processing=False)), t0 + 60)[0] == []
    assert watch_log(env) == []
    assert not (env / "queue").exists() or list((env / "queue").glob("*")) == []


def test_idle_slots_ignored(w, env):
    assert run(w, doc(slot(id_task=3, n_decoded=99999, processing=False)), 1.0)[0] == []


def test_incident_shaped_synthetic(w, env):
    """Shape of the motivating incident with synthetic numbers: uncapped, large context, ~55 tok/s."""
    t0 = 7_000_000.0
    n = 40000
    alerts_total = 0
    for i in range(6):
        alerts, _ = run(w, doc(slot(id_task=4242, n_decoded=n + i * 1650), model="test-model-b"), t0 + i * 30)
        alerts_total += len(alerts)
    assert alerts_total == 1
    [rec] = watch_log(env)
    assert rec["model"] == "test-model-b" and rec["n_ctx"] == 131072


def test_main_once_with_slots_file(w, env, tmp_path, capsys):
    f = tmp_path / "slots.json"
    f.write_text(json.dumps(doc(slot(id_task=77, n_decoded=50000))))
    assert w.main(["--once", "--slots-file", str(f)]) == 0
    out = capsys.readouterr().out
    assert "RUNAWAY test-model-a slot 0 task 77" in out


def test_notify_only_with_display(w, env, monkeypatch):
    calls = []
    monkeypatch.setattr(w.shutil, "which", lambda name: "/usr/bin/notify-send")
    monkeypatch.setattr(w.subprocess, "run", lambda *a, **k: calls.append(a[0]))
    w.notify("s", "b")
    assert calls == []
    monkeypatch.setenv("DISPLAY", ":0")
    w.notify("s", "b")
    assert calls and calls[0][0] == "notify-send"


def test_with_real_lib_queue(env, monkeypatch, capsys):
    if not (JUDGE / "lib" / "queue.py").is_file():
        pytest.skip("lib/queue.py not present yet")
    monkeypatch.setenv("SITE_ENV", str(env.parent / "no-site.env"))
    spec = importlib.util.spec_from_file_location("judge_runaway_real_lib", WATCH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = io.StringIO()
    alerts = mod.run_once(doc(slot(id_task=88, n_decoded=25000)), out, now=8_000_000.0)
    assert len(alerts) == 1 and "could not write" not in out.getvalue()
    [p] = list((env / "queue").glob("*-runaway.json"))
    from lib import queue as q  # type: ignore
    assert q.validate_request(json.loads(p.read_text())) == []
