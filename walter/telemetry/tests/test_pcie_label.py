"""PCIe link label: decided server-side (pcie_describe), rendered verbatim by app.js."""
import json

import pytest


def link(width, max_width=16, gen=4, max_gen=4):
    speeds = {1: "2.5 GT/s PCIe", 2: "5.0 GT/s PCIe", 3: "8.0 GT/s PCIe", 4: "16.0 GT/s PCIe"}
    return {"width": width, "max_width": max_width, "gen": gen, "max_gen": max_gen,
            "speed": speeds[gen].split(" PCIe")[0]}


@pytest.fixture
def m(load_main):
    return load_main()


# --- label logic --------------------------------------------------------------------------------
def test_x8_of_x16_without_expectation_is_not_a_warning(m):
    d = m.pcie_describe(link(8), None)
    assert d["label"] == "PCIe Gen4 x8"
    assert d["level"] == "ok"
    assert "x8 of a possible x16" in d["title"]
    assert "chipset" not in json.dumps(d).lower() and "by design" not in d["title"]


def test_x1_below_expected_8_warns(m):
    d = m.pcie_describe(link(1), 8)
    assert d["level"] == "warn"
    assert d["label"] == "PCIe Gen4 x1 · below x8"
    assert "below the expected x8" in d["title"]
    assert "chipset" not in json.dumps(d).lower()


def test_x8_meeting_expected_8_is_ok(m):
    d = m.pcie_describe(link(8), 8)
    assert d["level"] == "ok" and d["label"] == "PCIe Gen4 x8"
    assert d["expected_width"] == 8


def test_gen_downshift_is_neutral(m):
    d = m.pcie_describe(link(8, gen=1), None)
    assert d["level"] == "info"
    assert d["label"] == "PCIe Gen1 x8 · power-save"
    assert "Normal." in d["title"] and "warn" not in d["level"]


def test_gen_downshift_with_expected_width_met_stays_neutral(m):
    d = m.pcie_describe(link(8, gen=2), 8)
    assert d["level"] == "info"


def test_warn_wins_over_downshift(m):
    d = m.pcie_describe(link(1, gen=1), 8)
    assert d["level"] == "warn"
    assert d["label"] == "PCIe Gen1 x1 · power-save · below x8"


def test_full_link_is_ok(m):
    d = m.pcie_describe(link(16), None)
    assert d["level"] == "ok" and d["label"] == "PCIe Gen4 x16"
    assert "possible" not in d["title"]


def test_no_link_is_none(m):
    assert m.pcie_describe(None, 8) is None


def test_unknown_gen_still_labels_width(m):
    d = m.pcie_describe({"width": 8, "max_width": 16, "gen": None, "max_gen": None, "speed": "?"}, None)
    assert d["label"] == "PCIe x8" and d["level"] == "ok"


# --- the expected-width setting -------------------------------------------------------------------
@pytest.mark.parametrize("spec,want", [
    ("", {}),
    (None, {}),
    ("${TELEMETRY_GPU_EXPECTED_WIDTH}", {}),   # unrendered placeholder (setting absent from site.env)
    ("8", {"*": 8}),
    (" 8 ", {"*": 8}),
    ("0=16,1=8", {0: 16, 1: 8}),
    ("8,1=1", {"*": 8, 1: 1}),
    ("x8", {}),
    ("0", {}),
    ("a=8", {}),
])
def test_parse_expected_widths(m, spec, want):
    assert m.parse_expected_widths(spec) == want


def test_expected_width_lookup(m):
    w = {"*": 8, 1: 1}
    assert m.expected_width(0, w) == 8 and m.expected_width(1, w) == 1
    assert m.expected_width(0, {}) is None


def test_default_is_no_expectation(load_main):
    assert load_main().EXPECTED_WIDTHS == {}


def test_env_sets_expectation(load_main):
    assert load_main(TELEMETRY_GPU_EXPECTED_WIDTH="8").EXPECTED_WIDTHS == {"*": 8}


# --- end to end through the API payload --------------------------------------------------------
def dcgm(gpus):
    return [{"metric": {"gpu": str(i), "modelName": "NVIDIA GeForce RTX 3090",
                        "pci_bus_id": f"00000000:0{i + 1}:00.0"}, "value": [0, "5"]} for i in gpus]


def api_payload(mod, links, monkeypatch):
    monkeypatch.setattr(mod, "pcie_link", lambda bus: links.get(bus))
    mod.collector.fast = {"gpu_util": dcgm(range(len(links)))}
    mod.collector.snapshot_bytes = json.dumps(mod.collector.build()).encode()
    from starlette.testclient import TestClient

    r = TestClient(mod.app).get("/api/v1/telemetry")   # no `with`: the collector loop never starts
    assert r.status_code == 200
    return {g["index"]: g["pcie"] for g in r.json()["snapshot"]["gpus"]}


def test_api_two_x8_gpus_no_expectation(load_main, monkeypatch):
    mod = load_main()
    p = api_payload(mod, {"00000000:01:00.0": link(8), "00000000:02:00.0": link(8, gen=1)}, monkeypatch)
    assert p[0]["label"] == "PCIe Gen4 x8" and p[0]["level"] == "ok"
    assert p[1]["label"] == "PCIe Gen1 x8 · power-save" and p[1]["level"] == "info"


def test_api_x1_with_expected_8(load_main, monkeypatch):
    mod = load_main(TELEMETRY_GPU_EXPECTED_WIDTH="8")
    p = api_payload(mod, {"00000000:01:00.0": link(8), "00000000:02:00.0": link(1)}, monkeypatch)
    assert p[0]["level"] == "ok"
    assert p[1]["level"] == "warn" and p[1]["label"] == "PCIe Gen4 x1 · below x8"
