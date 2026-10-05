# Architecture

How 343 Guilty Spark fits together end to end: the hosts and the tunnel between them, how each kind of request travels, where the models run, how the agent judge reviews the local agent, how the stack is deployed and operated, and the security model. Deploy steps are in the component READMEs: [walter/](walter/README.md), [covenant/](covenant/README.md), [clients/](clients/README.md), [judge/](judge/README.md). Values in `${...}` come from `site.env` (see [site.env.example](site.env.example)); every address in this file is a placeholder.

Contents: [Topology](#topology) · [Request paths](#request-paths) · [Ports and bind addresses](#ports-and-bind-addresses) · [Models and GPU placement](#models-and-gpu-placement) · [The gateway hook](#the-gateway-hook-output-cap-and-mid-conversation-system-messages) · [The agent judge](#the-agent-judge) · [Operations](#operations) · [Security model](#security-model) · [Known quirks](#known-quirks-and-gotchas) · [Known issues / follow-ups](#known-issues--follow-ups)

## Topology

| Codename | Role | Where | Key facts |
|---|---|---|---|
| (hypervisor) | KVM/libvirt host, also the operator's workstation | home | AM5 desktop. Binds both RTX 3090s to `vfio-pci` and passes them to Walter, passes the models NVMe to Walter as a raw disk, autostarts Walter. Both GPUs sit on CPU root ports at PCIe Gen4 x8/x8 through a bifurcation riser. CUDA does not work on the host by design. Managed in a separate private IaC repo ([hypervisor/](hypervisor/README.md)). |
| workstation | The same machine, as the operator's desktop session | home | Runs the harness wrappers, the `hermes-gateway` user service on `${HYPERVISOR_BRIDGE_IP}:${HERMES_GATEWAY_PORT}`, and the agent judge's user units. |
| **Walter** | Backend VM | home, behind NAT | 2x RTX 3090 (48 GB), 16 vCPU, about 80 GiB RAM, `${MODELS_DIR}` on its own XFS disk. LAN address `${BACKEND_LAN_IP}` on the libvirt bridge, tunnel address `${BACKEND_WG_IP}`. Runs everything stateful: models, gateway, UI, identity, monitoring, telemetry, digest, backups. |
| **Covenant** | Edge | cloud, t3.small class (2 GB RAM) | Public address `${EDGE_PUBLIC_IP}`, tunnel address `${EDGE_WG_IP}`. nginx, oauth2-proxy (two instances), fail2ban, ufw, certbot. Stateless apart from certificates and its own secrets. |

```
                 internet (browsers, phones, remote API clients)
                                  |
                                  | HTTPS :443 (TLS terminated here)
                                  v
        +------------------------------------------------------+
        | Covenant  ${EDGE_PUBLIC_IP}                          |
        |  nginx  oauth2-proxy :4180/:4181  fail2ban  ufw      |
        +---------------------------+--------------------------+
                                    | WireGuard udp/${WG_PORT}
                                    | ${EDGE_WG_IP} <-> ${BACKEND_WG_IP}
                                    | (Walter dials out; edge is the listener)
  home -----------------------------+-----------------------------------------
                                    v
        +------------------------------------------------------+
        | Walter VM  ${BACKEND_WG_IP} / ${BACKEND_LAN_IP}       |
        |  Open WebUI  Pocket-ID  LiteLLM+Postgres  llama-swap |
        |  llama-server containers (GPU0, GPU1)                |
        |  monitoring  telemetry  digest  backups              |
        +---------------------------+--------------------------+
                                    | libvirt NAT bridge ${HYPERVISOR_BRIDGE_IP}
                                    | (GPUs via vfio-pci, models NVMe raw)
        +---------------------------+--------------------------+
        | Hypervisor / workstation                              |
        |  libvirt, harness wrappers, hermes-gateway,           |
        |  agent judge (user units)                             |
        +------------------------------------------------------+
```

Walter has no public address. It dials Covenant over WireGuard; Covenant has no route into the home LAN beyond Walter's tunnel address. The workstation reaches Walter directly over the bridge (SSH, judge probes) and reaches the public API hosts like any other client.

## Request paths

### 1. Chat (browser)

```
browser
  | HTTPS ${SPARK_CHAT_HOST}
  v
Covenant nginx  (TLS, login endpoints 5 r/m per IP, websockets)
  | wg0
  v
Open WebUI ${BACKEND_WG_IP}:3000 --OIDC (code + PKCE)--> Pocket-ID ${SPARK_ID_HOST}
  |                                                      (passkey; group ${CHAT_GROUP})
  | gateway docker network, key "open-webui"
  v
LiteLLM :4000  (spark_hooks.py: output cap, system-message rewrite)
  | ${BACKEND_WG_IP}:8080, llama-swap API key
  v
llama-swap  (matrix router, on-demand load/evict)
  | 127.0.0.1:100xx
  v
llama-server container (GPU0 / GPU1 / both)
```

- Login: Open WebUI redirects to Pocket-ID (OIDC, confidential client with PKCE). Pocket-ID only lets members of `${CHAT_GROUP}` through, and users authenticate with a passkey. New users get the Open WebUI role `user`; signup is off.
- Open WebUI reaches Pocket-ID through the public hostname (the hairpin path out through Covenant and back), so browser and server agree on the issuer URL.
- Model visibility uses Open WebUI access grants. The five aliases have public-read grants; a model with no grant (for example `hermes-agent`) is visible to admins only.
- A local break-glass admin (`${ADMIN_EMAIL}`) exists alongside the OIDC admins.

### 2. API clients and harnesses

```
claude-spark / codex-spark / hermes-spark / opencode / Cline / any OpenAI or Anthropic client
  | HTTPS ${SPARK_API_HOST}, bearer or x-api-key = that harness's own LiteLLM key
  v
Covenant nginx  (path allowlist, 401 without credentials, 10 r/s burst 40, 20 conns per IP)
  | wg0
  v
LiteLLM ${BACKEND_WG_IP}:4000  (virtual key: 60 rpm, 4 parallel)
  |   pre-call hook spark_hooks.py:
  |     - output cap: default 16384, clamp to the model maximum
  |     - mid-conversation system/developer -> user <system-reminder>
  |   /v1/messages passes through to llama-server's Anthropic endpoint;
  |   /v1/chat/completions and /v1/responses are translated
  v
llama-swap ${BACKEND_WG_IP}:8080 --> llama-server
```

| Harness | API it speaks | Entry point | Key file |
|---|---|---|---|
| Claude Code | Anthropic `/v1/messages` (+ `count_tokens`) | `claude-spark` (isolated config dir `~/.claude-spark`) | `~/.config/spark/claude-code.key` |
| Codex | `/v1/responses` | `codex-spark [--profile spark-fast]` | `~/.config/spark/codex.key` |
| Hermes Agent | `/v1/chat/completions` | `hermes-spark` | `~/.config/spark/hermes.key` |
| OpenCode | `/v1/chat/completions` | `opencode` (key via `{file:}`) | `~/.config/spark/opencode.key` |
| Cline | `/v1/chat/completions` | Cline's OpenAI-compatible provider | its own key |

The wrappers read the key file at run time (mode 600) and never put it in a config; plain `claude`, `codex` and `hermes` know nothing about the gateway. nginx forwards only `/v1/chat/completions`, `/v1/responses...`, `/v1/models...`, `/v1/messages` and `/v1/messages/count_tokens`. Everything else on `api.` (LiteLLM's UI, `/metrics`, key management) returns 404 at the edge. Add `/v1/embeddings` to the regex if it is ever needed. Details: [clients/README.md](clients/README.md).

### 3. Hermes Agent in chat (admins only)

```
browser --> Covenant --> Open WebUI (as in path 1; admin role required to see the model)
  | second OpenAI-compatible connection, over the libvirt bridge
  v
hermes-gateway ${HYPERVISOR_BRIDGE_IP}:${HERMES_GATEWAY_PORT}/v1   (workstation user unit, API_SERVER_KEY)
  |
  v
Hermes Agent on the workstation: tools run here, as the workstation user
  | its model calls: HTTPS ${SPARK_API_HOST} with the hermes key (path 2)
  v
LiteLLM --> llama-swap --> llama-server
```

Open WebUI has two OpenAI-compatible connections: LiteLLM and the hermes-gateway. The gateway runs only Hermes's API server (no messaging platforms) and binds the bridge address only. Commands that need interactive approval are blocked in API mode, and secret redaction is on. Because the tools act on the workstation with the operator's permissions, the `hermes-agent` model has no access grant and is admin-only. The agent judge's hooks apply to these sessions as to any other Hermes session.

### 4. Telemetry and digest sites

```
browser
  | HTTPS ${SPARK_TELEMETRY_HOST}                 | HTTPS ${SPARK_DIGEST_HOST}  (optional)
  v                                               v
Covenant nginx 50-telemetry                     Covenant nginx 60-digest
  | auth_request /oauth2/auth                     | auth_request /oauth2/auth
  v                                               v
oauth2-proxy 127.0.0.1:4180                     oauth2-proxy-digest 127.0.0.1:4181
  (Pocket-ID client, group ${TELEMETRY_GROUP})    (own client, group ${DIGEST_GROUP},
  |                                                own cookie + secrets)
  | 202 -> proxy on, identity headers only        |
  |        from auth_request_set                  |
  | wg0                                           | wg0
  v                                               v
telemetry app ${BACKEND_WG_IP}:3200             digest app ${BACKEND_WG_IP}:3300
  (host network; ufw: only ${EDGE_WG_IP})         (host network; ufw: only ${EDGE_WG_IP})
  |                                               |--> public feeds (HTTPS out)
  v                                               |--> LiteLLM :4000 (key "digest", model coder,
Prometheus 127.0.0.1:9090                         |    grammar-constrained JSON, bounded max_tokens)
                                                  v
                                                /srv/digest/state (runs, per-source cutoffs)
```

- Each site has its own oauth2-proxy instance, OIDC client, Pocket-ID group, cookie and secrets. A telemetry session does not open the digest. No route skips auth except `/oauth2/*`; `/api/*` returns a JSON 401 instead of a login redirect.
- Telemetry: `/api/v1/stream` (server-sent events every 2 s) is unbuffered in nginx. The app runs fixed server-side Prometheus queries and shares one cached snapshot between all viewers, so viewer count does not drive query load (about 28 MiB of RAM). Endpoints: `/api/v1/telemetry`, `/api/v1/stream`, `/healthz`.
- Digest: on demand only. `POST /api/runs/<watch>/now` starts one background run per watch (collect, dedupe, LLM curation, Markdown + JSON), with progress over SSE. The only state-changing route also refuses browser cross-site requests. An operator on Walter can trigger the same route with `curl`. Deployed only when `SPARK_DIGEST_HOST` is set; otherwise both deploy scripts skip it ([walter/digest/README.md](walter/digest/README.md), [runbook](docs/runbooks/digest-deploy.md)). It was built by the local Hermes agent under the agent judge (pilot 2, [docs/agent-judge.md](docs/agent-judge.md)).
- Both apps use host networking, which bypasses Docker's `DOCKER-USER` chain, so ufw on `wg0` is their gate ([firewall model](#network-exposure-and-firewall-layers)).

## Ports and bind addresses

### Walter

| Service | Bind | Reachable from | Gate |
|---|---|---|---|
| Open WebUI | `${BACKEND_WG_IP}:3000` | `${EDGE_WG_IP}` | `WALTER-PUBLISHED` |
| Pocket-ID | `${BACKEND_WG_IP}:1411` | `${EDGE_WG_IP}` | `WALTER-PUBLISHED` |
| LiteLLM | `${BACKEND_WG_IP}:4000` | `${EDGE_WG_IP}`, Walter's own containers | `WALTER-PUBLISHED` |
| Postgres 17 | gateway docker network only | LiteLLM | not published |
| telemetry app | `${BACKEND_WG_IP}:3200` (host network) | `${EDGE_WG_IP}` | ufw on `wg0` |
| digest app (optional) | `${BACKEND_WG_IP}:3300` (host network) | `${EDGE_WG_IP}`, Walter | ufw on `wg0` |
| llama-swap | `${BACKEND_WG_IP}:8080` | `${GATEWAY_DOCKER_SUBNET}`, Walter | ufw + llama-swap API key |
| llama-server instances | `127.0.0.1:10001+` | llama-swap | loopback |
| Prometheus / Grafana | `127.0.0.1:9090` / `127.0.0.1:3001` | SSH tunnel | loopback |
| node-exporter, cAdvisor, dcgm-exporter, blackbox | `127.0.0.1` | Prometheus | loopback |
| SSH | `:22` (all interfaces) | home LAN / bridge; ufw allows 22 | key-only; Walter has no public address |

### Covenant

| Service | Bind | Reachable from |
|---|---|---|
| nginx | `:80`, `:443` | internet |
| WireGuard | `udp/${WG_PORT}` | internet (Walter dials in) |
| SSH | `:22` | internet, fail2ban-protected, root login off |
| oauth2-proxy (telemetry) | `127.0.0.1:4180` | nginx |
| oauth2-proxy-digest (optional) | `127.0.0.1:4181` | nginx |

### Workstation

| Service | Bind | Reachable from |
|---|---|---|
| hermes-gateway | `${HYPERVISOR_BRIDGE_IP}:${HERMES_GATEWAY_PORT}` | the libvirt bridge (Walter's Open WebUI); requires `API_SERVER_KEY` |

## Models and GPU placement

<a id="llama-swap-matrix-and-gpu-placement"></a>

Harnesses name an alias, never a model file. The concrete IDs are also exposed as `local/<id>`, and any `claude-*` model name maps to `coder` as a safety net.

| Model ID | Alias | GPU | Split | Context | Output max | Extra |
|---|---|---|---|---|---|---|
| `qwen3.8-27b` | `coder` | 0 | `-sm none` | 131072 | 32768 | MTP: `--spec-type draft-mtp --spec-draft-n-max 2` (22.6 GB) |
| `qwen3.6-35b-a3b` | `coder-fast` | 1 | `-sm none` | 131072 | 32768 | MoE, about 3B active |
| `qwen3-coder-next` | `big` | 0+1 | layer, `-sm layer -ts 1,1` | 131072 | 32768 | `ttl` 1800 s, evict cost 5 |
| `gemma-4-31b` | `vision` | 0+1 | tensor, `-sm tensor` + `${tp}` | 131072 | 16384 | `--mmproj`, `--ctx-checkpoints 8`, `ttl` 1800 s |
| `hermes-4.3-36b` | `hermes` | 0+1 | tensor, `-sm tensor` + `${tp}` | 65536 | 16384 | `ttl` 1800 s |

Every model runs `--fit off -ngl all -np 1 -fa on -ctk q8_0 -ctv q8_0 --jinja --metrics`, so a model that does not fit fails at load instead of silently shrinking its context. Single-GPU models are pinned with `--gpus device=N -sm none`.

**Why the splits differ.** There is no NVLink and no P2P between GeForce cards under vfio. Layer split passes one small activation per token across the link, so it suits the 3B-active MoE `big`, which gains nothing from tensor split. llama.cpp's **experimental** tensor split does an allreduce every layer through host shared memory (NCCL), which on Gen4 x8 gives the dense models `hermes` and `vision` +39 to 67 % decode for 12 to 23 % slower prompt processing on long prompts. The `${tp}` macro adds `--shm-size 2g` to the container, because Docker's default 64 MB `/dev/shm` crashes the first allreduce. Row split does not load on the pinned build. To fall back, drop `${tp}` and put back `-sm layer -ts 1,1`. Measurements: [walter/llama-swap/BENCHMARKS.md](walter/llama-swap/BENCHMARKS.md).

**Matrix sets** (which models may be resident together):

| Set | Members | When |
|---|---|---|
| `coding` | `coder` + `coder-fast` | Default, one per GPU. Preloaded at startup. |
| `big` | `big` | Evicts the pair; unloads after 30 min idle. |
| `vision` | `vision` | Evicts the pair; unloads after 30 min idle. |
| `hermes` | `hermes` | Evicts the pair; unloads after 30 min idle. |

A request for a model outside the resident set evicts the set and loads the model's set. `big` has an eviction cost of 5 because loading it reads about 40 GB (about 20 s from a cold page cache, 7 s warm), so the router prefers not to bounce it. Since the x8/x8 riser, weights upload at 10 to 13 GB/s per GPU and cold loads are limited by the models disk (about 2.8 GB/s): `coder-fast` about 12 s, `hermes` and `vision` 10 to 12 s. After a split model unloads, the next `coder` or `coder-fast` request reloads the pair.

Each model runs as a llama.cpp `server-cuda` container pinned by digest. Containers start through `spark-docker-run.sh`, which waits until the previous container of the same name is gone. Without it, a config reload raced the old container's removal and left both models down until the next request. `cmdStop` uses `docker rm -f` because it is synchronous. llama-swap watches its config and reloads itself on change.

## The gateway hook: output cap and mid-conversation system messages

<a id="the-gateway-hook-mid-conversation-system-messages"></a>

`spark_hooks.py` is a LiteLLM pre-call hook (shipped under [walter/gateway](walter/README.md)). It covers `/v1/messages`, `/v1/chat/completions` and `/v1/responses`, so it protects every harness and every model behind an alias, not one client config.

- **Output cap.** A request without `max_tokens` (`max_output_tokens` on `/v1/responses`) gets 16384, and a limit above the model's maximum (32768 for `coder`, `coder-fast` and `big`, 16384 for `vision` and `hermes`) is clamped to it. llama-server also runs with `-n` set to the same maximum, but on the pinned build `-n` is only a default that a request can exceed, so the gateway clamp is the real limit. Root cause: a request with no `max_tokens` ran for 21 minutes to the 131K context limit ([walter/README.md](walter/README.md#output-cap-no-unbounded-generations)).
- **Mid-conversation system messages.** Claude Code inserts `system` or `developer` messages mid-conversation (environment updates, tool changes). Qwen's chat template rejects them ("System message must be at the beginning"), llama-server returns HTTP 500, and Claude Code retries for about three minutes. The hook rewrites every system or developer message after the first into a user message wrapped in `<system-reminder>` tags. This replaced an undocumented client variable found in the Claude Code binary, and it also fixed `coder-fast` silently dropping Claude Code's environment information.

## The agent judge

Hermes, running on the local models, is reviewed by a judge that works from evidence collected by deterministic code, not from the agent's own account. It runs entirely on the workstation, as Hermes shell hooks in `~/.hermes/config.yaml` plus systemd user units. Full design, rubric and pilot results: [docs/agent-judge.md](docs/agent-judge.md); operation: [judge/README.md](judge/README.md); interfaces: [judge/CONTRACT.md](judge/CONTRACT.md).

```
Hermes turn
  |- C2 gate (pre_tool_call, sync, no model, fail-closed) -- escalate to human / block / allow
  |- C3 verify (pre_verify, sync) -- bash -n, parsers, check-sanitized; nudge once
  |- C1/C4 enqueue (post_tool_call, session end) --> ~/.hermes/review/queue/
  |                                                       |
  |                          judge-review.path (+ .timer backstop) runs the runner
  |                            1. collector  -> evidence bundle (logs, gate decisions + outcomes,
  |                                             diffs vs. session snapshot, C3 results,
  |                                             read-only host probes, llama-server slots)
  |                            2. judge      -> frontier (claude CLI) for infra-class data,
  |                                             local alias via the gateway for sensitive data
  |                            3. validator  -> drops or downgrades items without quoted evidence
  |                                                       |
  |- C5 inject (pre_llm_call) <-- unacknowledged findings, labelled as data, not instructions
  `- C6 runaway watch (judge-runaway-watch, always on) -- polls slots, alerts, queues a review
```

- **Gate.** Deterministic policy: host-mutating commands for Walter or Covenant, writes to sensitive paths and reads of secret-shaped paths are escalated to the human; edits to allowlists, hooks and approval settings are blocked. After a refusal, C5 reminds the agent once that a refusal is a stop.
- **Enqueue and runner.** One request per turn or plan file (plan writes in one turn coalesce). A path unit runs the runner, a timer is the backstop, and failures raise a desktop alert through `judge-alert@`.
- **Runner modes.** `JUDGE_MODE=frontier`: infra-class requests go to the frontier judge, up to `JUDGE_FRONTIER_DAILY_MAX` calls per UTC day, each with a dollar cap. Sensitive requests, and anything over the cap, go to the local judge alias. Local findings are capped at medium severity and not injected by default, because a same-family local judge measured unreliable.
- **Data classes.** Each path is `secret`, `scratch`, `infra` or `sensitive`. Infra-class evidence may go to the frontier. A sensitive request goes to the local judge with diff stats instead of contents; a sensitive *completion* also gets a frontier review of a claims-only bundle (redacted, masked final answer plus gate, C3 and tool metadata, never contents, paths or the user's message, with a fail-closed self-check). Mixed bundles: see [the judge's data boundary](#the-judges-data-boundary).
- **Read-only toward hosts.** The judge reaches Walter and Covenant only through an allowlist of read-only probes over SSH. The runaway watcher alerts; it never cancels a generation.

## Operations

### Deploy model

Every component converges from this repo plus a gitignored `site.env`. The scripts are idempotent, never delete, and never overwrite an existing secret. Each supports a dry run.

| Script | Runs on | Converges |
|---|---|---|
| [`walter/deploy.sh`](walter/README.md) | Walter, as root | Render templates; pinned packages (NVIDIA driver, container toolkit, Docker); users; the `${MODELS_DIR}` mount; the llama-swap binary (version + sha256); files and env files; `gen-secrets.sh walter`; systemd units and drop-ins; ufw and the `DOCKER-USER` chain; optional restic offsite; the compose stacks `gateway` (and its LiteLLM keys), `webui`, `monitoring`, `telemetry`, optional `digest`; optional Hermes on Walter (`--with-hermes`). Flags: `--dry-run`, `--destdir`, `--no-start`, `--skip-packages`. |
| [`covenant/deploy.sh`](covenant/README.md) | Covenant, as root | Packages, sshd drop-in, unattended-upgrades, `gen-secrets.sh covenant`; WireGuard; ufw; nginx base, bootstrap (ACME-only port 80) then certificates then full sites; oauth2-proxy (and `-digest`); fail2ban. Reloads a service only when something it reads changed. `--setup-lock` opens the Pocket-ID first-claim window; `--set-client-secret [--instance digest]` reads an OIDC client secret from stdin. |
| [`clients/install.sh`](clients/README.md) (`clients/deploy.sh` wraps it) | the workstation, as the user | Wrappers into `~/.local/bin`, rendered harness configs (Claude Code in `~/.claude-spark`, Codex, Hermes fragment, OpenCode), optional `hermes-gateway` user unit. A differing file is left alone and the new one written as `<file>.spark` unless `--force` (which backs up first). |
| [`judge/install.sh`](judge/README.md) | the workstation, as the user | Dry run by default. `--apply` merges the managed hooks block into the Hermes config, creates the review dir and renders the gate policy; `--with-units --start` installs and starts `judge-review.{path,service,timer}`, `judge-alert@` and `judge-runaway-watch`. Hermes asks consent for each hook on first use. |

Live changes are made in the repo first: render the templates with the real `site.env`, diff against the live files (the only differences should be secret placeholders), then deploy. The live judge runs from a checkout on the workstation; after pulling judge changes, re-run `judge/install.sh --apply`.

### Secrets layout

Secrets are generated on the target host by `scripts/gen-secrets.sh <component>` (definitions in `scripts/secrets.d/`), are root-owned and mode 600 (or 0400/0440 copies for a container user), and are never printed or committed.

| Host | Secrets |
|---|---|
| Walter | WireGuard private key (`/etc/wireguard/`); llama-swap API key (`/etc/llama-swap/api-key`, copied read-only to monitoring and telemetry); `/srv/gateway/.env` (LiteLLM master and salt keys, Postgres password); LiteLLM virtual keys in `/srv/gateway/keys/<name>.key`; `/srv/webui/.env` and `pocket-id.env` (Open WebUI secret, Pocket-ID `ENCRYPTION_KEY`, OIDC client secret); break-glass and Grafana admin passwords; restic password and AWS credentials under `/etc/spark-restic/`. |
| Covenant | WireGuard private key; oauth2-proxy cookie and client secrets in `/etc/oauth2-proxy/` and `/etc/oauth2-proxy-digest/`, passed to the units with `LoadCredential=`; Let's Encrypt keys. |
| Workstation | Harness keys in `~/.config/spark/<harness>.key` (600); `~/.hermes/.env` (Hermes key, `API_SERVER_KEY`); the judge uses the claude CLI's own login and the edge SSH key named in `EDGE_SSH_KEY`. |

Rotation: [docs/runbooks/rotate-secrets.md](docs/runbooks/rotate-secrets.md).

### Backups and restore

- **Local.** `spark-backup.timer` runs `spark-backup.sh` daily at 03:30 UTC (up to 10 min random delay, `Persistent=true`, so a run missed while Walter was off happens at the next boot). Snapshots go to `${MODELS_DIR}/backups/<YYYY-mm-dd_HHMM>/`, on a different disk from the root filesystem. Retention: the newest of each of the last 14 days plus the newest of each of the last 8 Sundays. `LAST_OK` names the last full success.
- **Contents.** `pg_dump -Fc` of LiteLLM plus roles; online SQLite `.backup` of Open WebUI and Pocket-ID (integrity-checked); `pocket-id export`; volume tarballs without the live DB files; a config tarball of `/srv/*/`, `/etc/llama-swap`, units, scripts, ufw rules, fstab and the Hermes config; a manifest of versions and digests; `SHA256SUMS`. Local snapshots are root-only (700/600) and **not encrypted**; they contain every secret.
- **Offsite.** `spark-offsite.timer` (04:30 UTC, after the local backup) copies `${MODELS_DIR}/backups` with restic to a private, versioned S3 bucket, using an IAM user that can touch only that bucket. restic encrypts client-side; the repo password lives only in `/etc/spark-restic/password` (outside the backed-up tree) and in the owner's offline copy. Retention 14 daily, 8 weekly, 6 monthly; a 25 % data check every Sunday. Off when `RESTIC_BUCKET` is empty. Setup, fresh-machine restore and removal: [walter/backup/OFFSITE.md](walter/backup/OFFSITE.md).
- **Restore.** `spark-backup-restore-test.sh` restores the latest snapshot into throwaway containers and checks it (non-destructive). Per-component restore steps and a whole-VM rebuild are installed as `${MODELS_DIR}/backups/RESTORE.md`. Two traps: a restored Pocket-ID needs `DELETE FROM francis_hosts;` (a stale cluster heartbeat) before it starts, and Pocket-ID's signing keys are encrypted with `ENCRYPTION_KEY`, so the env file must come from the same snapshot.
- Covenant holds no user data and is not in these backups: it is rebuilt with `covenant/deploy.sh` (each live deploy snapshots `/etc/nginx` to `/var/backups/covenant/` first). The workstation's judge review data and Hermes state are outside these backups.

### Update check

`spark-update-check.timer` runs Mondays at 09:00 UTC (`Persistent=true`). It is **report-only**: it never pulls, upgrades or restarts.

- It checks every image in `/srv/*/compose.yaml` and in the llama-swap config for digest drift against the registry, compares GitHub releases (llama.cpp `bNNNN`, LiteLLM, Open WebUI, Pocket-ID, llama-swap, hermes-agent), and evaluates GitHub security advisories against the running version. For a digest-only image reference it reads the tag from the comment next to it, so keep those comments accurate.
- It also reports pending apt and `-security` updates, reboot-required, held packages, the NVIDIA driver against the apt candidate, and Docker and container-toolkit versions.
- Output: `/var/lib/spark-update-check/latest.md`, plus a one-line summary in the login banner. Exit codes: 0 OK, 10 updates available, 20 advisories affect current versions, 1 checker error.
- Covenant has no inbound key from Walter, so its check (`spark-edge-check`: apt security count, cert expiry, ufw, fail2ban, `nginx -t`) runs manually from the workstation. Unattended Ubuntu security upgrades are on for both hosts.

How to act on the report: [docs/runbooks/upgrade.md](docs/runbooks/upgrade.md).

### Monitoring and telemetry

- `/srv/monitoring` on Walter: Prometheus, Grafana, node-exporter, cAdvisor, dcgm-exporter, blackbox, and a small llama-swap target discovery that adds each running llama-server's `/metrics`. All bind 127.0.0.1. 16 targets, 12 alert rules, 3 dashboards.
- Access: `ssh -N -L 3001:127.0.0.1:3001 -L 9090:127.0.0.1:9090 ${BACKEND_SSH_USER}@${BACKEND_LAN_IP}`.
- LiteLLM `/metrics` is unauthenticated (an accepted trade-off). Only Walter and Covenant can reach it, and nginx on `api.` returns 404 for it.
- The telemetry site ([path 4](#4-telemetry-and-digest-sites)) is the read-only, browser-friendly view of the same Prometheus data. Gaps: llama-server updates its token counters only when a request finishes, so live speed is shown as decode steps per second; there is no KV-cache usage metric; per-key spend is not shown (costs are 0).

### fail2ban and abuse handling

Covenant runs three jails: `sshd` (with a `journalmatch` for Ubuntu 24.04's `ssh.service`), `nginx-limit-req` (bans IPs that keep hitting the nginx rate limits) and `recidive` (longer bans for repeat offenders). Bans are ufw rejects. `${ADMIN_SOURCE_IPS}` and the tunnel subnet `${WG_SUBNET}` are in `ignoreip`.

## Security model

### Authentication layers

| Surface | Who | How |
|---|---|---|
| `chat.` | people | Pocket-ID passkey (OIDC, PKCE), group `${CHAT_GROUP}`; Open WebUI role and per-model access grants; signup off. |
| `id.` | people | Pocket-ID itself. Passkeys only, no passwords; an admin creates each user and sends a one-time registration link. `/setup` was locked to `${ADMIN_SOURCE_IPS}` until the admin was claimed, then the lock was removed (the setup API returns 404). |
| `api.` | programs | LiteLLM virtual key per person and per harness; 401 at the edge without credentials. |
| `telemetry.`, digest | people | nginx `auth_request` to a per-site oauth2-proxy, then Pocket-ID passkey and a per-site group. |
| Walter internals | services | llama-swap API key (gateway, monitoring, telemetry); Postgres on an internal network only; `API_SERVER_KEY` for the hermes-gateway. |
| SSH | operator | Keys only. Covenant: root login off, fail2ban. Walter: no public address. |

There is no basic auth and there are no shared passwords.

### Network exposure and firewall layers

```
internet | Covenant (SG + ufw, fail2ban, nginx allowlist + limits) | wg0 | Walter (DOCKER-USER + ufw) | loopback (models, monitoring)
```

1. **Internet to Covenant.** ufw allows 22, 80, 443 and `udp/${WG_PORT}`; the cloud security group matches. An unknown `Host` gets nginx's 444, and the apex redirects to `chat.`. Rate limits per IP: login endpoints 5 r/m; Pocket-ID auth 10 r/m; API 10 r/s burst 40 with 20 connections; per-site zones for telemetry and digest.
2. **Covenant to Walter.** The only path is the tunnel. On Walter, Docker-published ports bypass ufw, so `docker-user-rules.service` installs a chain `WALTER-PUBLISHED` hooked from `DOCKER-USER` that admits ports 3000, 1411 and 4000 only from `${EDGE_WG_IP}` and drops the rest. The telemetry and digest apps use host networking, which bypasses that chain, so ufw gates them: `allow in on wg0 from ${EDGE_WG_IP} to ${BACKEND_WG_IP} port 3200` (and 3300). The digest rules sit in `# >>> digest` blocks of the firewall templates, which `walter/deploy.sh` drops unless the digest is on.
3. **Gateway to models.** ufw allows `${BACKEND_WG_IP}:8080` only from `${GATEWAY_DOCKER_SUBNET}`; the gateway compose network is pinned to that subnet so the rule stays valid. llama-swap also requires its own API key. llama-server containers bind loopback.
4. **Forwarded headers.** Every app behind the edge trusts `X-Forwarded-*` from `${EDGE_WG_IP}` only: Open WebUI `FORWARDED_ALLOW_IPS=${EDGE_WG_IP}`, Pocket-ID `TRUST_PROXY=true` (reachable only from the edge), LiteLLM `trusted_proxy_ranges` = `${EDGE_WG_IP}/32`. The oauth2-proxy sites pass identity headers to the app only from `auth_request_set`, never from the client. This keeps client IPs correct for rate limiting and logs without letting anything else spoof them.
5. **Workstation.** The hermes-gateway binds only the libvirt bridge address and requires `API_SERVER_KEY`.

### Key scoping

- Every person and every harness has its own LiteLLM virtual key (`SPARK_USERS`, `HARNESS_KEYS`; the digest app has its own `digest` key). Each key is limited to 60 requests per minute and 4 parallel requests, spend is attributed per key, and a key can be revoked without touching the others. The master key never leaves Walter.
- Open WebUI uses its own key, and its model list is further filtered by per-model access grants.
- The llama-swap key is internal to Walter. The Pocket-ID OIDC clients (Open WebUI, telemetry, digest) are separate, each restricted to its group.
- The judge's frontier calls use the claude CLI's own login against the normal Anthropic API, not this gateway; its local calls go through the public API host with the Hermes harness key by default (`JUDGE_LOCAL_KEY_FILE`).

### Public-repo sanitization

This repo is public. Real site values (domain, IPs, user names, hostnames, OIDC client IDs) live only in the gitignored `site.env`, `.sanitize-extra` and `.sanitize-words`; committed files use the placeholders in [site.env.example](site.env.example). `scripts/check-sanitized.sh --all` fails on any real value or secret-shaped string, and gitleaks scans changed files before a commit. Templates are rendered with an explicit variable list. See [CONVENTIONS.md](CONVENTIONS.md).

### The judge's data boundary

What leaves the house for the frontier judge is decided per path (`secret`, `scratch`, `infra`, `sensitive`). Infra-class evidence (this repo and its worktrees, the Hermes config, `/etc`, `/srv`, ...) may go to the frontier. Secret-shaped files are always sensitive. Sensitive requests are judged locally, with diff stats rather than contents; a sensitive completion additionally gets a frontier review of a claims-only bundle that never carries contents, diffs, paths, command output or the user's message, and that fails closed if a self-check finds an identifier. Since #43, a mixed request with up to `JUDGE_MIXED_MAX_SENSITIVE` (default 3) sensitive paths, and no more of them than infra paths, stays infra: the sensitive files' content is withheld and their names masked, but the bundle goes to the frontier (residual risks under [Known issues](#known-issues--follow-ups); `0` is strict mode). Details: [judge/README.md](judge/README.md#data-boundary), [decision 10](docs/decisions.md#10-frontier-review-of-claims-only-bundles-for-sensitive-sessions).

## Known quirks and gotchas

**Models and llama.cpp**
- Qwen and Gemma think by default. A small `max_tokens` returns empty content; clients need a bigger budget or `enable_thinking: false`.
- Output is never unbounded: the gateway sets `max_tokens` 16384 when a request has none and clamps larger values to the model's maximum. A long thinking answer can end with `finish_reason: length`; ask for more explicitly, up to the maximum.
- The startup preload logs `status 404` for each model, yet both models load and stay warm. Harmless; cause unknown.
- The 112 GB of models do not fit in Walter's page cache, so cycling through every set keeps loads cold; swapping back to a set just used takes 4 to 7 s.
- `hermes` and `vision` run llama.cpp's **experimental** tensor split. If either misbehaves after an image bump (crash at the first request, `ncclGroupEnd` errors, wrong output), fall back to layer split as described in [Models and GPU placement](#models-and-gpu-placement).
- `nvidia-smi` shows the links at Gen1 when idle; they rise to Gen4 under load (power saving, not a fault).

**LiteLLM**
- In LiteLLM 1.103.x, `openai/` deployments route `/v1/messages` through the Responses adapter, which drops llama-server's reasoning. Every alias therefore sets `model_info.supported_endpoints: ["/v1/chat/completions","/v1/responses","/v1/messages"]`, so Messages pass straight through to llama-server. Keep this on new aliases.
- Reasoning is not returned as Anthropic `thinking` blocks on the translated paths, and the `/v1/responses` reasoning summary is empty.
- `count_tokens` is estimated by LiteLLM and undercounts; it also logs a harmless 404 ERROR.
- Settings: `drop_params: true`, 900 s timeouts, `num_retries: 0`.

**Harnesses**
- Claude Code sends about 15K tokens of system prompt and tools per request. Context is set to 131072 with auto-compact at 85 % and max output 16384. Its default `auto` permission mode runs the safety classifier on the local model; use `--permission-mode default` or `acceptEdits` for anything sensitive.
- Codex 0.159 only supports `wire_api="responses"` and needs the custom model catalog, otherwise it assumes a 272K window. Its sandbox needs an AppArmor profile for `bwrap` on Ubuntu 24.04.
- Plain `hermes` gets a 401; use `hermes-spark`. Never run `hermes update`: it jumps to `main`, not the next release.

**Open WebUI and Pocket-ID**
- `ENABLE_PERSISTENT_CONFIG=false`: `.env` is the source of truth and admin-panel changes to those settings do not survive a restart.
- A model with no access grant is visible to admins only. Add a public-read grant for each new alias.
- The `local/*` models also appear in the model list; hide them in Admin → Models if wanted.
- The cyberpunk theme (`custom.css` and self-hosted OFL fonts, mounted read-only into `/app/build/static/`) applies in dark mode only. There is no server-side default theme, so users pick Dark in Settings → General. Open WebUI's branding is left intact as its license requires.

**Hosts**
- Walter: Ollama is retired but installed. A drop-in (`ConditionPathExists=/etc/ollama-enabled`) stops other units' `Wants=` from starting it at boot.
- Walter: nvidia-persistenced must start before anything GPU-bound; services that raced the driver at boot came up CPU-only.
- Hypervisor: `nvidia-cdi-refresh.{path,service}` is masked because both GPUs belong to `vfio-pci` and `nvidia-smi` cannot run there. Unmask it if a GPU ever returns to the host.
- Hypervisor: refer to the models NVMe by `/dev/disk/by-id/...`, never `/dev/nvmeXn1`; the kernel names have swapped between boots. Moving a GPU between slots changes its host PCI address, so Walter's `hostdev` entries must be updated before the VM starts.
- Covenant: Ubuntu 24.04 names the SSH unit `ssh.service`, so the stock fail2ban `sshd` jail matches nothing without a `journalmatch` override.

## Known issues / follow-ups

| Issue | Effect | Follow-up |
|---|---|---|
| **`RESTORE.md.tmpl` with an empty bucket.** The restore doc template references `RESTIC_BUCKET` and `RESTIC_REGION`. | Rendering it fails when `RESTIC_BUCKET` is empty, although offsite backups are optional. | Render the offsite section only when the bucket is set, or default the variables. |
| **`walter/deploy.sh` restarts more than it changed.** It always restarts `nvidia-persistenced` and always runs `compose up --build` for the telemetry and digest images. | A no-op deploy still bounces persistenced and rebuilds two images. | Restart persistenced only when its drop-in changed; build only when the build tree changed. |
| **CISA advisories feed returns 403.** The digest's `default` watch fetches CISA advisories; the server answers `403 Forbidden` (HTML). | The run reports "4 of 5 sources ok" with a coverage gap; the other feeds, including CISA KEV, work. | Find a fetch that CISA accepts (headers, a different feed URL) or replace the source. |
| **Judge code-correctness gap.** In digest pilot 2 the judge caught every process and report fault but missed both medium code defects. #43/#44 fixed the routing and the bundle budget that hid the diff. | Real code bugs in a large diff can still pass review. | Add a code-correctness item to the rubric and re-measure on the pilot-2 bundles. |
| **Mixed-bundle boundary.** With `JUDGE_MIXED_MAX_SENSITIVE=3` (the default the owner kept), a review with a few sensitive paths goes to the frontier with those files withheld and masked. | The agent's final answer is only redacted, so it could describe a withheld file; real site identifiers in Hermes log lines can reach the frontier for mixed bundles. | Acceptable while the stack handles only the owner's own infra. Before anything else (work, client or personal data): mask site values as `${KEY}` before frontier calls, run the claims self-check on the final answer when files are withheld, or set `0` (strict). |

Smaller items: the hermes-gateway relies on its bridge-only bind and API key (an optional host firewall rule limiting it to Walter is not part of this repo); `presence_penalty` for Qwen is untuned; the startup preload `404` is unexplained.
