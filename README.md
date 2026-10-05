# 343 Guilty Spark

> "I am the Monitor of Installation 04. I am 343 Guilty Spark."

A self-hosted LLM stack: two RTX 3090s at home, a thin edge in the cloud, and one OpenAI- and Anthropic-compatible gateway that every coding agent and chat user goes through.

- **Chat:** Open WebUI at `https://${SPARK_CHAT_HOST}`, signed in with a passkey through Pocket-ID.
- **API:** LiteLLM at `https://${SPARK_API_HOST}/v1`, with one virtual key per person and per harness. It speaks `/v1/chat/completions`, `/v1/responses` (Codex) and `/v1/messages` (Claude Code).
- **Harnesses:** Claude Code, Codex, Hermes Agent, OpenCode and Cline all point at the same endpoint and the same stable model aliases (`coder`, `coder-fast`, `big`, `vision`, `hermes`).
- **Telemetry:** a live GPU and request dashboard at `https://${SPARK_TELEMETRY_HOST}`, behind the same passkey login.
- **Digest (optional):** on-demand threat-intel and AI digests curated by the local `coder` model at `https://${SPARK_DIGEST_HOST}`, behind its own passkey gate. Off unless `SPARK_DIGEST_HOST` is set.

This is the third generation of the project. Gen2 (Ollama, basic auth and a static chat page) is archived in [docs/history/gen2](docs/history/gen2/INDEX.md).

## Why it is built this way

- **The GPUs stay at home.** The backend VM (**Walter**) has no public address and sits behind home NAT. It dials out to the edge over WireGuard.
- **The edge is small and dumb.** **Covenant** is a t3.small-class cloud VM. It terminates TLS, rate-limits, bans abusers and forwards to Walter over the tunnel. It holds no model, no database and no user data, so losing it costs one rebuild.
- **One gateway for every harness.** LiteLLM translates between the Anthropic, Responses and Chat Completions APIs and llama.cpp. Harness configs name an alias, never a model file, so swapping a model is a server-side change.
- **Passkeys, not passwords.** Pocket-ID is the only identity provider. Signup is off; an admin creates each user and sends a one-time link to register a passkey. Group membership decides who sees chat and who sees telemetry.
- **Everything is pinned.** Container images are pinned by digest and binaries by version and sha256. A weekly report says what is out of date; nothing updates itself except Ubuntu security patches.

## Architecture

```
  browser / phone                     API clients and harnesses
  (passkey via Pocket-ID)             (claude-spark, codex-spark, hermes-spark, opencode)
         |                                       |
         | HTTPS                                 | HTTPS + per-harness key
         v                                       v
+---------------------------------------------------------------------+
| Covenant (cloud edge, EDGE_PUBLIC_IP)                                |
|  nginx :443  chat.  -> Open WebUI        api. -> LiteLLM (allowlist) |
|              id.    -> Pocket-ID         telemetry. / digest.        |
|                                            -> oauth2-proxy -> app   |
|  ufw 22/80/443/udp WG_PORT, fail2ban, certbot                        |
+-------------------------------+-------------------------------------+
                                | WireGuard  EDGE_WG_IP <-> BACKEND_WG_IP
                                v
+---------------------------------------------------------------------+
| Walter (backend VM at home, 2x RTX 3090 Gen4 x8/x8, /models disk)   |
|                                                                     |
|  Open WebUI :3000 --+                                               |
|  Pocket-ID  :1411   |                                               |
|                     v                                               |
|  LiteLLM :4000 (+Postgres) --> llama-swap :8080 --> llama-server    |
|  spark_hooks.py              (matrix, on-demand)    containers      |
|                                                     GPU0 / GPU1     |
|  telemetry :3200   digest :3300   monitoring (127.0.0.1)   backups  |
+---------------------------------------------------------------------+
          |  (libvirt bridge, HYPERVISOR_BRIDGE_IP)
          v
  workstation / hypervisor: hermes-gateway :8642 ("Hermes Agent", admin only),
  harness wrappers, agent judge (user units)
```

Every Walter port binds to `BACKEND_WG_IP` (or loopback) and only accepts traffic from `EDGE_WG_IP`. The four request paths, ports, firewall layers, model placement, ops and security model are in [ARCHITECTURE.md](ARCHITECTURE.md).

## Components

| Dir | What it holds |
|---|---|
| [walter/](walter/README.md) | Backend VM: llama-swap config, gateway (LiteLLM + Postgres + hooks), webui (Open WebUI + Pocket-ID + theme), monitoring, telemetry app, the optional [digest app](walter/digest/README.md), backups, update-check, systemd units and drop-ins, firewall. |
| [covenant/](covenant/README.md) | Edge: nginx sites and snippets, oauth2-proxy (plus a second instance for the optional digest), fail2ban, ufw, WireGuard, certbot hooks. |
| [clients/](clients/README.md) | Workstation wrappers and configs for Claude Code, Codex, Hermes and OpenCode, plus the hermes-gateway service. |
| [judge/](judge/README.md) | A reviewer for the local agent: Hermes shell hooks gate risky tool calls and queue reviews; a frontier (or local) judge checks the agent's claims against collected evidence. See [docs/agent-judge.md](docs/agent-judge.md). |
| [hypervisor/](hypervisor/README.md) | Requirements only. The host layer (VFIO, libvirt domain, disk passthrough) lives in a private infrastructure repo. |
| [docs/](docs/) | [Decisions](docs/decisions.md), the [agent judge design](docs/agent-judge.md), [runbooks](docs/runbooks/) and [history](docs/history/gen2/INDEX.md). See the [docs map](#docs-map). |
| `scripts/` | `render.sh` (templates), `gen-secrets.sh` (secrets on the target host), `check-sanitized.sh` (pre-commit guard). |

Site-specific values (domain, IPs, user list) live in `site.env`, which is gitignored. Copy [site.env.example](site.env.example) and fill it in. Templates (`*.tmpl`) are rendered by `scripts/render.sh` with an explicit variable list. Secrets are never in the repo: `scripts/gen-secrets.sh` creates them on the host and never overwrites an existing one. See [CONVENTIONS.md](CONVENTIONS.md).

## Quickstart

The order matters; each step assumes the one before works. Deploy steps live in each component's README.

1. **Hypervisor and VM.** A KVM host with both GPUs bound to `vfio-pci` and passed to the backend VM, the models NVMe attached as a raw disk, about 80 GiB of VM RAM, and libvirt autostart on the VM. This layer is not in this repo (see `hypervisor/`).
2. **Walter.** Install the NVIDIA driver, the container toolkit and Docker, mount `${MODELS_DIR}`, download the GGUFs, then run `walter/deploy.sh`. Verify that llama-swap answers on `${BACKEND_WG_IP}:8080` and LiteLLM on `:4000`. See [walter/README.md](walter/README.md).
3. **Covenant.** Bring up WireGuard first and check that `ping ${BACKEND_WG_IP}` works from the edge. Then run `covenant/deploy.sh` for nginx, ufw, fail2ban and oauth2-proxy. See [covenant/README.md](covenant/README.md).
4. **DNS and TLS.** Point A records for `chat`, `api`, `id` and `telemetry` (and the digest host, if used) at `EDGE_PUBLIC_IP`. `covenant/deploy.sh` serves an ACME-only port-80 stub, issues one certificate per host with certbot, then enables the full sites. The deploy hook reloads nginx on renewal.
5. **Identity bootstrap.** Pocket-ID's `/setup` page is locked to `ADMIN_SOURCE_IPS` until you claim the admin account. Claim it **immediately**, then remove the lock. Create the OIDC clients (`open-webui`, `telemetry`) and the groups `${CHAT_GROUP}` and `${TELEMETRY_GROUP}`, then add users ([runbook](docs/runbooks/add-user.md)).
6. **Clients.** Run `clients/install.sh` ([clients/](clients/README.md)), put each harness key in `~/.config/spark/<harness>.key` (mode 600), and run each wrapper's smoke test. Add `--with-hermes-gateway` for the admin-only Hermes Agent model in Open WebUI.
7. **Optional.** Offsite backups: set `RESTIC_BUCKET` and follow [OFFSITE.md](walter/backup/OFFSITE.md). Digest site: set `SPARK_DIGEST_HOST` and follow the [digest runbook](docs/runbooks/digest-deploy.md). Agent judge for Hermes: `judge/install.sh --apply --with-units --start` ([judge/](judge/README.md)).

## Hardware and what it taught us

| Part | Detail |
|---|---|
| Host | AM5 desktop (X870 board, 16-core CPU, 128 GB RAM), KVM/libvirt |
| GPUs | 2x RTX 3090 24 GB (48 GB total), both passed through to Walter |
| Models disk | 4 TB NVMe, passed to Walter as a raw virtio disk, XFS at `/models` |
| Edge | t3.small-class cloud VM (2 GB RAM) with an Elastic IP |

Lessons:

- **Link width matters for loading and for tensor split, not for layer split.** Until the riser went in (October 2026) the second GPU sat in a chipset PCIe x1 slot: fine for inference once loaded, but `coder-fast` took about 20 s to load. An x8/x8 bifurcation riser now puts both GPUs on CPU lanes at Gen4 x8. Weights upload at 10–13 GB/s per GPU, so cold loads are limited by the models disk (about 2.8 GB/s), and `coder-fast` loads in about 12 s.
- **No NVLink and no P2P, but tensor split still pays for dense models.** llama.cpp's experimental `-sm tensor` does an allreduce every layer through host shared memory (the container needs `--shm-size 2g`). On x8 that gives the dense split models `hermes` and `vision` +39–67 % decode, at the cost of 12–23 % prompt processing on long prompts. The 3B-active MoE `big` gains nothing and stays on layer split (`-sm layer`), which only passes one small activation per token across the link. Row split does not load on the pinned build.
- **One model per GPU beats one big split.** The default working pair, `coder` on GPU0 and `coder-fast` on GPU1, stays loaded together. Larger models evict that pair on demand ([matrix](ARCHITECTURE.md#models-and-gpu-placement)).
- **MTP speculative decoding is nearly free speed.** Qwen3.8-27B carries its own multi-token-prediction head. With `--spec-type draft-mtp --spec-draft-n-max 2`, `coder` decodes at about 74 tok/s instead of 40.5 (+83%) with thinking off and about 70 instead of 40 with thinking on. It costs about 1.3 GB of VRAM, keeps the full 128K context, and greedy output is byte-identical.
- **`--fit off` everywhere.** Each model is sized to fail loudly at load time instead of silently shrinking its context.

## Models

Harnesses use aliases. The concrete IDs are also available as `local/<id>`, and any `claude-*` model name maps to `coder` as a safety net.

| Alias | Model (GGUF) | GPU | Context | Decode tok/s | Notes |
|---|---|---|---|---|---|
| `coder` | Qwen3.8-27B UD-Q4_K_XL | 0 | 128K | ~74 (MTP) | Default for chat and agents. |
| `coder-fast` | Qwen3.6-35B-A3B UD-IQ4_NL_XL | 1 | 128K | ~137 | MoE; Claude Code's Haiku tier, titles, subagents. |
| `big` | Qwen3-Coder-Next 80B-A3B UD-IQ4_NL | 0+1 (layer split) | 128K | ~117 | Evicts the coding pair. Unloads after 30 min idle. |
| `vision` | Gemma 4 31B QAT UD-Q4_K_XL + mmproj | 0+1 (tensor split) | 128K | ~52 | Image input. Thinks by default. |
| `hermes` | Hermes 4.3 36B Q4_K_M | 0+1 (tensor split) | 64K | ~52 | Hermes Agent's native model. |
| `hermes-agent` | Hermes Agent on the workstation | n/a | n/a | n/a | Open WebUI only, admins only. Runs tools on the workstation. |

Qwen and Gemma think by default: a small `max_tokens` can return empty content. Give them a larger budget or send `enable_thinking: false`.

## Security posture

- **Public surface:** only Covenant, with ufw allowing 22, 80, 443 and the WireGuard UDP port. fail2ban runs the `sshd`, `nginx-limit-req` and `recidive` jails. Root SSH login is off.
- **Authentication:** passkeys via Pocket-ID for chat, telemetry and the digest (each group-gated; telemetry and digest through their own oauth2-proxy instance), bearer virtual keys for the API. Requests to `api.` without credentials get 401 at the edge. There is no basic auth and there are no shared passwords.
- **API allowlist:** nginx forwards only the chat, responses, models, messages and count_tokens paths. LiteLLM's admin UI and `/metrics` are not reachable from the internet.
- **Rate limits:** per-IP limits in nginx (login endpoints 5 r/m; API 10 r/s burst 40, 20 connections) and per-key limits in LiteLLM (60 rpm, 4 parallel requests).
- **Backend isolation:** Walter's services bind to the WireGuard address, a `DOCKER-USER` chain (`WALTER-PUBLISHED`) and ufw admit only the edge, and every app trusts forwarded headers from `EDGE_WG_IP` only.
- **Secrets:** generated on the host, root-owned, mode 600, never committed; keys reach harnesses via files, not configs.
- **Updates:** pinned digests, a weekly report-only update and advisory check, unattended Ubuntu security upgrades.
- **Backups:** local snapshots are unencrypted but root-only on the models disk; the offsite copy is restic-encrypted in a private S3 bucket.
- **Agent judge:** the local agent's risky tool calls are gated and its work is reviewed from evidence; only infra-class data reaches the frontier judge ([data boundary](ARCHITECTURE.md#the-judges-data-boundary)).
- **Known trade-offs:** The `hermes-agent` model executes tools on the workstation, so it is admin-only. LiteLLM `/metrics` is unauthenticated but reachable only from Walter and Covenant.

The stack is model-agnostic. For security work (code review, log triage, data that cannot leave the building), a strong general model with tools usually beats a small security-specific model without them. Small specialist models such as Foundation-sec-8B fit best as a cheap first-pass classifier in front of `coder`. Build an evaluation set from your own past findings before trusting either.

## Docs map

| Doc | What it covers |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Topology, the four request paths, ports, model placement, the gateway hook, the judge in brief, deploy model, secrets, backups, monitoring, security model, known issues. |
| [CONVENTIONS.md](CONVENTIONS.md) | Public-repo rules, `site.env` names, templating, secrets, sanitization checks, PR flow. |
| [docs/decisions.md](docs/decisions.md) | Why llama.cpp and not vLLM, why nginx, why LiteLLM, why Pocket-ID, and more. |
| [docs/agent-judge.md](docs/agent-judge.md) | Why and how a judge reviews the local agent; rubric, data boundary, pilot results. |
| **Components** | |
| [walter/README.md](walter/README.md) | Backend VM: layout, pinned versions, deploy, OIDC bootstrap, secrets, output cap, verify, rollback. |
| [walter/llama-swap/BENCHMARKS.md](walter/llama-swap/BENCHMARKS.md) | Model throughput, MTP, layer vs. tensor split. |
| [walter/backup/OFFSITE.md](walter/backup/OFFSITE.md) | restic offsite backups: setup, restore from a fresh machine, removal. |
| [walter/digest/README.md](walter/digest/README.md) | The optional digest app: pipeline, API, security model, operations. |
| [covenant/README.md](covenant/README.md) | The edge: layout, secrets, AWS setup, first deploy, digest instance, verify, rollback. |
| [clients/README.md](clients/README.md) | Harness wrappers and configs, keys, smoke tests, the optional hermes-gateway. |
| [judge/README.md](judge/README.md) | Installing and operating the judge: hooks, units, gate rules, configuration, data boundary, troubleshooting. |
| [judge/CONTRACT.md](judge/CONTRACT.md) | The binding interfaces between the judge's parts. |
| [judge/runner/units/README.md](judge/runner/units/README.md) | The judge's systemd user units. |
| [hypervisor/README.md](hypervisor/README.md) | What the host layer must provide. |
| **Runbooks** | |
| [power-loss-recovery.md](docs/runbooks/power-loss-recovery.md) | Bringing everything back after an outage. |
| [rotate-secrets.md](docs/runbooks/rotate-secrets.md) | Rotating keys and secrets. |
| [add-user.md](docs/runbooks/add-user.md) | Adding a person: Pocket-ID account, groups, LiteLLM key. |
| [add-model.md](docs/runbooks/add-model.md) | Adding a model and alias. |
| [upgrade.md](docs/runbooks/upgrade.md) | Acting on the weekly update report. |
| [digest-deploy.md](docs/runbooks/digest-deploy.md) | Deploying the optional digest site. |
| [agent-judge-pilot.md](docs/runbooks/agent-judge-pilot.md) | Running a judge pilot. |
| [agent-judge-rejudge.md](docs/runbooks/agent-judge-rejudge.md) | Re-judging stored bundles. |
| **History** | |
| [docs/history/gen2](docs/history/gen2/INDEX.md) | The retired Ollama-era setup and why it was replaced. |

## Status

As of October 2026 everything above is live:

- **Serving:** both GPUs on Gen4 x8/x8 since the bifurcation riser; the `coding` pair resident by default; `hermes` and `vision` on tensor split, `big` on layer split.
- **Sites:** chat, API, identity, telemetry and the digest site are up behind the edge; offsite backups run nightly.
- **Agent judge:** live on the workstation in frontier mode (local `coder-fast` for sensitive reviews, claims-only frontier review of sensitive completions), after two digest-site pilots.
- **Open items** are tracked in [ARCHITECTURE.md, Known issues / follow-ups](ARCHITECTURE.md#known-issues--follow-ups).
