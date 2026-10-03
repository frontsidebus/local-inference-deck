# Rotate secrets

When to use this: a key has leaked (pasted into a chat, committed, left in a backup file), a person leaves, or on a schedule.

## Ground rules

- Secrets are generated on the host by `scripts/gen-secrets.sh`, which **never overwrites** an existing secret. To rotate one, move the old file aside (`mv x x.old-$(date +%F)`), run the generator, restart the consumer, verify, then delete the `.old` file.
- The authoritative list of secrets (name, path, mode, consumer) is in each component README: [walter/](../../walter/README.md#secrets), [covenant/](../../covenant/README.md#secrets), [clients/](../../clients/README.md#secrets).
- Never print a secret to a terminal you are recording, and never paste one into a chat, including the local models.
- **Backups contain every secret** in plain form. Rotating does not remove the old value from older snapshots in `${MODELS_DIR}/backups`. After a leak, treat old snapshots as sensitive until they age out (14 days / 8 weeks), or delete them.
- Editor and tool backups (`*.bak*`) also keep old values. After rotating, look for them: `sudo grep -rl '<first 8 chars of old value>' /srv /etc ~/.hermes ~/.config 2>/dev/null`.

## Secret inventory and how to rotate each

| Secret | Lives on | Consumers | Rotate by | Blast radius |
|---|---|---|---|---|
| LiteLLM virtual key (one per user and per harness) | Walter `/srv/gateway/keys/<name>.key`; workstation `~/.config/spark/<harness>.key` | that person or harness | New key, swap, delete old (below) | One key |
| LiteLLM master key | Walter `/srv/gateway/.env` | LiteLLM admin, key management | Replace in `.env`, `docker compose up -d litellm` | Admin API and UI; virtual keys keep working |
| Postgres password | Walter `/srv/gateway/.env` | LiteLLM | `ALTER ROLE litellm PASSWORD ...` in the db container, update `.env`, `up -d litellm` | Gateway only |
| llama-swap API key | Walter `/etc/llama-swap/api-key` | LiteLLM model list, monitoring discovery | Replace file, update the gateway `.env`, restart llama-swap then litellm | All inference until both sides match |
| Open WebUI secret key | Walter `/srv/webui/.env` | Open WebUI sessions | Replace, `up -d open-webui` | Logs everyone out |
| Open WebUI break-glass admin password | Walter `/srv/webui/admin-password` | `${ADMIN_EMAIL}` login | Change in Open WebUI (Settings → Account), then update the file | One account |
| OIDC client secrets (`open-webui`, `telemetry`) | Pocket-ID; Walter `/srv/webui/.env`; Covenant `/etc/oauth2-proxy/` | Open WebUI, oauth2-proxy | Pocket-ID admin → OIDC clients → regenerate secret, paste into consumer, restart it | Login for that app |
| oauth2-proxy cookie secret | Covenant `/etc/oauth2-proxy/` (passed with `LoadCredential=`) | oauth2-proxy | Replace, `systemctl restart oauth2-proxy` | Telemetry sessions |
| Pocket-ID `ENCRYPTION_KEY` | Walter `/srv/webui/pocket-id.env` | Pocket-ID (encrypts its signing keys in the DB) | **Do not rotate by swapping the value**; the DB becomes unreadable. Follow Pocket-ID's documented key-rotation procedure, back up first. | All logins |
| Pocket-ID temporary `STATIC_API_KEY` | Walter `/srv/webui/pocket-id.env` | admin scripts | Remove it as soon as the task is done | Full Pocket-ID admin |
| hermes-gateway `API_SERVER_KEY` | workstation `~/.hermes/.env`; Walter `/srv/webui/.env` (`OPENAI_API_KEYS`) | Open WebUI → Hermes Agent | Replace both, restart `hermes-gateway` (user unit) and `open-webui` | Hermes Agent model |
| Grafana admin password | Walter `/srv/monitoring/grafana-admin-password` | Grafana | `grafana cli admin reset-admin-password` in the container, update file | Monitoring |
| WireGuard keys | Walter and Covenant `/etc/wireguard/` (600) | the tunnel | New keypair on one side, update the peer's `PublicKey`, restart `wg-quick@wg0` on both | Everything, briefly |
| Let's Encrypt keys | Covenant `/etc/letsencrypt/` | nginx | `certbot renew --force-renewal --cert-name <host>` | One host |

## Rotate a LiteLLM virtual key

On Walter, with the master key loaded into the environment from `.env` (not typed on the command line):

```bash
cd /srv/gateway
set -a; . ./.env; set +a
L=http://${BACKEND_WG_IP}:4000

# 1. Read the old key's settings
curl -s "$L/key/info?key=$(sudo cat keys/<name>.key)" -H "Authorization: Bearer $LITELLM_MASTER_KEY" | jq '.info | {key_alias, rpm_limit, max_parallel_requests, models}'

# 2. Create the new key with the same alias pattern and limits
curl -s "$L/key/generate" -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json' \
  -d '{"key_alias":"<name>-'"$(date +%Y%m%d)"'","rpm_limit":60,"max_parallel_requests":4}' \
  | jq -r .key | sudo install -m 600 /dev/stdin keys/<name>.key.new

# 3. Hand the new key over (harness: ~/.config/spark/<harness>.key, mode 600), test it, then:
sudo mv keys/<name>.key.new keys/<name>.key
curl -s "$L/key/delete" -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json' \
  -d '{"keys":["<old key>"]}'
```

For the `open-webui` key, also update `OPENAI_API_KEYS` in `/srv/webui/.env` and run `sudo docker compose -f /srv/webui/compose.yaml up -d open-webui`.

## After any rotation

- Run one request through each affected path (chat, one harness, telemetry login).
- Run `sudo systemctl start spark-backup` so the newest snapshot holds the new values.
- If the old value was ever committed anywhere, rotating is the fix; rewriting git history is not enough.
