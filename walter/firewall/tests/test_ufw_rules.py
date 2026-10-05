"""Tests for walter/firewall/ufw-rules.sh.tmpl: SSH (22/tcp) is scoped, and the change cannot lock out.

The template is rendered with scripts/render.sh (site.env.example, and the checkout's gitignored
site.env when present, or SITE_ENV_REAL=<path>), then the rendered script runs with fake `ufw` and
`ip` commands on PATH that only log their arguments. Nothing touches the real firewall.

Run: python3 -m pytest walter/firewall/tests -q -p no:cacheprovider
"""
import ipaddress
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
TEMPLATE = REPO / "walter" / "firewall" / "ufw-rules.sh.tmpl"
RENDER = REPO / "scripts" / "render.sh"
LEGACY = "ufw allow 22/tcp"


def _site_envs():
    envs = [pytest.param(REPO / "site.env.example", id="site.env.example")]
    real = Path(os.environ.get("SITE_ENV_REAL", REPO / "site.env"))
    if real.is_file():
        envs.append(pytest.param(real, id="site.env"))
    return envs


def _load(env_file: Path) -> dict:
    """Site values as render.sh sees them (the env file is shell syntax)."""
    out = subprocess.run(["bash", "-c", 'set -a; . "$1"; set +a; env -0', "_", str(env_file)],
                         check=True, capture_output=True, env={"PATH": os.environ["PATH"]}).stdout
    return dict(kv.split("=", 1) for kv in out.decode().split("\0") if "=" in kv)


FAKE_UFW = """#!/bin/sh
echo "ufw $*" >> "$FW_LOG"
if [ "$1" = show ] && [ "$2" = added ]; then
  echo "Added user rules (see 'ufw status' for running firewall):"
  cat "$FW_STATE"
elif [ "$1 $2 $3 $4" = "--force delete allow 22/tcp" ]; then
  grep -vx 'ufw allow 22/tcp' "$FW_STATE" > "$FW_STATE.new" || true
  mv "$FW_STATE.new" "$FW_STATE"
elif [ "$1" = allow ] && [ "$*" = "allow 22/tcp" ]; then
  echo "ufw allow 22/tcp" >> "$FW_STATE"
fi
exit 0
"""

# `ip -4 -o route show dev IF scope link proto kernel` -> the subnet line, like the kernel prints it.
FAKE_IP = """#!/bin/sh
echo "ip $*" >> "$FW_LOG"
[ -n "$FAKE_ROUTE" ] && echo "$FAKE_ROUTE src 0.0.0.0 metric 100"
exit 0
"""


@pytest.fixture(params=_site_envs())
def site(request, tmp_path):
    env_file = request.param
    rendered = tmp_path / "ufw-rules.sh"
    subprocess.run([str(RENDER), "-e", str(env_file), str(TEMPLATE), str(rendered)], check=True,
                   capture_output=True)
    vals = _load(env_file)
    # The bridge subnet as the kernel would report it: BACKEND_LAN_IP's /24 (libvirt default).
    lan_net = str(ipaddress.ip_network(vals["BACKEND_LAN_IP"] + "/24", strict=False))
    return {"script": rendered, "vals": vals, "lan_net": lan_net, "tmp": tmp_path}


def run(site, *, legacy=True, route="default"):
    tmp = site["tmp"]
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    for name, body in (("ufw", FAKE_UFW), ("ip", FAKE_IP)):
        p = bindir / name
        p.write_text(body)
        p.chmod(0o755)
    log, state = tmp / "calls.log", tmp / "state"
    if not state.exists():
        state.write_text(LEGACY + "\n" if legacy else "")
    log.write_text("")
    env = {"PATH": f"{bindir}:{os.environ['PATH']}", "FW_LOG": str(log), "FW_STATE": str(state),
           "FAKE_ROUTE": site["lan_net"] if route == "default" else route}
    proc = subprocess.run(["sh", str(site["script"])], env=env, capture_output=True, text=True)
    calls = [line for line in log.read_text().splitlines() if line.startswith("ufw ")]
    return proc, calls, state.read_text().splitlines()


def port22(calls):
    return [c for c in calls if re.search(r"(^ufw allow 22/tcp$)|\bport 22\b|delete allow 22/tcp", c)]


def test_rendered_text_has_no_unscoped_allow(site):
    text = site["script"].read_text()
    assert not re.search(r"^\s*ufw allow 22/tcp\s*$", text, re.M)
    assert "${" not in re.sub(r"\$\{?[a-z_]+\}?", "", text), "unrendered site variable left in script"


def test_ssh_allowed_only_from_bridge_subnet_and_edge(site):
    proc, calls, _ = run(site)
    assert proc.returncode == 0, proc.stderr
    v = site["vals"]
    # The fake ufw logs "$*", so the comment arrives split into words; compare the rule part exactly.
    allows = [c.split()[1:13] for c in calls if c.startswith("ufw allow") and "port 22" in c]
    expected = [
        ["allow", "in", "on", v["BACKEND_LAN_IF"], "proto", "tcp", "from", site["lan_net"],
         "to", "any", "port", "22"],
        ["allow", "in", "on", "wg0", "proto", "tcp", "from", v["EDGE_WG_IP"], "to", v["BACKEND_WG_IP"],
         "port", "22"],
    ]
    assert allows == expected
    # The workstation (the only SSH client seen) is inside the allowed LAN source.
    assert ipaddress.ip_address(v["HYPERVISOR_BRIDGE_IP"]) in ipaddress.ip_network(site["lan_net"])
    assert "ufw allow 22/tcp" not in calls


def test_scoped_allows_come_before_legacy_delete(site):
    proc, calls, state = run(site, legacy=True)
    assert proc.returncode == 0, proc.stderr
    ssh = port22(calls)
    delete = ssh.index("ufw --force delete allow 22/tcp")
    assert delete == len(ssh) - 1, ssh
    assert sum("port 22" in c for c in ssh[:delete]) == 2
    assert LEGACY not in state
    # ufw is enabled last, after every rule is in place.
    assert calls[-1] == "ufw --force enable"


def test_rerun_is_idempotent_and_skips_delete(site):
    run(site, legacy=True)
    proc, calls, state = run(site)  # same state file: legacy rule already gone
    assert proc.returncode == 0, proc.stderr
    assert not any("delete" in c for c in calls)
    assert sum("port 22" in c for c in calls) == 2


def test_fresh_host_without_legacy_rule(site):
    proc, calls, _ = run(site, legacy=False)
    assert proc.returncode == 0, proc.stderr
    assert not any("delete" in c for c in calls)


@pytest.mark.parametrize("route", ["", "203.0.113.0/24"], ids=["no-subnet", "workstation-outside"])
def test_unsafe_subnet_aborts_before_touching_ssh(site, route):
    proc, calls, state = run(site, legacy=True, route=route)
    assert proc.returncode != 0
    assert "refusing to change the SSH rules" in proc.stderr
    assert port22(calls) == []
    assert LEGACY in state  # the old rule stays, so access is unchanged
    assert "ufw --force enable" not in calls


def test_digest_strip_keeps_ssh_block(site):
    """deploy.sh deletes the '# >>> digest' block when the digest is off; SSH rules must survive."""
    script = site["script"]
    subprocess.run(["sed", "-i", "/^# >>> digest/,/^# <<< digest/d", str(script)], check=True)
    proc, calls, _ = run(site)
    assert proc.returncode == 0, proc.stderr
    assert sum("port 22" in c for c in calls) == 2
    assert not any("port 3300" in c for c in calls)
