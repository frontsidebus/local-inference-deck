#!/usr/bin/env bash
# SSH tunnel from this machine to the backend's LiteLLM, so eval traffic skips the public edge. On 2026-10-07 one
# request stalled 5 minutes on a dead TCP connection through the edge until --timeout fired; the edge path adds
# a WAN round trip and a second TLS hop for no benefit when the workstation is on the backend's LAN.
#
# LiteLLM listens only on the backend's WireGuard address (BACKEND_WG_IP:LITELLM_PORT), not on its LAN interface,
# so the tunnel forwards to that address from the backend itself. Nothing changes on either firewall; it uses the
# SSH access the operator already has. Requests still need the LiteLLM key, which travels inside SSH.
#
# Usage (plans call these; EVAL_RUN as in lib.sh):
#   evals/tools/gateway-tunnel.sh start   # prints EVAL_BASE_URL=http://127.0.0.1:<port>/v1, pid in the state dir
#   evals/tools/gateway-tunnel.sh stop
# Env: EVAL_GPU_SSH / site.env BACKEND_SSH_USER, BACKEND_LAN_IP (ssh target), BACKEND_WG_IP, LITELLM_PORT (4000),
#      EVAL_TUNNEL_PORT (local port, default 14000)
set -u
. "$(dirname "$0")/lib.sh"
PIDF="$EVAL_STATE_DIR/gateway-tunnel.pid"
LPORT="${EVAL_TUNNEL_PORT:-14000}"

target() {
  local t="${EVAL_GPU_SSH:-}"
  if [ -z "$t" ]; then
    local u h; u="$(site_value BACKEND_SSH_USER)"; h="$(site_value BACKEND_LAN_IP)"
    [ -n "$h" ] && t="${u:+$u@}$h"
  fi
  echo "$t"
}

case "${1:-}" in
  start)
    t="$(target)"; wg="$(site_value BACKEND_WG_IP)"; port="$(site_value LITELLM_PORT)"; port="${port:-4000}"
    [ -n "$t" ] && [ -n "$wg" ] || { echo "need BACKEND_LAN_IP and BACKEND_WG_IP in site.env (or EVAL_GPU_SSH)" >&2; exit 2; }
    if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
      echo "EVAL_BASE_URL=http://127.0.0.1:$LPORT/v1"; exit 0
    fi
    # a small supervisor restarts ssh if the connection drops (ServerAlive ends a dead one within ~45 s)
    (
      trap 'kill $child 2>/dev/null; exit 0' TERM INT
      while :; do
        ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
          -L "127.0.0.1:$LPORT:$wg:$port" "$t" </dev/null >>"$EVAL_STATE_DIR/gateway-tunnel.log" 2>&1 &
        child=$!
        wait $child
        echo "$(date -u +%FT%TZ) ssh exited ($?); restarting in 3 s" >> "$EVAL_STATE_DIR/gateway-tunnel.log"
        sleep 3
      done
    ) </dev/null >/dev/null 2>&1 &
    echo $! > "$PIDF"
    for _ in $(seq 1 20); do
      if (exec 3<>"/dev/tcp/127.0.0.1/$LPORT") 2>/dev/null; then
        echo "EVAL_BASE_URL=http://127.0.0.1:$LPORT/v1"; exit 0
      fi
      kill -0 "$(cat "$PIDF")" 2>/dev/null || break
      sleep 0.5
    done
    echo "tunnel did not come up" >&2; kill "$(cat "$PIDF")" 2>/dev/null; rm -f "$PIDF"; exit 1 ;;
  stop)
    [ -f "$PIDF" ] && kill "$(cat "$PIDF")" 2>/dev/null; rm -f "$PIDF" ;;
  *) echo "usage: $0 start|stop" >&2; exit 2 ;;
esac
