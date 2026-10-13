"""Tests for walter/firewall/docker-user-rules.sh.tmpl: the model containers' bridge has no egress.

The template is rendered with site.env.example, its iptables path is pointed at a fake that only logs its
arguments, and the script runs twice (it must be idempotent). Nothing touches the real firewall.

Run: python3 -m pytest walter/firewall/tests -q -p no:cacheprovider
"""
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TEMPLATE = REPO / "walter" / "firewall" / "docker-user-rules.sh.tmpl"

FAKE_IPT = """#!/bin/sh
echo "$*" >> "$FW_LOG"
# -D (delete a jump) fails like iptables when there is nothing to delete; everything else succeeds.
case "$*" in *" -D "*) exit 1 ;; esac
exit 0
"""


def run_script(tmp_path):
    rendered = tmp_path / "docker-user-rules.sh"
    subprocess.run([str(REPO / "scripts/render.sh"), "-e", str(REPO / "site.env.example"), str(TEMPLATE),
                    str(rendered)], check=True, capture_output=True)
    fake = tmp_path / "iptables"
    fake.write_text(FAKE_IPT)
    fake.chmod(0o755)
    text = rendered.read_text()
    assert "IPT=/usr/sbin/iptables\n" in text
    rendered.write_text(text.replace("IPT=/usr/sbin/iptables\n", f"IPT={fake}\n"))
    log = tmp_path / "calls.log"
    env = {"PATH": os.environ["PATH"], "FW_LOG": str(log)}
    for _ in range(2):
        subprocess.run(["sh", str(rendered)], check=True, env=env, capture_output=True)
    return log.read_text().splitlines()


def test_llama_bridge_has_no_egress(tmp_path):
    calls = run_script(tmp_path)
    once = calls[: len(calls) // 2]
    rules = [c for c in once if c.startswith("-w -A WALTER-LLAMA-EGRESS")]
    assert rules == [
        "-w -A WALTER-LLAMA-EGRESS -i br-llama -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN",
        "-w -A WALTER-LLAMA-EGRESS -i br-llama -m comment --comment walter: no egress from llama-net (br-llama) -j DROP",
        "-w -A WALTER-LLAMA-EGRESS -j RETURN",
    ]
    assert "-w -F WALTER-LLAMA-EGRESS" in once   # rebuilt, not appended to, on every run
    # one jump to each chain at the top of DOCKER-USER, after removing old ones
    assert "-w -I DOCKER-USER 1 -j WALTER-LLAMA-EGRESS" in once
    assert "-w -I DOCKER-USER 1 -j WALTER-PUBLISHED" in once
    assert once.index("-w -D DOCKER-USER -j WALTER-LLAMA-EGRESS") < once.index("-w -I DOCKER-USER 1 -j WALTER-LLAMA-EGRESS")
    # the existing published-port chain is unchanged in shape
    assert any(c.startswith("-w -A WALTER-PUBLISHED -i wg0 -s ") and c.endswith("-j RETURN") for c in once)
    assert calls[len(calls) // 2:] == once   # idempotent: the second run issues the same commands
