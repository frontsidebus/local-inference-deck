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
| 6 | Covenant | site + cert (one deploy run), second oauth2-proxy with its client secret |
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

`site.env.example` documents each variable. Check that the host's `site.env` is otherwise complete
and matches what is live:
- on Walter, `scripts/render.sh` refuses to render any template whose variables are empty (for
  example `RESTIC_BUCKET` / `RESTIC_REGION` for `walter/backup`), so the deploy stops at "render";
- on Covenant, `WG_BACKEND_PUBLIC_KEY` must be Walter's real WireGuard public key
  (`sudo cat /etc/wireguard/publickey` on Walter; it is not a secret). With `CHANGEME` the live
  deploy stops at step 2/7, and with a wrong key it would rewrite the edge's `wg0` peer and cut the
  tunnel. The dry run only warns `WG_BACKEND_PUBLIC_KEY is CHANGEME`.

The deploy scripts read `site.env` from the root of the checkout they run from (`../site.env` next
to `walter/` or `covenant/`). If a host has no checkout, copy one there first (for example
`git archive <commit> | ssh <host> tar -x -C ~/<dir>`) and put the same `site.env` next to it, with
mode 0600. Compare `sha256sum site.env` across the copies.

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

`/srv/webui/pocketid-bootstrap.py` does **not** create this client or group: it only knows
`open-webui` and the telemetry client. Without the UI, use the API method in
[walter/README.md](../../walter/README.md#pocket-id--oidc-bootstrap) (step 3, temporary
`STATIC_API_KEY`). That method recreates the `pocket-id` container twice, so logins to every site
pause for a few seconds each time. Check in the admin UI (User groups) that `${DIGEST_GROUP}` exists
and has its members **before** using the API method, so the two recreates are not spent on a run
that stops at a missing group.

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

Expected in the dry run:
- `provision-keys: would create harness key digest`;
- `WARN: digest key missing` and `WARN: digest not started: key missing`. On the first deploy these
  are expected in the **dry run** only: the key is created by the live run's `provision-keys.py`.

Read the dry run for **non-digest** changes before the live run. `walter/deploy.sh` converges the
whole host, not only the digest:
- every file whose live copy differs from the repo is installed. The `files` list labels a file
  `new` both when it is missing and when it differs; even a comment-only difference counts;
- a changed file in a stack force-recreates that stack, for example `/srv/gateway/hooks/*` →
  LiteLLM and Postgres (`--force-recreate --wait`), or `/srv/monitoring/prometheus/*` → monitoring;
- every run also runs `apt-get install` (skip it with `--skip-packages`), `ufw-rules.sh` and
  `systemctl restart nvidia-persistenced`. `--no-start` skips that restart, but also every compose
  stack, the digest included;
- every run rebuilds the telemetry image (`up -d --build`). Even with every build layer cached, the
  new image gets a new ID, so `telemetry-app` is recreated (a few seconds). The dry run does not
  show this.

On a host that was set up by hand or has drifted from the repo, the first run can therefore restart
LiteLLM and other stacks. Reconcile those files, or schedule the run when a short gateway restart is
acceptable. The digest part on its own restarts nothing that already exists.

Expected in the live output:
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

## 6. Covenant: deploy (issues the cert), client secret

Do **not** run `certs.sh` for the digest name before the deploy: `00-default` does not answer ACME
for it (its `:80 default_server` returns 444 and its ACME block lists only the apex, chat, api and
id names), so the http-01 challenge would fail. `deploy.sh` links an ACME-only port-80 stub for the
digest first, issues the cert, then links the full site, with no downtime for the other sites.

On the edge, from the repo checkout:

```bash
set -a; . ./site.env; set +a
# 6a. dry run: read it
sudo covenant/deploy.sh --dry-run
#   4/7: no cert yet for optional site(s): 60-digest (<digest host>); ACME-only stub, no downtime
#        install .../sites-available/60-digest-acme, link sites-enabled/60-digest -> 60-digest-acme,
#        + nginx -t, + systemctl reload nginx, + certbot certonly ... -d <digest host> --key-type ecdsa ...
#   5/7: link sites-enabled/60-digest -> sites-available/60-digest once step 4 has issued the cert
#   6/7: oauth2-proxy-digest config + unit staged
# 6b. deploy: stub -> cert -> full site in one run
sudo covenant/deploy.sh
#   4/7: the same lines, then certbot's "Successfully received certificate"
#   5/7: link /etc/nginx/sites-enabled/60-digest -> /etc/nginx/sites-available/60-digest
#        remove /etc/nginx/sites-available/60-digest-acme
#   expected warning: oauth2-proxy-digest NOT started: /etc/oauth2-proxy-digest/client-secret missing
readlink /etc/nginx/sites-enabled/60-digest                                     # expected: /etc/nginx/sites-available/60-digest
sudo certbot certificates --cert-name "$SPARK_DIGEST_HOST" | grep 'Key Type'   # expected: Key Type: ECDSA
# 6c. client secret from step 3 (read without echo, stored root 0600, instance restarted)
sudo covenant/deploy.sh --set-client-secret --instance digest
```

As on Walter, `covenant/deploy.sh` converges the whole edge, so read 6a for non-digest lines:
- `install /etc/wireguard/wg0.conf` means the live file differs from the rendered one. Its diff is
  not shown, because the file holds the private key. The live run applies it with `wg syncconf`.
  Compare it with the private key masked before the live run, and check `WG_BACKEND_PUBLIC_KEY`
  (step 1);
- any other `install ...` with a diff under it is a non-digest file that the live run replaces;
- some lines appear in every dry run and are not changes. The dry run cannot see unit state, so it
  prints `+ systemctl start oauth2-proxy` even when the unit is running, along with the `ufw` rule
  list.

If certbot fails in 6b (the DNS record from step 2 not resolving yet, port 80 blocked), the deploy
does not stop and does not take anything down: the digest stays on the stub, and deploy warns
`60-digest: no cert for <digest host> yet, so only its ACME stub is linked` with the next step.
Fix the cause, then run
`sudo LETSENCRYPT_EMAIL="$LETSENCRYPT_EMAIL" covenant/scripts/certs.sh "$SPARK_DIGEST_HOST"` (the stub
now answers the challenge) and `sudo covenant/deploy.sh` again; that run prints
`certs present for all 6 names` and links the full site.

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
sudo rm /etc/nginx/sites-enabled/60-digest && sudo nginx -t && sudo systemctl reload nginx   # full site or stub
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
