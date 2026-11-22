"""Unit tests for hypervisor/gaming/gaming-exporter: parsers and exposition only (no root, no libvirt,
no guest). Run: python3 -m pytest hypervisor/gaming/tests -q"""

import fcntl
import importlib.machinery
import importlib.util
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "gaming-exporter"


def _load():
    loader = importlib.machinery.SourceFileLoader("gaming_exporter", str(SCRIPT))
    spec = importlib.util.spec_from_loader("gaming_exporter", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gaming_exporter"] = mod
    loader.exec_module(mod)
    return mod


ge = _load()

GOOD = "NVIDIA GeForce RTX 3090, 14, 32, 1254, 24576, 60, 121.97, 350.00, 1695, 0, 4, 8"


# ------------------------------------------------------------------------------------------ nvidia-smi
def test_nvidia_smi_good_line():
    g = ge.parse_nvidia_smi(GOOD)
    assert g == {
        "name": "NVIDIA GeForce RTX 3090", "utilization_percent": 14, "encoder_utilization_percent": 32,
        "memory_used_bytes": 1254 * 1048576, "memory_total_bytes": 24576 * 1048576,
        "temperature_celsius": 60, "power_watts": 121.97, "power_limit_watts": 350, "sm_clock_mhz": 1695,
        "fan_percent": 0, "pcie_link_gen": 4, "pcie_link_width": 8,
    }


def test_nvidia_smi_na_values_are_omitted():
    line = "NVIDIA GeForce RTX 3090, 14, [N/A], 1254, 24576, 60, [N/A], [Not Supported], 1695, [N/A], 4, 8"
    g = ge.parse_nvidia_smi(line)
    for k in ("encoder_utilization_percent", "power_watts", "power_limit_watts", "fan_percent"):
        assert k not in g
    assert g["utilization_percent"] == 14 and g["pcie_link_width"] == 8


@pytest.mark.parametrize("bad", ["", "garbage", "a,b,c", GOOD + ", 9", "No devices were found",
                                 "NVIDIA, RTX, 3090, 14, 32, 1254, 24576, 60, 121.97, 350.00, 1695, 0, 4, 8"])
def test_nvidia_smi_wrong_shape_is_rejected(bad):
    # a comma in the name would shift every value: the whole line is dropped instead
    assert ge.parse_nvidia_smi(bad) is None


@pytest.mark.parametrize("val", ["1e9", "0x10", "inf", "nan", "-0", "12abc", "1.2.3", "１２", "9999999999",
                                 "1.1234567", " 5 5", "$(reboot)"])
def test_nvidia_smi_odd_numbers_are_dropped(val):
    g = ge.parse_nvidia_smi(GOOD.replace(", 14,", ", %s," % val, 1))
    if val == "-0":
        assert g["utilization_percent"] == 0       # well-formed, in range
    else:
        assert "utilization_percent" not in g


def test_nvidia_smi_out_of_range_dropped():
    g = ge.parse_nvidia_smi("X, 101, -1, 1, 1, 999, 99999, 1, 1, 101, 0, 65")
    for k in ("utilization_percent", "encoder_utilization_percent", "temperature_celsius", "power_watts",
              "fan_percent", "pcie_link_gen", "pcie_link_width"):
        assert k not in g


def test_name_is_sanitised():
    g = ge.parse_nvidia_smi('Evil"} 1\\n# x{y="z, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1')
    assert g["name"] == "Evil 1n xyz"
    assert re.fullmatch(r"[A-Za-z0-9 ._-]{1,64}", g["name"])
    long = ge.parse_nvidia_smi("A" * 500 + ", 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1")
    assert long["name"] == "A" * 64
    assert "name" not in ge.parse_nvidia_smi('"$\'`, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1')


def test_only_first_gpu_line_used():
    g = ge.parse_nvidia_smi(GOOD + "\nOther GPU, 99, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1")
    assert g["name"] == "NVIDIA GeForce RTX 3090" and g["utilization_percent"] == 14


# ------------------------------------------------------------------------------------------ guest output
def guest_out(gpu=GOOD, sunshine="1", stream="1", end=True):
    parts = ["@gpu", gpu, "@sunshine", sunshine, "@stream", stream]
    return "\n".join(p for p in parts if p is not None) + ("\n@end\n" if end else "\n")


def test_guest_full():
    g = ge.parse_guest(guest_out())
    assert g["sunshine"] is True and g["stream"] is True and g["gpu"]["pcie_link_gen"] == 4


def test_guest_stream_unknown_when_ss_missing():
    g = ge.parse_guest("@gpu\n%s\n@sunshine\n1\n@stream\n@end\n" % GOOD)
    assert g["stream"] is None and g["sunshine"] is True


def test_guest_no_sunshine_means_no_stream():
    assert ge.parse_guest(guest_out(sunshine="0", stream="1"))["stream"] is False


def test_guest_without_gpu():
    g = ge.parse_guest(guest_out(gpu=""))
    assert g["gpu"] is None and g["sunshine"] is True


@pytest.mark.parametrize("text", [
    guest_out(end=False),                                 # cut short
    guest_out() + "@end\n",                               # duplicated section
    guest_out() + "trailing\n",                           # text after @end
    "@gpu\n" + "x\n" * 100 + "@end\n",                    # too many lines
    "@gpu\n" + "x" * 1000 + "\n@end\n",                   # line too long
    "",
])
def test_guest_malformed_rejected(text):
    assert ge.parse_guest(text) is None


@pytest.mark.parametrize("v", ["yes", "2", "1 ", "11", ""])
def test_guest_flags_strict(v):
    g = ge.parse_guest(guest_out(sunshine=v))
    assert g["sunshine"] is None


def test_guest_injected_metric_lines_do_not_reach_output():
    text = guest_out(gpu='X\nhv_gaming_vm_running 1\n# TYPE evil gauge') + ""
    g = ge.parse_guest(text)
    assert g is not None and g["gpu"] is None


def test_guest_script_shape():
    s = ge.guest_script(47989)
    assert "sport = :47998" in s and s.count("@") == 4
    assert "utilization.encoder" in s and "pcie.link.width.current" in s


# ------------------------------------------------------------------------------------------ domstats
DOMSTATS_RUNNING = """
Domain: 'linux-gaming'
  state.state=1
  state.reason=1
  cpu.time=1511764594000
  cpu.user=1427072532000
  balloon.current=25165824
  balloon.maximum=25165824
  vcpu.current=16
  vcpu.maximum=16
  vcpu.0.state=1
  vcpu.0.time=130510000000
  net.count=2
  net.0.name=vnet65
  net.0.rx.bytes=1000
  net.0.tx.bytes=10
  net.1.rx.bytes=500
  net.1.tx.bytes=5
  block.count=1
  block.0.name=vda
  block.0.path=/data/vms/linux-gaming.raw
  block.0.rd.bytes=3972034560
  block.0.wr.bytes=28295510016
"""


def test_domstats_running():
    d = ge.parse_domstats(DOMSTATS_RUNNING)
    assert d == {"state": "running", "vcpus": 16, "cpu_seconds": 1511.764594, "memory_bytes": 25165824 * 1024,
                 "disk_read": 3972034560, "disk_write": 28295510016, "net_rx": 1500, "net_tx": 15}


def test_domstats_shut_off():
    d = ge.parse_domstats("Domain: 'linux-gaming'\n  state.state=5\n  state.reason=1\n")
    assert d == {"state": "shut off"}


def test_domstats_unknown_state_and_junk():
    d = ge.parse_domstats("state.state=42\ncpu.time=abc\nballoon.maximum=1024\nWEIRD KEY=1\n")
    assert d == {"state": "unknown", "memory_bytes": 1024 * 1024}


# ------------------------------------------------------------------------------------------ state file
def test_state_absent_is_ai():
    assert ge.parse_state_file(None) == {"mode": "ai", "phase": "none", "since": None}


def test_state_game_mode():
    st = ge.parse_state_file("mode=split\nphase=done\nsince=2026-11-21T18:29:07-06:00\n"
                             "judge=judge-review.path judge-review.timer \n")
    assert st["mode"] == "split" and st["phase"] == "done"
    assert st["since"] == pytest.approx(1795307347.0)


def test_state_switching_and_garbage():
    st = ge.parse_state_file("mode=split\nphase=walter-start\nsince=\n")
    assert st == {"mode": "split", "phase": "walter-start", "since": None}
    st = ge.parse_state_file('mode=Bad"Mode\nphase=$(x)\nsince=yesterday\n')
    assert st == {"mode": "unknown", "phase": "unknown", "since": None}
    assert ge.parse_state_file("")["phase"] == "none"
    assert ge.parse_state_file("since=2026-11-21T18:29:07\n")["since"] is None   # no zone: ambiguous


# ------------------------------------------------------------------------------------------ switch detection
@pytest.mark.parametrize("argv,want", [
    (["/bin/bash", "/home/u/bin/game-mode", "--yes"], "to-game"),
    (["bash", "/home/u/bin/ai-mode"], "to-ai"),
    (["bash", "-x", "/x/gpu-mode", "game"], "to-game"),
    (["/x/gpu-mode", "ai", "--yes"], "to-ai"),
    (["/x/gpu-mode", "status"], None),
    (["/x/gpu-mode"], None),
    (["bash", "/x/game-mode", "status"], None),
    (["bash", "/x/game-mode", "ai"], "to-ai"),
    (["bash", "/x/ai-mode", "--help"], None),
    (["vim", "/x/game-mode"], None),
    (["python3", "/x/game-mode"], None),
    ([], None),
])
def test_switch_direction(argv, want):
    assert ge.switch_direction(argv) == want


def test_lock_held_parsing():
    dev, ino = os.makedev(259, 4), 136943907
    text = ("1: FLOCK  ADVISORY  WRITE 2147 103:04:404662721 0 EOF\n"
            "2: FLOCK  ADVISORY  WRITE 99 103:04:136943907 0 EOF\n")
    assert ge.lock_held(text, dev, ino)
    assert not ge.lock_held(text.splitlines()[0], dev, ino)
    assert not ge.lock_held("3: -> FLOCK  ADVISORY  WRITE 99 103:04:136943907 0 EOF\n", dev, ino)
    assert not ge.lock_held("4: POSIX  ADVISORY  WRITE 99 103:04:136943907 0 EOF\n", dev, ino)


def test_lock_held_live(tmp_path):
    f = tmp_path / "gpu-mode.lock"
    f.write_text("")
    st = f.stat()
    locks = lambda: Path("/proc/locks").read_text()  # noqa: E731
    assert not ge.lock_held(locks(), st.st_dev, st.st_ino)
    with open(f, "w") as h:
        fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held = ge.lock_held(locks(), st.st_dev, st.st_ino)
    if not held:
        pytest.skip("this filesystem's st_dev differs from /proc/locks (e.g. btrfs); cmdline scan covers it")
    assert not ge.lock_held(locks(), st.st_dev, st.st_ino)


# ------------------------------------------------------------------------------------------ site values
def test_site_value_like_gpu_mode():
    text = 'A=1\nHYPERVISOR_BRIDGE_IP="192.168.122.1"   # bridge\nB=\nA=2\n#C=3\n'
    assert ge.site_value(text, "HYPERVISOR_BRIDGE_IP") == "192.168.122.1"
    assert ge.site_value(text, "A") == "2"
    assert ge.site_value(text, "B", "d") == "d"
    assert ge.site_value(text, "C", "d") == "d"


def test_bind_and_guest_ip():
    assert ge.parse_bind("192.168.122.1:9188") == ("192.168.122.1", 9188)
    for bad in ("0.0.0.0:9188", ":9188", "192.168.122.1", "192.168.122.1:0", "host:80", "::1:9188"):
        with pytest.raises(ValueError):
            ge.parse_bind(bad)
    assert ge.guest_ip("192.168.150") == "192.168.150.10"
    for bad in ("", "192.168.150.0", "999.1.1", "a.b.c"):
        with pytest.raises(ValueError):
            ge.guest_ip(bad)


# ------------------------------------------------------------------------------------------ exposition
def test_render_format_and_escaping():
    text = ge.render([
        ("hv_exporter_build_info", {"version": "1"}, 1),
        ("hv_gaming_gpu_info", {"name": 'a"b\\c\nd'}, 1),
        ("hv_gaming_gpu_power_watts", {}, 121.97),
        ("hv_gaming_vm_memory_bytes", {}, 25769803776.0),
        ("hv_gaming_vm_cpu_seconds_total", {}, 1511.764594),
        ("hv_gpu_mode_switch_active", {"direction": "to-game"}, 0),
        ("hv_gpu_mode_switch_active", {"direction": "to-ai"}, 1),
        ("hv_gaming_guest_up", {}, True),
    ])
    lines = text.splitlines()
    assert text.endswith("\n")
    assert 'hv_gaming_gpu_info{name="a\\"b\\\\c\\nd"} 1' in lines
    assert "hv_gaming_gpu_power_watts 121.97" in lines
    assert "hv_gaming_vm_memory_bytes 25769803776" in lines
    assert "# TYPE hv_gaming_vm_cpu_seconds_total counter" in lines
    assert lines.index("# TYPE hv_exporter_build_info gauge") < lines.index("# TYPE hv_gpu_mode_switch_active gauge")
    assert text.count("# TYPE hv_gpu_mode_switch_active gauge") == 1
    assert "hv_gaming_guest_up 1" in lines
    sample = re.compile(r'[a-z_]+(\{([a-z_]+="([^"\\\n]|\\.)*",?)*\})? -?[0-9.e+]+')
    for line in lines:
        assert line.startswith("# HELP ") or line.startswith("# TYPE ") or sample.fullmatch(line), line


def test_render_rejects_unknown_family():
    with pytest.raises(KeyError):
        ge.render([("evil_metric", {}, 1)])


def test_all_contract_names_present():
    contract = """hv_exporter_build_info hv_gpu_mode_state hv_gpu_mode_phase hv_gpu_mode_since_seconds
    hv_gpu_mode_switch_active hv_gaming_vm_running hv_gaming_vm_state hv_gaming_vm_vcpus
    hv_gaming_vm_cpu_seconds_total hv_gaming_vm_memory_bytes hv_gaming_vm_disk_read_bytes_total
    hv_gaming_vm_disk_write_bytes_total hv_gaming_vm_net_rx_bytes_total hv_gaming_vm_net_tx_bytes_total
    hv_gaming_guest_up hv_gaming_guest_poll_seconds hv_gaming_gpu_info hv_gaming_gpu_utilization_percent
    hv_gaming_gpu_encoder_utilization_percent hv_gaming_gpu_memory_used_bytes hv_gaming_gpu_memory_total_bytes
    hv_gaming_gpu_temperature_celsius hv_gaming_gpu_power_watts hv_gaming_gpu_power_limit_watts
    hv_gaming_gpu_sm_clock_mhz hv_gaming_gpu_fan_percent hv_gaming_gpu_pcie_link_gen
    hv_gaming_gpu_pcie_link_width hv_gaming_sunshine_running hv_gaming_stream_active hv_host_allowed_cpus"""
    assert set(contract.split()) <= set(ge.FAMILIES)
    assert {"hv_gaming_gpu_" + s for _, s, _, _ in ge.GPU_FIELDS[1:]} <= set(ge.FAMILIES)


# ------------------------------------------------------------------------------------------ collector + HTTP
class FakeGuest:
    def __init__(self, result):
        self.result, self.calls = result, 0

    def poll(self):
        self.calls += 1
        return self.result


def make_collector(tmp_path, monkeypatch, domstats_rc=0, domstats=DOMSTATS_RUNNING, guest=None):
    state = tmp_path / "gpu-mode"
    state.write_text("mode=split\nphase=done\nsince=2026-11-21T18:29:07-06:00\n")
    cg = tmp_path / "cg"
    for u in ge.SLICES:
        (cg / u).mkdir(parents=True, exist_ok=True)
        (cg / u / "cpuset.cpus.effective").write_text("8-15,24-31\n")

    def fake_run(argv, timeout, limit=0):
        assert argv[:4] == ge.VIRSH and "domstats" in argv
        return domstats_rc, domstats if domstats_rc == 0 else "", "" if domstats_rc == 0 else \
            "error: failed to get domain 'linux-gaming'"
    monkeypatch.setattr(ge, "run_bounded", fake_run)
    monkeypatch.setattr(ge, "scan_switches", lambda: [])
    g = guest or FakeGuest(ge.parse_guest(guest_out()))
    return ge.Collector(g, state=str(state), lock=str(tmp_path / "nolock"), cgroup=str(cg)), g


def test_collect_running(tmp_path, monkeypatch):
    c, g = make_collector(tmp_path, monkeypatch)
    c.collect()
    lines = c.text.splitlines()
    for want in ('hv_gpu_mode_state{mode="split"} 1', 'hv_gpu_mode_phase{phase="done"} 1',
                 'hv_gpu_mode_switch_active{direction="unknown"} 0', "hv_gaming_vm_running 1",
                 'hv_gaming_vm_state{state="running"} 1', "hv_gaming_vm_vcpus 16", "hv_gaming_guest_up 1",
                 'hv_gaming_gpu_info{name="NVIDIA GeForce RTX 3090"} 1', "hv_gaming_gpu_pcie_link_width 8",
                 "hv_gaming_sunshine_running 1", "hv_gaming_stream_active 1",
                 'hv_host_allowed_cpus{slice="user.slice",cpus="8-15,24-31"} 1'):
        assert want in lines, want
    assert g.calls == 1 and not (tmp_path / "nolock").exists()


def test_collect_vm_off_does_not_poll_guest(tmp_path, monkeypatch):
    c, g = make_collector(tmp_path, monkeypatch, domstats="Domain: 'linux-gaming'\n  state.state=5\n")
    c.collect()
    assert g.calls == 0
    assert "hv_gaming_vm_running 0" in c.text and "hv_gaming_guest_up 0" in c.text
    assert "hv_gaming_gpu_" not in c.text and "hv_gaming_vm_vcpus" not in c.text


def test_collect_vm_missing_and_guest_down(tmp_path, monkeypatch):
    c, g = make_collector(tmp_path, monkeypatch, domstats_rc=1)
    c.collect()
    assert 'hv_gaming_vm_state{state="missing"} 1' in c.text and g.calls == 0
    c, g = make_collector(tmp_path, monkeypatch, guest=FakeGuest(None))
    c.collect()
    assert "hv_gaming_guest_up 0" in c.text and "hv_gaming_guest_poll_seconds" in c.text
    assert "hv_gaming_gpu_" not in c.text


def test_http_routes(tmp_path, monkeypatch):
    c, _ = make_collector(tmp_path, monkeypatch)
    srv = ge.Server(("127.0.0.1", 0), ge.Handler)
    srv.collector = c
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/healthz", timeout=5)
        assert e.value.code == 503                         # nothing collected yet
        c.collect()
        r = urllib.request.urlopen(base + "/metrics", timeout=5)
        assert r.headers["Content-Type"].startswith("text/plain; version=0.0.4")
        assert b"hv_gaming_vm_running 1" in r.read()
        assert urllib.request.urlopen(base + "/healthz", timeout=5).read() == b"ok\n"
        for p in ("/", "/metrics/x", "/../etc/passwd"):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + p, timeout=5)
            assert e.value.code == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_run_bounded_caps_output_and_time():
    rc, out, _ = ge.run_bounded(["sh", "-c", "yes"], timeout=5, limit=1000)
    assert rc is None and len(out) <= 1000 + 65536
    rc, _, _ = ge.run_bounded(["sleep", "5"], timeout=0.3)
    assert rc is None
    assert ge.run_bounded(["sh", "-c", "echo hi; echo err >&2"], timeout=5) == (0, "hi\n", "err\n")
    assert ge.run_bounded(["/nonexistent/x"], timeout=1)[0] is None
