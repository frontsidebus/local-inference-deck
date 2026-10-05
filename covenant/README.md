# covenant/ - the edge

Covenant is the public edge: a small cloud VM (live: AWS EC2 t3.small, Ubuntu 24.04)
with an Elastic IP. It terminates TLS, rate-limits, and reverse-proxies over a
WireGuard tunnel to the backend (Walter), which sits behind NAT and dials out to the
edge. Nothing runs here but nginx, oauth2-proxy, WireGuard, ufw and fail2ban; all
application state lives on the backend.

```
Internet ──443──> nginx ──wg0 (EDGE_WG_IP -> BACKEND_WG_IP)──> backend
  SPARK_DOMAIN          301 -> chat.
  SPARK_CHAT_HOST       Open WebUI   :WEBUI_PORT     (websockets; /api/v1/auths/ 5r/m)
  SPARK_ID_HOST         Pocket-ID    :POCKETID_PORT  (auth 10r/m, OIDC 10r/s)
  SPARK_API_HOST        LiteLLM      :LITELLM_PORT   (/v1 allowlist, 401 w/o creds, 10r/s b40, 20 conns)
  SPARK_TELEMETRY_HOST  telemetry    :TELEMETRY_PORT behind oauth2-proxy (127.0.0.1:4180, Pocket-ID, TELEMETRY_GROUP)
  SPARK_DIGEST_HOST     digest       :DIGEST_PORT    behind oauth2-proxy-digest (127.0.0.1:4181, Pocket-ID, DIGEST_GROUP); optional
  unknown Host          444
```

## Layout

| Repo path | Installed as | Notes |
|---|---|---|
| `nginx/nginx.conf` | `/etc/nginx/nginx.conf` | Ubuntu stock, except `ssl_protocols TLSv1.2 TLSv1.3` |
| `nginx/conf.d/phase3-http.conf` | `/etc/nginx/conf.d/` | `server_tokens off`, limit zones, websocket + no-credentials maps |
| `nginx/snippets/{proxy,tls}-common.conf` | `/etc/nginx/snippets/` | proxy headers; certbot TLS params + HSTS etc. |
| `nginx/sites-available/*.tmpl` | `/etc/nginx/sites-available/{00-default,10-apex,20-chat,30-id,40-api,50-telemetry}` (+ `60-digest` when `SPARK_DIGEST_HOST` is set) | all symlinked into `sites-enabled/`; the stock `sites-enabled/default` link is removed (the package's `sites-available/default` file stays, unused) |
| `nginx/bootstrap/00-acme-bootstrap.tmpl` | `sites-available/00-acme-bootstrap` | temporary port-80-only site for the first certs |
| `nginx/bootstrap/60-digest-acme.tmpl` | `sites-available/60-digest-acme` | digest only: ACME-only port-80 stub that `sites-enabled/60-digest` points at until the digest cert exists (removed once the full site is linked) |
| `nginx/snippets/pocketid-setup-lock.conf.in` | `snippets/pocketid-setup-lock.conf` | optional; rendered by `scripts/setup-lock.sh` (one `allow` per admin IP) |
| `letsencrypt/renewal-hooks/deploy/reload-nginx.sh` | same path under `/etc/letsencrypt` | `nginx -t -q && systemctl reload nginx` |
| `oauth2-proxy/oauth2-proxy.cfg.tmpl` | `/etc/oauth2-proxy/oauth2-proxy.cfg` (root:oauth2-proxy 0640) | no secrets in it |
| `oauth2-proxy/oauth2-proxy.service.tmpl` | `/etc/systemd/system/oauth2-proxy.service` | hardened; secrets via `LoadCredential=` |
| `oauth2-proxy/oauth2-proxy-digest.cfg.tmpl` | `/etc/oauth2-proxy-digest/oauth2-proxy.cfg` (root:oauth2-proxy 0640) | optional second instance (digest gate, 127.0.0.1:4181; only when `SPARK_DIGEST_HOST` is set); no secrets in it |
| `oauth2-proxy/oauth2-proxy-digest.service.tmpl` | `/etc/systemd/system/oauth2-proxy-digest.service` | optional second instance; secrets via `LoadCredential=` |
| `fail2ban/jail.local.tmpl` | `/etc/fail2ban/jail.local` | sshd, nginx-limit-req, recidive; `banaction = ufw` |
| `fail2ban/jail.d/sshd-ubuntu.local` | `/etc/fail2ban/jail.d/` | Ubuntu 24.04 unit is `ssh.service`; without this the sshd jail matched nothing |
| `wireguard/wg0.conf.tmpl` | `/etc/wireguard/wg0.conf` (0600) | private key injected at install from `/etc/wireguard/privatekey` |
| `ssh/sshd_config.d/10-hardening.conf` | `/etc/ssh/sshd_config.d/` | `PermitRootLogin no`, no password / keyboard-interactive auth |
| `apt/apt.conf.d/20auto-upgrades` | `/etc/apt/apt.conf.d/` | unattended-upgrades on (`50unattended-upgrades` is the stock file, not customized) |
| `scripts/ufw.sh` | - | firewall rules |
| `scripts/certs.sh` | - | certbot issuance, one ECDSA cert per name |
| `scripts/setup-lock.sh` | - | Pocket-ID first-claim lock on/off |
| `deploy.sh` | - | the whole thing, idempotent |

`*.tmpl` files are rendered with `envsubst` and an **explicit** variable list (see
`COVENANT_VARS` in `deploy.sh`), so nginx's own `$host`, `$request_uri`, ... survive.

### site.env variables used

Shared: `SPARK_DOMAIN SPARK_CHAT_HOST SPARK_API_HOST SPARK_ID_HOST SPARK_TELEMETRY_HOST
LETSENCRYPT_EMAIL EDGE_WG_IP BACKEND_WG_IP WG_PORT ADMIN_SOURCE_IPS TELEMETRY_GROUP`
(`EDGE_PUBLIC_IP` is only needed for DNS / the backend's WireGuard `Endpoint`).

Covenant section: `WG_SUBNET` (fail2ban ignoreip), `WG_BACKEND_PUBLIC_KEY` (wg0 peer),
`WEBUI_PORT POCKETID_PORT LITELLM_PORT TELEMETRY_PORT` (upstreams on `BACKEND_WG_IP`),
`OAUTH2_PROXY_CLIENT_ID` (Pocket-ID client id for the telemetry gate). All are required.

Optional digest site: `SPARK_DIGEST_HOST` switches it on. Then `DIGEST_PORT` (default 3300),
`DIGEST_GROUP` (default `digest-viewers`) and `OAUTH2_PROXY_DIGEST_CLIENT_ID` (default `digest`)
are added to `COVENANT_VARS`. With `SPARK_DIGEST_HOST` empty none of them is needed and the deploy
is exactly the six-site one, plus a single `digest: off` line.

Both client ids must be the ids Pocket-ID really has, which are not always the client names (see
[Pocket-ID clients and groups](#pocket-id-clients-and-groups)). They are not secrets.

## Secrets

Generated on the host by `scripts/gen-secrets.sh covenant` (`scripts/secrets.d/covenant.sh`),
which `deploy.sh` calls. Existing files are never overwritten; values are never printed.

| Name | Path | Mode | Source |
|---|---|---|---|
| oauth2-proxy cookie secret | `/etc/oauth2-proxy/cookie-secret` | root:root 0600 | 32 random bytes, URL-safe base64, no newline |
| oauth2-proxy client secret | `/etc/oauth2-proxy/client-secret` | root:root 0600 | **from Pocket-ID** when the `OAUTH2_PROXY_CLIENT_ID` client is created; store with `deploy.sh --set-client-secret` |
| oauth2-proxy digest cookie secret | `/etc/oauth2-proxy-digest/cookie-secret` | root:root 0600 | ditto; the digest instance, only when `SPARK_DIGEST_HOST` is set |
| oauth2-proxy digest client secret | `/etc/oauth2-proxy-digest/client-secret` | root:root 0600 | **from Pocket-ID** for the `OAUTH2_PROXY_DIGEST_CLIENT_ID` client; store with `deploy.sh --set-client-secret --instance digest` |
| WireGuard private key | `/etc/wireguard/privatekey` (+ inlined into `wg0.conf`, 0600) | root:root 0600 | `wg genkey` |
| WireGuard public key | `/etc/wireguard/publickey` | 0644 | `wg pubkey`; not secret - give it to the backend |
| TLS keys | `/etc/letsencrypt/live/<name>/privkey.pem` | certbot | issued by `scripts/certs.sh` |

oauth2-proxy never reads the secret files directly: systemd's `LoadCredential=` hands the
unprivileged `oauth2-proxy` user a private copy under `%d`.

`--set-client-secret` (with `--instance digest` for the second instance) reads the secret from
stdin with no echo, writes it root 0600 without a trailing newline, and restarts the instance. It
checks `site.env` like a full run, so run it from the checkout that holds the edge's `site.env`. It
refuses to replace an existing secret unless `--force` is given. Piped input needs a trailing
newline: without one, `read` fails at the end of the input and the script exits without storing
anything. Rotation of every secret here: [docs/runbooks/rotate-secrets.md](../docs/runbooks/rotate-secrets.md).

## Pocket-ID clients and groups

Pocket-ID (on Walter) is the only identity provider. Every web app has **its own OIDC client and its
own group**, so access to one site never implies another:

| Site | Client id (`site.env`) | Group | Gate |
|---|---|---|---|
| `SPARK_CHAT_HOST` | `open-webui` | `CHAT_GROUP` (`chat-users`) | Open WebUI's own OIDC login (client secret on Walter) |
| `SPARK_TELEMETRY_HOST` | `OAUTH2_PROXY_CLIENT_ID` (`telemetry`) | `TELEMETRY_GROUP` (`telemetry-viewers`) | `oauth2-proxy` on 127.0.0.1:4180 |
| `SPARK_DIGEST_HOST` (optional) | `OAUTH2_PROXY_DIGEST_CLIENT_ID` (`digest`, or a UUID) | `DIGEST_GROUP` (`digest-viewers`) | `oauth2-proxy-digest` on 127.0.0.1:4181 |

Every client is set up the same way: confidential (it has a client secret), PKCE on (oauth2-proxy
sends `code_challenge_method=S256`), callback `https://<site>/oauth2/callback` (Open WebUI:
`/oauth/oidc/callback`), launch URL `https://<site>`, and **allowed user groups** set to the one
group. The oauth2-proxy instances check the group a second time (`allowed_groups`), so a user
outside it is refused even if the client restriction is ever lost. Giving someone a site means
adding them to its group: [docs/runbooks/add-user.md](../docs/runbooks/add-user.md).

**Client id vs name.** Pocket-ID shows a client's name, but oauth2-proxy sends its **id**. When you
create a client in the UI, Pocket-ID generates a random id (a UUID) unless you set a custom one. The
telemetry and open-webui clients were created with custom ids; a client created without one needs
its real id in `site.env`. Check an id with Pocket-ID's public metadata endpoint (no login):

```
curl -s -w ' %{http_code}\n' https://SPARK_ID_HOST/api/oidc/clients/<client id>/meta
# 200 {"id":"<client id>","name":...,"launchURL":...}   or   404 "OIDC client not found"
```

`walter/webui/pocketid-bootstrap.py` creates the chat and telemetry groups and clients; the digest
group and client are created in the UI (or with the API method in walter/README.md), as described in
[docs/runbooks/digest-deploy.md](../docs/runbooks/digest-deploy.md#3-pocket-id-group-and-client).

## Pinned versions

| What | Version | Checksum |
|---|---|---|
| oauth2-proxy | v7.15.5 (`oauth2-proxy-v7.15.5.linux-amd64.tar.gz`) | tarball sha256 `f63f94bf72c5f46ab002a0a275aa8b3cf19b4d828aed08a13978cb9a62c3a1fd`; binary sha256 `ed5063f5a655560048a594974210f1c9e4f539bbfe81a834addb6fc740f3309d` (= live `/usr/local/bin/oauth2-proxy`) |
| distro packages | Ubuntu 24.04 apt, not pinned | live: nginx 1.24.0-2ubuntu7.18, certbot 2.9.0-1, fail2ban 1.0.2-3ubuntu0.1, ufw 0.36.2-6, wireguard-tools 1.0.20210914-1ubuntu4, unattended-upgrades 2.9.1+nmu4ubuntu1 |

## AWS (one-time, by hand)

- **Instance:** t3.small (2 vCPU / 2 GiB is plenty; oauth2-proxy is capped at 96M), Ubuntu 24.04, default `ubuntu` user with your key pair.
- **Elastic IP** associated with the instance = `EDGE_PUBLIC_IP`. Point DNS A records for
  `SPARK_DOMAIN`, `SPARK_CHAT_HOST`, `SPARK_API_HOST`, `SPARK_ID_HOST`, `SPARK_TELEMETRY_HOST`
  (and `SPARK_DIGEST_HOST` if the digest is on) at it.
- **Security group, inbound** (outbound: default allow-all):

  | Proto | Port | Source | Why |
  |---|---|---|---|
  | tcp | 22 | each of `ADMIN_SOURCE_IPS`/32 | SSH (ufw allows 22 from anywhere; the SG is the narrow gate) |
  | tcp | 80 | 0.0.0.0/0, ::/0 | ACME http-01 + redirect to https |
  | tcp | 443 | 0.0.0.0/0, ::/0 | sites |
  | udp | `WG_PORT` | 0.0.0.0/0 (or the backend's egress IP if it is static) | WireGuard. **udp only** - there is no tcp/WG_PORT rule |

## First deploy (fresh instance)

1. AWS as above; DNS resolving to the Elastic IP for all five names (six with the digest).
2. On the edge: clone the repo, `cp site.env.example site.env`, fill it in.
   `WG_BACKEND_PUBLIC_KEY` comes from the backend (`wg pubkey < /etc/wireguard/privatekey` there).
   It must be the real key: with `CHANGEME` the dry run only warns, and the live run stops at step
   2/7.
3. `sudo covenant/deploy.sh --dry-run`, read it, then
   `sudo covenant/deploy.sh --setup-lock` (the lock is only for step 5's window). It runs, in order:
   1. packages, sshd hardening (`sshd -t` then reload), unattended-upgrades, secrets;
   2. WireGuard `wg0` (enabled + started);
   3. ufw (22, 80, 443/tcp, `WG_PORT`/udp; default deny in);
   4. nginx base config, then **TLS bootstrap**: because every 443 server needs a cert that
      does not exist yet, all sites are disabled and only `00-acme-bootstrap` (port 80:
      ACME webroot for all names, everything else 444) is enabled; `scripts/certs.sh`
      then issues one ECDSA cert per name:
      ```
      certbot certonly --webroot -w /var/www/letsencrypt --cert-name NAME -d NAME --key-type ecdsa \
        --deploy-hook /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh \
        -m "$LETSENCRYPT_EMAIL" --agree-tos --no-eff-email --non-interactive --keep-until-expiring
      ```
      (`--staging-certs` rehearses against the LE staging CA.) certbot's
      `options-ssl-nginx.conf` / `ssl-dhparams.pem` are seeded from the package because
      `certonly` never writes them and `tls-common.conf` includes them;
   5. the six full sites, seven with the digest (bootstrap link removed, `nginx -t`, reload; on failure `/etc/nginx`
      is restored from `/var/backups/covenant/nginx-<ts>.tar.gz`);
   6. oauth2-proxy (telemetry), plus oauth2-proxy-digest when the digest is on: system user,
      verified binary, configs, units (each started only once its client secret exists);
   7. fail2ban (`fail2ban-client --test`, restart).
4. Give the backend the edge public key (`/etc/wireguard/publickey`) and `EDGE_PUBLIC_IP:WG_PORT`
   as its peer endpoint. The backend initiates (it is behind NAT) and keeps the tunnel alive with
   `PersistentKeepalive`; the edge's peer entry deliberately has no `Endpoint`. Check: `sudo wg show`
   shows a recent handshake; `ping BACKEND_WG_IP`.
5. **Immediately** claim the Pocket-ID admin at `https://SPARK_ID_HOST/setup` from an admin IP
   (everyone else gets 403 while the lock is on), then `sudo covenant/deploy.sh` without
   `--setup-lock` (or `covenant/scripts/setup-lock.sh disable`).
6. In Pocket-ID create the OIDC client for the telemetry gate: client id `OAUTH2_PROXY_CLIENT_ID`,
   callback `https://SPARK_TELEMETRY_HOST/oauth2/callback`, confidential, PKCE on, allowed group
   `TELEMETRY_GROUP` (`walter/webui/pocketid-bootstrap.py` does this). Check the id with the `/meta`
   probe ([Pocket-ID clients and groups](#pocket-id-clients-and-groups)), then
   `sudo covenant/deploy.sh --set-client-secret` (paste; no echo) - this starts oauth2-proxy.
   The digest gate (optional) is set up the same way - see the Digest section below.

**Re-running on a live edge.** `deploy.sh` converges the whole edge, not just what you changed, so
read every dry run for unexpected lines. `install /etc/wireguard/wg0.conf` appears whenever the live
file differs from the render, even only in a comment, and without a diff (the file holds the private
key); compare the two with the key masked before the live run, which applies it with
`wg syncconf`. A few lines are printed by every dry run and are not changes: the `ufw` rule list, and
`+ systemctl start oauth2-proxy` (the dry run cannot see unit state).

Adding a new hostname later: add the site + its name to `deploy.sh`. A core name (in `NAMES`)
with a missing cert makes the next run fall back to the bootstrap site, which it refuses to do on
a live host unless you pass `--allow-bootstrap-downtime` (443 is down for the minute that takes).
An optional site instead goes in `OPT_SITE_HOST` with an ACME-only stub
`nginx/bootstrap/<site>-acme.tmpl`; like the digest, it is enabled without downtime (see below).

## Digest (optional; second oauth2-proxy instance)

The digest app on Walter ([walter/digest/README.md](../walter/digest/README.md)) is published by
`60-digest` and gated by a **second oauth2-proxy instance** on `127.0.0.1:4181`: the same binary,
with its own config dir (`/etc/oauth2-proxy-digest`, root:oauth2-proxy 0750), cookie and client
secrets, unit (`oauth2-proxy-digest`), OIDC client and group. A telemetry session does not open the
digest and vice versa. The owner checklist, with probes, is
[docs/runbooks/digest-deploy.md](../docs/runbooks/digest-deploy.md).

**Opt-in.** Everything below is skipped unless `SPARK_DIGEST_HOST` is set in `site.env`:
`60-digest` is neither rendered nor linked, no cert is requested for it, no digest secret is
generated, and `oauth2-proxy-digest` is not installed. The deploy prints one `digest: off` line and
is otherwise identical to a deploy without the feature. If the digest is off but
`sites-enabled/60-digest` still exists (a half-done rollback), deploy warns and leaves it alone.

Enabling it on a live edge:

1. `site.env`: set `SPARK_DIGEST_HOST` (and `DIGEST_GROUP` / `OAUTH2_PROXY_DIGEST_CLIENT_ID` if
   the defaults do not suit). The DNS A record must already resolve to `EDGE_PUBLIC_IP`. Create the
   Pocket-ID group and client (step 4) **before** the deploy, so that the client id in `site.env` is
   the real one (`/meta` probe) and the client secret is at hand for step 5.
2. **Do not issue the cert by hand first.** `00-default` does not answer ACME for the digest
   name: its `:80 default_server` returns 444 and its ACME block lists only the apex, chat, api
   and id names. The digest's own `:80` ACME block lives in `60-digest`, next to a `:443` server
   that needs the cert, so linking the full site before the cert exists fails `nginx -t`.
   `deploy.sh` resolves this itself, with no downtime and no manual nginx edit (step 3).
3. `sudo covenant/deploy.sh --dry-run`, read it, then `sudo covenant/deploy.sh`. With the core
   certs present and only the digest cert missing, step 4/7 prints
   `no cert yet for optional site(s): 60-digest (...); ACME-only stub, no downtime`, links
   `sites-enabled/60-digest` to the port-80-only stub `60-digest-acme`, runs `nginx -t` and a
   reload (all other sites stay up), then issues the cert with `scripts/certs.sh` (one ECDSA cert,
   like every other name). Step 5/7 then links the full `60-digest` and removes the stub. It also
   generates `/etc/oauth2-proxy-digest/cookie-secret`, installs the config and unit, and warns that
   the instance is not started (no client secret yet). In the dry run, step 5/7 says the full site
   is linked once step 4 has issued the cert.

   **From here until step 5 the full site is public and answers 500**: nginx's auth subrequest
   cannot reach 127.0.0.1:4181 yet. That is fail-closed (nothing reaches the backend), but run
   step 5 right away.

   If certbot fails (DNS not resolving yet, port 80 blocked), the deploy does not stop: the digest
   stays on the stub (the name answers ACME and nothing else) and deploy prints the next step,
   `certs.sh <digest host>` and then `deploy.sh` again. The re-run finds the cert and links the
   full site. Check the cert with
   `sudo certbot certificates --cert-name "$SPARK_DIGEST_HOST"` (`Key Type: ECDSA`).
4. The Pocket-ID group `DIGEST_GROUP` and OIDC client (do this before step 3, see step 1): callback
   `https://SPARK_DIGEST_HOST/oauth2/callback`, confidential, PKCE on, allowed group `DIGEST_GROUP`.
   If Pocket-ID gave it a UUID id, that UUID is `OAUTH2_PROXY_DIGEST_CLIENT_ID`. A wrong id is only
   fixed by correcting `site.env` and re-running `deploy.sh`: `--set-client-secret` does not
   re-render the config.
5. `sudo covenant/deploy.sh --set-client-secret --instance digest` (paste; no echo). It stores
   `/etc/oauth2-proxy-digest/client-secret` (root 0600) and restarts `oauth2-proxy-digest`, which
   ends the 500 window.

`--instance` takes `telemetry` (the default) or `digest` and only matters with
`--set-client-secret`. It needs a value (`--instance` alone exits 2 with a message), and
`--instance digest` is refused while the digest is off.

## Verify

```
sudo nginx -t && systemctl is-active nginx wg-quick@wg0 oauth2-proxy fail2ban ufw
systemctl is-active oauth2-proxy-digest   # only with the digest on
ss -ltn | grep -E ':418[01] '             # 127.0.0.1:4180 (and 127.0.0.1:4181 with the digest)
ls -l /etc/nginx/sites-enabled/           # 00-default ... 50-telemetry (+ 60-digest), all -> sites-available
sudo wg show wg0 latest-handshakes
sudo ufw status verbose                   # 22, 80, 443/tcp + WG_PORT/udp; plus any fail2ban REJECT lines
sudo fail2ban-client status            # jails: nginx-limit-req, recidive, sshd
sudo certbot certificates; sudo certbot renew --dry-run   # every cert Key Type: ECDSA
curl -sI https://SPARK_DOMAIN | grep -i location                     # -> https://SPARK_CHAT_HOST/...
curl -s -o /dev/null -w '%{http_code}\n' https://SPARK_API_HOST/v1/models   # 401 (no key)
curl -s -o /dev/null -w '%{http_code}\n' https://SPARK_API_HOST/metrics     # 404
curl -s https://SPARK_TELEMETRY_HOST/api/v1/telemetry                       # {"error":"unauthorized"}
curl -s https://SPARK_DIGEST_HOST/api/watches                                # {"error":"unauthorized"} (digest on)
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' https://SPARK_DIGEST_HOST/  # 302 -> SPARK_ID_HOST/authorize?client_id=...
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: nope' -k https://EDGE_PUBLIC_IP/   # 000 (444)
sudo sshd -T | grep -E 'permitrootlogin|passwordauthentication'               # no / no
```

Every renewal config in `/etc/letsencrypt/renewal/` should say `authenticator = webroot`,
`key_type = ecdsa`, the ACME v2 production `server`, and the `reload-nginx.sh` hook. A cert issued
outside `certs.sh` may also carry `installer = nginx`; that is harmless (renewal still uses the
webroot, and the installer only reloads nginx afterwards).

## Rollback

- nginx: each live deploy first snapshots `/etc/nginx` to `/var/backups/covenant/nginx-<ts>.tar.gz`;
  `tar -C / -xzf <that>` then `nginx -t && systemctl reload nginx`.
- oauth2-proxy: `systemctl disable --now oauth2-proxy` takes the telemetry site down to
  500s on auth_request (no data leaks); disable `sites-enabled/50-telemetry` to remove it.
  The digest instance is the same: `systemctl disable --now oauth2-proxy-digest` and remove
  `sites-enabled/60-digest` (whether it points at the full site or the `60-digest-acme` stub), then clear `SPARK_DIGEST_HOST` so later deploys skip it. Full removal
  (files, secrets dir, cert): [walter/digest/README.md#rollback](../walter/digest/README.md#rollback).
- WireGuard / fail2ban / ufw: previous files are not kept by deploy.sh; they are small and fully
  described by this directory, so re-render from a known-good commit.

## Local test (no host needed)

```
DESTDIR=/tmp/covenant-stage covenant/deploy.sh --env /path/to/test-site.env
```
installs every rendered file under the prefix (staging-quality dummy secrets, oauth2-proxy
downloaded and checksum-verified) and only prints the host commands. `--dry-run` shows unified
diffs against an existing tree (never for `wg0.conf`, which holds the private key).
