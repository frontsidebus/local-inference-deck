# Deploy the digest app

The digest is an optional app: on-demand threat-intel and AI digests curated by the local `coder`
model, served at `https://${SPARK_DIGEST_HOST}` behind its own passkey gate. What it is and how it
works: [walter/digest/README.md](../../walter/digest/README.md). The edge side:
[covenant/README.md](../../covenant/README.md#digest-optional-second-oauth2-proxy-instance).

The app was built by the local Hermes agent under the agent judge, as the judge's pilot build
([docs/agent-judge.md](../agent-judge.md)), then reviewed and fixed by hand.

Until step 1 is done, nothing changes: with `SPARK_DIGEST_HOST` empty, both `walter/deploy.sh` and
`covenant/deploy.sh` print one `digest: off` line and otherwise behave exactly as before the app
existed. Follow the steps in order. Every value below is a placeholder from `site.env`; never paste
a secret into a command line or a chat.

| Step | Where | Result |
|---|---|---|
| 1 | every `site.env` | digest switched on |
| 2 | DNS provider | `${SPARK_DIGEST_HOST}` resolves to the edge |
| 3 | Pocket-ID admin UI | OIDC client `digest` and group `${DIGEST_GROUP}` |
| 4 | `site.env` | `digest` in `HARNESS_KEYS` (its LiteLLM key) |
| 5 | Walter | app running, key copied, state seeded |
| 6 | Covenant | cert, site, second oauth2-proxy with its client secret |
| 7 | both | firewall rules checked |
| 8 | anywhere | verification probes pass |
| 9 | both | rollback, if needed |

## 1. site.env

In the real `site.env` on the workstation and in the checkout on **each** host (Walter, Covenant):

```bash
SPARK_DIGEST_HOST=digest.example.com          # your real name; this switches the digest on
# defaults, only add them to change them:
# DIGEST_PORT=3300                            # must stay 3300
# DIGEST_GROUP=digest-viewers
# OAUTH2_PROXY_DIGEST_CLIENT_ID=digest
```

`site.env.example` documents each variable. Check that the host's `site.env` is otherwise complete:
on Walter, `scripts/render.sh` refuses to render any template whose variables are empty (for
example `RESTIC_BUCKET` / `RESTIC_REGION` for `walter/backup`).

## 2. DNS

Add an A record `${SPARK_DIGEST_HOST}` → `${EDGE_PUBLIC_IP}`, then wait until it resolves:

```bash
dig +short ${SPARK_DIGEST_HOST}       # expected: ${EDGE_PUBLIC_IP}
```

## 3. Pocket-ID client and group

In the Pocket-ID admin UI at `https://${SPARK_ID_HOST}`:

1. **User groups → add** `${DIGEST_GROUP}`. Add yourself (and anyone else who should read digests).
2. **OIDC clients → add**:
   - name and client id `${OAUTH2_PROXY_DIGEST_CLIENT_ID}`;
   - callback URL `https://${SPARK_DIGEST_HOST}/oauth2/callback`;
   - PKCE on;
   - allowed user groups: `${DIGEST_GROUP}`.
3. Keep the **client secret** window open, or regenerate the secret later; step 6 needs it. Do not
   save it in a file or a chat.

## 4. LiteLLM key `digest`

Append `digest` to `HARNESS_KEYS` in `site.env` (same three copies as step 1):

```bash
HARNESS_KEYS="claude-code codex hermes opencode open-webui digest"
```

`provision-keys.py` creates the virtual key (alias `digest`) during the Walter deploy and writes it
to `/srv/gateway/keys/digest.key`. The deploy then copies it for the container.

## 5. Walter: deploy and seed

```bash
ss -ltn 'sport = :3300'                       # expected: only the header line (port free)
sudo walter/deploy.sh --dry-run               # read it: /srv/digest files, ufw + WALTER-PUBLISHED 3300, digest stack
sudo walter/deploy.sh
```

Expected in the output:
- `create   /srv/digest/secrets/digest-litellm-key (copy of /srv/gateway/keys/digest.key)`;
- `== digest` followed by `docker compose ... up -d ... --build`;
- no `digest: off`, no `WARN: digest ...`.

Then seed the watch state **before the first run**, so the first digest reports only what is new
(optional; skip it to start fresh). From the workstation:

```bash
scp ~/.hermes/threat-intel-watches/default.json \
    ~/.hermes/ai-digest-watches/ai-security.json ~/.hermes/ai-digest-watches/ai-research.json \
    ${BACKEND_SSH_USER}@${BACKEND_LAN_IP}:/tmp/
ssh ${BACKEND_SSH_USER}@${BACKEND_LAN_IP} 'sudo install -d -o 10001 -g 10001 -m 0750 /srv/digest/state/state &&
  sudo install -o 10001 -g 10001 -m 0640 /tmp/default.json /tmp/ai-security.json /tmp/ai-research.json /srv/digest/state/state/ &&
  rm /tmp/default.json /tmp/ai-security.json /tmp/ai-research.json'
```

The path really is `state/state/`: the volume is `/srv/digest/state` and the pipeline reads
`STATE_DIR/state/<watch>.json`.

## 6. Covenant: cert, deploy, client secret

On the edge, from the repo checkout:

```bash
set -a; . ./site.env; set +a
# 6a. cert first: 00-default answers ACME on :80 for any name, so no bootstrap downtime is needed
sudo LETSENCRYPT_EMAIL="$LETSENCRYPT_EMAIL" covenant/scripts/certs.sh "$SPARK_DIGEST_HOST"
sudo certbot certificates --cert-name "$SPARK_DIGEST_HOST" | grep 'Key Type'   # expected: Key Type: ECDSA
# 6b. deploy
sudo covenant/deploy.sh --dry-run             # read it: 60-digest installed + linked, oauth2-proxy-digest staged
sudo covenant/deploy.sh
# expected warning: oauth2-proxy-digest NOT started: /etc/oauth2-proxy-digest/client-secret missing
# 6c. client secret from step 3 (read without echo, stored root 0600, instance restarted)
sudo covenant/deploy.sh --set-client-secret --instance digest
```

If 6a is skipped, 6b stops with "full sites are live but certs are missing" instead of taking 443
down. Run 6a, then 6b again.

## 7. Firewall

Walter's deploy installs two rules, both only for traffic from the edge over the tunnel:

```bash
# on Walter
sudo ufw status | grep 3300
#   expected: ${BACKEND_WG_IP} 3300/tcp on wg0   ALLOW   ${EDGE_WG_IP}   # covenant -> digest (host-network)
sudo iptables -S WALTER-PUBLISHED | grep -c 'ctorigdstport 3300'
#   expected: 2 (DROP for port 3300 on wg0 and on the LAN interface, after the RETURN for ${EDGE_WG_IP})
grep -n '^PORTS' /usr/local/sbin/docker-user-rules.sh
#   expected: PORTS="3000 1411 4000 3200" and PORTS="$PORTS 3300"
```

Covenant needs no new firewall rule: nginx already listens on 80/443 and oauth2-proxy-digest
listens on loopback.

## 8. Verification probes

| Where | Command | Expected |
|---|---|---|
| Walter | `curl -fsS http://${BACKEND_WG_IP}:3300/healthz` | `{"ok":true}` |
| Walter | `sudo docker compose -f /srv/digest/compose.yaml ps` | `app` Up, `(healthy)` |
| Walter | `sudo stat -c '%a %U:%g' /srv/digest/secrets/digest-litellm-key` | `440 root:10001` |
| workstation (must fail) | `curl -m5 http://${BACKEND_LAN_IP}:3300/healthz` | connection refused or timeout: the app binds the WireGuard address only |
| workstation (must fail) | `curl -m5 http://${BACKEND_WG_IP}:3300/healthz` | timeout / no route |
| Covenant | `curl -fsS http://${BACKEND_WG_IP}:3300/healthz` | `{"ok":true}` |
| Covenant | `ss -ltn 'sport = :4181'` | one LISTEN line on `127.0.0.1:4181` |
| Covenant | `systemctl is-active oauth2-proxy-digest oauth2-proxy` | `active` twice |
| Covenant | `sudo nginx -t` | `syntax is ok`, `test is successful` |
| anywhere | `curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' https://${SPARK_DIGEST_HOST}/` | `302` to Pocket-ID (`https://${SPARK_ID_HOST}/...`) |
| anywhere | `curl -s -w ' %{http_code}\n' https://${SPARK_DIGEST_HOST}/api/watches` | `{"error":"unauthorized"} 401` |
| anywhere | `curl -s -o /dev/null -w '%{http_code}\n' https://${SPARK_DIGEST_HOST}/healthz` | `302` (every path needs a session) |
| anywhere | `echo \| openssl s_client -connect ${SPARK_DIGEST_HOST}:443 -servername ${SPARK_DIGEST_HOST} 2>/dev/null \| openssl x509 -noout -text \| grep 'Public Key Algorithm'` | `Public Key Algorithm: id-ecPublicKey` |
| anywhere | `curl -sI https://${SPARK_DIGEST_HOST}/api/watches \| grep -i strict-transport` | `strict-transport-security: max-age=31536000; includeSubDomains` |
| browser | open `https://${SPARK_DIGEST_HOST}`, sign in with a passkey | the dashboard with three watch cards |
| browser | a user **not** in `${DIGEST_GROUP}` signs in | refused: Pocket-ID does not admit them to the client, or oauth2-proxy answers 403 |
| browser | RUN NOW on each watch | progress streams `collecting` → `curating` → `done`, then the digest renders |
| browser | RUN NOW again on the same watch | only new items (often "No new items"); per-source cutoffs advance in `state/state/<watch>.json` |
| workstation | `ssh ${BACKEND_SSH_USER}@${BACKEND_LAN_IP} 'curl -fsS -X POST http://${BACKEND_WG_IP}:3300/api/runs/default/now'` | `{"run_id":"<YYYYmmddTHHMMSSZ>"}` (HTTP 202); 409 while that watch runs |
| Walter | `sudo docker compose -f /srv/digest/compose.yaml logs app \| grep -i 'curation failed'` | nothing; otherwise see [troubleshooting](../../walter/digest/README.md#troubleshooting) |
| LiteLLM | spend / request count of the key alias `digest` (LiteLLM `/key/info`, or the telemetry dashboard's per-key view) | grows with each curated run |

## 9. Rollback

Quick disable (minutes, nothing deleted):

```bash
# Covenant
sudo systemctl disable --now oauth2-proxy-digest
sudo rm /etc/nginx/sites-enabled/60-digest && sudo nginx -t && sudo systemctl reload nginx
# Walter
cd /srv/digest && sudo docker compose down
```

Then remove `SPARK_DIGEST_HOST` from every `site.env`, so later deploys skip the digest again.
Walter's next deploy renders the firewall scripts without the digest block; the ufw rule itself
stays until it is deleted.

Full removal (firewall rule, `docker-user-rules.sh` block, `/srv/digest`, edge files, cert,
Pocket-ID client and group, LiteLLM key, DNS): follow
[walter/digest/README.md#rollback](../../walter/digest/README.md#rollback). The firewall step there
deletes only the lines between `# >>> digest` and `# <<< digest`, and is checked with
`grep '^PORTS='` (the other published ports must still be listed).
