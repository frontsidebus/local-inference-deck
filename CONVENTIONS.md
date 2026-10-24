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
evals/       security eval harness (run.py, scorers, report); benchmark data and results are gitignored
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
- Run `scripts/check-sanitized.sh` (staged and untracked files) or `scripts/check-sanitized.sh --all` (every tracked file). It reads the real site values from the first site env it finds: `--site-env FILE`, `$SITE_ENV`, `~/.config/deck/site.env`, then the gitignored `site.env` at the repo root. It adds `judge.env` when present (`$JUDGE_ENV`, else `~/.config/judge/judge.env`) and the optional `.sanitize-extra` (literal strings) and `.sanitize-words` (whole words such as real user names) from `~/.config/deck/` and the repo root. It also flags secret-shaped strings.
- **Check its exit code**, not only its last line: `scripts/check-sanitized.sh --all; echo "exit=$?"`. It exits 0 when clean, 1 on any finding, and 2 when it finds no real-value source or a dangling symlink (the message names the paths it tried). Never pipe it into `tail` or `head`, which hides the status. Make `~/.config/deck/site.env` a symlink to your site.env once, so every worktree is checked: `mkdir -p ~/.config/deck && ln -s <path-to>/site.env ~/.config/deck/site.env`.
- `--history` scans every commit for the real values and names and reports commit, path and key name (never the value). `--regex-only` (generic secret shapes only) is for CI and refused unless `CI=true`.
- Install the pre-commit hook once per clone. It runs `check-sanitized.sh` on staged and untracked files and blocks the commit on exit 1 or 2. Either `git config core.hooksPath hooks` (this replaces `.git/hooks` for this clone and its worktrees), or keep other hooks and link just this one: `ln -s ../../hooks/pre-commit "$(git rev-parse --git-common-dir)/hooks/pre-commit"` (adjust the relative path if the common dir is not `<clone>/.git`; an absolute path also works).
- Run gitleaks on the changed files as a second, independent check, for example `docker run --rm -v "$PWD:/scan" zricethezav/gitleaks:latest dir /scan/<path>` for each changed path (or `gitleaks dir <path>` if it is installed).
- Grep the added lines (`git diff --cached -U0 | grep '^+'`) for the real domain, IPs, user names and home paths. The expected count is 0.
- Pin everything: image digests, binary versions with sha256.
- When capturing a change made on a live host, render the templates with your real `site.env` and diff against the live files. The only differences should be secret placeholders.

## Branches, worktrees and PRs
- `main` is protected by convention: no direct commits. Work on a topic branch (`feat/...`, `fix/...`, `docs/...`).
- Use one git worktree per workstream (`git worktree add ../<repo>-<topic> -b <branch> origin/main`), so parallel work never shares a checkout. A worktree has no `site.env`: `check-sanitized.sh` and `render.sh` fall back to `~/.config/deck/site.env`; without that, copy it in (it is gitignored) before running them, and remove the worktree once the PR is merged, because it holds that copy.
- The live deployment runs from its own checkout. Don't develop in it; pull merged changes into it and deploy from there.
- Commits made with an AI agent carry a `Co-Authored-By:` trailer naming it.
- Agents and contributors push the branch and open a PR (or leave it for the owner, if told to). **The owner reviews and merges**; nobody else merges, and nothing is force-pushed to `main`.
- A PR description says what changed, how it was verified (tests, sanitize exit code, gitleaks, live checks), what is live, and any follow-ups.
