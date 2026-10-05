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
| 1 | every `site.env` | digest switched on; the rest of each `site.env` complete |
| 2 | DNS provider | `${SPARK_DIGEST_HOST}` resolves to the edge |
| 3 | Pocket-ID admin UI | group `${DIGEST_GROUP}` with its members, then the OIDC client; its id checked |
| 4 | `site.env` | `digest` in `HARNESS_KEYS` (its LiteLLM key) |
| 5 | Walter | app running on the tunnel address, key copied, state seeded |
| 6 | Covenant | site + cert in one deploy run, then the second oauth2-proxy's client secret |
| 7 | both | firewall rules checked |
| 8 | anywhere | verification probes pass |
| 9 | both | staged copies and temporary files cleaned up |

Later: [update the app](#update-the-app-walter-only), [feed sources](#feed-sources),
[rollback](#rollback).

## Before you start: both deploys converge the whole host

`walter/deploy.sh` and `covenant/deploy.sh` are not digest installers. Each converges its whole host
to the repo, so on a host that was set up by hand or has drifted, the first run also changes things
that have nothing to do with the digest. Read every dry run for non-digest lines (steps 5 and 6 say
what to look for). Then either reconcile the drifted files first, or schedule the run for a time
when the side effects are acceptable. The digest part on its own restarts nothing that already
exists.

The deploy scripts read `site.env` from the root of the checkout they run from (`../site.env` next
to `walter/` or `covenant/`). If a host has no checkout, stage one first and put the same `site.env`
next to it, mode 0600:

```bash
git archive --prefix=lid-digest/ <commit> | ssh <host> tar -x -C '~'
scp -p site.env <host>:lid-digest/site.env && ssh <host> chmod 600 lid-digest/site.env
sha256sum site.env; ssh <host> sha256sum lid-digest/site.env       # must match
```

Step 9 removes these staged copies again.

## 1. site.env

In the real `site.env` on the workstation and in the checkout on **each** host (Walter, Covenant):

```bash
SPARK_DIGEST_HOST=digest.example.com          # your real name; this switches the digest on
# defaults, only add them to change them:
# DIGEST_PORT=3300                            # must stay 3300
# DIGEST_GROUP=digest-viewers
# OAUTH2_PROXY_DIGEST_CLIENT_ID=digest        # step 3 tells you whether to set it
```

`site.env.example` documents each variable. Check that each copy is otherwise complete and matches
what is live, because both deploys stop or misbehave on a stale value:
- **Walter:** `scripts/render.sh` refuses to render any template whose variables are empty (for
  example `RESTIC_BUCKET` / `RESTIC_REGION` for `walter/backup`), so the deploy stops at "render".
  `scripts/render.sh --check walter/<component>` lists the empty ones beforehand.
- **Covenant:** `WG_BACKEND_PUBLIC_KEY` must be Walter's real WireGuard public key
  (`sudo cat /etc/wireguard/publickey` on Walter; it is not a secret). With `CHANGEME`, the dry run
  only warns `WG_BACKEND_PUBLIC_KEY is CHANGEME` and the live deploy stops at step 2/7. With a wrong
  key, the deploy would rewrite the edge's `wg0` peer and cut the tunnel.

## 2. DNS

Add an A record `${SPARK_DIGEST_HOST}` → `${EDGE_PUBLIC_IP}`, then wait until it resolves:

```bash
dig +short ${SPARK_DIGEST_HOST}       # expected: ${EDGE_PUBLIC_IP}
```

Until step 6 the name gets no answer (the edge's catch-all returns 444); that is expected.

## 3. Pocket-ID group and client

The digest gets its own group and its own OIDC client, like every app behind Pocket-ID (see
[covenant/README.md](../../covenant/README.md#pocket-id-clients-and-groups)). In the Pocket-ID admin
UI at `https://${SPARK_ID_HOST}`:

1. **User groups → add** `${DIGEST_GROUP}` (that exact name; the friendly name can be anything).
   Add yourself, and anyone else who should read digests. Create the group **first**: the client
   below is restricted to it.
2. **OIDC clients → add**, mirroring the telemetry client:
   - name `Digest` (only a label) and, if you can, a **custom client id** `digest`;
   - callback URL `https://${SPARK_DIGEST_HOST}/oauth2/callback`; launch URL
     `https://${SPARK_DIGEST_HOST}`;
   - confidential (not a public client), PKCE on;
   - allowed user groups: `${DIGEST_GROUP}`.
3. The UI shows the **client secret** once. Keep the window open, or regenerate the secret later;
   step 6 needs it. Do not save it in a file or a chat.

**Find out the real client id.** oauth2-proxy-digest sends the client id, not the name. Pocket-ID
generates a random id (a UUID) unless you set a custom one when you create the client. If the id is
not `digest`, put the real one in every `site.env` as `OAUTH2_PROXY_DIGEST_CLIENT_ID` (it is not a
secret). Check it with Pocket-ID's public metadata endpoint, which needs no login:

```bash
curl -s -w ' %{http_code}\n' "https://${SPARK_ID_HOST}/api/oidc/clients/${OAUTH2_PROXY_DIGEST_CLIENT_ID:-digest}/meta"
#   expected: {"id":"<that id>","name":"Digest",...,"launchURL":"https://<digest host>",...} 200
#   404 "OIDC client not found": the id is wrong. Do not go on to step 6 until this returns 200.
```

The edge config takes the id from `site.env` at deploy time, and `--set-client-secret` does not
re-render the config. A wrong id is therefore only fixed by correcting `site.env` and running
`covenant/deploy.sh` again.

**Without the UI.** `/srv/webui/pocketid-bootstrap.py` does **not** create this group or client: it
only knows `open-webui` and the telemetry client. Use the API method in
[walter/README.md](../../walter/README.md#pocket-id--oidc-bootstrap) (step 3, temporary
`STATIC_API_KEY`). That method recreates the `pocket-id` container twice, so logins to every site
pause for a few seconds each time. Make sure `${DIGEST_GROUP}` exists and has its members **before**
you use it, so the two recreates are not spent on a run that stops at a missing group. For a
read-only look at the Pocket-ID database, use `python3`'s `sqlite3` module with a
`file:...?mode=ro` URI; Walter has no `sqlite3` CLI.

## 4. LiteLLM key `digest`

Append `digest` to `HARNESS_KEYS` in `site.env` (the same copies as step 1):

```bash
HARNESS_KEYS="claude-code codex hermes opencode open-webui digest"
```

`provision-keys.py` creates the virtual key (alias `digest`) during the live Walter deploy and
writes it to `/srv/gateway/keys/digest.key`. The deploy then copies it for the container.

## 5. Walter: deploy and seed

```bash
ss -ltn 'sport = :3300'                       # expected: only the header line (port free)
sudo walter/deploy.sh --dry-run --skip-packages
```

The dry run should show the digest items:
- the `/srv/digest` tree, the `ufw` and `WALTER-PUBLISHED` rules for 3300, and the digest stack;
- `provision-keys: would create harness key digest`;
- `WARN: digest key missing` and `WARN: digest not started: key missing`. These two are expected in
  the first **dry run** only, because the live run's `provision-keys.py` creates the key.

Then read it for **non-digest** changes:
- every file whose live copy differs from the repo is installed. The file list labels a file `new`
  both when it is missing and when it differs, and a comment-only difference counts;
- a changed file in a stack force-recreates that stack. For example, `/srv/gateway/hooks/*` →
  LiteLLM and Postgres (`--force-recreate --wait`), or `/srv/monitoring/prometheus/*` → monitoring.
  If the difference is only comments, you can avoid the recreate: back up the drifted live file,
  copy the rendered repo version over it, and dry-run again until gateway and monitoring show a
  plain `up -d`;
- every run runs `apt-get install` (skip it with `--skip-packages`), `ufw-rules.sh`, and
  `systemctl restart nvidia-persistenced`. Only `--no-start` skips that restart, and it also skips
  every compose stack, the digest included. Pick a time when the GPUs are idle;
- every run rebuilds the telemetry image (and the digest image, once it exists) with
  `up -d --build`. Even with every build layer cached, the new image gets a new ID, so the container
  is recreated (a few seconds). The dry run does not show this.

Then the live run:

```bash
sudo walter/deploy.sh --skip-packages
```

Expected in the output:
- `created harness key digest -> keys/digest.key`;
- `create   /srv/digest/secrets/digest-litellm-key (copy of /srv/gateway/keys/digest.key)`;
- `== digest` followed by `docker compose ... up -d ... --build`;
- no `digest: off`, no `WARN: digest ...`.

Check it on Walter: `curl -fsS http://${BACKEND_WG_IP}:3300/healthz` → `{"ok":true}`.

**Seed the watch state before the first run** (optional; skip it to start fresh). The first digest
then reports only what is new since an earlier tool's runs. From the workstation:

```bash
scp ~/.hermes/threat-intel-watches/default.json \
    ~/.hermes/ai-digest-watches/ai-security.json ~/.hermes/ai-digest-watches/ai-research.json \
    ${BACKEND_SSH_USER}@${BACKEND_LAN_IP}:/tmp/
ssh ${BACKEND_SSH_USER}@${BACKEND_LAN_IP} 'sudo install -d -o 10001 -g 10001 -m 0750 /srv/digest/state/state &&
  sudo install -o 10001 -g 10001 -m 0640 /tmp/default.json /tmp/ai-security.json /tmp/ai-research.json /srv/digest/state/state/ &&
  rm /tmp/default.json /tmp/ai-security.json /tmp/ai-research.json'
```

The path really is `state/state/`: the volume is `/srv/digest/state`, and the pipeline reads
`STATE_DIR/state/<watch>.json`.

The app now runs, but only the edge can reach it, over the tunnel. It can stay like this until step
6. You can already trigger a run from Walter (see the CLI row in step 8).

## 6. Covenant: deploy (issues the cert), client secret

Do **not** run `certs.sh` for the digest name before the deploy. `00-default` does not answer ACME
for it (its `:80 default_server` returns 444, and its ACME block lists only the apex, chat, api and
id names), so the http-01 challenge would fail. Instead, `deploy.sh` links an ACME-only port-80 stub
for the digest, issues the cert, then links the full site. All of this happens in one run, with no
downtime for the other sites.

Have three things ready before 6b:
- the client id has passed the step 3 `/meta` probe;
- the client secret is at hand;
- you can run 6c right after 6b.

Between 6b and 6c the full site is public and answers **500**. nginx's auth subrequest cannot reach
`127.0.0.1:4181`, because oauth2-proxy-digest has no client secret yet and is not running. This is
fail-closed (nothing reaches Walter), but internet scanners find a new name within minutes, so keep
the window short.

On the edge, from the repo checkout:

```bash
set -a; . ./site.env; set +a
sudo tar -czpf /root/pre-digest-$(date -u +%Y%m%dT%H%M%SZ).tar.gz \
  /etc/nginx /etc/oauth2-proxy* /etc/letsencrypt/renewal /etc/systemd/system/oauth2-proxy*   # rollback point

# 6a. dry run: read it
sudo covenant/deploy.sh --dry-run
#   4/7: no cert yet for optional site(s): 60-digest (<digest host>); ACME-only stub, no downtime
#        install .../sites-available/60-digest-acme, link sites-enabled/60-digest -> 60-digest-acme,
#        + nginx -t, + systemctl reload nginx, + certbot certonly ... -d <digest host> --key-type ecdsa ...
#   5/7: link sites-enabled/60-digest -> sites-available/60-digest once step 4 has issued the cert
#   6/7: oauth2-proxy-digest config + unit staged;
#        !! oauth2-proxy-digest NOT started: /etc/oauth2-proxy-digest/client-secret missing
```

Read 6a for non-digest lines too:
- `install /etc/wireguard/wg0.conf` means that the live file differs from the rendered one, even if
  only in a comment. Its diff is not shown, because the file holds the private key; the live run
  applies it with `wg syncconf`. Compare the two files with the private key masked, and check
  `WG_BACKEND_PUBLIC_KEY` (step 1). With the right key, a comment-only difference is harmless: the
  peer and `AllowedIPs` stay the same, and the tunnel does not drop;
- any other `install ...` with a diff under it is a non-digest file that the live run replaces;
- some lines appear in every dry run and are not changes. The dry run cannot see unit state, so it
  prints `+ systemctl start oauth2-proxy` even when the unit is running. The `ufw` rule list is
  printed every time too.

The dry run shows a diff only for files that already exist, so it never shows the new digest
config. Check its `client_id` by rendering the template yourself. (`render.sh` refuses it while
`DIGEST_GROUP` is unset, because that default lives in `deploy.sh`.)

```bash
( export DIGEST_GROUP=${DIGEST_GROUP:-digest-viewers} OAUTH2_PROXY_DIGEST_CLIENT_ID=${OAUTH2_PROXY_DIGEST_CLIENT_ID:-digest}
  envsubst '${DIGEST_GROUP} ${OAUTH2_PROXY_DIGEST_CLIENT_ID} ${SPARK_DIGEST_HOST} ${SPARK_ID_HOST}' \
    <covenant/oauth2-proxy/oauth2-proxy-digest.cfg.tmpl | grep -E '^(client_id|allowed_groups)' )
#   expected: client_id = "<the id that the step 3 /meta probe answers 200 for>"
#             allowed_groups = ["<DIGEST_GROUP>"]
```

Then deploy, and set the secret:

```bash
# 6b. deploy: stub -> cert -> full site in one run
sudo covenant/deploy.sh
#   4/7: the same lines as the dry run, then certbot's "Successfully received certificate"
#   5/7: link /etc/nginx/sites-enabled/60-digest -> /etc/nginx/sites-available/60-digest
#        remove /etc/nginx/sites-available/60-digest-acme
#   6/7: !! oauth2-proxy-digest NOT started: ... client-secret missing   (expected: the 500 window starts)
readlink /etc/nginx/sites-enabled/60-digest                                     # /etc/nginx/sites-available/60-digest
sudo certbot certificates --cert-name "$SPARK_DIGEST_HOST" | grep 'Key Type'   # Key Type: ECDSA

# 6c. right away: the client secret from step 3
sudo covenant/deploy.sh --set-client-secret --instance digest
```

`--set-client-secret` reads the secret from stdin with no echo. It stores it as
`/etc/oauth2-proxy-digest/client-secret` (root 0600) and restarts `oauth2-proxy-digest`, which ends
the 500 window. It checks `site.env` like a full run, so run it from the same checkout.

It can also read from a pipe, for example from a root-only file, without printing the secret. The
input then needs a trailing newline; without one, `read` fails at the end of the input, and the
script exits without storing anything. Delete any such file afterwards (`shred -u`). An existing
secret is only replaced with `--force` (see
[rotate-secrets](rotate-secrets.md#rotate-an-oauth2-proxy-client-secret)).

**If certbot fails in 6b** (the DNS record from step 2 does not resolve yet, or port 80 is blocked),
the deploy does not stop and takes nothing down. The digest stays on the stub, so there is no 500
window, and deploy warns `60-digest: no cert for <digest host> yet, so only its ACME stub is linked`,
with the next step. Fix the cause, then run
`sudo LETSENCRYPT_EMAIL="$LETSENCRYPT_EMAIL" covenant/scripts/certs.sh "$SPARK_DIGEST_HOST"` (the stub
now answers the challenge), and `sudo covenant/deploy.sh` again. That run prints
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

Covenant needs no new firewall rule: nginx already listens on 80/443, and oauth2-proxy-digest
listens on loopback.

## 8. Verification probes

Run these after 6c. Run them again after any later change to the digest: an app update, a secret
rotation, or a redeploy of either host.

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
| Covenant | `sudo journalctl -u oauth2-proxy-digest -n 20 --no-pager` | no errors since the last start |
| Covenant | `sudo nginx -t` | `syntax is ok`, `test is successful` |
| Covenant | `sudo certbot renew --dry-run --cert-name "$SPARK_DIGEST_HOST"` | all simulated renewals succeeded |
| anywhere | `curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' https://${SPARK_DIGEST_HOST}/` | `302` to `https://${SPARK_ID_HOST}/authorize?...`, with `client_id=<the step 3 id>` and `code_challenge_method=S256` |
| anywhere | `curl -s -w ' %{http_code}\n' https://${SPARK_DIGEST_HOST}/api/watches` | `{"error":"unauthorized"} 401` |
| anywhere | the same, with `-H 'X-Forwarded-User: admin' -H 'X-Auth-Request-User: admin'` | still `401`: client-supplied identity headers are ignored |
| anywhere | `curl -s -o /dev/null -w '%{http_code}\n' https://${SPARK_DIGEST_HOST}/healthz` | `302` (every path needs a session) |
| anywhere | `curl -s -o /dev/null -w '%{http_code}\n' http://${SPARK_DIGEST_HOST}/` | `301` (to https) |
| anywhere | `echo \| openssl s_client -connect ${SPARK_DIGEST_HOST}:443 -servername ${SPARK_DIGEST_HOST} 2>/dev/null \| openssl x509 -noout -text \| grep 'Public Key Algorithm'` | `Public Key Algorithm: id-ecPublicKey` |
| anywhere | `curl -sI https://${SPARK_DIGEST_HOST}/api/watches \| grep -i strict-transport` | `strict-transport-security: max-age=31536000; includeSubDomains` |
| anywhere | the apex, chat, id, api and telemetry probes in [covenant/README.md](../../covenant/README.md#verify) | the same codes as before the deploy |
| browser | open `https://${SPARK_DIGEST_HOST}`, sign in with a passkey | the dashboard with three watch cards |
| browser | a user **not** in `${DIGEST_GROUP}` signs in | refused: Pocket-ID does not admit them to the client, or oauth2-proxy answers 403. Both layers are configured (the client's allowed group, and `allowed_groups` in the proxy config), so checking the config is enough if you have no test user |
| browser | RUN NOW on each watch | progress streams `collecting` → `curating` → `done`, then the digest renders |
| browser | RUN NOW again on the same watch | only new items (often "No new items"); per-source cutoffs advance in `state/state/<watch>.json` |
| workstation | `ssh ${BACKEND_SSH_USER}@${BACKEND_LAN_IP} 'curl -fsS -X POST http://${BACKEND_WG_IP}:3300/api/runs/default/now'` | `{"run_id":"<YYYYmmddTHHMMSSZ>"}` (HTTP 202); 409 while that watch runs |
| Walter | `sudo docker compose -f /srv/digest/compose.yaml logs app \| grep -i 'curation failed'` | nothing; otherwise see [troubleshooting](../../walter/digest/README.md#troubleshooting) |
| LiteLLM | spend / request count of the key alias `digest` (LiteLLM `/key/info`, or the telemetry dashboard's per-key view) | grows with each curated run |

## 9. Clean up

- Remove the staged checkouts on both hosts (`rm -rf ~/lid-digest`), but keep the Covenant one
  until 6c has run.
- `shred -u` any file that the client secret passed through.
- Keep the backups (Covenant `/root/pre-digest-*.tar.gz`, any `*.bak-*` files on Walter) until the
  digest has run for a while, then delete them.

## Update the app (Walter only)

A change that touches only the app code under `walter/digest/build/` (Python, static files,
Dockerfile, requirements) does not need a full `walter/deploy.sh` with its whole-host side effects
(see [Before you start](#before-you-start-both-deploys-converge-the-whole-host)). Copy the build,
and rebuild the one stack:

```bash
# workstation, in a checkout of the commit to deploy
git diff --stat <live commit> HEAD -- walter/digest     # only build/ files? then this section applies
ssh ${BACKEND_SSH_USER}@${BACKEND_LAN_IP} \
  'sudo tar -C /srv/digest -czf /srv/digest/build.bak-$(date -u +%Y%m%dT%H%M%SZ).tgz build'   # rollback point
rsync -rcpi --delete --dry-run --rsync-path='sudo rsync' --chown=root:root --chmod=D755,F644 \
  walter/digest/build/ ${BACKEND_SSH_USER}@${BACKEND_LAN_IP}:/srv/digest/build/   # read the file list
rsync -rcpi --delete           --rsync-path='sudo rsync' --chown=root:root --chmod=D755,F644 \
  walter/digest/build/ ${BACKEND_SSH_USER}@${BACKEND_LAN_IP}:/srv/digest/build/
ssh ${BACKEND_SSH_USER}@${BACKEND_LAN_IP} 'cd /srv/digest && sudo docker compose up -d --build'
```

`up -d --build` rebuilds `walter-digest:local` and recreates only the `app` container. In-flight runs
are lost; `state/` and the run history stay. If a run is in progress, wait for `done` first.

A change to `walter/digest/compose.yaml.tmpl` also changes `/srv/digest/compose.yaml`, which is
rendered from `site.env`. Render it with `scripts/render.sh walter/digest/compose.yaml.tmpl <out>`,
back up the live file, install the render as root 0644, and run the same `up -d --build`. Anything
outside `walter/digest/` (the firewall scripts, the gateway key, the edge) needs the full deploy of
that host.

**Verify** on Walter, then from outside:

```bash
sudo docker compose -f /srv/digest/compose.yaml ps                   # app Up (healthy), created just now
curl -fsS http://${BACKEND_WG_IP}:3300/healthz                       # {"ok":true}
sudo docker compose -f /srv/digest/compose.yaml logs --since 5m app  # no tracebacks
```

Then run the "anywhere" rows of step 8 (302 / 401) and a passkey login. Use RUN NOW on at least the
watch that the change affects, and check that `done` arrives and nothing says "Curation failed".

**Roll back** an update:
`cd /srv/digest && sudo rm -rf build && sudo tar -xzf build.bak-<ts>.tgz && sudo docker compose up -d --build`.
Delete the `build.bak-*.tgz` once the update has proven itself; the nightly backup already covers
`/srv/digest`.

## Feed sources

The collectors fetch public feeds from Walter over the internet, so a source can fail for reasons
outside this system. A failed source shows up under "Coverage gaps" in that run's digest; the other
sources are unaffected.

- **CISA advisories** (threat-intel watch `default`) sits behind Akamai, which can answer **403** to
  one source IP for a while after heavy fetching (for example, many manual runs or collector dry
  runs from the same address in a short time). This is transient: the gap clears on its own after a
  pause, and changing the collector does not help. A 403 that lasts for days is a different
  problem; see "a source is always in Coverage gaps" in the app's
  [troubleshooting](../../walter/digest/README.md#troubleshooting).

## Rollback

Quick disable (minutes, nothing deleted):

```bash
# Covenant
sudo systemctl disable --now oauth2-proxy-digest
sudo rm /etc/nginx/sites-enabled/60-digest && sudo nginx -t && sudo systemctl reload nginx   # full site or stub
# Walter
cd /srv/digest && sudo docker compose down
```

Without the site, the name falls back to the 444 catch-all. Then remove `SPARK_DIGEST_HOST` from
every `site.env`, so later deploys skip the digest again. Walter's next deploy renders the firewall
scripts without the digest block; the ufw rule itself stays until it is deleted. The
`/root/pre-digest-*.tar.gz` from step 6 holds the edge files as they were before the deploy, to
compare against or to restore one by hand (extracting it does not remove files that the deploy
added).

Full removal (firewall rule, `docker-user-rules.sh` block, `/srv/digest`, edge files, cert,
Pocket-ID client and group, LiteLLM key, DNS): follow
[walter/digest/README.md#rollback](../../walter/digest/README.md#rollback). The firewall step there
deletes only the lines between `# >>> digest` and `# <<< digest`, and is checked with
`grep '^PORTS='` (the other published ports must still be listed).
