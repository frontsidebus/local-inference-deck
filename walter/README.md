# walter/ — the backend VM

Walter is the inference and application backend: an Ubuntu 24.04 VM with two NVIDIA GPUs
passed through and a separate XFS disk for models. It is reachable from the internet only
through the edge (Covenant) over WireGuard. Everything here is generic. Site values come from
`../site.env` (template: `../site.env.example`) and secrets are generated on the host.

```
edge (Covenant) ──wg0──► BACKEND_WG_IP
                           :3000  Open WebUI      (/srv/webui, docker)   ─┐
                           :1411  Pocket-ID       (/srv/webui, docker)    │ DOCKER-USER chain
                           :4000  LiteLLM         (/srv/gateway, docker) ─┘ WALTER-PUBLISHED: only EDGE_WG_IP
                           :3200  telemetry app   (/srv/telemetry, host network; ufw: only EDGE_WG_IP on wg0)
                           :3300  digest app      (/srv/digest, host network; ufw: only EDGE_WG_IP on wg0; optional)
                           :8080  llama-swap      (systemd, user llamaswap; ufw: only GATEWAY_DOCKER_SUBNET)
                                    └─► llama-server containers on 127.0.0.1:<dynamic> (GPU0 / GPU1 / both)
                    127.0.0.1:3001/9090/...  monitoring (/srv/monitoring, host network, SSH tunnel only)
```

Request path: Open WebUI → LiteLLM `:4000` (virtual keys, aliases `coder`, `coder-fast`, `big`,
`vision`, `hermes`, `local/*`, `claude-*`; every request gets a bounded output limit, see
[Output cap](#output-cap-no-unbounded-generations)) → llama-swap `:8080` → llama.cpp `llama-server`
(image pinned by digest). Models and the GPU matrix are in `llama-swap/config.yaml.tmpl` and
[`models.md`](models.md).

## Layout (repo → host)

| repo | host | notes |
|---|---|---|
| `base/nvidia-persistenced.service.d/override.conf` | `/etc/systemd/system/nvidia-persistenced.service.d/` | persistence mode on, `StopWhenUnneeded=false` |
| `base/fstab-models.tmpl` | line in `/etc/fstab` | `UUID=${MODELS_FS_UUID}` XFS at `${MODELS_DIR}`, `nofail` |
| `wireguard/wg0.conf.tmpl` | `/etc/wireguard/wg0.conf` (0600) | client side; `@WG_PRIVATE_KEY@` filled by gen-secrets |
| `llama-swap/config.yaml.tmpl` | `/etc/llama-swap/config.yaml` (root:llamaswap 0640) | `--watch-config`: edits reload by themselves |
| `llama-swap/llama-swap.service.tmpl` | `/etc/systemd/system/llama-swap.service` | binds `${BACKEND_WG_IP}:8080`, API key from `/etc/llama-swap/api-key` |
| `llama-swap/spark-docker-run.sh` | `/usr/local/sbin/` | waits for a same-named container to be gone (safe reloads) |
| `llama-swap/fetch-models.sh.tmpl` | `${MODELS_DIR}/fetch-models.sh` | GGUF downloader, see `models.md` |
| `gateway/*` | `/srv/gateway/` | LiteLLM + Postgres 17 compose, `litellm.yaml`, `hooks/spark_hooks.py`, `.env.example`, `provision-keys.py` |
| `webui/*` | `/srv/webui/` (0750) | Open WebUI + Pocket-ID compose, `.env.example`, `pocket-id.env.example`, `theme/`, `pocketid-bootstrap.py` |
| `monitoring/*` | `/srv/monitoring/` | Prometheus, Grafana, node-exporter, cAdvisor, dcgm-exporter, blackbox, llama-swap SD sidecar ([README](monitoring/README.md.tmpl)) |
| `telemetry/*` | `/srv/telemetry/` | Starlette dashboard app + backup-status sidecar ([README](telemetry/README.md.tmpl)) |
| `digest/*` | `/srv/digest/` | **optional** (only when `SPARK_DIGEST_HOST` is set): on-demand intelligence digest app (host network, uid 10001), `secrets/` + `state/` ([README](digest/README.md)) |
| `backup/*` | `/usr/local/sbin/spark-backup*.sh`, units, `${MODELS_DIR}/backups/RESTORE.md` | nightly local backup + non-destructive restore test ([RESTORE](backup/RESTORE.md.tmpl)) |
| `backup/spark-offsite*`, `backup/offsite-setup.sh` | `/usr/local/sbin/spark-offsite.sh`, `spark-offsite.{service,timer}`, `/usr/local/bin/restic`, `/etc/spark-restic/` | optional encrypted offsite copy of the backups (restic to S3, bucket-scoped IAM user), when `RESTIC_BUCKET` is set ([OFFSITE](backup/OFFSITE.md)) |
| `update-check/*` | `/usr/local/sbin/spark-update-check.py`, units, `/etc/update-motd.d/90-spark-updates`, `/var/lib/spark-update-check/README.md` | weekly report-only update/advisory check ([README](update-check/README.md.tmpl)) |
| `firewall/docker-user-rules.sh.tmpl` + `.service` | `/usr/local/sbin/`, `/etc/systemd/system/` | `WALTER-PUBLISHED` chain in DOCKER-USER |
| `firewall/ufw-rules.sh.tmpl` | `/usr/local/sbin/ufw-rules.sh` | ufw defaults + the two INPUT allows |
| `hermes/*` | `~${BACKEND_SSH_USER}/.local/bin/hermes-spark`, merged into `~/.hermes/config.yaml` | optional, `--with-hermes` |

`*.tmpl` files are rendered by `../scripts/render.sh` (envsubst with an explicit variable list).
Templates of installed docs (`*.md.tmpl`) render to the host copy of the doc.

### Pinned versions (as live)

| component | pin |
|---|---|
| llama-swap | v260 (`fcefa7b`), binary sha256 `1392a6cdb3fec96845d091254b43ed42768082ed9c511e5708ed4a1d1ba95101` (`deploy.sh` verifies it) |
| llama.cpp server | `ghcr.io/ggml-org/llama.cpp:server-cuda@sha256:7149a45c…` (build 11277), in `config.yaml.tmpl` |
| LiteLLM | v1.103.1 by digest; Postgres 17 by digest (`gateway/compose.yaml.tmpl`) |
| Open WebUI / Pocket-ID | v0.11.4 / v2.16.0, tag + digest (`webui/compose.yaml.tmpl`) |
| monitoring / telemetry images | all tag + digest in their compose files; Python deps fully pinned in `telemetry/build/requirements.txt` |
| NVIDIA driver | `nvidia-driver-580` from the Ubuntu archive (live: 580.178.04-0ubuntu0.24.04.1, DKMS) |
| NVIDIA container toolkit | 1.20.1-1 from NVIDIA's apt repo (`deploy.sh` pins it) |
| Docker | Ubuntu `docker.io` (live 29.1.3) + `docker-compose-v2` (live 2.40.3) |
| Hermes Agent (optional) | tag `v2026.9.24` (v0.21.5) |
| restic (offsite backups) | 0.19.1 release binary, sha256 `f4154156…` of the `.bz2` (`backup/offsite-setup.sh` verifies it) |

## Prerequisites

- Fresh Ubuntu 24.04 VM, two NVIDIA GPUs passed through (live: 2x RTX 3090, 16 vCPU, 80 GiB RAM).
  GPU0 should be the faster PCIe link: single-GPU models are pinned by index in `config.yaml.tmpl`.
- A second disk for models and backups (live: a raw NVMe given to the VM as `virtio-models-990pro`).
  Format it once, by stable path, and put its UUID in `site.env`:
  ```bash
  sudo mkfs.xfs -L models /dev/disk/by-id/virtio-<serial>     # NOT /dev/vdX or /dev/nvmeXn1
  sudo blkid -s UUID -o value /dev/disk/by-id/virtio-<serial> # -> MODELS_FS_UUID
  ```
  The disk is mounted `nofail`, so the VM still boots without it (llama-swap and backups won't work).
- The edge's WireGuard public key in `WG_EDGE_PUBLIC_KEY` (from `covenant/`).
- `site.env` filled in (walter uses the identity/network/access sections plus `# --- walter`).

## Deploy

```bash
git clone <this repo> && cd <repo> && cp site.env.example site.env && $EDITOR site.env
sudo walter/deploy.sh --dry-run          # what would change
sudo walter/deploy.sh                    # do it (re-run any time; idempotent)
```

`deploy.sh` steps: render → apt packages (Docker, WireGuard tools, ufw, NVIDIA driver 580, container
toolkit 1.20.1-1, `nvidia-ctk runtime configure`) → `llamaswap` system user (uid 995/gid 987 when
free) and `docker` group membership → fstab line + mount for `${MODELS_DIR}` → llama-swap binary
(download, sha256 check) → install files → create `.env` files from their `.example` (never
overwrites; reports drift) → `scripts/gen-secrets.sh walter` → enable units
(`nvidia-persistenced`, `wg-quick@wg0`, `docker-user-rules`, `docker`, `llama-swap`,
`spark-backup.timer`, `spark-update-check.timer`) and run `ufw-rules.sh` → offsite backups
(`backup/offsite-setup.sh`, only when `RESTIC_BUCKET` is set and `/etc/spark-restic/aws.env` exists) → compose stacks in order:
gateway (`--wait`) → `provision-keys.py` → webui (`--wait`) → monitoring → telemetry (`--build`) →
digest (key copy, `--build`; only when `SPARK_DIGEST_HOST` is set).
A stack is force-recreated only when one of its files changed.

Options: `--dry-run`, `--destdir DIR` (install under a prefix and skip every system action — useful
for review), `--skip-packages`, `--no-start`, `--with-hermes`, `--site-env FILE`.

A new NVIDIA driver needs a reboot. On a fresh VM: deploy, reboot, deploy again.

### After the first deploy

1. **WireGuard.** `cat /etc/wireguard/publickey` → `WG_BACKEND_PUBLIC_KEY` in site.env, then deploy
   the edge. Check: `sudo wg show wg0` shows a recent handshake.
2. **Models.** `sudo -u ${BACKEND_SSH_USER} ${MODELS_DIR}/fetch-models.sh` (about 117 GB). llama-swap
   preloads `coder` and `coder-fast` at start. After the download, run `sudo systemctl restart llama-swap`.
   The startup preload logs a harmless `status 404` even though both models load.
3. **LiteLLM keys.** `provision-keys.py` already created `/srv/gateway/keys/<name>.key` (root 0600) for
   every name in `HARNESS_KEYS` and `SPARK_USERS` (rpm 60, 4 parallel requests, alias = name).
   Hand each key to its harness or user (clients/ expects them in `~/.config/spark/<harness>.key`).
   Add a name to site.env and re-run deploy to add a key. Admin UI: `http://${BACKEND_WG_IP}:4000/ui`,
   user `admin`, password = `LITELLM_MASTER_KEY` from `/srv/gateway/.env`.
4. **Pocket-ID / OIDC bootstrap** (below).
5. **Open WebUI break-glass admin** (below).
6. **Open WebUI model access.** v0.11 hides models that have no access grant from non-admins. In
   Admin → Models, give `coder`, `coder-fast`, `big`, `vision` and `hermes` public read access. Leave
   `hermes-agent` (the workstation Hermes connection) without grants, so only admins see it.
   Optionally hide the `local/*` entries.
7. **Hermes connection (optional).** Copy the workstation hermes-gateway's `API_SERVER_KEY`
   (clients/hermes) into `/srv/webui/hermes-gateway.key` (root 0600) and re-run deploy. `provision-keys.py`
   then replaces the `PENDING` slot in `OPENAI_API_KEYS`. If you don't use it, the second connection
   simply stays unreachable.

### Pocket-ID / OIDC bootstrap

1. **Claim the admin first.** As soon as `https://${SPARK_ID_HOST}` is live, open
   `https://${SPARK_ID_HOST}/setup` and register the admin passkey. Until then anyone could claim
   it. The edge restricts the setup paths to `ADMIN_SOURCE_IPS` until you remove the lock (see covenant/).
2. **Groups, clients, users:** `sudo /srv/webui/pocketid-bootstrap.py [--users FILE] [--dry-run]`.
   - It creates the groups `${CHAT_GROUP}` and `${TELEMETRY_GROUP}`.
   - It creates the OIDC client `open-webui` (confidential, PKCE, callback
     `https://${SPARK_CHAT_HOST}/oauth/oidc/callback`, allowed group `${CHAT_GROUP}`) and writes its
     secret into `/srv/webui/.env` `OAUTH_CLIENT_SECRET`. Open WebUI is recreated.
   - It creates the OIDC client `${OAUTH2_PROXY_CLIENT_ID}` (PKCE, callback
     `https://${SPARK_TELEMETRY_HOST}/oauth2/callback`, allowed group `${TELEMETRY_GROUP}`) and writes
     its secret to `/srv/webui/oidc/${OAUTH2_PROXY_CLIENT_ID}.client-secret`. Give it to the edge with
     `covenant/deploy.sh --set-client-secret`.
   - `--users FILE`, with lines `username email [group,group]`, creates users, adds them to groups,
     and writes 24-hour one-time login links to `/srv/webui/oidc/login-links.txt` (0600). Send each
     link to its user, who registers a passkey. Delete the file afterwards.
   - The script talks to `http://${BACKEND_WG_IP}:1411` using the temporary-key method below, and
     always removes the key at the end.
3. **Manual equivalent (temporary `STATIC_API_KEY`).** Use this if the script fails, for example
   after a Pocket-ID API change:
   ```bash
   K=$(openssl rand -hex 32)
   echo "STATIC_API_KEY=$K" | sudo tee -a /srv/webui/pocket-id.env >/dev/null
   sudo docker compose -f /srv/webui/compose.yaml up -d pocket-id
   curl -s -H "X-API-KEY: $K" http://${BACKEND_WG_IP}:1411/api/user-groups        # ...and other API calls
   sudo sed -i '/^STATIC_API_KEY=/d' /srv/webui/pocket-id.env                    # ALWAYS remove it
   sudo docker compose -f /srv/webui/compose.yaml up -d pocket-id; unset K
   ```
   The UI route works as well: Pocket-ID admin → Groups, then OIDC Clients (enable PKCE, set the
   callback, restrict to the group, generate the secret).
4. **Promote yourself.** The first OIDC login creates an Open WebUI account with role `user`. Promote
   it to admin from the break-glass admin account.

### Open WebUI break-glass admin

`ENABLE_SIGNUP=false` and login is through Pocket-ID. The local admin `${ADMIN_EMAIL}` (password
in `/srv/webui/admin-password`, generated) is the fallback. To create it once, before or right
after go-live:

1. Open a tunnel: `ssh -L 3000:${BACKEND_WG_IP}:3000 ${BACKEND_SSH_USER}@${BACKEND_LAN_IP}`. Host-originated
   traffic isn't filtered by `WALTER-PUBLISHED`. Then open `http://localhost:3000`.
2. If the sign-up form is refused because signup is disabled, set `ENABLE_SIGNUP=true` in
   `/srv/webui/.env` for the moment and run `docker compose up -d open-webui`.
3. Sign up with `${ADMIN_EMAIL}` and the generated password. The first account becomes admin.
4. Set `ENABLE_SIGNUP=false` again and run `docker compose up -d open-webui`.

`ENABLE_PERSISTENT_CONFIG=false`, so `.env` is the source of truth for every setting it names.

### Hermes Agent on Walter (optional)

Live Walter also runs the Hermes CLI for `BACKEND_SSH_USER`, pointed at LiteLLM over wg0.
1. Install Hermes at the pinned tag. Do not use `hermes update`, because it follows `main`. Use the
   upstream installer, then `git -C ~/.hermes/hermes-agent checkout v2026.9.24`.
2. Run `sudo walter/deploy.sh --with-hermes`. This installs `~/.local/bin/hermes-spark` and copies
   `/srv/gateway/keys/hermes.key` to `~/.config/spark/hermes.key` (0600). It then merges
   `hermes/config.spark.yaml` (`model`, `custom_providers.spark`, `security.redact_secrets`) into
   `~/.hermes/config.yaml`, keeping a timestamped backup.
3. Use `hermes-spark` (plain `hermes` gets a 401).

## Digest app (optional)

On-demand intelligence digests (threat intel / AI security / AI research), published as
`https://${SPARK_DIGEST_HOST}` through Covenant (nginx + a second oauth2-proxy instance). Full
documentation: [digest/README.md](digest/README.md); deploy checklist:
[docs/runbooks/digest-deploy.md](../docs/runbooks/digest-deploy.md).

1. **Off by default.** With `SPARK_DIGEST_HOST` empty, `deploy.sh` prints one
   `digest: off` line and skips everything else: nothing is rendered or installed under
   `/srv/digest`, the firewall scripts are rendered without their `# >>> digest` blocks (byte-identical
   to a deploy without the app), and no stack is started.
2. **Deploy.** Set `SPARK_DIGEST_HOST` and add `digest` to `HARNESS_KEYS` so `provision-keys.py`
   creates `/srv/gateway/keys/digest.key`. `deploy.sh` then copies it to
   `/srv/digest/secrets/digest-litellm-key` (0440 root:10001, never printed; kept if present), adds
   port 3300 to ufw (`wg0`, from `${EDGE_WG_IP}` only) and to `WALTER-PUBLISHED`, and starts the
   stack (`--build`). Without the key the stack is not started and deploy warns.
3. **Verify (from Walter).** `curl -fsS http://${BACKEND_WG_IP}:3300/healthz` → `{"ok":true}`;
   `cd /srv/digest && sudo docker compose ps` (healthy).
4. **Rollback.** See [digest/README.md#rollback](digest/README.md#rollback). The firewall part
   deletes only the `# >>> digest` … `# <<< digest` block of `docker-user-rules.sh`, so the other
   published ports stay filtered.

## Secrets

Generated on the host by `scripts/gen-secrets.sh walter` (spec: `scripts/secrets.d/walter.sh`).
Existing values are never overwritten and never printed.

| secret | path | mode / owner | how |
|---|---|---|---|
| WireGuard private key | `/etc/wireguard/privatekey` (+ filled into `wg0.conf`) | 0600 root | `wg genkey` |
| WireGuard public key (not secret) | `/etc/wireguard/publickey` | 0644 root | `wg pubkey`, goes to the edge |
| llama-swap API key | `/etc/llama-swap/api-key` | 0640 root:llamaswap | `openssl rand -hex 32` |
| `LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY` | `/srv/gateway/.env` | 0600 root | `sk-` + 48 hex. **Never change the salt key after first start** (it encrypts stored credentials) |
| `POSTGRES_PASSWORD` | `/srv/gateway/.env` | 0600 root | 32 alnum |
| `LLAMA_SWAP_KEY` | `/srv/gateway/.env` | 0600 root | copy of the llama-swap API key |
| LiteLLM virtual keys | `/srv/gateway/keys/<name>.key` | 0600 root | `provision-keys.py` (LiteLLM `/key/generate`) |
| `WEBUI_SECRET_KEY` | `/srv/webui/.env` | 0600 root | 48 alnum |
| `OAUTH_CLIENT_SECRET` | `/srv/webui/.env` | 0600 root | Pocket-ID, via `pocketid-bootstrap.py` |
| `OPENAI_API_KEYS` | `/srv/webui/.env` | 0600 root | `provision-keys.py`: `<open-webui key>;<hermes-gateway key or PENDING>` |
| hermes-gateway key (optional) | `/srv/webui/hermes-gateway.key` | 0600 root | copied from the workstation |
| Pocket-ID `ENCRYPTION_KEY` | `/srv/webui/pocket-id.env` | 0600 root | 32 random bytes, base64url |
| telemetry OIDC client secret | `/srv/webui/oidc/<client>.client-secret` | 0600 root | Pocket-ID, via `pocketid-bootstrap.py`. Goes to the edge |
| Open WebUI break-glass admin password | `/srv/webui/admin-password` | 0600 root | 24 alnum |
| Grafana admin password | `/srv/monitoring/grafana-admin-password` | 0600 root | 32 alnum |
| container copies | `/srv/monitoring/secrets/grafana-admin-password` (0400 472:472), `/srv/monitoring/secrets/llama-swap-api-key` (0400 65534:65534), `/srv/telemetry/secrets/llama-swap-api-key` (0440 root:10001) | | copies. After a rotation, re-copy by hand (gen-secrets warns on mismatch) |
| Hermes key (optional) | `~${BACKEND_SSH_USER}/.config/spark/hermes.key` | 0600 user | copy of `keys/hermes.key` |
| digest LiteLLM key | `/srv/digest/secrets/digest-litellm-key` | 0440 root:10001 | copy of `keys/digest.key`, made by `deploy.sh` after provision-keys (never printed; kept if present) |
| restic repo password (offsite) | `/etc/spark-restic/password` | 0600 root, dir 0700 | `openssl rand -hex 24` by `offsite-setup.sh`, once. **Owner keeps an offline copy; without it the offsite backups are unrecoverable** |
| AWS key of IAM user `spark-restic` | `/etc/spark-restic/aws.env` | 0600 root | `aws iam create-access-key`, piped straight to Walter (backup/OFFSITE.md) |

Backups (`${MODELS_DIR}/backups`) contain all of these and are root-only but **not encrypted**. The offsite copy
is encrypted by restic. `/etc/spark-restic` is deliberately in neither, so the restic password is never inside the
repo it protects.

## Output cap (no unbounded generations)

llama-server's default `n_predict` is -1: a request without `max_tokens` generates until the context
is full. One such request to `coder` ran for 21 minutes to 131072 tokens. Two layers prevent that:

| layer | where | what it does |
|---|---|---|
| gateway (per request) | `gateway/hooks/spark_hooks.py`, `cap_output()` | A request with no output limit (or a non-positive or non-integer one) gets `max_tokens` (chat, text completion, `/v1/messages`) or `max_output_tokens` (`/v1/responses`) = 16384. A limit above the model's maximum is clamped to it; a smaller limit passes unchanged. Maxima: `coder`, `coder-fast`, `big` 32768; `vision`, `hermes` 16384; `claude-*` as `coder`; `local/<id>` as that ID's alias, unknown IDs 16384. The LiteLLM log shows `spark_hooks: output cap ...` for every change. `SPARK_DEFAULT_MAX_OUTPUT` (env of the litellm service) overrides the default. |
| llama-server (backstop) | `llama-swap/config.yaml.tmpl`, `-n <N>` in every model's `cmd` | Same maxima as the gateway. Covers anything that reaches llama-swap without a limit (direct calls from Walter, a hook failure). **`-n` is only a default**: on build 11277 a request that asks for more than `-n` still gets more (tested with `-n 8` and `max_tokens 20` → 20 tokens). The clamp therefore has to stay in the gateway. |

The three places that hold the maxima (`model_info.max_tokens` in `litellm.yaml`, `-n` in the llama-swap
config, `MODEL_MAX_OUTPUT` in the hook) are checked against each other by
`gateway/tests/test_spark_hooks.py` (`python3 -m pytest walter/gateway/tests -q -p no:cacheprovider`).

Check on the host (`/slots` shows the `n_predict` of the slot's **last** task; a fresh slot has none):
```bash
docker ps --filter label=llama-swap=1 --format '{{.Names}} {{.Ports}}'         # per-model ports
docker inspect qwen3.8-27b --format '{{join .Args " "}}' | grep -o -- '-n [0-9]*'  # backstop flag
curl -s 127.0.0.1:<port>/slots | jq '.[].params.n_predict'   # 16384 after a gateway request without a limit
sudo docker logs --since 10m gateway-litellm-1 2>&1 | grep 'output cap'
```

## Verify

```bash
systemctl is-active llama-swap docker-user-rules wg-quick@wg0 nvidia-persistenced
nvidia-smi --query-gpu=name,persistence_mode,pcie.link.width.current --format=csv
sudo iptables -S WALTER-PUBLISHED; sudo ufw status verbose
for s in gateway webui monitoring telemetry digest; do sudo docker compose -f /srv/$s/compose.yaml ps; done   # all (healthy)
K=$(sudo cat /srv/gateway/keys/claude-code.key)
curl -s http://${BACKEND_WG_IP}:4000/v1/models -H "Authorization: Bearer $K" | jq -r '.data[].id'; unset K
curl -s http://${BACKEND_WG_IP}:4000/metrics/ | head -3                      # LiteLLM metrics (unauthenticated)
ssh -N -L 3001:127.0.0.1:3001 -L 9090:127.0.0.1:9090 ${BACKEND_SSH_USER}@${BACKEND_LAN_IP}   # Grafana / Prometheus: all targets up
sudo systemctl start spark-backup && sudo /usr/local/sbin/spark-backup-restore-test.sh
systemctl list-timers 'spark-*'
cat /var/lib/spark-offsite/LAST_OK; journalctl -u spark-offsite -n 20   # offsite (if enabled)
```

## Rollback

- **A config change:** every stack is a plain directory. Revert the file (or `git checkout` the
  previous repo revision and re-run deploy), then `docker compose up -d --force-recreate` that stack.
  `llama-swap` reloads `config.yaml` by itself.
- **Output cap:** restore the `*.bak-outputcap-<ts>` copies of `/etc/llama-swap/config.yaml`
  (`install -m 0640 -o root -g llamaswap`; `--watch-config` reloads and restarts the loaded models) and
  `/srv/gateway/hooks/spark_hooks.py`, then `docker compose -f /srv/gateway/compose.yaml up -d
  --force-recreate --no-deps litellm`. The hook is a single-file bind mount, so a new file needs a
  recreate, not just a restart.
- **A stack:** `sudo docker compose -f /srv/<stack>/compose.yaml down`. Volumes are kept unless
  you add `-v`.
- **Host units:** `sudo systemctl disable --now llama-swap spark-backup.timer spark-update-check.timer spark-offsite.timer`.
  `docker-user-rules` should stay enabled while any port is published.
- **Data:** restore from `${MODELS_DIR}/backups` (`backup/RESTORE.md.tmpl`, installed as
  `${MODELS_DIR}/backups/RESTORE.md`), or from the offsite restic repo (its section 7).
- **Offsite backups:** remove units, restic, the IAM user and the bucket: `backup/OFFSITE.md` section 6.

## Deliberately not captured

- **Ollama retirement drop-ins** (`ollama.service.d/10-wait-for-gpu.conf`, `20-retired.conf`). They
  only exist because the old Ollama install is still on live Walter, disabled. A fresh install
  never installs Ollama, so they are not needed. Ollama-era docs are in `docs/history/`.
- **The VM-provisioning readiness service/timer and its GPU motd hook.** They belong to the
  hypervisor/VM provisioning layer (private iac repo; see `hypervisor/`). On live Walter that
  readiness unit `Wants=ollama.service`, which is why the Ollama `20-retired.conf` drop-in exists.
- **`/opt/vllm`** (kept on live for a possible future vLLM entry) and leftover experiments in the
  operator's home.
- **Docker daemon.json.** `nvidia-ctk runtime configure --runtime=docker` writes it. Live contains
  only the `nvidia` runtime entry.

## Site assumptions

- **Fixed ports.** Walter publishes 3000/1411/4000/3200/8080 as live. The edge's `WEBUI_PORT`,
  `POCKETID_PORT`, `LITELLM_PORT` and `TELEMETRY_PORT` must keep their defaults, or you must edit
  the walter compose files, firewall scripts and probe lists to match.
- **GPU indices** `device=0` / `device=1` and the matrix in `config.yaml.tmpl` assume two ~24 GB GPUs.
- **Docker group IDs.** Grafana runs as uid 472, the llama-swap SD sidecar as 65534, and telemetry
  as 10001. The secret copies are owned accordingly.
- The interface names `wg0` and `BACKEND_LAN_IF` are used in `docker-user-rules.sh` and in the
  telemetry network panel.
