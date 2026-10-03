# Decisions

Short records of the choices behind gen3. Each says what was decided, why, and what would change it. Dates are when the decision was made.

---

## 1. llama-swap + llama.cpp instead of vLLM

**Date:** 2026-09-30 · **Status:** accepted

**Context.** Two RTX 3090s, no NVLink, no P2P, and the second card on a PCIe x1 link. Agent harnesses need 64K–128K context and want several models available (a strong coder, a fast one, a vision model).

**Decision.** Serve GGUF models with llama.cpp's `llama-server`, one container per model, managed by llama-swap. llama-swap loads models on demand, and its `matrix` router decides which models may be resident together.

**Why.**
- vLLM's strengths are tensor parallel and high-concurrency batching. Tensor parallel needs an all-reduce per layer, which an x1 link cannot carry, and the load here is a handful of concurrent agents, not hundreds of users. Ampere has no FP8, so vLLM would also need a separate set of AWQ/GPTQ checkpoints.
- llama.cpp's layer split passes one small activation per token between GPUs, so the x1 link costs only load time.
- llama.cpp ships Anthropic `/v1/messages`, OpenAI chat, Jinja templates with tool calling, multimodal projectors and MTP speculative decoding (+74–83% decode on `coder`).
- llama-swap gives one endpoint, aliases and model swapping without a restart.

**Revisit when** a bifurcation riser gives both GPUs x8: a vLLM entry inside llama-swap becomes possible, preferring pipeline parallel or single-GPU over tensor parallel.

---

## 2. nginx instead of Caddy on the edge

**Date:** 2026-09-30 · **Status:** accepted

**Decision.** Keep nginx on Covenant.

**Why.** nginx has `limit_req` and `limit_conn` built in, which fail2ban's `nginx-limit-req` jail reads directly. It was already running with working certbot integration. Caddy's automatic TLS is nice but its rate limiting needs a third-party plugin and a custom build. The edge config is small (one file per host plus shared snippets) so nginx's verbosity costs little.

---

## 3. LiteLLM as the single gateway

**Date:** 2026-09-30 · **Status:** accepted

**Decision.** Every client, including Open WebUI, talks to LiteLLM. Nothing but LiteLLM talks to llama-swap.

**Why.**
- Each harness speaks a different API: Claude Code uses Anthropic Messages, Codex uses Responses, the rest use Chat Completions. LiteLLM serves all three from one endpoint.
- Stable aliases (`coder`, `coder-fast`, `big`, `vision`, `hermes`) decouple harness configs from model files. A `claude-*` catch-all keeps a misconfigured Claude Code working.
- Virtual keys per person and per harness give revocation, rate limits (60 rpm, 4 parallel) and usage attribution without touching the edge.
- One place for request fixes: the pre-call hook that rewrites mid-conversation system messages (see [ARCHITECTURE](../ARCHITECTURE.md#the-gateway-hook-mid-conversation-system-messages)).

**Costs.** A Postgres to run and back up; translation quirks (reasoning dropped on translated paths, so `/v1/messages` is passed through; estimated `count_tokens`). LiteLLM has a busy advisory history, so it is pinned and checked weekly.

---

## 4. Pocket-ID with passkeys for identity

**Date:** 2026-09-30 · **Status:** accepted

**Context.** Gen2 used HTTP basic auth and was being brute-forced (about 900 failed attempts).

**Decision.** Pocket-ID is the only identity provider. It is passkey-only. Open WebUI and the telemetry dashboard (through oauth2-proxy) use it over OIDC with PKCE, and access is group-gated (`${CHAT_GROUP}`, `${TELEMETRY_GROUP}`).

**Why.** Passkeys are phishing-resistant and leave nothing to brute-force or reuse. Pocket-ID is a single small container with SQLite, cheap to back up. Signup is off: an admin creates users and sends one-time registration links. Open WebUI keeps one local break-glass admin in case the IdP is down.

---

## 5. Raw-disk passthrough for the models disk

**Date:** 2026-09-30 · **Status:** accepted

**Decision.** Pass the 4 TB NVMe to Walter as a whole raw virtio disk (identified by a fixed serial), formatted XFS inside the VM and mounted at `/models` by UUID with `nofail`.

**Why.** Model loads are sequential reads of 20–40 GB; a raw disk avoids a qcow2 or host-filesystem layer. Keeping models off the VM's root disk keeps that disk small and lets backups land on a different device. Hypervisor-side references always use `/dev/disk/by-id`, because NVMe kernel names have swapped between boots.

**Trade-off.** The disk is tied to this VM; the hypervisor cannot use it while Walter runs.

---

## 6. Retire Ollama

**Date:** 2026-09-30 · **Status:** done

**Decision.** Stop and disable Ollama, delete its models, and gate the unit so nothing can start it by accident.

**Why.** Ollama split every model across both GPUs (one model at a time), ran at 16K context, had no Anthropic or Responses API and no per-key auth. llama-swap + llama.cpp covers everything it did with explicit GPU placement. Its binary stays installed but disabled; a drop-in requires `/etc/ollama-enabled`, because another unit's `Wants=` restarted it at one boot. A stale per-user Ollama unit was also disabled. Gen2 is archived in [history/gen2](history/gen2/INDEX.md).

---

## 7. Gateway and UI on the backend, not the edge

**Date:** 2026-09-30 · **Status:** accepted

**Decision.** LiteLLM, Postgres, Open WebUI and Pocket-ID run on Walter. Covenant stays a thin edge: TLS, rate limits, fail2ban and an API path allowlist.

**Why.**
- The edge is a small cloud VM (about 2 GB RAM); Open WebUI and LiteLLM would not fit comfortably.
- User data (chats, keys, identities) stays at home with the GPUs and is covered by the same backups.
- If the edge is compromised, the attacker gets TLS keys and a tunnel to services that still require their own auth, not the databases.
- Losing the edge costs one redeploy from this repo.

**Cost.** Chat, login and API are all down when the home connection or power is. That is acceptable because inference is down then anyway.

---

## 8. Report-only update checks

**Date:** 2026-10-01 · **Status:** accepted

**Decision.** Pin everything by digest; run a weekly checker that reports digest drift, new releases and security advisories, but never changes anything. Only Ubuntu security packages update automatically.

**Why.** Several components (LiteLLM, Open WebUI) run DB migrations on upgrade and change behaviour between releases; an unattended upgrade can break every harness at once. A report with advisory matching keeps the risk visible without that failure mode.

---

## 9. Fix client quirks in the gateway

**Date:** 2026-10-01 · **Status:** accepted

**Decision.** When a harness sends something a model's chat template rejects, fix it in a LiteLLM hook, not with client-side flags.

**Why.** The first fix for Claude Code's mid-conversation system messages was an undocumented environment variable found in its binary, which can break on any upgrade and only helped one client. The hook covers every harness and every alias, and fixed a second bug (`coder-fast` dropping environment info) for free.
