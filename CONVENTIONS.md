# Repo conventions

This repo is **PUBLIC**. Everything committed must be generic and secret-free.

## Layout
```
walter/      backend VM: llama-swap, gateway (LiteLLM+Postgres), webui (Open WebUI+Pocket-ID+theme),
             monitoring, telemetry app, backups, update-check, systemd units/drop-ins, firewall
covenant/    edge: nginx sites/snippets, oauth2-proxy, fail2ban, ufw, wireguard, certbot hooks
clients/     workstation harness wrappers/configs (claude/codex/hermes/opencode) + hermes-gateway
judge/       agent judge: gates, evidence collector and reviewer for a local agent (self-contained; see judge/CONTRACT.md)
hypervisor/  requirements for the host layer (managed in a separate IaC project)
docs/        decisions, agent-judge design, runbooks, history (old Ollama-era docs live in docs/history/)
scripts/     repo-level: render.sh, gen-secrets.sh, check-sanitized.sh
site.env.example
```
Each deployable component dir has its own `README.md` (what it is, deploy steps, verify, rollback) and an entry point: `walter/deploy.sh`, `covenant/deploy.sh`, `clients/install.sh` (wrapped by `clients/deploy.sh`), `judge/install.sh`. `hypervisor/` has a README only.

## Site values → `site.env` (gitignored), template in `site.env.example`
Use these names. If you need a new one, add it to `site.env.example` under your component's section with a comment and a safe example value. The block below is the shared core; [site.env.example](site.env.example) is the complete, authoritative list (it also has the edge, backend, client and `JUDGE_*` settings).

```
# --- identity / DNS
SPARK_DOMAIN=example.com
SPARK_CHAT_HOST=chat.example.com
SPARK_API_HOST=api.example.com
SPARK_ID_HOST=id.example.com
SPARK_TELEMETRY_HOST=telemetry.example.com
SPARK_DIGEST_HOST=""                     # optional digest app; empty = off (e.g. digest.example.com)
LETSENCRYPT_EMAIL=admin@example.com
ADMIN_EMAIL=admin@example.com            # Open WebUI break-glass local admin
# --- network
EDGE_PUBLIC_IP=203.0.113.10              # edge Elastic IP (TEST-NET-3 example)
EDGE_WG_IP=10.100.0.1
BACKEND_WG_IP=10.100.0.2
WG_PORT=51820
BACKEND_LAN_IP=192.168.122.10            # backend VM address on the hypervisor bridge
HYPERVISOR_BRIDGE_IP=192.168.122.1       # where the workstation hermes-gateway listens
ADMIN_SOURCE_IPS=198.51.100.7            # trusted admin IP(s): fail2ban ignoreip, setup locks (space-separated)
GATEWAY_DOCKER_SUBNET=172.30.40.0/24
# --- backend paths / users
MODELS_DIR=/models
BACKEND_SSH_USER=operator
# --- access
SPARK_USERS="alice bob"                  # one LiteLLM key + Pocket-ID account each
HARNESS_KEYS="claude-code codex hermes opencode open-webui"
TELEMETRY_GROUP=telemetry-viewers
DIGEST_GROUP=digest-viewers             # only used when SPARK_DIGEST_HOST is set
CHAT_GROUP=chat-users
```

## Templating
- Templates end in `.tmpl` and are rendered by `scripts/render.sh` using **`envsubst` with an explicit variable list** (never bare `envsubst`, which would eat nginx `$host`, `$request_uri`, etc.). Syntax in templates: `${SPARK_API_HOST}`. Optional blocks: `@@if NAME@@ … @@else@@ … @@endif@@` keeps the first part only when `NAME` is set and not `CHANGEME` (directive lines are dropped; a malformed block fails the render), so a template can render with an optional value unset.
- Non-templated files are copied verbatim.

## Secrets — NEVER in the repo
- No key, token, password, client secret, cookie secret, master key, salt, WireGuard private key, cert, or `.env` with values. Not even "old/rotated" ones.
- Each component documents its secrets in its README (name, path on host, mode, how it's generated) and `scripts/gen-secrets.sh` generates them on the target host (idempotent: never overwrite an existing secret).
- `.env` files are shipped as `*.env.example` with `CHANGEME` placeholders.

## Forbidden in committed content (checked by scripts/check-sanitized.sh)
Real domain names of the live site, real public IPs, the admin's home IP, real email addresses, real user names from the live user list, real hostnames/usernames of the operator's machines, AWS key-pair names, Pocket-ID/OIDC client UUIDs, LiteLLM key aliases tied to real people, anything from `/home/<user>` paths. Use the example values above. Codenames "Walter" (backend) and "Covenant" (edge) ARE allowed — they're the project's lore.

## Before every commit
- Run `scripts/check-sanitized.sh` (staged and untracked files) or `scripts/check-sanitized.sh --all` (every tracked file). It reads the real site values from your gitignored `site.env`, plus optional gitignored `.sanitize-extra` (literal strings) and `.sanitize-words` (whole words such as real user names). It also flags secret-shaped strings.
- **Check its exit code**, not only its last line: `scripts/check-sanitized.sh --all; echo "exit=$?"`. It exits 0 on success and 1 on any finding. Never pipe it into `tail` or `head`, which hides the status. Without a `site.env` it only checks generic secret shapes and prints a warning, so run it from a checkout that has one.
- Run gitleaks on the changed files as a second, independent check, for example `docker run --rm -v "$PWD:/scan" zricethezav/gitleaks:latest dir /scan/<path>` for each changed path (or `gitleaks dir <path>` if it is installed).
- Grep the added lines (`git diff --cached -U0 | grep '^+'`) for the real domain, IPs, user names and home paths. The expected count is 0.
- Pin everything: image digests, binary versions with sha256.
- When capturing a change made on a live host, render the templates with your real `site.env` and diff against the live files. The only differences should be secret placeholders.

## Branches, worktrees and PRs
- `main` is protected by convention: no direct commits. Work on a topic branch (`feat/...`, `fix/...`, `docs/...`).
- Use one git worktree per workstream (`git worktree add ../<repo>-<topic> -b <branch> origin/main`), so parallel work never shares a checkout. A worktree has no `site.env`: copy it in (it is gitignored) before running `check-sanitized.sh` or `render.sh`, and remove the worktree once the PR is merged, because it holds that copy.
- The live deployment runs from its own checkout. Don't develop in it; pull merged changes into it and deploy from there.
- Commits made with an AI agent carry a `Co-Authored-By:` trailer naming it.
- Agents and contributors push the branch and open a PR (or leave it for the owner, if told to). **The owner reviews and merges**; nobody else merges, and nothing is force-pushed to `main`.
- A PR description says what changed, how it was verified (tests, sanitize exit code, gitleaks, live checks), what is live, and any follow-ups.
