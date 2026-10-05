# Gen2: the Ollama era (retired)

This directory is an archive. Nothing here is deployed or maintained. The current stack is described in the top-level [README](../../../README.md) and [ARCHITECTURE](../../../ARCHITECTURE.md).

## What gen2 was

Gen2 is the setup that ran until the end of September 2026:

- **Backend:** one Ollama service in the GPU-passthrough VM (Walter), serving Llama 3.3 70B Q4_K_M split across both RTX 3090s at about 19 tok/s. Later it served a handful of 27B–36B models, all at 16K context, each split across both GPUs.
- **Edge:** nginx on a small EC2 instance (Covenant), with TLS from Let's Encrypt and HTTP **basic auth** in front of everything.
- **UI:** a single static HTML page (`server/index.html`), a terminal-style chat that streamed from Ollama's OpenAI-compatible endpoint.
- **Link:** one WireGuard tunnel between the edge and the VM. This part survives into gen3 unchanged.
- **vLLM:** a parked launcher, unit and nginx block for tensor-parallel serving. It never ran in production because the second GPU sat on a PCIe x1 link at the time (both GPUs have been Gen4 x8 since the October 2026 bifurcation riser).

## Why it was replaced

An audit on 2026-09-30 found:

| Problem | Effect |
|---|---|
| Every model split across both GPUs | Loading one model evicted the other; no way to run two models at once. |
| 16K context | Too small for coding agents (Claude Code and Hermes need 64K+). |
| No Anthropic `/v1/messages` and no `/v1/responses` | Claude Code and Codex could not use the stack at all. |
| `/v1/` proxied to a dead vLLM port | The API path for agents was broken. |
| Basic auth on the public internet | About 900 failed brute-force attempts in the logs; one shared credential model, no per-user identity. |
| Static bearer tokens in the nginx config | Secrets in plaintext on the edge. |
| ufw off, no fail2ban | The edge had no host firewall or ban policy. |
| The SPA hardcoded a deleted model | The UI was broken whenever the model list changed. |

Gen3 replaces Ollama with llama-swap + llama.cpp, puts LiteLLM in front as the one gateway for every harness, replaces basic auth with Pocket-ID passkeys, and replaces the SPA with Open WebUI. See [decisions](../../decisions.md) for the reasoning.

## Contents

| Path | What it was |
|---|---|
| [README.md](README.md) | The gen2 front door: architecture, hardware notes, setup walkthrough. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | The gen2 handoff doc: component inventory, burn-in data, boot behaviour, work log. |
| `models/` | An Ollama Modelfile for Llama 3.3 70B with full GPU offload. |
| `nginx-configs/` | The basic-auth edge config (`spark-ollama.conf`) and the parked vLLM config (`nginx.conf`). |
| `scripts/` | `inference-baseline.sh` (throughput test) and `vllm-serve.bash` (parked vLLM launcher). |
| `server/index.html` | The single-page chat UI. |
| `unit-files/` | Ollama override and preload units, and the parked vLLM unit. |

## Redactions in this archive

These files were public on `main` before the move. When they were moved here, site-specific values were replaced with the project's example values so that the archive passes `scripts/check-sanitized.sh`: the live domain became `example.com`, the edge's public IP became `203.0.113.10`, LAN addresses became `192.168.122.x` / `192.168.0.10`, machine, VM and libvirt network names became `hypervisor-host`, `walter` and `lab` / `virbr-lab`, the host login name became `hostuser`, the edge WireGuard public key was removed, and the private infrastructure repo is referred to generically. The git history of `main` still holds the original text.
