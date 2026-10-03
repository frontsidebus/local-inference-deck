# clients/ — workstation harness setup

Wrappers and config templates that point local agent harnesses at the Spark gateway
(`https://${SPARK_API_HOST}`: nginx on Covenant → LiteLLM on Walter → llama-swap).
Everything here runs as your normal user. Nothing needs root, except the optional
AppArmor profile for Codex's sandbox.

| Harness | Entry point | Config written | Gateway API |
|---|---|---|---|
| Claude Code (tested 2.1.286–2.1.288) | `claude-spark` | `~/.claude-spark/settings.json` (isolated `CLAUDE_CONFIG_DIR`) | Anthropic `/v1/messages` |
| Codex CLI 0.159.3 | `codex-spark [--profile spark-fast]` | `~/.codex/config.toml`, `spark.config.toml`, `spark-fast.config.toml`, `spark-models.json` | `/v1/responses` |
| Hermes Agent v0.21.5 (tag `v2026.9.24`) | `hermes-spark` | merges a fragment into `~/.hermes/config.yaml` | `/v1/chat/completions` |
| OpenCode 1.18.34 | `opencode` (no wrapper) | `~/.config/opencode/opencode.json` | `/v1/chat/completions` |
| Cline (VS Code) | — | manual, see below | `/v1/chat/completions` |
| hermes-gateway (optional) | systemd user service | `~/.config/systemd/user/hermes-gateway.service` | serves Open WebUI |

Model aliases are the gateway's stable names, so harness configs never change when a
model is swapped on Walter:

| Alias | Today | Context | Notes |
|---|---|---|---|
| `coder` | Qwen3.8-27B dense, reasoning | 131072 | default everywhere |
| `coder-fast` | Qwen3.6-35B-A3B MoE, reasoning | 131072 | small/background model; shares the GPUs with `coder` |
| `big` | Qwen3-Coder-Next, no reasoning | 131072 | evicts the `coder` pair |
| `vision` | Gemma 4 31B, image input | 131072 | evicts the `coder` pair |
| `hermes` | Hermes 4.3 36B | 65536 | evicts the `coder` pair |

Use `big`, `vision` and `hermes` sparingly when others share the box: loading any of
them unloads `coder` + `coder-fast` for everyone.

## Layout

```
clients/
  install.sh                      installs wrappers + renders configs for $USER (see below)
  deploy.sh                       = install.sh (per-component convention)
  spark-env.tmpl                  -> ~/.config/spark/env   (SPARK_API_HOST, SPARK_KEY_DIR)
  bin/claude-spark                -> ~/.local/bin/          Claude Code wrapper
  bin/codex-spark                 -> ~/.local/bin/          Codex wrapper
  bin/hermes-spark                -> ~/.local/bin/          Hermes wrapper
  claude/settings.json            -> ~/.claude-spark/settings.json
  codex/config.toml.tmpl          -> ~/.codex/config.toml
  codex/spark.config.toml         -> ~/.codex/              profile "spark" (coder)
  codex/spark-fast.config.toml    -> ~/.codex/              profile "spark-fast" (coder-fast)
  codex/spark-models.json         -> ~/.codex/              custom model catalog
  codex/apparmor-bwrap            -> /etc/apparmor.d/bwrap  (manual, root; Ubuntu 24.04+)
  opencode/opencode.json.tmpl     -> ~/.config/opencode/opencode.json
  hermes/config.spark.yaml.tmpl   -> merged into ~/.hermes/config.yaml
  hermes/hermes-gateway.service   -> ~/.config/systemd/user/   (optional)
  hermes/gateway.env.example.tmpl    lines the gateway needs in ~/.hermes/.env (optional)
```

## How the gateway URL and keys reach each harness

One approach, used everywhere:

- **Host:** `SPARK_API_HOST` comes from `site.env` (or `--api-host`). `install.sh`
  writes it into `~/.config/spark/env`, which the wrappers source at runtime, and
  renders it into the config files that the harnesses read on their own (Codex,
  Hermes, OpenCode). If the host changes, re-run `install.sh --force`.
- **Keys:** one LiteLLM virtual key per harness, each in its own file,
  `${SPARK_KEY_DIR:-~/.config/spark}/<harness>.key` (dir mode 700, files mode 600).
  Keys are read at **runtime** and never written into a config, shell rc or
  environment file:
  - `claude-spark` reads `claude-code.key` (override: `SPARK_KEY_FILE`) and exports it as `ANTHROPIC_AUTH_TOKEN` for that process only.
  - `codex-spark` reads `codex.key` (override: `SPARK_CODEX_KEY_FILE`) and exports `SPARK_CODEX_KEY`, which `env_key` in config.toml names.
  - `hermes-spark` reads `hermes.key` (override: `SPARK_HERMES_KEY_FILE`) and exports `SPARK_HERMES_API_KEY`, which `custom_providers[spark].key_env` names.
  - OpenCode reads `opencode.key` itself via `"apiKey": "{file:~/.config/spark/opencode.key}"`.

  Plain `claude`, `codex` and `hermes` know nothing about the gateway (Codex and
  Hermes return 401 when run without the wrapper). Binary paths can be overridden
  with `CLAUDE_BIN`, `CODEX_BIN` and `HERMES_BIN`.

## Getting a key

Keys are LiteLLM virtual keys minted on Walter (one per entry in `HARNESS_KEYS`, plus
one per person in `SPARK_USERS`; rpm 60, 4 parallel requests, spend attributed per
key). See `walter/` for how they are generated; they live in
`/srv/gateway/keys/<name>.key` (root, mode 600).

- **Operator workstation:** copy the harness keys without echoing them:
  ```
  install -d -m 700 ~/.config/spark
  for k in claude-code codex hermes opencode; do
    ( umask 077; ssh ${BACKEND_SSH_USER}@${BACKEND_LAN_IP} "sudo cat /srv/gateway/keys/$k.key" > ~/.config/spark/$k.key )
  done
  ```
- **Other people:** ask the admin for your personal key (it arrives out of band) and
  save it once per harness file, or point every wrapper at one file with
  `SPARK_KEY_DIR` / the `*_KEY_FILE` variables. Cline should get its own key.
- Never paste a key into a config file, a shell rc or a chat. Rotate by replacing the
  file contents; nothing else needs to change.

## Install

```
cp site.env.example site.env    # at the repo root; set SPARK_API_HOST (and HYPERVISOR_BRIDGE_IP for the gateway)
clients/install.sh --dry-run    # shows every file it would create and a diff for every file it would change
clients/install.sh              # creates new files; leaves differing ones alone and writes <file>.spark beside them
clients/install.sh --force      # replaces differing files, backing each up to <file>.bak-<timestamp>
clients/install.sh --with-hermes-gateway   # also the optional gateway user service
```

Options: `--site-env FILE`, `--api-host HOST`, `--key-dir DIR`. Precedence for each
setting is flag, then environment, then `site.env`.

What install.sh guarantees:
- It only writes under `$HOME`: `~/.config/spark/`, `~/.local/bin/`, `~/.claude-spark/`,
  `~/.codex/`, `~/.config/opencode/`, `~/.hermes/config.yaml`, and with
  `--with-hermes-gateway` also `~/.config/systemd/user/` and `~/.hermes/.env`.
- It refuses to write `~/.claude/` or `~/.claude.json` (your normal Claude Code setup).
- It never reads or prints key contents: it only checks that each key file exists and is mode 600.
- It never runs `sudo`, `systemctl`, or a harness. It prints the commands instead.
- `--dry-run` writes nothing at all, not even temp files.
- Re-running is idempotent (`unchanged`).

`~/.hermes/config.yaml` is a large personal file, so it is handled differently: the
fragment's `model:`, `custom_providers[spark]` and `security.redact_secrets` keys are
compared with PyYAML (the Hermes venv's python, or `python3`). If they differ, the
fragment is written to `config.yaml.spark` for a manual merge; `--force` merges it in
(backup first; YAML comments in the file are lost). With no existing config the
fragment becomes the config and Hermes fills in defaults on first run.

## Claude Code (`claude-spark`)

- Isolation: `CLAUDE_CONFIG_DIR=~/.claude-spark` (own settings, history, sessions),
  and the wrapper unsets `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` and the
  Bedrock/Vertex/Foundry switches, so gateway and real Anthropic credentials never mix.
- Model mapping: main/opus/sonnet → `coder`; haiku and background tasks (titles,
  summaries) → `coder-fast`; subagents → `coder`. `SPARK_MODEL=coder-fast claude-spark`
  changes the main model; `--model haiku` also gets `coder-fast`.
- **Context:** Claude Code assumes 200K for unknown model IDs. The wrapper sets
  `CLAUDE_CODE_MAX_CONTEXT_TOKENS=131072`, `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=85` (compact
  early, because `count_tokens` is only estimated by LiteLLM and undercounts) and
  `CLAUDE_CODE_MAX_OUTPUT_TOKENS=16384` (the output reservation eats into the window).
  Each request starts with about 15K tokens of system prompt and tools; first token
  typically takes 10–15 s uncached.
- `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` strips `anthropic-beta` headers and beta
  body fields the gateway can't serve. `API_TIMEOUT_MS=1200000` covers cold model loads.
  Telemetry, error reporting, auto-update and other non-essential traffic are off.
- **History: `CLAUDE_CODE_MODEL_CAPABILITIES`.** Claude Code sends `role:"system"`
  entries mid-conversation to unrecognized models. The Qwen chat templates reject them
  (`coder`: HTTP 500 "System message must be at the beginning", then ~3 min of retries)
  or silently drop them (`coder-fast`). The first fix was the undocumented
  `CLAUDE_CODE_MODEL_CAPABILITIES="-mid_conv_system,-mid_conv_tool_change"` (found in
  the 2.1.286 binary). It is **no longer needed**: a LiteLLM pre-call hook on the
  gateway rewrites those messages into user `<system-reminder>` text for Messages,
  Chat and Responses. If 500s mentioning "System message" ever come back after a gateway
  change, re-add that export to the wrapper (it is in a comment there).
- Default permission mode is `auto`, whose safety classifier would run on Qwen, not
  Claude. Use `--permission-mode default` or `acceptEdits` for anything sensitive.
- Thinking blocks are shown. `/cost` figures are fake (priced as Claude). Remote
  Control, fast mode and auto-update are unavailable.

## Codex (`codex-spark`)

- `wire_api = "responses"` is the only wire API in Codex 0.159; the gateway serves
  `/v1/responses`. Provider `home`, key from `env_key = "SPARK_CODEX_KEY"`.
- **The custom model catalog is required** (`model_catalog_json`, rendered as an
  absolute path to `~/.codex/spark-models.json`). Without it Codex assumes a 272K
  window and OpenAI-only features. The catalog declares 131072 context, no reasoning
  summaries or verbosity, text-only input, no hosted search, and shell-based editing
  (no freeform `apply_patch`). Its fields are version-sensitive: after a Codex upgrade
  run `codex-spark doctor`. It lists only `coder` and `coder-fast` on purpose.
- Profiles are separate files since Codex 0.134 (`[profiles.*]` tables are ignored):
  `--profile spark` (coder) and `--profile spark-fast` (coder-fast).
- `web_search` is disabled (OpenAI-hosted tool), `multi_agent` and `goals` are off to
  keep the tool surface small, `show_raw_agent_reasoning = true` shows Qwen's
  `reasoning_text` in the TUI (`exec --json` does not emit it).
- **Sandbox on Ubuntu 24.04+:** `kernel.apparmor_restrict_unprivileged_userns=1` stops
  bubblewrap (`bwrap: loopback: Failed RTM_NEWADDR` / `setting up uid map`). Install
  the profile once, as root:
  ```
  sudo install -m 644 clients/codex/apparmor-bwrap /etc/apparmor.d/bwrap
  sudo apparmor_parser -r /etc/apparmor.d/bwrap
  codex-spark sandbox -c sandbox_mode='"workspace-write"' -- true   # re-test
  ```
  `install.sh` prints this reminder when the profile is missing and the restriction is on.
- Install/upgrade: `npm install -g @openai/codex --prefix ~/.local`.

## Hermes Agent (`hermes-spark`)

- Provider `custom:spark` (`api_mode: chat_completions`), default model `coder`,
  `max_tokens: 32768`. Per-model `context_length` lives under
  `custom_providers[spark].models`; don't set `model.context_length`, which would
  override all of them (including `hermes`, which is 64K).
- Switch models with `hermes-spark -m coder-fast` or in-session `/model custom:spark:coder-fast`.
- The wrapper sets `HERMES_API_TIMEOUT` and `HERMES_API_CALL_STALE_TIMEOUT` to 900 s
  (cold loads take ~30 s).
- **Secret redaction is on** (`security.redact_secrets: true`; equivalently
  `hermes config set security.redact_secrets true`). It is not retroactive.
- **Upgrades: pin a release tag. Don't run `hermes update`**, which jumps to `main`
  rather than the next stable release. Instead:
  ```
  cd ~/.hermes/hermes-agent && git fetch --tags && git checkout v2026.9.24   # or the next release tag
  # then reinstall deps the way the release notes say, and restart hermes-gateway if used
  ```
  Back up `~/.hermes/config.yaml` first; config migrations bump `_config_version`.

## OpenCode

- Provider `spark` via `@ai-sdk/openai-compatible`, `baseURL https://${SPARK_API_HOST}/v1`.
- **The key is loaded by OpenCode itself** with `"apiKey": "{file:~/.config/spark/opencode.key}"`,
  so there is no wrapper and no environment variable.
- `enabled_providers: ["spark"]` hides every other provider; `share: "disabled"`, `autoupdate: false`.
- `model: spark/coder`; `small_model: spark/coder-fast` (session titles etc.).
- Reasoning models use `interleaved.field = "reasoning_content"` (LiteLLM's field name).
  All five aliases are defined; `vision` accepts image attachments.
- Install/upgrade: `npm i -g opencode-ai --prefix ~/.local`.

## Cline (VS Code)

Manual: API Provider **OpenAI Compatible**, Base URL `https://${SPARK_API_HOST}/v1`,
API key = the contents of a dedicated key (ideally its own, not another harness's),
Model ID `coder` / `coder-fast` / etc. Set the context window to 131072 (65536 for
`hermes`) and max output manually in the model config section.

## Smoke tests

Each is a single read-only API call (seconds when the model is already loaded; a cold
load adds ~30 s).

```
claude-spark -p "Reply with exactly: pong" --model haiku           # -> pong
codex-spark exec --profile spark-fast --skip-git-repo-check -s read-only "Reply with exactly: pong"
hermes-spark chat -Q --oneshot -m coder-fast -q "Reply with exactly: pong"
opencode run -m spark/coder-fast "Reply with exactly: pong"
codex-spark doctor                                                 # config/auth/reachability
```

A 401 means the key file is missing, wrong, or the harness was run without its wrapper.
`claude-spark` prints a harmless `[claude-code:unrecognized_model]` line for gateway aliases.

## Optional: hermes-gateway (Hermes agent inside Open WebUI)

Runs the Hermes API server on the workstation so Open WebUI on Walter can offer a
"Hermes Agent (workstation)" model. No messaging platforms are configured; only the
API server runs.

- systemd **user** service `hermes-gateway`, with linger on so it runs without a login.
- Bound to the hypervisor bridge only: `API_SERVER_HOST=${HYPERVISOR_BRIDGE_IP}`,
  `API_SERVER_PORT=${HERMES_GATEWAY_PORT}` (8642) in `~/.hermes/.env` (mode 600).
- Requires a bearer key, `API_SERVER_KEY`, in `~/.hermes/.env`. The backend's Open WebUI
  lists `http://${HYPERVISOR_BRIDGE_IP}:${HERMES_GATEWAY_PORT}/v1` in `OPENAI_API_BASE_URLS`,
  with the same key in the matching `OPENAI_API_KEYS` slot (see `walter/`), and
  `AIOHTTP_CLIENT_TIMEOUT=900`.
- The unit starts Hermes through `hermes-spark`, so it reads the gateway key from
  `~/.config/spark/hermes.key`. The original setup instead kept a copy of that key in
  `~/.hermes/.env` as `SPARK_HERMES_API_KEY` and ran the venv python directly;
  both work, but the key-file way leaves one copy of the secret.

Install: `clients/install.sh --with-hermes-gateway [--force]`, then
```
loginctl enable-linger "$USER"
systemctl --user daemon-reload && systemctl --user enable --now hermes-gateway
ss -ltn | grep ":${HERMES_GATEWAY_PORT}"     # should show ${HYPERVISOR_BRIDGE_IP}:8642 only
```
Without `--force`, install.sh only lists the `.env` variables that are missing. With
`--force` it appends just the missing ones (backup first) and generates `API_SERVER_KEY`
with `openssl rand -hex 32`. Existing values are never read or printed.

**Security caveats. Read these before enabling it:**
- **Tools run unsandboxed on the workstation as your user**: files, terminal, network,
  everything that user can reach. Anyone who can chat with "Hermes Agent" effectively
  has a shell as you. Commands that need approval are blocked in API mode, but that
  is not a sandbox.
- **The Open WebUI connection must stay admin-only.** In Open WebUI (v0.11
  `access_grants`) a model without grants is visible to admins only. Never give
  `hermes-agent` a public or group grant. Check with a regular test user that the
  model is invisible to them and returns 400.
- **`API_SERVER_KEY` is the only authentication.** Keep it long and random, keep
  `~/.hermes/.env` mode 600, and rotate it in both places together.
- Keep the bind on the bridge address; never `0.0.0.0`. Optionally add a host firewall
  rule that allows the port on the bridge only from the backend VM
  (`${BACKEND_LAN_IP}`).
- Keep `security.redact_secrets: true`.

Rollback: `systemctl --user disable --now hermes-gateway`, remove the unit, and
remove the `hermes-agent` connection from Open WebUI.

## Secrets (none in this repo)

| Name | Path | Mode | Source |
|---|---|---|---|
| Claude Code harness key | `~/.config/spark/claude-code.key` | 600 | LiteLLM virtual key `claude-code` (Walter `/srv/gateway/keys/`) |
| Codex harness key | `~/.config/spark/codex.key` | 600 | LiteLLM virtual key `codex` |
| Hermes harness key | `~/.config/spark/hermes.key` | 600 | LiteLLM virtual key `hermes` |
| OpenCode harness key | `~/.config/spark/opencode.key` | 600 | LiteLLM virtual key `opencode` |
| `API_SERVER_KEY` (optional gateway) | `~/.hermes/.env` | 600 | `openssl rand -hex 32`; also in Open WebUI's `OPENAI_API_KEYS` |
| `SPARK_HERMES_API_KEY` (legacy layout only) | `~/.hermes/.env` | 600 | copy of `hermes.key`; not needed with this unit |

## Verify / rollback

- Verify: `clients/install.sh --dry-run` reports `unchanged` everywhere, and the smoke tests pass.
- Roll back a file: every replaced file has a `<file>.bak-<timestamp>` next to it.
  Remove the wrappers from `~/.local/bin` and `~/.claude-spark/` to uninstall;
  nothing else on the system was changed.
