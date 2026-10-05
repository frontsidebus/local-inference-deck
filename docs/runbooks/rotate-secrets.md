# Rotate secrets

When to use this: a key has leaked (pasted into a chat, committed, left in a backup file), a person leaves, or on a schedule.

## Ground rules

- Secrets are generated on the host by `scripts/gen-secrets.sh`, which **never overwrites** an existing secret. To rotate one, move the old file aside (`mv x x.old-$(date +%F)`), run the generator, restart the consumer, verify, then delete the `.old` file.
- The authoritative list of secrets (name, path, mode, consumer) is in each component README: [walter/](../../walter/README.md#secrets), [covenant/](../../covenant/README.md#secrets), [clients/](../../clients/README.md#secrets).
- Never print a secret to a terminal you are recording, and never paste one into a chat, including the local models. Read secrets into shell variables (`OLD=$(sudo cat file)`) and `unset` them when done.
- **Backups contain every secret** in plain form. Rotating does not remove the old value from older snapshots in `${MODELS_DIR}/backups`. After a leak, treat old snapshots as sensitive until they age out (14 days / 8 weeks), or delete them.
- Editor and tool backups (`*.bak*`) also keep old values. After rotating, look for them without printing the value: `sudo grep -rlF -- "$OLD" /srv /etc /root /home 2>/dev/null`, then `shred -u` what is left.

## Secret inventory and how to rotate each

| Secret | Lives on | Consumers | Rotate by | Blast radius |
|---|---|---|---|---|
| LiteLLM virtual key (one per user and per harness) | Walter `/srv/gateway/keys/<name>.key`, plus the copies listed [below](#rotate-a-litellm-virtual-key) | that person, harness or app | [New key, swap every copy, delete the old one](#rotate-a-litellm-virtual-key) | One key |
| LiteLLM master key | Walter `/srv/gateway/.env` | LiteLLM admin, key management | Replace in `.env`, `docker compose up -d litellm` | Admin API and UI; virtual keys keep working |
| Postgres password | Walter `/srv/gateway/.env` | LiteLLM | `ALTER ROLE litellm PASSWORD ...` in the db container, update `.env`, `up -d litellm` | Gateway only |
| llama-swap API key | Walter `/etc/llama-swap/api-key` | LiteLLM model list, monitoring discovery | Replace file, update the gateway `.env`, restart llama-swap then litellm | All inference until both sides match |
| Open WebUI secret key | Walter `/srv/webui/.env` | Open WebUI sessions | Replace, `up -d open-webui` | Logs everyone out |
| Open WebUI break-glass admin password | Walter `/srv/webui/admin-password` | `${ADMIN_EMAIL}` login | Change in Open WebUI (Settings → Account), then update the file | One account |
| OIDC client secret `open-webui` | Pocket-ID; Walter `/srv/webui/.env` (`OAUTH_CLIENT_SECRET`) | Open WebUI | Regenerate in Pocket-ID, put it in `.env`, `docker compose -f /srv/webui/compose.yaml up -d open-webui` | Chat logins |
| OIDC client secret, telemetry gate | Pocket-ID client `${OAUTH2_PROXY_CLIENT_ID}`; Covenant `/etc/oauth2-proxy/client-secret` | oauth2-proxy | [Regenerate, then `--set-client-secret --force`](#rotate-an-oauth2-proxy-client-secret) | Telemetry logins |
| OIDC client secret, digest gate (optional) | Pocket-ID client `${OAUTH2_PROXY_DIGEST_CLIENT_ID}`; Covenant `/etc/oauth2-proxy-digest/client-secret` | oauth2-proxy-digest | [Regenerate, then `--set-client-secret --instance digest --force`](#rotate-an-oauth2-proxy-client-secret) | Digest logins |
| oauth2-proxy cookie secrets | Covenant `/etc/oauth2-proxy/cookie-secret`, `/etc/oauth2-proxy-digest/cookie-secret` (passed with `LoadCredential=`) | that oauth2-proxy instance | [Move aside, regenerate, restart](#rotate-an-oauth2-proxy-cookie-secret) | Ends that site's sessions |
| digest LiteLLM key copy (optional) | Walter `/srv/digest/secrets/digest-litellm-key` (root:10001 0440) | digest app | Rotate the `digest` virtual key, then re-copy it ([walter/digest/README.md](../../walter/digest/README.md#operations)); read per call, no restart | Digest curation (falls back to uncurated) |
| Pocket-ID `ENCRYPTION_KEY` | Walter `/srv/webui/pocket-id.env` | Pocket-ID (encrypts its signing keys in the DB) | **Do not rotate by swapping the value**; the DB becomes unreadable. Follow Pocket-ID's documented key-rotation procedure, back up first. | All logins |
| Pocket-ID temporary `STATIC_API_KEY` | Walter `/srv/webui/pocket-id.env` | admin scripts | Remove it as soon as the task is done | Full Pocket-ID admin |
| hermes-gateway `API_SERVER_KEY` | workstation `~/.hermes/.env`; Walter `/srv/webui/.env` (`OPENAI_API_KEYS`) | Open WebUI → Hermes Agent | Replace both, restart `hermes-gateway` (user unit) and `open-webui` | Hermes Agent model |
| Grafana admin password | Walter `/srv/monitoring/grafana-admin-password` | Grafana | `grafana cli admin reset-admin-password` in the container, update file | Monitoring |
| WireGuard keys | Walter and Covenant `/etc/wireguard/` (600) | the tunnel | New keypair on one side, update the peer's `PublicKey` (on Covenant: `WG_BACKEND_PUBLIC_KEY` in `site.env`, then `covenant/deploy.sh`), restart `wg-quick@wg0` on both | Everything, briefly |
| Let's Encrypt keys | Covenant `/etc/letsencrypt/` | nginx | `certbot renew --force-renewal --cert-name <host>` | One host |

## Rotate a LiteLLM virtual key

The new key gets the same models and limits as the old one, every copy is swapped, every process
that holds the old key is restarted, and only then is the old key deleted. Nothing breaks along the
way, because both keys work until the last step.

**Where the copies are.** Walter's `/srv/gateway/keys/<name>.key` is the master copy. The usual
other copies:

| Key | Other copies | Who reads it, and when |
|---|---|---|
| a harness key (`claude-code`, `codex`, `hermes`, `opencode`) | `~/.config/spark/<harness>.key` on every machine that runs the harness (the workstation, and Walter if the harness runs there too) | the `*-spark` wrapper, once at process start |
| `hermes`, older installs | also `SPARK_HERMES_API_KEY=` in `~/.hermes/.env` (the legacy gateway layout, see [clients/](../../clients/README.md#optional-hermes-gateway-hermes-agent-inside-open-webui)) | Hermes, at process start |
| `open-webui` | Walter `/srv/webui/.env` (`OPENAI_API_KEYS`, the LiteLLM slot) | Open WebUI, at container start |
| `digest` | Walter `/srv/digest/secrets/digest-litellm-key` | the digest app, on every call |
| a person's key | wherever they stored it | their harness |

When in doubt, search for the old value without printing it (Ground rules). The list of files it
returns is your swap list.

**1. On Walter, create the new key.** The master key comes from `.env` and the old key from its
file, so neither is typed or printed. LiteLLM key aliases must be unique, so the new key gets a
temporary alias until the old one is gone:

```bash
cd /srv/gateway
set -a; . ./.env; set +a
L=http://${BACKEND_WG_IP}:4000
N=<name>                                   # alias = key file name, e.g. hermes
OLD=$(sudo cat keys/$N.key)

# same models and limits as the old key, temporary alias <name>-rotated
BODY=$(curl -s "$L/key/info?key=$OLD" -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  | jq --arg a "$N-rotated" '.info | {models, rpm_limit, tpm_limit, max_parallel_requests} + {key_alias: $a}')
echo "$BODY"                               # holds no secret: check the models and limits
curl -s "$L/key/generate" -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json' \
  -d "$BODY" | jq -r .key | sudo install -m 600 /dev/stdin keys/$N.key.new
NEW=$(sudo cat keys/$N.key.new)
curl -s -o /dev/null -w '%{http_code}\n' "$L/v1/models" -H "Authorization: Bearer $NEW"   # 200
```

**2. Swap every copy.** Walter's master copy first, then the others:

```bash
sudo mv keys/$N.key.new keys/$N.key       # mode 600 root, as before
```

- Key files on another machine: copy without echoing, then replace, keeping mode 600:
  ```bash
  ( umask 077; ssh ${BACKEND_SSH_USER}@${BACKEND_LAN_IP} "sudo cat /srv/gateway/keys/<name>.key" > ~/.config/spark/<name>.key.new )
  mv ~/.config/spark/<name>.key.new ~/.config/spark/<name>.key
  ```
- `.env` lines (`SPARK_HERMES_API_KEY`, `OPENAI_API_KEYS`): edit the line in an editor, so the key
  stays off the command line. Keep the file's mode (600).
- The digest copy: `sudo install -m 0440 -o root -g 10001 /srv/gateway/keys/digest.key /srv/digest/secrets/digest-litellm-key`.

**3. Restart what holds the old key in memory**, and test each path with one request:
- harness wrappers read the key once at start. Restart long-running sessions, including the
  owner's interactive ones, and `systemctl --user restart hermes-gateway` for the Open WebUI Hermes
  connection. Every process that uses the key must have started after the swap;
- Open WebUI: `sudo docker compose -f /srv/webui/compose.yaml up -d open-webui` (it reads `.env` at
  container start);
- the digest app and LiteLLM itself need no restart.

**4. Delete the old key and restore the alias:**

```bash
printf '{"keys": ["%s"]}' "$OLD" |
  curl -s "$L/key/delete" -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json' -d @- >/dev/null
curl -s -o /dev/null -w '%{http_code}\n' "$L/v1/models" -H "Authorization: Bearer $OLD"   # 401: the old key is dead
printf '{"key": "%s", "key_alias": "%s"}' "$NEW" "$N" |
  curl -s "$L/key/update" -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json' -d @- >/dev/null
curl -s "$L/key/info?key=$NEW" -H "Authorization: Bearer $LITELLM_MASTER_KEY" | jq -r .info.key_alias   # <name>
curl -s -o /dev/null -w '%{http_code}\n' "$L/v1/models" -H "Authorization: Bearer $NEW"   # 200
unset OLD NEW BODY
```

Restoring the alias keeps spend attribution and the per-key views under the same name, and lets
`provision-keys.py` (which skips a name whose alias already exists) see the key as present.

**5. Clean up and record it:** `shred -u` any backup copy made along the way, search for the old
value once more (Ground rules), and store the new key wherever the owner keeps a copy (a password
manager).

## Rotate an oauth2-proxy client secret

The telemetry and digest gates each have their own Pocket-ID client and their own oauth2-proxy
instance on Covenant. Regenerating a client secret in Pocket-ID invalidates the old one at once, so
do the two steps back to back. Sessions that already exist keep working (they live in the cookie);
new logins to that site fail until step 2 is done.

1. Pocket-ID admin UI → **OIDC clients** → the client (`${OAUTH2_PROXY_CLIENT_ID}` for telemetry,
   `${OAUTH2_PROXY_DIGEST_CLIENT_ID}` for the digest) → regenerate the client secret. The UI shows
   it once.
2. On Covenant, from a repo checkout with the edge's `site.env` (the script checks it):
   ```bash
   sudo covenant/deploy.sh --set-client-secret --force                     # telemetry: /etc/oauth2-proxy/client-secret
   sudo covenant/deploy.sh --set-client-secret --instance digest --force   # digest: /etc/oauth2-proxy-digest/client-secret
   ```
   It reads the secret from stdin with no echo, replaces the file (root 0600) and restarts the
   matching unit (`oauth2-proxy` or `oauth2-proxy-digest`). Without `--force` it refuses to replace
   an existing secret. When the secret is piped in (for example from a root-only file), the input
   needs a trailing newline, or the script exits without storing it; `shred -u` that file
   afterwards.
3. Verify: `systemctl is-active oauth2-proxy oauth2-proxy-digest`, no errors in
   `journalctl -u <unit> -n 20`, and a **new** login to the site (a private browser window)
   completes. The `302` and `401` probes in [covenant/README.md](../../covenant/README.md#verify)
   still pass.

The `open-webui` client works the same way in Pocket-ID, but its secret lives on Walter: put it in
`OAUTH_CLIENT_SECRET` in `/srv/webui/.env`, then
`sudo docker compose -f /srv/webui/compose.yaml up -d open-webui`.

## Rotate an oauth2-proxy cookie secret

This ends every session of that site; users sign in again with their passkey. On Covenant, from the
repo checkout:

```bash
sudo mv /etc/oauth2-proxy-digest/cookie-secret /etc/oauth2-proxy-digest/cookie-secret.old-$(date +%F)   # or /etc/oauth2-proxy/
sudo scripts/gen-secrets.sh covenant          # creates only the missing secret (44 chars, root 0600)
sudo systemctl restart oauth2-proxy-digest    # or oauth2-proxy
sudo shred -u /etc/oauth2-proxy-digest/cookie-secret.old-*
```

## After any rotation

- Run one request through each affected path (chat, one harness, the telemetry and digest logins).
- Run `sudo systemctl start spark-backup` so the newest snapshot holds the new values.
- If the old value was ever committed anywhere, rotating is the fix; rewriting git history is not enough.
