"""Hypervisor / gaming VM panel: snapshot["hypervisor"] is built server-side by hypervisor_view() from the
raw hv_* PromQL results (gaming-exporter); app.js only displays it. No Prometheus needed."""
import json

import pytest


@pytest.fixture
def m(load_main):
    return load_main()


# --- PromQL result builders ----------------------------------------------------------------------
def s(value, **labels):
    return {"metric": {"job": "hypervisor", **labels}, "value": [1_700_000_000, str(value)]}


def one(value, **labels):
    return [s(value, **labels)]


def base(mode="ai", phase="none", switch=None):
    """Exporter up, given mode/phase; switch = direction of an active switch process (or None)."""
    r = {
        "hv_up": one(1, instance="hv:9188"),
        "hv_mode_state": one(1, mode=mode),
        "hv_mode_phase": one(1, phase=phase),
        "hv_switch": one(1 if switch else 0, direction=switch or "unknown"),
        "hv_host_cpus": [s(1, slice="system.slice", cpus="0-31"), s(1, slice="user.slice", cpus="0-31")],
        "hv_vm_running": one(0),
        "hv_vm_state": one(1, state="shut off"),
        "hv_vm_vcpus": one(16),
        "hv_guest_up": one(0),
    }
    if mode == "split":
        r["hv_mode_since"] = one(1_700_000_000)
    return r


def vm_up(r, guest=True):
    r.update({
        "hv_vm_running": one(1),
        "hv_vm_state": one(1, state="running"),
        "hv_vm_cpu": one(4.0),                 # 4 of 16 vCPU-seconds per second
        "hv_vm_mem": one(32 * 1024**3),
        "hv_vm_disk_r": one(1234.4),
        "hv_vm_disk_w": one(0),
        "hv_vm_net_rx": one(5e6),
        "hv_vm_net_tx": one(2.5e7),
        "hv_guest_up": one(1 if guest else 0),
    })
    if guest:
        r.update({
            "hv_sunshine": one(1),
            "hv_stream": one(1),
            "hv_gpu_info": one(1, name="NVIDIA GeForce RTX 3090"),
            "hv_gpu_util": one(97),
            "hv_gpu_enc": one(12.5),
            "hv_gpu_mem_used": one(8 * 1024**3),
            "hv_gpu_mem_total": one(24 * 1024**3),
            "hv_gpu_temp": one(71),
            "hv_gpu_power": one(312.25),
            "hv_gpu_power_limit": one(350),
            "hv_gpu_sm_clock": one(1905),
            "hv_gpu_fan": one(64),
            "hv_gpu_pcie_gen": one(4),
            "hv_gpu_pcie_width": one(8),
        })
    return r


KEYS = {"up", "mode", "direction", "phase", "since", "label", "host_cpus", "vm", "gpu"}
VM_KEYS = {"state", "running", "vcpus", "cpu_pct", "mem_gib", "disk_read_bps", "disk_write_bps",
           "net_rx_bps", "net_tx_bps", "guest_up", "sunshine", "streaming"}
GPU_KEYS = {"name", "util", "enc_util", "mem_used_gib", "mem_total_gib", "temp", "power", "power_limit",
            "sm_clock", "fan", "pcie"}


def check_shape(v):
    assert set(v) == KEYS
    if v["vm"] is not None:
        assert set(v["vm"]) == VM_KEYS
    if v["gpu"] is not None:
        assert set(v["gpu"]) == GPU_KEYS
    json.dumps(v)


# --- modes -----------------------------------------------------------------------------------------
def test_ai_mode(m):
    v = m.hypervisor_view(base())
    check_shape(v)
    assert v["up"] is True and v["mode"] == "ai" and v["label"] == "AI MODE"
    assert v["direction"] is None and v["phase"] is None and v["since"] is None
    assert v["host_cpus"] == "0-31"
    assert v["vm"]["state"] == "shut off" and v["vm"]["running"] is False and v["vm"]["guest_up"] is False
    assert v["vm"]["cpu_pct"] is None and v["vm"]["net_rx_bps"] is None   # absent series -> null, not 0
    assert v["vm"]["sunshine"] is None and v["vm"]["streaming"] is None
    assert v["gpu"] is None


@pytest.mark.parametrize("phase", ["judge-paused", "walter-shutdown", "walter-reconfig", "walter-start", "gaming-start"])
def test_switching_to_game_each_phase(m, phase):
    v = m.hypervisor_view(base("split", phase, switch="to-game"))
    check_shape(v)
    assert v["mode"] == "switching" and v["direction"] == "to-game" and v["phase"] == phase
    assert v["label"] == f"SWITCHING → GAME · {phase}"
    assert v["since"] == 1_700_000_000


def test_switch_process_before_state_file_exists(m):
    v = m.hypervisor_view(base("ai", "none", switch="to-game"))
    assert v["mode"] == "switching" and v["direction"] == "to-game" and v["phase"] is None
    assert v["label"] == "SWITCHING → GAME" and v["since"] is None


def test_state_file_mid_phase_without_process_is_switching(m):
    v = m.hypervisor_view(base("split", "walter-start"))
    assert v["mode"] == "switching" and v["direction"] is None and v["phase"] == "walter-start"
    assert v["label"] == "SWITCHING · walter-start"


def test_unknown_direction_is_null(m):
    v = m.hypervisor_view(base("split", "walter-reconfig", switch="unknown"))
    # direction="unknown" series is 1: still an active switch, but no arrow
    assert v["mode"] == "switching" and v["direction"] is None and v["label"] == "SWITCHING · walter-reconfig"


def test_game_mode_guest_up(m):
    v = m.hypervisor_view(vm_up(base("split", "done")))
    check_shape(v)
    assert v["mode"] == "game" and v["label"] == "GAME MODE" and v["phase"] is None and v["direction"] is None
    assert v["since"] == 1_700_000_000
    assert v["vm"] == {
        "state": "running", "running": True, "vcpus": 16, "cpu_pct": 25.0, "mem_gib": 32.0,
        "disk_read_bps": 1234, "disk_write_bps": 0, "net_rx_bps": 5000000, "net_tx_bps": 25000000,
        "guest_up": True, "sunshine": True, "streaming": True,
    }
    g = v["gpu"]
    assert g["name"] == "NVIDIA GeForce RTX 3090" and g["util"] == 97 and g["enc_util"] == 12.5
    assert g["mem_used_gib"] == 8.0 and g["mem_total_gib"] == 24.0
    assert g["temp"] == 71 and g["power"] == 312.2 and g["power_limit"] == 350 and g["sm_clock"] == 1905 and g["fan"] == 64
    assert g["pcie"]["label"] == "PCIe Gen4 x8" and g["pcie"]["level"] == "ok" and "gaming VM" in g["pcie"]["title"]
    assert set(g["pcie"]) == {"label", "level", "title"}


def test_game_mode_guest_down(m):
    r = vm_up(base("split", "done"), guest=False)
    v = m.hypervisor_view(r)
    check_shape(v)
    assert v["mode"] == "game"
    assert v["vm"]["running"] is True and v["vm"]["guest_up"] is False
    assert v["vm"]["sunshine"] is None and v["vm"]["streaming"] is None
    assert v["gpu"] is None


def test_guest_down_ignores_lingering_gpu_series(m):
    r = vm_up(base("split", "done"))
    r["hv_guest_up"] = one(0)
    v = m.hypervisor_view(r)
    assert v["gpu"] is None and v["vm"]["sunshine"] is None


def test_switching_to_ai(m):
    v = m.hypervisor_view(vm_up(base("split", "done", switch="to-ai")))
    check_shape(v)
    assert v["mode"] == "switching" and v["direction"] == "to-ai"
    assert v["phase"] is None   # "done" belongs to the finished to-game switch, not shown
    assert v["label"] == "SWITCHING → AI"


def test_switching_to_ai_with_phase(m):
    v = m.hypervisor_view(base("split", "walter-start", switch="to-ai"))
    assert v["label"] == "SWITCHING → AI · walter-start"


# --- exporter absent / down / partial ---------------------------------------------------------------
OFFLINE = {"up": False, "mode": "unknown", "direction": None, "phase": None, "since": None,
           "label": "HYPERVISOR OFFLINE", "host_cpus": None, "vm": None, "gpu": None}


@pytest.mark.parametrize("r", [
    {},
    None,
    {"hv_up": None},                              # query failed
    {"hv_up": []},                                # job not configured
])
def test_exporter_absent(m, r):
    assert m.hypervisor_view(r) == OFFLINE


def test_exporter_down_hides_stale_data(m):
    r = vm_up(base("split", "done"))
    r["hv_up"] = one(0)
    assert m.hypervisor_view(r) == OFFLINE


def test_partial_results_mode_query_failed(m):
    r = base()
    r["hv_mode_state"] = None
    v = m.hypervisor_view(r)
    check_shape(v)
    assert v["up"] is True and v["mode"] == "unknown" and v["label"] == "MODE UNKNOWN"


def test_partial_results_vm_queries_failed(m):
    r = base("split", "done")
    for k in ("hv_vm_running", "hv_vm_state", "hv_vm_vcpus", "hv_guest_up"):
        r[k] = None
    v = m.hypervisor_view(r)
    assert v["mode"] == "game" and v["vm"] is None and v["gpu"] is None


def test_missing_gpu_series_are_null(m):
    r = vm_up(base("split", "done"))
    for k in ("hv_gpu_enc", "hv_gpu_fan", "hv_gpu_power_limit", "hv_gpu_mem_total", "hv_gpu_pcie_width", "hv_stream"):
        del r[k]
    v = m.hypervisor_view(r)
    g = v["gpu"]
    assert g["enc_util"] is None and g["fan"] is None and g["power_limit"] is None and g["mem_total_gib"] is None
    assert g["pcie"] is None and v["vm"]["streaming"] is None
    assert g["util"] == 97


def test_vcpus_missing_gives_null_cpu_pct(m):
    r = vm_up(base("split", "done"))
    del r["hv_vm_vcpus"]
    v = m.hypervisor_view(r)
    assert v["vm"]["vcpus"] is None and v["vm"]["cpu_pct"] is None


# --- garbage from Prometheus / the guest ------------------------------------------------------------
def test_garbage_labels(m):
    r = vm_up(base("split", "<script>", switch="sideways"))
    r["hv_vm_state"] = one(1, state="running; rm -rf /")
    r["hv_gpu_info"] = one(1, name='<img src=x onerror=alert(1)>"' + "A" * 200)
    r["hv_host_cpus"] = one(1, slice="system.slice", cpus="0-31; evil")
    v = m.hypervisor_view(r)
    check_shape(v)
    assert v["mode"] == "switching" and v["direction"] is None and v["phase"] is None
    assert v["label"] == "SWITCHING"
    assert v["vm"]["state"] == "unknown"
    assert v["host_cpus"] is None
    name = v["gpu"]["name"]
    assert len(name) <= 64 and set(name) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 ._-")


def test_garbage_mode_label_is_unknown(m):
    r = base()
    r["hv_mode_state"] = one(1, mode="party")
    assert m.hypervisor_view(r)["mode"] == "unknown"


def test_empty_gpu_name_falls_back(m):
    r = vm_up(base("split", "done"))
    r["hv_gpu_info"] = one(1, name="<<>>")
    assert m.hypervisor_view(r)["gpu"]["name"] == "GPU"


def test_ambiguous_mode_is_unknown(m):
    r = base()
    r["hv_mode_state"] = [s(1, mode="ai"), s(1, mode="split")]
    assert m.hypervisor_view(r)["mode"] == "unknown"


def test_zero_valued_series_are_not_active(m):
    r = base()
    r["hv_mode_state"] = [s(1, mode="ai"), s(0, mode="split")]
    r["hv_switch"] = [s(0, direction="to-game"), s(0, direction="to-ai")]
    assert m.hypervisor_view(r)["mode"] == "ai"


@pytest.mark.parametrize("bad", ["NaN", "+Inf", "abc", None])
def test_non_numeric_values(m, bad):
    r = vm_up(base("split", "done"))
    r["hv_gpu_temp"] = one(bad)
    r["hv_vm_cpu"] = one(bad)
    r["hv_mode_since"] = one(bad)
    v = m.hypervisor_view(r)
    assert v["gpu"]["temp"] is None and v["vm"]["cpu_pct"] is None and v["since"] is None


def test_malformed_series_do_not_raise(m):
    r = vm_up(base("split", "done"))
    r["hv_gpu_util"] = [{"metric": None, "value": []}, "junk", 5]
    r["hv_vm_state"] = "not a list"
    r["hv_mode_phase"] = [{"metric": {"phase": ["done"]}, "value": [0, "1"]}]   # unhashable label value
    v = m.hypervisor_view(r)
    check_shape(v)
    assert v["mode"] == "switching" and v["phase"] is None   # phase unreadable -> not "done"


def test_cpu_pct_clamped_and_rates_non_negative(m):
    r = vm_up(base("split", "done"))
    r["hv_vm_cpu"] = one(40)      # > vcpus
    r["hv_vm_net_rx"] = one(-5)
    v = m.hypervisor_view(r)
    assert v["vm"]["cpu_pct"] == 100.0 and v["vm"]["net_rx_bps"] == 0


def test_pcie_bad_values(m):
    assert m._hv_pcie(None, None) is None
    assert m._hv_pcie(4, 0) is None
    assert m._hv_pcie(99, 16)["label"] == "PCIe x16"


def test_future_since_ignored(m):
    r = base("split", "done")
    r["hv_mode_since"] = one(4_000_000_000)
    assert m.hypervisor_view(r)["since"] is None


# --- wiring: queries in the fast set, snapshot key, history key, spark ------------------------------
def test_queries_are_in_fast_set(m):
    for k in ("hv_up", "hv_mode_state", "hv_mode_phase", "hv_switch", "hv_vm_running", "hv_gpu_util"):
        assert k in m.FAST_QUERIES
    assert m.FAST_QUERIES["hv_up"] == 'up{job="hypervisor"}'
    assert "[30s]" in m.FAST_QUERIES["hv_vm_cpu"] and "[30s]" in m.FAST_QUERIES["hv_vm_net_rx"]
    assert "hv_gaming_gpu_utilization_percent" in m.HISTORY_QUERIES["gpu_gaming"]


def test_snapshot_and_api(m):
    m.collector.fast = vm_up(base("split", "done"))
    snap = m.collector.build()
    assert snap["hypervisor"]["mode"] == "game"
    assert snap["spark"]["gpu_gaming"] == 97
    assert snap["gpus"] == []                     # Walter's own GPU list unaffected
    m.collector.snapshot_bytes = json.dumps(snap).encode()
    from starlette.testclient import TestClient

    r = TestClient(m.app).get("/api/v1/telemetry")
    assert r.status_code == 200 and r.json()["snapshot"]["hypervisor"]["label"] == "GAME MODE"


def test_snapshot_without_exporter(m):
    m.collector.fast = {}
    snap = m.collector.build()
    assert snap["hypervisor"] == OFFLINE and snap["spark"]["gpu_gaming"] is None


def test_restoring_state_is_switching_to_ai(m):
    # ai-mode writes mode=restoring; if it failed and left the file behind, no switch process runs.
    v = m.hypervisor_view(base(mode="restoring", phase="done"))
    assert v["mode"] == "switching" and v["direction"] == "to-ai" and v["label"] == "SWITCHING → AI"


def test_hypervisor_target_never_degrades_status(m):
    # The exporter is optional and has its own indicator: its scrape target never counts as "down".
    m.collector.fast = {}
    m.collector.slow = {"up": [{"metric": {"job": "hypervisor", "instance": "hypervisor"}, "value": [0, "0"]},
                               {"metric": {"job": "node", "instance": "walter"}, "value": [0, "1"]}]}
    snap = m.collector.build()
    assert snap["targets"] == {"up": 1, "total": 1, "down": []}
    assert not any("scrape target" in r for r in snap["status"]["reasons"])
