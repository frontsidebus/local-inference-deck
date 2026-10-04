# Architecture

How a request travels through 343 Guilty Spark, where each service listens, who may talk to whom, and the operational pieces around it. Deploy steps are in the component READMEs: [walter/](walter/README.md), [covenant/](covenant/README.md), [clients/](clients/README.md). Values in `${...}` come from `site.env` (see [site.env.example](site.env.example)).

## Hosts

| Codename | Role | Where | Key facts |
|---|---|---|---|
| (hypervisor) | KVM/libvirt host | home | Binds both GPUs to `vfio-pci`, passes the models NVMe to Walter as a raw disk, autostarts Walter. CUDA does not work on the host by design. |
| **Walter** | Backend VM | home, behind NAT | 2x RTX 3090, 16 vCPU, ~80 GiB RAM, `${MODELS_DIR}` on its own XFS disk. LAN address `${BACKEND_LAN_IP}`, tunnel address `${BACKEND_WG_IP}`. Runs everything stateful. |
| **Covenant** | Edge | cloud, t3.small class | Public address `${EDGE_PUBLIC_IP}`, tunnel address `${EDGE_WG_IP}`. nginx, oauth2-proxy, fail2ban, certbot. Stateless apart from certificates. |
| workstation | Operator's desktop (on the hypervisor) | home | Runs the harness wrappers and the `hermes-gateway` on `${HYPERVISOR_BRIDGE_IP}:8642`. |

Walter dials Covenant over WireGuard (`udp/${WG_PORT}`); Covenant is the listener and has no route into the home LAN beyond Walter's tunnel address.

## Request paths

### Chat

```
browser --HTTPS--> Covenant nginx (${SPARK_CHAT_HOST})
        --wg0--> Open WebUI ${BACKEND_WG_IP}:3000
        --docker network--> LiteLLM :4000  (key: open-webui)
        --> llama-swap ${BACKEND_WG_IP}:8080
        --> llama-server container 127.0.0.1:100xx (GPU0 / GPU1 / both)
```

- Login: Open WebUI redirects to Pocket-ID at `https://${SPARK_ID_HOST}` (OIDC, confidential client with PKCE). Pocket-ID only lets members of `${CHAT_GROUP}` through. Users authenticate with a passkey. New users get the Open WebUI role `user`; signup is off.
- Open WebUI reaches Pocket-ID through the public hostname (the hairpin path out through Covenant and back), so both browser and server agree on the issuer URL.
- Model visibility uses Open WebUI access grants. The five aliases have public-read grants; a model with no grant (for example `hermes-agent`) is visible to admins only.
- A local break-glass admin (`${ADMIN_EMAIL}`) exists alongside the OIDC admins.

### Hermes Agent (chat, admins only)

```
Open WebUI --libvirt bridge--> hermes-gateway ${HYPERVISOR_BRIDGE_IP}:8642/v1 (API_SERVER_KEY)
           --> Hermes Agent on the workstation --> api. --> LiteLLM --> llama-swap
```

Open WebUI has two OpenAI-compatible connections: LiteLLM and the hermes-gateway. Hermes runs its tools on the workstation with the workstation user's permissions. Commands that need interactive approval are blocked in API mode, and secret redaction is on. This model is deliberately admin-only.

### API (harnesses)

```
harness --HTTPS + key--> Covenant nginx (${SPARK_API_HOST}, path allowlist, 401 without credentials)
        --wg0--> LiteLLM ${BACKEND_WG_IP}:4000 (virtual key, pre-call hook)
        --> llama-swap ${BACKEND_WG_IP}:8080 --> llama-server
```

| Harness | API it speaks | Entry point |
|---|---|---|
| Claude Code | Anthropic `/v1/messages` (+ `count_tokens`) | `claude-spark` (isolated config dir) |
| Codex | `/v1/responses` | `codex-spark [--profile spark-fast]` |
| Hermes Agent | `/v1/chat/completions` | `hermes-spark` |
| OpenCode, Cline | `/v1/chat/completions` | `opencode`, Cline's OpenAI-compatible provider |

nginx forwards only `/v1/chat/completions`, `/v1/responses...`, `/v1/models...`, `/v1/messages` and `/v1/messages/count_tokens`. Everything else on `api.` (LiteLLM's UI, `/metrics`, key management) returns 404 at the edge. Add `/v1/embeddings` to the regex if it is ever needed.

LiteLLM sends `/v1/messages` straight through to llama-server's own Anthropic endpoint, so thinking blocks survive (see [quirks](#known-quirks-and-gotchas)). Chat and Responses are translated by LiteLLM.

### Telemetry

```
browser --HTTPS--> Covenant nginx (${SPARK_TELEMETRY_HOST})
        --> oauth2-proxy 127.0.0.1:4180 (Pocket-ID OIDC, group ${TELEMETRY_GROUP})
        --wg0--> telemetry app ${BACKEND_WG_IP}:3200 --> Prometheus 127.0.0.1:9090
```

- `/api/*` returns a JSON 401 instead of a login redirect. `/api/v1/stream` (server-sent events every 2 s) is unbuffered in nginx.
- The app runs Prometheus queries fixed on the server side and shares one cached snapshot between all viewers, so viewer count does not drive query load. It uses about 28 MiB of RAM.
- Endpoints: `/api/v1/telemetry`, `/api/v1/stream`, `/healthz`.

## Ports and bind addresses

### Walter

| Service | Bind | Reachable from | Gate |
|---|---|---|---|
| Open WebUI | `${BACKEND_WG_IP}:3000` | `${EDGE_WG_IP}` | `WALTER-PUBLISHED` |
| Pocket-ID | `${BACKEND_WG_IP}:1411` | `${EDGE_WG_IP}` | `WALTER-PUBLISHED` |
| LiteLLM | `${BACKEND_WG_IP}:4000` | `${EDGE_WG_IP}`, Walter | `WALTER-PUBLISHED` |
| Postgres 17 | gateway docker network only | LiteLLM | not published |
| telemetry app | `${BACKEND_WG_IP}:3200` (host network) | `${EDGE_WG_IP}` | ufw on `wg0` |
| llama-swap | `${BACKEND_WG_IP}:8080` | `${GATEWAY_DOCKER_SUBNET}`, Walter | ufw + API key |
| llama-server instances | `127.0.0.1:10001+` | llama-swap | loopback |
| Prometheus / Grafana | `127.0.0.1:9090` / `127.0.0.1:3001` | SSH tunnel | loopback |
| node-exporter, cAdvisor, dcgm-exporter, blackbox | `127.0.0.1` | Prometheus | loopback |
| SSH | `${BACKEND_LAN_IP}:22` | home LAN | |

### Covenant

| Service | Bind | Reachable from |
|---|---|---|
| nginx | `:80`, `:443` | internet |
| WireGuard | `udp/${WG_PORT}` | internet (Walter dials in) |
| SSH | `:22` | internet, fail2ban-protected |
| oauth2-proxy | `127.0.0.1:4180` | nginx |

### Workstation

| Service | Bind | Reachable from |
|---|---|---|
| hermes-gateway | `${HYPERVISOR_BRIDGE_IP}:8642` | the libvirt bridge (Walter) |

## Trust boundaries and firewall model

```
internet | Covenant (ufw, fail2ban, nginx allowlist + limits) | wg0 | Walter (DOCKER-USER + ufw) | loopback (models, monitoring)
```

1. **Internet to Covenant.** ufw allows 22, 80, 443 and `udp/${WG_PORT}`; the cloud security group matches. fail2ban runs `sshd` (with `journalmatch` for Ubuntu 24.04's `ssh.service`), `nginx-limit-req` and `recidive`. An unknown `Host` gets nginx's 444. The apex redirects to `chat.`.
2. **Covenant to Walter.** The only path is the tunnel. On Walter, Docker-published ports bypass ufw, so `docker-user-rules.service` installs a chain `WALTER-PUBLISHED` hooked from `DOCKER-USER` that admits ports 3000, 1411 and 4000 only from `${EDGE_WG_IP}` and drops the rest. The telemetry app uses host networking, which bypasses that chain, so it is gated by ufw instead: `allow in on wg0 from ${EDGE_WG_IP} to ${BACKEND_WG_IP} port 3200`. (Port 3200 is also listed in the chain script for consistency.)
3. **Gateway to models.** ufw allows `${BACKEND_WG_IP}:8080` only from `${GATEWAY_DOCKER_SUBNET}`; the gateway compose network is pinned to that subnet so the rule stays valid. llama-swap also requires its own API key. llama-server containers bind loopback.
4. **Forwarded headers.** Every app behind the edge trusts `X-Forwarded-*` from `${EDGE_WG_IP}` only:
   - Open WebUI: `FORWARDED_ALLOW_IPS=${EDGE_WG_IP}`
   - Pocket-ID: `TRUST_PROXY=true`, reachable only from the edge
   - LiteLLM: `trusted_proxy_ranges` = `${EDGE_WG_IP}/32`

   This keeps client IPs correct for rate limiting and logs without letting anything else spoof them.
5. **Identity.** Pocket-ID is the only IdP. `/setup` is locked to `${ADMIN_SOURCE_IPS}` in nginx until the admin is claimed; after that the lock snippet is removed and the setup API returns 404.

## llama-swap matrix and GPU placement

GPU0 sits on the CPU's x16 slot; GPU1 sits on a chipset x1 slot. Single-GPU models are pinned with `--gpus device=N -sm none`; split models use `-sm layer -ts 1,1`. Every model runs `--fit off -ngl all -np 1 -fa on -ctk q8_0 -ctv q8_0 --jinja --metrics`, so a model that does not fit fails at load instead of shrinking.

| Model ID | Alias | GPU | Context | Extra |
|---|---|---|---|---|
| `qwen3.8-27b` | `coder` | 0 | 131072 | MTP: `--spec-type draft-mtp --spec-draft-n-max 2` (22.6 GB) |
| `qwen3.6-35b-a3b` | `coder-fast` | 1 | 131072 | |
| `qwen3-coder-next` | `big` | 0+1 | 131072 | `ttl` 1800 s, evict cost 5 |
| `gemma-4-31b` | `vision` | 0+1 | 131072 | `--mmproj`, `--ctx-checkpoints 8`, `ttl` 1800 s |
| `hermes-4.3-36b` | `hermes` | 0+1 | 65536 | `ttl` 1800 s |

Matrix sets (which models may be resident together):

| Set | Members | When |
|---|---|---|
| `coding` | `coder` + `coder-fast` | Default. Preloaded at startup. |
| `big` | `big` | Evicts the pair; unloads after 30 min idle. |
| `vision` | `vision` | Evicts the pair. |
| `hermes` | `hermes` | Evicts the pair. |

The `big` model has an eviction cost of 5 because loading it moves about 40 GB, half of it over the x1 link. After a split model unloads, the next `coder` or `coder-fast` request reloads the pair (about 9 s and 16 s).

Each model runs as a llama.cpp `server-cuda` container pinned by digest. Containers start through `spark-docker-run.sh`, which waits until the previous container of the same name is gone. Without it, a config reload raced the old container's removal and left both models down until the next request. `cmdStop` uses `docker rm -f` because it is synchronous.

## The gateway hook: mid-conversation system messages

Claude Code inserts `system` or `developer` messages in the middle of a conversation, for example environment updates and tool changes. Qwen's chat template rejects them with "System message must be at the beginning". llama-server turns that into HTTP 500, and Claude Code then retries for about three minutes.

The fix lives in the gateway, not the client. `spark_hooks.py` is a LiteLLM pre-call hook (shipped under [walter/](walter/README.md)) that rewrites every system or developer message after the first into a user message wrapped in `<system-reminder>` tags. It covers `/v1/messages`, `/v1/chat/completions` and `/v1/responses`.

The same hook also caps output. A request without `max_tokens` (`max_output_tokens` on `/v1/responses`) gets 16384, and a limit above the model's maximum (32768 for `coder`, `coder-fast` and `big`, 16384 for `vision` and `hermes`) is clamped to it. llama-server additionally runs with `-n` set to the same maximum, but on the pinned build `-n` is only a default that a request can exceed, so the gateway clamp is the real limit. Root cause: a request with no `max_tokens` ran for 21 minutes to the 131K context limit ([walter/README.md](walter/README.md#output-cap-no-unbounded-generations)).

Why here:
- It protects every harness and every model behind the alias, not only one client config.
- The client-side workaround was an undocumented Claude Code variable (`CLAUDE_CODE_MODEL_CAPABILITIES="-mid_conv_system,-mid_conv_tool_change"`) found in the binary, which can break on any upgrade. It is no longer needed.
- It also fixed `coder-fast` silently dropping Claude Code's environment information.

## The agent judge

Hermes, running on the local models, is reviewed by a judge that works from evidence rather than from the agent's own account ([judge/](judge/README.md), rationale in [docs/agent-judge.md](docs/agent-judge.md)). It runs entirely on the workstation, as Hermes shell hooks in `~/.hermes/config.yaml` plus systemd user units for the runner and the runaway watcher:

- **Gates are synchronous and deterministic.** `pre_tool_call` escalates host-mutating commands for Walter or Covenant, writes to sensitive paths, and reads of secret-shaped paths (terminal or `read_file`) to the human, and blocks edits to allowlists, hooks and approval settings. `pre_verify` runs fast checks (`bash -n`, parsers, `check-sanitized.sh`) before the agent may stop, and records every result.
- **Judging is asynchronous.** Hooks drop one request per turn in `~/.hermes/review/queue/`; a path unit runs the collector (logs, gate decisions with whether each call ran, diffs against a session-start snapshot minus Hermes's bookkeeping files, `pre_verify` results, host `find -newermt` in UTC, host-state probes chosen from the agent's claims, llama-server slots, read-only probes) and then the judge. Findings with evidence come back to the agent at the next turn through `pre_llm_call`, as data. The agent can acknowledge a finding, but only the human closes a high one.
- **Data boundary.** Only infra-class evidence goes to the frontier judge (the normal Anthropic API, not this gateway); anything else goes to a local alias through the gateway, with diff stats instead of contents. A sensitive completion additionally gets a frontier review of a claims-only bundle: the agent's final answer (redacted, paths masked) and gate/C3/tool metadata, never file contents, diffs, paths or the user's message ([decision 10](docs/decisions.md#10-frontier-review-of-claims-only-bundles-for-sensitive-sessions)). A validator downgrades any `false` verdict that does not quote contradicting evidence. Local findings are advisory: capped at medium severity and not shown to the agent by default, because the first measured run found a same-family local judge unreliable.
- **Read-only toward hosts.** The judge reaches Walter and Covenant only through an allowlist of read-only probes over SSH. A runaway watcher polls llama-server slots and alerts; it never cancels a generation.

## Backups and restore

- `spark-backup.timer` runs `spark-backup.sh` daily at 03:30 UTC (up to 10 min random delay) with `Persistent=true`, so a run missed while Walter was off happens at the next boot.
- Snapshots go to `${MODELS_DIR}/backups/<YYYY-mm-dd_HHMM>/`, on a different disk from the root filesystem. Retention: the newest of each of the last 14 days plus the newest of each of the last 8 Sundays. `LAST_OK` names the last full success.
- Contents: `pg_dump -Fc` of LiteLLM plus roles; online SQLite `.backup` of Open WebUI and Pocket-ID (integrity-checked); `pocket-id export`; volume tarballs without the live DB files; a config tarball of `/srv/*/`, `/etc/llama-swap`, units, scripts, ufw rules, fstab and the Hermes config; a manifest of versions and digests; `SHA256SUMS`.
- Snapshots are root-only (700/600) and **not encrypted**; they contain every secret.
- Offsite: `spark-offsite.timer` (04:30 UTC, after the local backup) copies `${MODELS_DIR}/backups` with restic to a private, versioned S3 bucket, using an IAM user that can touch only that bucket. restic encrypts client-side; the repo password lives only in `/etc/spark-restic/password` (outside the backed-up tree) and in the owner's offline copy. Retention 14 daily, 8 weekly, 6 monthly; a 25% data check every Sunday. Setup, restore from a fresh machine and removal: [walter/backup/OFFSITE.md](walter/backup/OFFSITE.md).
- `spark-backup-restore-test.sh` restores the latest snapshot into throwaway containers and checks it. It is non-destructive.
- Restore steps per component, and a whole-VM rebuild, ship with the backup units in [walter/](walter/README.md) and are installed as `${MODELS_DIR}/backups/RESTORE.md`. Two traps: a restored Pocket-ID needs `DELETE FROM francis_hosts;` (a stale cluster heartbeat) before it starts, and Pocket-ID's signing keys are encrypted with `ENCRYPTION_KEY`, so the env file must come from the same snapshot.

## Monitoring and telemetry

- `/srv/monitoring` on Walter: Prometheus, Grafana, node-exporter, cAdvisor, dcgm-exporter, blackbox and a small llama-swap target discovery that adds each running llama-server's `/metrics`. All bind 127.0.0.1. 16 targets, 12 alert rules, 3 dashboards.
- Access: `ssh -N -L 3001:127.0.0.1:3001 -L 9090:127.0.0.1:9090 ${BACKEND_SSH_USER}@${BACKEND_LAN_IP}`.
- LiteLLM `/metrics` is unauthenticated (an accepted trade-off). Only Walter and Covenant can reach it, and nginx on `api.` returns 404 for it.
- The telemetry dashboard (above) is the read-only, browser-friendly view of the same Prometheus data.
- Telemetry gaps: llama-server updates its token counters only when a request finishes, so live speed is shown as decode steps per second; there is no KV-cache usage metric; per-key spend is not shown (costs are 0).

## Update check

`spark-update-check.timer` runs Mondays at 09:00 UTC (`Persistent=true`). It is **report-only**: it never pulls, upgrades or restarts.

- It checks every image in `/srv/*/compose.yaml` and in the llama-swap config for digest drift against the registry, compares GitHub releases (llama.cpp `bNNNN`, LiteLLM, Open WebUI, Pocket-ID, llama-swap, hermes-agent), and evaluates GitHub security advisories against the running version. For a digest-only image reference it reads the tag from the comment next to it, so keep those comments accurate.
- It also reports pending apt and `-security` updates, reboot-required, held packages, the NVIDIA driver against the apt candidate, and Docker and container-toolkit versions.
- Output: `/var/lib/spark-update-check/latest.md`, plus a one-line summary in the login banner. Exit codes: 0 OK, 10 updates available, 20 advisories affect current versions, 1 checker error.
- Covenant has no inbound key from Walter, so its check (`spark-edge-check`: apt security count, cert expiry, ufw, fail2ban, `nginx -t`) runs manually from the workstation.

How to act on the report: [docs/runbooks/upgrade.md](docs/runbooks/upgrade.md).

## Known quirks and gotchas

**Models and llama.cpp**
- Qwen and Gemma think by default. A small `max_tokens` returns empty content; clients need a bigger budget or `enable_thinking: false`.
- Output is never unbounded: the gateway sets `max_tokens` 16384 when a request has none and clamps larger values to the model's maximum (32768 or 16384). A long thinking answer can end with `finish_reason: length`; ask for more explicitly, up to the maximum.
- The startup preload logs `status 404` for each model, yet both models load and stay warm. Harmless; cause unknown.
- Loads over the x1 link are slow: `coder-fast` ~16 s, `big` ~20 s.

**LiteLLM**
- In LiteLLM 1.103.x, `openai/` deployments route `/v1/messages` through the Responses adapter, which drops llama-server's reasoning. Every alias therefore sets `model_info.supported_endpoints: ["/v1/chat/completions","/v1/responses","/v1/messages"]`, so Messages pass straight through to llama-server. Keep this on new aliases.
- Reasoning is not returned as Anthropic `thinking` blocks on the translated paths, and the `/v1/responses` reasoning summary is empty.
- `count_tokens` is estimated by LiteLLM and undercounts; it also logs a harmless 404 ERROR.
- Settings: `drop_params: true`, 900 s timeouts, `num_retries: 0`.

**Harnesses**
- Claude Code sends about 15K tokens of system prompt and tools per request. Context is set to 131072 with auto-compact at 85% and max output 16384. Its default `auto` permission mode runs the safety classifier on the local model; use `--permission-mode default` or `acceptEdits` for anything sensitive.
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
- Hypervisor: refer to the models NVMe by `/dev/disk/by-id/...`, never `/dev/nvmeXn1`; the kernel names have swapped between boots.
- Covenant: Ubuntu 24.04 names the SSH unit `ssh.service`, so the stock fail2ban `sshd` jail matches nothing without a `journalmatch` override.
