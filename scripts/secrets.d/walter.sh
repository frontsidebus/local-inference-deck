# shellcheck shell=bash
# Walter (backend) secrets. Sourced by `scripts/gen-secrets.sh walter`, which provides the
# gs_* helpers, DESTDIR and DRY_RUN. Runs ON Walter as root, AFTER walter/deploy.sh has
# installed the env files (*.env.example -> .env) and wg0.conf, and created the llamaswap user.
#
#   path                                        mode  owner            how
#   /etc/wireguard/privatekey                   0600  root:root        wg genkey
#   /etc/wireguard/publickey                    0644  root:root        wg pubkey (not secret: put it in
#                                                                      site.env WG_BACKEND_PUBLIC_KEY for the edge)
#   /etc/wireguard/wg0.conf  @WG_PRIVATE_KEY@   0600  root:root        filled from privatekey
#   /etc/llama-swap/api-key                     0640  root:llamaswap   openssl rand -hex 32
#   /srv/gateway/.env  LITELLM_MASTER_KEY       0600  root:root        "sk-" + 48 hex
#                      LITELLM_SALT_KEY                                "sk-" + 48 hex (never change after first start:
#                                                                      it encrypts credentials stored in Postgres)
#                      POSTGRES_PASSWORD                               32 alnum
#                      LLAMA_SWAP_KEY                                  = /etc/llama-swap/api-key
#   /srv/webui/.env    WEBUI_SECRET_KEY         0600  root:root        48 alnum
#                      OAUTH_CLIENT_SECRET                             NOT generated: webui/pocketid-bootstrap.py
#                      OPENAI_API_KEYS                                 NOT generated: gateway/provision-keys.py
#   /srv/webui/pocket-id.env ENCRYPTION_KEY     0600  root:root        32 random bytes, base64url
#   /srv/webui/admin-password                   0600  root:root        24 alnum (Open WebUI break-glass admin)
#   /srv/monitoring/grafana-admin-password      0600  root:root        32 alnum (canonical copy)
#   /srv/monitoring/secrets/grafana-admin-password 0400 472:472        copy for the grafana container
#   /srv/monitoring/secrets/llama-swap-api-key  0400  65534:65534      copy of /etc/llama-swap/api-key
#   /srv/telemetry/secrets/llama-swap-api-key   0440  root:10001       copy of /etc/llama-swap/api-key
# Created later by other tools (listed for the inventory; not touched here):
#   /srv/gateway/keys/<name>.key                0600  root:root        gateway/provision-keys.py (LiteLLM /key/generate)
#   /srv/webui/hermes-gateway.key               0600  root:root        copied by hand from the workstation
#                                                                      (~/.hermes/.env API_SERVER_KEY), optional
#   /srv/webui/oidc/<client>.client-secret      0600  root:root        webui/pocketid-bootstrap.py
#   ~BACKEND_SSH_USER/.config/spark/hermes.key  0600  user             deploy.sh --with-hermes (copy of keys/hermes.key)

_w_wg_genkey() {
  if command -v wg >/dev/null 2>&1; then wg genkey
  elif [[ -n $DESTDIR ]]; then head -c 32 /dev/urandom | base64   # staging only (no wireguard-tools)
  else gs_note "wg not installed (apt-get install wireguard-tools)"; return 1; fi
}
_w_wg_pubkey() {
  if command -v wg >/dev/null 2>&1; then wg pubkey <"$DESTDIR/etc/wireguard/privatekey"
  else echo "STAGING-NO-WG-TOOLS"; fi
}
_w_sk() { printf 'sk-%s\n' "$(openssl rand -hex 24)"; }

# ---- directories (modes as live)
if [[ $DRY_RUN != 1 ]]; then
  install -d -m 0700 "$DESTDIR/etc/wireguard"
  install -d -m 0700 "$DESTDIR/srv/gateway/keys"
  install -d -m 0700 "$DESTDIR/srv/monitoring/secrets" "$DESTDIR/srv/telemetry/secrets"
fi

# ---- WireGuard
gs_file /etc/wireguard/privatekey 0600 root root _w_wg_genkey
gs_file /etc/wireguard/publickey  0644 root root _w_wg_pubkey
gs_placeholder /etc/wireguard/wg0.conf @WG_PRIVATE_KEY@ /etc/wireguard/privatekey
[[ $DRY_RUN == 1 ]] || chmod 600 "$DESTDIR/etc/wireguard/wg0.conf" 2>/dev/null || true

# ---- llama-swap API key (shared by LiteLLM, Prometheus, the SD sidecar and telemetry)
gs_file /etc/llama-swap/api-key 0640 root llamaswap gs_rand_hex 32

# ---- gateway
gs_env /srv/gateway/.env LITELLM_MASTER_KEY _w_sk
gs_env /srv/gateway/.env LITELLM_SALT_KEY   _w_sk
gs_env /srv/gateway/.env POSTGRES_PASSWORD  gs_rand_alnum 32
gs_env_from_file /srv/gateway/.env LLAMA_SWAP_KEY /etc/llama-swap/api-key

# ---- webui
gs_env /srv/webui/.env WEBUI_SECRET_KEY gs_rand_alnum 48
gs_env /srv/webui/pocket-id.env ENCRYPTION_KEY gs_rand_b64url 32
gs_file /srv/webui/admin-password 0600 root root gs_rand_alnum 24
for _w_v in OAUTH_CLIENT_SECRET OPENAI_API_KEYS; do
  if [[ -f $DESTDIR/srv/webui/.env ]] && grep -q "^$_w_v=CHANGEME$" "$DESTDIR/srv/webui/.env"; then
    gs_note "pending $_w_v in /srv/webui/.env (set by $([[ $_w_v == OAUTH_CLIENT_SECRET ]] && echo pocketid-bootstrap.py || echo provision-keys.py))"
  fi
done

# ---- monitoring
gs_file /srv/monitoring/grafana-admin-password 0600 root root gs_rand_alnum 32
gs_copy /srv/monitoring/grafana-admin-password /srv/monitoring/secrets/grafana-admin-password 0400 472 472
gs_copy /etc/llama-swap/api-key /srv/monitoring/secrets/llama-swap-api-key 0400 65534 65534

# ---- telemetry
gs_copy /etc/llama-swap/api-key /srv/telemetry/secrets/llama-swap-api-key 0440 root 10001

if [[ $DRY_RUN != 1 && -s $DESTDIR/etc/wireguard/publickey ]]; then
  gs_note "backend WireGuard public key is in /etc/wireguard/publickey; set WG_BACKEND_PUBLIC_KEY in site.env for the edge"
fi
unset -f _w_wg_genkey _w_wg_pubkey _w_sk
unset _w_v
