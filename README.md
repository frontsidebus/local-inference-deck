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
  browser / phone                     workstation harnesses
  (passkey via Pocket-ID)             (claude-spark, codex-spark, hermes-spark, opencode)
         |                                       |
         | HTTPS                                 | HTTPS + bearer key
         v                                       v
+---------------------------------------------------------------------+
| Covenant (cloud edge, EDGE_PUBLIC_IP)                                |
|  nginx :443  chat.  -> Open WebUI        api. -> LiteLLM (allowlist) |
|              id.    -> Pocket-ID         telemetry. -> oauth2-proxy  |
|  ufw 22/80/443/udp WG_PORT, fail2ban, certbot                        |
+-------------------------------+-------------------------------------+
                                | WireGuard  EDGE_WG_IP <-> BACKEND_WG_IP
                                v
+---------------------------------------------------------------------+
| Walter (backend VM at home, 2x RTX 3090, /models disk)              |
|                                                                     |
|  Open WebUI :3000 --+                                               |
|  Pocket-ID  :1411   |                                               |
|                     v                                               |
|  LiteLLM :4000 (+Postgres) --> llama-swap :8080 --> llama-server    |
|  spark_hooks.py              (matrix, on-demand)    containers      |
|                                                     GPU0 / GPU1     |
|  telemetry :3200    monitoring (127.0.0.1)    spark-backup timer    |
+---------------------------------------------------------------------+
          |  (libvirt bridge, HYPERVISOR_BRIDGE_IP)
          v
  workstation: hermes-gateway :8642  ("Hermes Agent" model, admin only)
```

Every Walter port binds to `BACKEND_WG_IP` (or loopback) and only accepts traffic from `EDGE_WG_IP`. The request paths, ports and firewall layers are in [ARCHITECTURE.md](ARCHITECTURE.md).

## Components

| Dir | What it holds |
|---|---|
| [walter/](walter/README.md) | Backend VM: llama-swap config, gateway (LiteLLM + Postgres + hooks), webui (Open WebUI + Pocket-ID + theme), monitoring, telemetry app, the optional [digest app](walter/digest/README.md), backups, update-check, systemd units and drop-ins, firewall. |
| [covenant/](covenant/README.md) | Edge: nginx sites and snippets, oauth2-proxy (plus a second instance for the optional digest), fail2ban, ufw, WireGuard, certbot hooks. |
| [clients/](clients/README.md) | Workstation wrappers and configs for Claude Code, Codex, Hermes and OpenCode, plus the hermes-gateway service. |
| [judge/](judge/README.md) | A reviewer for the local agent: Hermes shell hooks gate risky tool calls and queue reviews; a frontier (or local) judge checks the agent's claims against collected evidence. See [docs/agent-judge.md](docs/agent-judge.md). |
| `hypervisor/` | Pointer only. The host layer (VFIO, libvirt domain, disk passthrough) lives in a private infrastructure repo. |
| [docs/](docs/) | [Decisions](docs/decisions.md), [runbooks](docs/runbooks/) and [history](docs/history/gen2/INDEX.md). |
| `scripts/` | `render.sh` (templates), `gen-secrets.sh` (secrets on the target host), `check-sanitized.sh` (pre-commit guard). |

Site-specific values (domain, IPs, user list) live in `site.env`, which is gitignored. Copy [site.env.example](site.env.example) and fill it in. Templates (`*.tmpl`) are rendered by `scripts/render.sh` with an explicit variable list. Secrets are never in the repo: `scripts/gen-secrets.sh` creates them on the host and never overwrites an existing one. See [CONVENTIONS.md](CONVENTIONS.md).

## Quickstart

The order matters; each step assumes the one before works. Deploy steps live in each component's README.

1. **Hypervisor and VM.** A KVM host with both GPUs bound to `vfio-pci` and passed to the backend VM, the models NVMe attached as a raw disk, about 80 GiB of VM RAM, and libvirt autostart on the VM. This layer is not in this repo (see `hypervisor/`).
2. **Walter.** Install the NVIDIA driver, the container toolkit and Docker, mount `${MODELS_DIR}`, download the GGUFs, then run `walter/deploy.sh`. Verify that llama-swap answers on `${BACKEND_WG_IP}:8080` and LiteLLM on `:4000`. See [walter/README.md](walter/README.md).
3. **Covenant.** Bring up WireGuard first and check that `ping ${BACKEND_WG_IP}` works from the edge. Then run `covenant/deploy.sh` for nginx, ufw, fail2ban and oauth2-proxy. See [covenant/README.md](covenant/README.md).
4. **DNS and TLS.** Point A records for `chat`, `api`, `id` and `telemetry` at `EDGE_PUBLIC_IP`, then issue one certificate per host with certbot. The deploy hook reloads nginx.
5. **Identity bootstrap.** Pocket-ID's `/setup` page is locked to `ADMIN_SOURCE_IPS` until you claim the admin account. Claim it **immediately**, then remove the lock. Create the OIDC clients (`open-webui`, `telemetry`) and the groups `${CHAT_GROUP}` and `${TELEMETRY_GROUP}`, then add users ([runbook](docs/runbooks/add-user.md)).
6. **Clients.** Install the wrappers from [clients/](clients/README.md), put each harness key in `~/.config/spark/<harness>.key` (mode 600), and run each wrapper's smoke test.

## Hardware and what it taught us

| Part | Detail |
|---|---|
| Host | AM5 desktop (X870 board, 16-core CPU, 128 GB RAM), KVM/libvirt |
| GPUs | 2x RTX 3090 24 GB (48 GB total), both passed through to Walter |
| Models disk | 4 TB NVMe, passed to Walter as a raw virtio disk, XFS at `/models` |
| Edge | t3.small-class cloud VM (2 GB RAM) with an Elastic IP |

Lessons:

- **The second GPU runs on a PCIe x1 link.** On this board the second full-length slot is a chipset x1 slot. It works fine for inference once a model is loaded, but loading weights across it is slow. That is why `coder-fast` takes about 16 s to load and the 40 GB split models are expensive to swap in. A x8/x8 bifurcation riser is planned.
- **No NVLink and no P2P, so no tensor parallel.** Tensor parallel needs an all-reduce every layer and would choke on x1. The stack uses one model per GPU, or a layer split (`-sm layer`) across both, which only passes one small activation per token across the link.
- **One model per GPU beats one big split.** The default working pair, `coder` on GPU0 and `coder-fast` on GPU1, stays loaded together. Larger models evict that pair on demand ([matrix](ARCHITECTURE.md#llama-swap-matrix-and-gpu-placement)).
- **MTP speculative decoding is nearly free speed.** Qwen3.8-27B carries its own multi-token-prediction head. With `--spec-type draft-mtp --spec-draft-n-max 2`, `coder` decodes at about 74 tok/s instead of 40.5 (+83%) with thinking off and about 70 instead of 40 with thinking on. It costs about 1.3 GB of VRAM, keeps the full 128K context, and greedy output is byte-identical.
- **`--fit off` everywhere.** Each model is sized to fail loudly at load time instead of silently shrinking its context.

## Models

Harnesses use aliases. The concrete IDs are also available as `local/<id>`, and any `claude-*` model name maps to `coder` as a safety net.

| Alias | Model (GGUF) | GPU | Context | Decode tok/s | Notes |
|---|---|---|---|---|---|
| `coder` | Qwen3.8-27B UD-Q4_K_XL | 0 | 128K | ~74 (MTP) | Default for chat and agents. |
| `coder-fast` | Qwen3.6-35B-A3B UD-IQ4_NL_XL | 1 | 128K | ~137 | MoE; Claude Code's Haiku tier, titles, subagents. |
| `big` | Qwen3-Coder-Next 80B-A3B UD-IQ4_NL | 0+1 | 128K | ~117 | Evicts the coding pair. Unloads after 30 min idle. |
| `vision` | Gemma 4 31B QAT UD-Q4_K_XL + mmproj | 0+1 | 128K | ~39 | Image input. Thinks by default. |
| `hermes` | Hermes 4.3 36B Q4_K_M | 0+1 | 64K | ~34 | Hermes Agent's native model. |
| `hermes-agent` | Hermes Agent on the workstation | n/a | n/a | n/a | Open WebUI only, admins only. Runs tools on the workstation. |

Qwen and Gemma think by default: a small `max_tokens` can return empty content. Give them a larger budget or send `enable_thinking: false`.

## Security posture

- **Public surface:** only Covenant, with ufw allowing 22, 80, 443 and the WireGuard UDP port. fail2ban runs the `sshd`, `nginx-limit-req` and `recidive` jails. Root SSH login is off.
- **Authentication:** passkeys via Pocket-ID for chat and telemetry (group-gated), bearer virtual keys for the API. Requests to `api.` without credentials get 401 at the edge. There is no basic auth and there are no shared passwords.
- **API allowlist:** nginx forwards only the chat, responses, models, messages and count_tokens paths. LiteLLM's admin UI and `/metrics` are not reachable from the internet.
- **Rate limits:** per-IP limits in nginx (login endpoints 5 r/m; API 10 r/s burst 40, 20 connections) and per-key limits in LiteLLM (60 rpm, 4 parallel requests).
- **Backend isolation:** Walter's services bind to the WireGuard address, a `DOCKER-USER` chain (`WALTER-PUBLISHED`) and ufw admit only the edge, and every app trusts forwarded headers from `EDGE_WG_IP` only.
- **Secrets:** generated on the host, root-owned, mode 600, never committed; keys reach harnesses via files, not configs.
- **Updates:** pinned digests, a weekly report-only update and advisory check, unattended Ubuntu security upgrades.
- **Known trade-offs:** backups are local and unencrypted (root-only on the models disk; offsite is planned). The `hermes-agent` model executes tools on the workstation, so it is admin-only. LiteLLM `/metrics` is unauthenticated but reachable only from Walter and Covenant.

The stack is model-agnostic. For security work (code review, log triage, data that cannot leave the building), a strong general model with tools usually beats a small security-specific model without them. Small specialist models such as Foundation-sec-8B fit best as a cheap first-pass classifier in front of `coder`. Build an evaluation set from your own past findings before trusting either.

## Further reading

- [ARCHITECTURE.md](ARCHITECTURE.md): request paths, ports, trust boundaries, the llama-swap matrix, the gateway hook, backups, monitoring, quirks.
- [docs/decisions.md](docs/decisions.md): why llama.cpp and not vLLM, why nginx, why LiteLLM, and more.
- [docs/agent-judge.md](docs/agent-judge.md): why and how a judge reviews the local agent, and the [pilot runbook](docs/runbooks/agent-judge-pilot.md).
- [docs/runbooks/](docs/runbooks/): [power loss](docs/runbooks/power-loss-recovery.md), [rotate secrets](docs/runbooks/rotate-secrets.md), [add a user](docs/runbooks/add-user.md), [add a model](docs/runbooks/add-model.md), [upgrade](docs/runbooks/upgrade.md), [deploy the digest](docs/runbooks/digest-deploy.md).
