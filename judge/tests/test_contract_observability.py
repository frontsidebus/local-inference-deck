"""C7 consumer (agent-judge): the probes and the runaway watcher read the backend the way the deck's
observability contract describes it.

Reads the vendored fixtures/contracts/observability.yaml (a copy of the deck's contracts/observability.yaml;
refresh with `python3 contracts/tools/contracts.py vendor --write` in the deck repo). Tests only: no ssh, no
docker, no network (FakeRunner answers every command).
"""
import importlib.util
import json

import pytest

from collector_testlib import FIXTURES, JUDGE_DIR, FakeRunner, make_env

import extras
import probe
from lib import config

yaml = pytest.importorskip("yaml")
OBS = yaml.safe_load((FIXTURES / "contracts" / "observability.yaml").read_text())
MC = OBS["model_containers"]
ACCEPTS = "1."


@pytest.fixture
def env(tmp_path, monkeypatch):
    return make_env(tmp_path, monkeypatch)


def test_contract_version_is_accepted():
    assert OBS["contract"] == "observability" and OBS["contract_version"].startswith(ACCEPTS)


def _runner():
    port = 10001
    slots = json.dumps(OBS["slots"]["example"])
    return FakeRunner([
        (r"docker ps", (0, f"qwen3.8-27b\t127.0.0.1:{port}->8080/tcp\n", "")),
        (r"/slots", (0, f"=== {port}\n{slots}\n", "")),
    ]), port


def test_slots_probe_uses_the_label_the_loopback_port_and_the_slots_path(env):
    r, port = _runner()
    res = probe.slots_summary(config.load_config(), runner=r)
    docker, curl = (c["argv"][-1] for c in r.calls)
    assert f"docker ps --filter {MC['docker_ps_filter']} " in docker
    assert f"http://127.0.0.1:{port}{OBS['slots']['path']}" in curl
    # the container name is the model id; the slot fields are read as the contract lists them
    s0, s1 = res["qwen3.8-27b"]
    ex0 = OBS["slots"]["example"][0]
    assert s0["id"] == ex0["id"] and s0["id_task"] == ex0["id_task"] and s0["is_processing"] is True
    assert s0["n_decoded"] == ex0["next_token"][0]["n_decoded"] and s0["n_predict"] == ex0["params"]["n_predict"]
    assert s0["max_tokens"] == ex0["params"]["max_tokens"] and s1["is_processing"] is False


def test_contract_example_is_the_probe_fixture():
    """The judge's own slots fixture is the contract's example layout."""
    assert json.loads((FIXTURES / "slots-walter.json").read_text()) == OBS["slots"]["example"]


def test_runaway_watcher_reads_the_slot_fields_and_stops_like_cmdstop(monkeypatch):
    spec = importlib.util.spec_from_file_location("judge_runaway_contract", JUDGE_DIR / "watch" / "runaway.py")
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    monkeypatch.setattr(w, "_import", lambda name: None)
    w._site_cache = {}
    slots = list(w.iter_slots({"qwen3.8-27b": OBS["slots"]["example"]}))
    assert [s["model"] for s in slots] == ["qwen3.8-27b"] * 2
    assert slots[0]["n_decoded"] == 48213 and slots[0]["is_processing"] and slots[1]["n_predict"] == 2048
    stop = MC["stop"].replace("${MODEL_ID}", "qwen3.8-27b")
    assert w.unload_command("qwen3.8-27b").endswith(" " + stop)


def test_known_units_are_the_contracts():
    by_host = {}
    for unit, host in extras.KNOWN_UNITS.items():
        by_host.setdefault(host, set()).add(unit)
    assert by_host == {h: set(u) for h, u in OBS["units"].items()}
    for units in OBS["units"].values():
        for u in units:
            assert probe.UNIT_RE.match(u + ".service"), u     # unit_state / unit_journal accept the name
