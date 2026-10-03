#!/usr/bin/env bash
# Covenant host firewall (ufw). Idempotent: ufw skips rules that already exist.
#
# Live state this reproduces:
#   Default: deny (incoming), allow (outgoing), disabled (routed)
#   22/tcp, 80/tcp, 443/tcp, WG_PORT/udp  ALLOW IN  Anywhere (+ v6)
# SSH is open to the world at the host level on purpose; the AWS security group
# narrows 22 to the admin IP(s) (see covenant/README.md), and fail2ban guards it.
#
# Usage: WG_PORT=51820 covenant/scripts/ufw.sh [--dry-run]
set -euo pipefail

dry=0
[[ ${1:-} == --dry-run ]] && dry=1
: "${WG_PORT:?WG_PORT must be set (source site.env)}"

run() {
  if ((dry)); then printf '+ %s\n' "$*"; else "$@"; fi
}

run ufw default deny incoming
run ufw default allow outgoing
# Order matters: allow SSH before enabling, so an enable never locks us out.
run ufw allow 22/tcp
run ufw allow 80/tcp
run ufw allow 443/tcp
run ufw allow "${WG_PORT}/udp"

if ((dry)) || ! ufw status 2>/dev/null | grep -q '^Status: active'; then
  run ufw --force enable
fi
((dry)) || ufw status verbose
