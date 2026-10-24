"""C2: the edge <-> backend port set is the same on both sides (no template edits, render only).

Covenant's nginx sites proxy to ${BACKEND_WG_IP}:<port>; Walter publishes/binds those ports on
BACKEND_WG_IP (compose), lets only the edge reach them (docker-user-rules.sh PORTS, and the ufw
rules for the host-network apps), and walter/deploy.sh pins DIGEST_PORT to 3300. The ports are
fixed invariants: WebUI 3000, Pocket-ID 1411, LiteLLM 4000, telemetry 3200, digest 3300.

Everything is rendered with scripts/render.sh and site.env.example (digest turned on with a fake
example host, as in test_deploy.env_on). Run:
    python3 -m pytest walter/tests -q -p no:cacheprovider
"""
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "site.env.example"
RENDER = REPO / "scripts" / "render.sh"

INVARIANT = {"WEBUI_PORT": 3000, "POCKETID_PORT": 1411, "LITELLM_PORT": 4000,
             "TELEMETRY_PORT": 3200, "DIGEST_PORT": 3300}
EDGE_PORTS = set(INVARIANT.values())
HOST_NETWORK = {3200, 3300}          # telemetry and digest bind BACKEND_WG_IP themselves (ufw-gated)
# covenant site -> the backend port it must proxy to
SITE_PORT = {"20-chat": 3000, "30-id": 1411, "40-api": 4000, "50-telemetry": 3200, "60-digest": 3300}
# walter stacks that serve the edge
EDGE_STACKS = ("webui", "gateway", "telemetry", "digest")


def _example_value(key):
    m = re.search(rf"^{key}=([^\s#]*)", EXAMPLE.read_text(), re.M)
    return m.group(1).strip('"') if m else None


BACKEND_WG_IP = _example_value("BACKEND_WG_IP")
EDGE_WG_IP = _example_value("EDGE_WG_IP")


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("c2")
    text = EXAMPLE.read_text()
    text = re.sub(r"^SPARK_DIGEST_HOST=.*$", 'SPARK_DIGEST_HOST="digest.example.com"', text, flags=re.M)
    env = tmp / "site.env"
    env.write_text(text)
    out = {}
    srcs = [*(REPO / "covenant/nginx/sites-available").glob("*.tmpl"),
            REPO / "walter/firewall/ufw-rules.sh.tmpl",
            REPO / "walter/firewall/docker-user-rules.sh.tmpl",
            *(REPO / f"walter/{s}/compose.yaml.tmpl" for s in EDGE_STACKS)]
    for src in srcs:
        rel = src.relative_to(REPO).as_posix()
        dst = tmp / "out" / rel.removesuffix(".tmpl")
        r = subprocess.run(["bash", str(RENDER), "-e", str(env), str(src), str(dst)],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"render {rel}: {r.stderr}"
        out[rel.removesuffix(".tmpl")] = dst.read_text()
    return out


def _proxy_ports(text):
    return {int(p) for p in re.findall(rf"proxy_pass\s+https?://{re.escape(BACKEND_WG_IP)}:(\d+)", text)}


def test_example_ports_are_the_invariants():
    for k, v in INVARIANT.items():
        assert _example_value(k) == str(v), k


def test_deploy_pins_digest_port():
    assert re.search(r"DIGEST_PORT:-3300\} != 3300", (REPO / "walter/deploy.sh").read_text())


def test_edge_proxies_exactly_the_invariant_ports(rendered):
    sites = {k.rsplit("/", 1)[1]: v for k, v in rendered.items() if "/sites-available/" in k}
    for site, port in SITE_PORT.items():
        assert _proxy_ports(sites[site]) == {port}, site
    edge = set().union(*(_proxy_ports(t) for t in sites.values()))
    assert edge == EDGE_PORTS


def _compose_ports(text):
    """Host ports a compose file exposes on BACKEND_WG_IP: published ports and BIND addresses."""
    ip = re.escape(BACKEND_WG_IP)
    pub = re.findall(rf'^\s*-\s*"?{ip}:(\d+):\d+"?\s*$', text, re.M)
    bind = re.findall(rf"^\s*BIND:\s*{ip}:(\d+)\s*$", text, re.M)
    return {int(p) for p in pub}, {int(p) for p in bind}


def test_backend_compose_serves_the_invariant_ports(rendered):
    published, bound = set(), set()
    for s in EDGE_STACKS:
        p, b = _compose_ports(rendered[f"walter/{s}/compose.yaml"])
        published |= p
        bound |= b
    assert bound == HOST_NETWORK
    assert published == EDGE_PORTS - HOST_NETWORK
    assert published | bound == EDGE_PORTS


def _docker_user_ports(text, digest=True):
    if not digest:   # walter/deploy.sh drops the digest blocks when SPARK_DIGEST_HOST is empty
        text = re.sub(r"^# >>> digest.*?^# <<< digest.*?$", "", text, flags=re.M | re.S)
    ports = set()
    for m in re.finditer(r'^PORTS="([^"]*)"', text, re.M):
        ports |= {int(p) for p in m.group(1).split() if p.isdigit()}
    return ports


def test_docker_user_guards_the_invariant_ports(rendered):
    text = rendered["walter/firewall/docker-user-rules.sh"]
    assert _docker_user_ports(text) == EDGE_PORTS
    assert _docker_user_ports(text, digest=False) == EDGE_PORTS - {3300}
    assert re.search(rf"^EDGE={re.escape(EDGE_WG_IP)}$", text, re.M)
    assert re.search(rf"^SVC_IP={re.escape(BACKEND_WG_IP)}$", text, re.M)


def test_ufw_admits_only_the_edge_to_host_network_ports(rendered):
    text = rendered["walter/firewall/ufw-rules.sh"]
    rx = (rf"^ufw allow in on wg0 proto tcp from {re.escape(EDGE_WG_IP)} "
          rf"to {re.escape(BACKEND_WG_IP)} port (\d+)\b")
    allowed = {int(p) for p in re.findall(rx, text, re.M)}
    # Of the edge ports, ufw opens exactly the host-network ones; the published ones are gated by
    # DOCKER-USER. (Other edge rules, such as SSH on 22, are not part of C2.)
    assert allowed & EDGE_PORTS == HOST_NETWORK
