# judge/: a reviewer for the local agent

A frontier model, or a local one for sensitive work, reviews what the local agent (Hermes on `coder` / `coder-fast`) did, using **evidence collected by deterministic code** rather than the agent's own account. Hermes shell hooks drive it: a synchronous gate and verifiers, and an asynchronous review queue. The judge reads, probes read-only and reports. It never changes a host, and its findings only recommend.

This file is the **operator reference**. Why the judge exists, how its parts fit, the data boundary, the gate's rules, measured results and the bug history: [docs/agent-judge.md](../docs/agent-judge.md). The exact interfaces (schemas, bundle files, hook I/O): [CONTRACT.md](CONTRACT.md). Runbooks: [pilot](../docs/runbooks/agent-judge-pilot.md), [re-judge](../docs/runbooks/agent-judge-rejudge.md).

Contents: [How it works](#how-it-works) · [Components](#components) · [Install](#install-and-reinstall) · [Configuration](#configuration) · [Units](#units) · [Commands](#commands) · [Review directory](#review-directory) · [Cost](#cost-and-caps) · [Troubleshooting](#troubleshooting) · [Uninstall](#uninstall) · [Extracting](#extracting-to-its-own-repo)

## How it works

```
                       Hermes (worker, local model)
                                   |
  plan written ------------- C1 post_tool_call  -> enqueue.py: record the call; plan edit -> plan request --+
  risky tool call ---------- C2 pre_tool_call   -> gate.py (no model, fail_closed): pass | approve (ask   |
                                                   the human) | block; escalations + blocks -> gate request -+
  "done" after edits ------- C3 pre_verify      -> verify.py: bash -n, JSON/YAML, check-sanitized, ssh -G,   |
                                                   claimed changes; nudge once on failure -> completion req --+
  turn ends ---------------- C4 on_session_end  -> enqueue.py: completion audit (merged per turn) -----------+
  next turn starts --------- C5 pre_llm_call    <- inject.py: unacked findings, "data, not instructions"     |
  always ------------------- C6 watch/runaway.py (user unit, polls llama-server slots) -> alert, runaway req -+
                                                                                                             v
   judge-review.path / .timer -> run_judge.py --pending:                           $JUDGE_REVIEW_DIR/queue/
     1. collector/collect.py  -> evidence/<id>/   (window-bounded logs, gate decisions with outcome, tool calls,
                                                   refusals, the agent's diff vs. others' changes, C3 results,
                                                   host find in UTC, read-only host probes, slots)
     2. judge                 -> frontier `claude -p` for infra data; local gateway alias for sensitive data;
                                 sensitive completions also: frontier on a claims-only bundle;
                                 every frontier input has site.env values masked as ${KEY} (lib/sitemask)
     3. validate.py           -> findings/<id>.json + .md (+ <id>.claims.json); request -> done/
```

| # | Hermes event | Script | Sync | Effect |
|---|---|---|---|---|
| C1 | `post_tool_call`, matcher `write_file\|patch\|terminal\|memory\|skill_manage\|read_file` | `hooks/enqueue.py` | async | Records every matched call for attribution and gate outcomes. A plan edit queues one plan review per plan file and turn. |
| C2 | `pre_tool_call`, matcher `terminal\|write_file\|patch\|read_file`, `fail_closed: true` | `hooks/gate.py` | sync, ms | Escalates host-mutating commands, sensitive writes and secret reads to you; blocks changes to its own oversight. |
| C3 | `pre_verify` | `hooks/verify.py` | sync, < 60 s | Verifies the changed paths, nudges once on a failure, records every result. |
| C4 | `on_session_end` (+ `on_session_start` for the snapshot) | `hooks/enqueue.py` | async | Snapshot at start; a completion audit per turn. |
| C5 | `pre_llm_call` | `hooks/inject.py` | sync, fast | Shows open findings to the agent as reviewer data (an R8 item as a "code defect (R8)" with its quoted code lines and failure scenario); a one-time reminder after a refused escalation; a runner-stall warning. |
| C6 | none (telemetry) | `watch/runaway.py` | async | Alerts on runaway generations; never cancels. |

## Components

| Path | What it is |
|---|---|
| `install.sh` | Merges the hooks block into `$HERMES_HOME/config.yaml`, creates the review dir, renders the gate policy, optionally installs the user units. Dry run by default. |
| `hooks/` | Hermes shell-hook entrypoints: JSON on stdin, JSON on stdout. All but `gate.py` fail open and log to `hook-errors.log`; `gate.py` fails closed. |
| `policy/gate-policy.json.tmpl` | C2 rules. Host patterns are `${VARS}` from `site.env`; rendered to `$JUDGE_REVIEW_DIR/gate-policy.json`. |
| `collector/collect.py`, `extras.py`, `claims_only.py` | The evidence bundle; C3 results and host-state probes; the claims-only bundle for the frontier claims stage, and the self-check of the agent's free text in mixed bundles. |
| `lib/sitemask.py` | Masks the site's identifier values from `site.env` (domain, hosts, addresses, SSH users and aliases, buckets, site name) as `${KEY}` before every frontier call; the runner unmasks the finding locally. |
| `probes/probe.py` | The only way the judge touches hosts: an allowlist of read-only probes with per-argument validation. |
| `runner/` | `run_judge.py` (frontier or local), `prompt.md` and `prompt-claims.md`, `validate.py`, `rejudge.py`, `alert.py`, and the units in `units/`. |
| `bin/` | `judge-findings` and `judge-ack`, the operator CLIs. |
| `watch/` | `runaway.py` (C6) and its unit. |
| `lib/` | `config.py`/`config.sh` (settings and the data-class rules), `queue.py`, `snapshot.py`, `hermeslog.py`, `redact.py`, `refusals.py`, `toolcalls.py`. |
| `schema/` | JSON schemas for requests and findings. |
| `tests/` | pytest, one module per part. Tests always use a temp `HERMES_HOME`, and `conftest.py` keeps them away from the desktop and the live units. |

Runtime code is Python 3.10+ stdlib only. The installer uses the Hermes venv's Python for PyYAML, because it edits `config.yaml`.

## Install and reinstall

Prerequisites: Hermes with its venv at `$HERMES_HOME/hermes-agent/venv`, `python3` 3.10 or newer, a filled-in `site.env` at the repo root (see [Configuration](#configuration)), and the `claude` CLI logged in if you use frontier mode.

```bash
judge/install.sh                                 # dry run: the hooks block and a diff of config.yaml
judge/install.sh --apply                         # back up config.yaml, merge, create the review dir, render the policy
judge/install.sh --apply --with-units --start    # ...and install, enable and (re)start the user units
```

What `--apply` does:

1. Copies `config.yaml` to `config.yaml.bak-judge-<UTC timestamp>`.
2. Merges six entries under `hooks:`, each tagged `managed_by: agent-judge` (Hermes ignores the key). Your own hook entries, `hooks.outbound` and everything outside `hooks:` are left alone. Comments inside the `hooks:` block are not preserved (the backup keeps them). Re-running changes nothing.
3. Checks each event name against the installed Hermes source and skips, with a warning, any event it cannot run as a shell hook.
4. Creates `$JUDGE_REVIEW_DIR` and its subdirectories (mode 700) and renders `gate-policy.json` (mode 600) from `site.env`.
5. Refuses to run if a hook script is missing: a missing `gate.py` with `fail_closed: true` would block every tool call.

With `--with-units` it renders the units into `~/.config/systemd/user/` and enables `judge-review.path`, `judge-review.timer` and `judge-runaway-watch.service` (never the service a path or timer triggers, never the alert template). `--start` runs `daemon-reload`, `reset-failed`, `enable --now` and a `restart` of the path and timer units; without it the commands are printed. Other options: `--hermes-home`, `--review-dir`, `--site-env`, `--python` (the interpreter the hooks run with), `--hermes-python`, `--hermes-src`, `--unit-dir`; see `judge/install.sh --help`.

The merged block (paths are absolute on your machine; this matches the live install):

```yaml
hooks:
  pre_tool_call:
  - matcher: terminal|write_file|patch|read_file
    command: /usr/bin/python3 /path/to/repo/judge/hooks/gate.py
    timeout: 10
    fail_closed: true
    managed_by: agent-judge
  post_tool_call:
  - matcher: write_file|patch|terminal|memory|skill_manage|read_file
    command: /usr/bin/python3 /path/to/repo/judge/hooks/enqueue.py
    managed_by: agent-judge
  on_session_start:
  - command: /usr/bin/python3 /path/to/repo/judge/hooks/enqueue.py
    managed_by: agent-judge
  on_session_end:
  - command: /usr/bin/python3 /path/to/repo/judge/hooks/enqueue.py
    managed_by: agent-judge
  pre_verify:
  - command: /usr/bin/python3 /path/to/repo/judge/hooks/verify.py
    timeout: 60
    managed_by: agent-judge
  pre_llm_call:
  - command: /usr/bin/python3 /path/to/repo/judge/hooks/inject.py
    timeout: 10
    managed_by: agent-judge
```

### Consent

Hermes asks for approval the first time it sees each `(event, command)` pair and records the answer in `$HERMES_HOME/shell-hooks-allowlist.json`. The installer never sets `hooks_auto_accept: true` and warns if it is set: auto-accept would let any future edit to `hooks:` run code without asking you.

1. Start an interactive session (`hermes chat`) and approve each judge hook. Expect six prompts: `enqueue.py` serves three events, approved separately.
2. `hermes hooks list` shows each as allowed; `hermes hooks doctor` checks exec bits, consent, mtime drift and JSON output.
3. Smoke test: `hermes hooks test pre_tool_call --for-tool terminal`; an empty decision means the call passes.

Non-TTY runs (the gateway, cron) never prompt; they skip unapproved hooks with a warning, so approve in a terminal first.

### After pulling judge changes

- **Code only** (gate rules, collector, runner, prompts): nothing to do. Hooks and the runner load the code on every call. Requests record the code that wrote them (`code_version`: git tree id of `judge/` + dirty flag, read from `.git` without running git; a copy outside git can carry a `judge/VERSION` file), so a finding notes when a request was written by older code. `hermes hooks doctor` shows the mtime drift; review the diff. The C6 watcher is long-running: `systemctl --user restart judge-runaway-watch` after a change under `watch/`.
- **A matcher, a hook entry or the policy template changed:** re-run `judge/install.sh --apply`, then restart Hermes and the gateway (they read hooks at start). No new consent: it is keyed on `(event, command)`, not on the matcher or the script's contents.
- **A unit changed:** `judge/install.sh --apply --with-units --start` (it also clears a `failed` state).

## Configuration

Settings come from `site.env` at the repo root (template: [site.env.example](../site.env.example), section `# --- judge`). Unless a row says otherwise, a setting is read **environment > `site.env` > default**, on every hook call or runner run, so a change needs no restart. The units also read `~/.config/judge/judge.env` (optional, `KEY=value`), which is environment and so **overrides** `site.env` for the units only; leave it absent unless you want a unit-only override. Check what the judge resolves with `python3 judge/lib/config.py --get KEY`.

### Policy as deployed

The workstation runs (in `site.env`):

```bash
JUDGE_MODE=frontier                  # infra-class requests -> frontier judge (claude -p)
JUDGE_FRONTIER_DAILY_MAX=40          # each sensitive completion adds a claims-stage call, so 20 runs out
JUDGE_FRONTIER_EXTRA_ARGS="--setting-sources project"   # keep the user's ~/.claude settings and hooks out of the judge
JUDGE_LOCAL_MODEL=coder-fast         # sensitive-class requests -> already loaded, no model eviction
JUDGE_LOCAL_MAX_SEVERITY=medium      # a local finding is never above medium
JUDGE_INJECT_LOCAL=0                 # local findings go to you, never into the agent's context
JUDGE_SENSITIVE_FRONTIER_CLAIMS=1    # sensitive completions also get a frontier claims-only review
```

`coder-fast` is the worker's own model family. The cap and the no-injection rule are what make it acceptable for sensitive-only reviews; do not raise the cap or turn injection on with it, and read its findings as hints. `vision` (Gemma) is the recommended alternative but has not been measured as a judge: [re-judge](#commands) stored bundles with it first.

### Every setting

**Judges and cost**

| Variable | Default | Meaning |
|---|---|---|
| `JUDGE_MODE` | `frontier` | `frontier`: infra requests go to the frontier CLI. `local`: every request goes to the local judge (no frontier spend). Sensitive requests always go to the local judge. |
| `JUDGE_FRONTIER_CMD` | `claude` | Frontier CLI. Run with endpoint variables (`ANTHROPIC_BASE_URL`, anything naming the gateway) stripped, so it always uses the normal Anthropic API. |
| `JUDGE_FRONTIER_MODEL` | empty | `--model` for the CLI; empty = the CLI's default. |
| `JUDGE_FRONTIER_EXTRA_ARGS` | empty | Extra CLI arguments (shell-split). |
| `JUDGE_FRONTIER_DAILY_MAX` | `20` | Frontier calls per UTC day, counted in `usage.json`. Past it, requests go to the local judge and the claims stage is skipped. |
| `JUDGE_FRONTIER_MAX_USD` | `2` | Per-call `--max-budget-usd`. |
| `JUDGE_FRONTIER_TIMEOUT` | `900` | Seconds per frontier call. |
| `JUDGE_LOCAL_MODEL` | `big` | Gateway alias of the local judge. |
| `JUDGE_LOCAL_URL` | empty | Base URL override; empty = `https://${SPARK_API_HOST}/v1`. |
| `JUDGE_LOCAL_KEY_FILE` | `~/.config/spark/hermes.key` | File holding the gateway key the local judge sends. |
| `JUDGE_LOCAL_TIMEOUT` | `600` | Seconds per local call. |
| `JUDGE_LOCAL_MAX_TOKENS` | `4096` | Output cap of every local call. |
| `JUDGE_LOCAL_RETRY_MAX_TOKENS` | 2 × `JUDGE_LOCAL_MAX_TOKENS` | Output cap of the re-ask after a reply was cut off (never less than the first cap). |
| `JUDGE_LOCAL_MAX_SEVERITY` | `medium` | Highest severity (`low`, `medium`, `high`) a local item may carry, including sensitive bundles and cap fallbacks; capped items are noted. |
| `JUDGE_CODE_REVIEW` | `1` | `1`: rubric R8 (code correctness) for `infra` completion and plan bundles whose `agent-diff.patch` changes a code file (not docs or static assets); the judge may file up to 3 `defect` items that quote the diff. `0`: R8 off for every judge. |
| `JUDGE_LOCAL_CODE_REVIEW` | `0` | `1`: R8 for the local judge too (`JUDGE_MODE=local`, or a frontier-cap fallback). Off by default: local models' unverified code items were mostly noise in the pilots. Sensitive bundles have no diff content, so they never get R8. |
| `JUDGE_SENSITIVE_FRONTIER_CLAIMS` | `1` | `1`: a sensitive completion also gets a frontier review of its claims-only bundle (only with `JUDGE_MODE=frontier`; counts against the daily cap). `0`: local only. |
| `JUDGE_BUNDLE_MAX_CHARS` | `150000` frontier, `60000` local | Size budget of the judge input. |
| `JUDGE_PROBES` | `1` | `1`: the judge may ask for one round of extra allowlisted read-only probes. |
| `JUDGE_MAX_ATTEMPTS` | `3` | A request whose judge backend fails stays queued and is retried; after this many attempts it gets a placeholder finding. |

**Data boundary** (see [docs/agent-judge.md](../docs/agent-judge.md#4-data-boundary))

| Variable | Default | Meaning |
|---|---|---|
| `JUDGE_REPO_DIR` | the repo containing `judge/` | The deck repo; it and every git worktree of it are infra. |
| `JUDGE_INFRA_REPOS` | empty | Extra directories (space-separated) whose files are infra. Name only the directory itself. Plain dirs are snapshotted at session start; git repos are diffed through git. Worktrees of `JUDGE_REPO_DIR` need no entry. |
| `JUDGE_SECRET_GLOBS` | empty | Extra basename globs of secret files, never infra, on top of the built-in list (`site.env`, `*.env`, `*.key`, `*.pem`, `*secret*`, `secrets/`, `.sanitize-*`, ...). |
| `JUDGE_SCRATCH_GLOBS` | empty | Extra globs over absolute paths (`*` crosses `/`) of agent scratch dirs, on top of `$HERMES_HOME/cache`, `$HERMES_HOME/tmp`, `~/.cache` and tool caches. They never decide the class and are metadata only. |
| `JUDGE_DATA_FILES` | `1` | `1`: infra completion/plan bundles get `data-files.txt`, redacted excerpts of the data/config files (`.json`, `.yaml`, `.toml`, `.csv`, ...) the agent read in the window or the diff names, from infra locations only (never secret-shaped, scratch or non-infra files: those are counted in the manifest, not named), for R8. `0`: off. |
| `JUDGE_DATA_FILES_MAX` | `3` | At most this many data files per bundle (`0` = off). |
| `JUDGE_DATA_FILE_BYTES` | `8192` | Characters shown per data file (min 512). Long JSON keeps every top-level key and elides long objects/arrays with a count; other files are cut in the middle. Files over 512 KiB are withheld. |
| `JUDGE_MIXED_MAX_SENSITIVE` | `3` | An infra request may carry up to this many sensitive paths (no more than its infra paths), withheld and masked. `0` = strict. Gate requests are always strict. Read the [owner's security note](../docs/agent-judge.md#4-data-boundary) before raising it. |

**Requests**

| Variable | Default | Meaning |
|---|---|---|
| `JUDGE_PLAN_DEBOUNCE_S` | `120` | A plan review waits for the turn end, or this many seconds with no further write to the plan. `0` = ready at once (writes still coalesce until the runner takes the request). |
| `JUDGE_COMPLETION_DEDUPE_SECONDS` | `900` | Completion dedupe window. It applies only when one of the two requests has no Hermes turn id; otherwise the turn decides. |
| `JUDGE_REVIEW_TEXT_ONLY` | `1` | `1`: a turn with no tool calls is still reviewed when its answer qualifies (below). `0`: never. |
| `JUDGE_REVIEW_TEXT_ONLY_MIN_CHARS` | `200` | ...the answer must be at least this long... |
| `JUDGE_REVIEW_TEXT_ONLY_REQUIRE_CLAIMS` | `1` | ...and contain a claim word (done, fixed, blocked, ran, verified, escalated, gate, ...) or coincide with a gate decision. `0` = any long enough answer. |
| `JUDGE_ENQUEUE_ALWAYS` | unset | Environment only. `1`: enqueue a completion for every turn, with or without activity. |

**Evidence**

| Variable | Default | Meaning |
|---|---|---|
| `JUDGE_WINDOW_GRACE_SECONDS` | `10` | The evidence window ends at `created` + this, never past the start of the session's next turn. The runner waits until `created` + this + 3 s before collecting. |
| `JUDGE_LOG_TZ` | empty | Time zone of Hermes log timestamps: empty = the workstation's local zone, `UTC`, `+02:00` or an IANA name. Hosts are always queried in UTC. |
| `JUDGE_NOISE_GLOBS` | empty | Extra globs (absolute paths, space- or comma-separated) of bookkeeping files that never count as a change, on top of Hermes's own (`skills/.usage.json`, `skills/.locks/`, `skills/.curator_*`, `$HERMES_HOME/*.lock`). |
| `JUDGE_LOG_NOISE_LOGGERS` | empty | Read from `site.env` (the environment overrides only a value set there). Extra untagged Hermes loggers (`name` or `prefix.*`) dropped from the bundle's log context. |
| `JUDGE_SNAPSHOT_MAX_FILES` | `2000` | Files snapshotted per opted-in plain directory; beyond it the root is truncated and new files in it go unseen. |
| `JUDGE_SNAPSHOT_MAX_BYTES` | `1048576` | Larger files in an opted-in directory are hashed, not copied. |
| `JUDGE_HOST_PROBES` | `1` | `0` stops the collector's read-only host-state probes (at most 4 per request). |
| `JUDGE_REFUSAL_NEXT_CALLS` | `6` | Read from `site.env` (the environment overrides only a value set there). Tool calls listed after each refusal in `refusals.jsonl` (1–20). |
| `JUDGE_PROBE_TIMEOUT` | `20` | Seconds per probe step. |
| `JUDGE_SSH_CONNECT_TIMEOUT` | `8` | SSH `ConnectTimeout` for the collector and probes (always `BatchMode`). |
| `JUDGE_SSH_ALIASES` | empty | SSH aliases that reach the edge (space-separated). The gate treats them as the edge; C3 and the probes test them. |
| `EDGE_SSH_USER`, `EDGE_SSH_KEY` | `ubuntu`, `~/.ssh/edge.pem` | How read-only probes reach Covenant (`EDGE_SSH_USER@EDGE_PUBLIC_IP`) when either is set explicitly; otherwise through the first `JUDGE_SSH_ALIASES` alias. Walter is reached as `BACKEND_SSH_USER@BACKEND_LAN_IP` (default user `operator`). |

**C3 verifiers**

| Variable | Default | Meaning |
|---|---|---|
| `JUDGE_VERIFY_BUDGET` | `45` | Total seconds for the verifiers (the hook's timeout is 60). |
| `JUDGE_CLAIM_MTIME_WINDOW` | `3600` | A claimed change also counts as confirmed when the file was modified within this many seconds. |
| `JUDGE_YAML_PYTHON` | unset | Python with PyYAML for the YAML check; else the Hermes venv or any Python that has it. |

**C5 injection**

| Variable | Default | Meaning |
|---|---|---|
| `JUDGE_INJECT_MIN_SEVERITY` | `medium` | Lowest severity injected. |
| `JUDGE_INJECT_WINDOW_HOURS` | `24` | Only findings (and refusals, for the reminder) this recent. |
| `JUDGE_INJECT_MAX_CHARS` | `2000` | Size cap of the injected block. An R8 item takes up to about 1,200 characters, so the default fits one in full; items that don't fit are counted in a "more not shown" line. |
| `JUDGE_INJECT_LOCAL` | `0` | `1` also injects local-judge findings. Skips are counted in `inject.log`. |
| `JUDGE_INJECT_REFUSAL_REMINDER` | `1` | After a refused escalation or a block, remind the agent once, at its next turn, that a refusal is a stop. `0` = off. |
| `JUDGE_STALL_MINUTES` | `15` | C5 and `judge-findings` warn when the oldest ready request has waited this long and no runner is busy. `0` = off. |
| `JUDGE_HERMES_AGENT_DIR` | `$HERMES_HOME/hermes-agent` | Where `inject.py` loads Hermes's threat patterns from (a small built-in set when absent). |

**C6 watcher**

| Variable | Default | Meaning |
|---|---|---|
| `JUDGE_RUNAWAY_TOKENS` | `24000` | Alert when a slot has decoded this many tokens, whatever its `n_predict`. Above the gateway's default output limit (16384) and below the 32768 model maximum, so a near-maximum generation is flagged about three quarters of the way. Models capped at 16384 hit only the time trigger. |
| `JUDGE_RUNAWAY_MINUTES` | `10` | ...or when one call has run this long. |

**Operator tools and locations**

| Variable | Default | Meaning |
|---|---|---|
| `HERMES_HOME` | `~/.hermes` | Hermes profile. |
| `JUDGE_REVIEW_DIR` | `$HERMES_HOME/review` | Review data. |
| `SITE_ENV` (or `JUDGE_SITE_ENV` for `install.sh`) | `<repo>/site.env` | Path of the site file, for an extracted judge or another checkout. |
| `JUDGE_ACK_AGENT_ENV` | empty | Environment only. Extra env names (comma-separated) that mark an agent session for `judge-ack`; it can only add markers. |
| `JUDGE_ALERT_MIN_INTERVAL_S` | `900` | Environment only (`judge.env`). Seconds between two desktop alerts for the same failed unit; `runner.log` gets every alert. |
| `JUDGE_CHECK_UNITS` | `1` | Environment only. `0`: `judge-findings` skips `systemctl --user is-failed` (the tests set it). |

The judge also reads the site's `BACKEND_LAN_IP`, `BACKEND_WG_IP`, `EDGE_PUBLIC_IP`, `EDGE_WG_IP`, `SPARK_DOMAIN`, `SPARK_*_HOST` and `SPARK_SITE_NAME` (gate host patterns, probe targets, claims masking). `JUDGE_DIR` and `JUDGE_PYTHON` are rendered into the units by the installer; they are not settings.

## Units

systemd **user** units, installed from the checkout the hooks run from (`install.sh --with-units`). Details and the reasons for each limit: [runner/units/README.md](runner/units/README.md).

| Unit | Enabled | What it does |
|---|---|---|
| `judge-review.path` | yes | Watches `$JUDGE_REVIEW_DIR/queue/` (`PathChanged=`) and starts the service. Deferred plan requests in `queue/deferred/` do not trigger it. `TriggerLimitBurst=1000` per 2 s. |
| `judge-review.service` | no (triggered) | Oneshot `run_judge.py --pending`: release due deferred requests, judge every ready request, re-scan for requests that arrived meanwhile. No start limit. `TimeoutStartSec=3600`. |
| `judge-review.timer` | yes | Backstop: starts the service 2 min after the timer starts, then 5 min after each run. A failed or stopped path unit delays reviews; it never stalls them. |
| `judge-alert@.service` | never (template) | Started by `OnFailure=` of the path unit and the service: logs `ALERT: judge unit … failed … Fix: …` to `runner.log` and runs `notify-send` when a display is set. Never restarts anything. |
| `judge-runaway-watch.service` | yes | C6: `watch/runaway.py --interval 30`. Read-only: polls the slots probe and alerts in `watch.log`, the journal and the desktop; never cancels or unloads. |

The rendered `judge-review.service` sets `PATH=%h/.local/bin:...` (where `claude` usually lives), `UnsetEnvironment=ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN`, and `EnvironmentFile=-%h/.config/judge/judge.env`. The frontier judge never uses the local gateway; do not "fix" a failing frontier call by pointing `claude` at it.

```bash
systemctl --user status judge-review.path judge-review.timer judge-runaway-watch.service
systemctl --user stop judge-review.path judge-review.timer     # pause: hooks keep queueing, nothing is judged
systemctl --user start judge-review.path judge-review.timer    # resume...
systemctl --user start judge-review.service                    # ...and judge the backlog now
systemctl --user disable --now judge-review.path judge-review.timer judge-runaway-watch.service   # off for good
```

Stopping only the path unit is not a pause: the timer still runs the queue every 5 minutes. To keep reviewing without frontier spend, set `JUDGE_MODE=local`. Logs: `journalctl --user -u judge-review`, `runner.log`, `journalctl --user -u judge-runaway-watch`, `watch.log`.

**Health.** `judge-findings` prints a `WARNING:` on stderr when a judge-review unit is `failed` or the oldest ready request has waited more than `JUDGE_STALL_MINUTES`; the agent gets the stall warning once through C5, with the instruction to tell you rather than repair it.

## Commands

| Command | What it does |
|---|---|
| `judge/bin/judge-findings` | One line per finding of the last 7 days, ending `open:N agent-acked:N closed:N` (plus `defect:N` when it has R8 code-defect items). The item views print an R8 item as `verdict=DEFECT (code defect)` with `defect:`, `code:` (the quoted lines), `failure scenario:` and `recommendation:` lines. Options: `<request-id>` (every item of that request, including its claims stage), `--items`, `--needs-human` (high items no human has closed: **start here**), `--unacked`, `--min-severity LEVEL`, `--since HOURS` (`0` = all), `--json`. |
| `judge/bin/judge-ack <request-id> <item-id> "<reason>"` | Acknowledge an item as you (`actor: human`). The agent uses `--agent`. Inside an agent session (Hermes's environment markers, or a Hermes process among its parents) it records `agent` whatever you pass, and says so. Run it from your own shell. A human ack replaces an agent ack, never the reverse. R8 `defect` items are acked like any other item. |
| `python3 judge/runner/run_judge.py --pending` / `<request-id>` | Judge every ready request, or one request now (its `not_before` is ignored). |
| `judge/runner/rejudge.py <id>... --out DIR [--mode local\|frontier\|frontier-claims] [--model X] [--no-budget] [--sensitive-local]` | Re-judge stored bundles with the current prompt and validator; writes only to `DIR` (never inside the review dir). A sensitive bundle is never sent to the frontier: `--mode frontier` refuses it (exit 1) unless `--sensitive-local` judges it locally. `--mode frontier-claims` runs the claims stage on any bundle. `--no-budget` keeps the calls out of the daily cap. Procedure: [agent-judge-rejudge.md](../docs/runbooks/agent-judge-rejudge.md). |
| `judge/hooks/gate.py --explain < payload.json` | The gate's decision and every rule hit for a tool-call payload, with no log line and no request. Example payload: `{"hook_event_name":"pre_tool_call","tool_name":"terminal","tool_input":{"command":"cat ~/.config/spark/x.key"},"session_id":"explain","cwd":"/tmp"}`. |
| `python3 judge/lib/config.py --classify PATH...` / `--explain PATH...` | The data class of paths; `--explain` prints each path's class (`secret`, `scratch`, `infra`, `sensitive`) and the request's, as JSON. |
| `judge/collector/claims_only.py <evidence-dir>` | Print the exact claims-only message for a stored bundle (exit 1 if the self-check refuses it). |
| `judge/collector/collect.py <request-id>` | Build a bundle by hand (normally the runner does it). |
| `judge/runner/validate.py <finding.json> --bundle <evidence-dir> [--local-max-severity LEVEL]` | Re-validate a stored reply without a model call. |
| `python3 judge/watch/runaway.py --once -v [--slots-file F]` | One watcher pass, printing each slot. |

Findings are in `$JUDGE_REVIEW_DIR/findings/<id>.md` (to read) and `.json`. Every item has a rubric code (R1–R8), a severity, the claim, the **evidence** (bundle text it quotes), a verdict and a recommendation; an R8 code-defect item has verdict `defect` and a `failure_scenario` (shown in the `.md`, by `judge-findings` and in C5); the finding's `notes` list every item the validator dropped, downgraded or capped. An item is **closed** when you acked it, or when the agent acked it and it is not `high`; any ack stops C5 from showing it again. The agent runs as your user, so it can still forge a human ack deliberately; look at who acked anything `high` ([why](../docs/agent-judge.md#findings-c5-injection-and-acks)).

## Review directory

`$JUDGE_REVIEW_DIR` (files 600, dirs 700); full layout in [CONTRACT.md](CONTRACT.md#runtime-directories-env-overridable):

```
queue/<id>.json            ready requests          queue/deferred/<id>.json   plan requests not due yet
evidence/<id>/             bundles                 done/<id>.json             judged requests
findings/<id>.json|.md     findings                findings/<id>.claims.json  claims stage
acks/<id>.<item>           acknowledgements        snapshots/<session>/       session-start copies, events, C3 results
gate.log  watch.log  inject.log  runner.log  hook-errors.log  usage.json  gate-policy.json  (+ state files)
```

## Cost and caps

- Frontier calls happen only in the runner, never in a hook; C2 and C3 cost nothing.
- At most `JUDGE_FRONTIER_DAILY_MAX` frontier calls per UTC day, each capped at `JUDGE_FRONTIER_MAX_USD`. Every `claude -p` call counts: the retry after an invalid reply, the follow-up after a probe round, the claims stage of each sensitive completion. At the cap, requests fall back to the local judge and the claims stage is skipped (never re-run locally); the finding says so.
- `usage.json` holds today's count, the cap and the summed cost the CLI reported (`frontier_usd`; on a subscription login that is the API-equivalent figure, not a bill).
- Local reviews cost GPU time only. To stop all frontier spend at once, set `JUDGE_MODE=local`.

## Troubleshooting

| Symptom | Check |
|---|---|
| Hooks never fire | `hermes hooks list`: not allowed means consent is missing; approve in a TTY session. The gateway does not prompt. |
| Every terminal or write call is blocked with "failed closed" | `gate.py` crashed, timed out or printed non-JSON. Run `hermes hooks test pre_tool_call --for-tool terminal` and read `hook-errors.log`. Emergency: `judge/install.sh --uninstall`. |
| The installer says "does not round-trip" | Unusual YAML around `hooks:` (anchors, flow style). Nothing was written; merge the printed block by hand. |
| No findings appear | `systemctl --user status judge-review.path judge-review.timer`; is `queue/` filling? `systemctl --user start judge-review.service` runs the queue now. Then `runner.log` and `journalctl --user -u judge-review`. |
| Both runner units `failed` (`start-limit-hit`), or `runner.log` has `ALERT: judge unit … failed` | Units installed before #40 hit systemd's start limit on bursts. Re-install: `judge/install.sh --apply --with-units --start`. By hand: `systemctl --user reset-failed judge-review.service judge-review.path && systemctl --user start judge-review.path`, then start the service for the backlog. A judge backend failure also exits non-zero and alerts: read `runner.log`. |
| Frontier calls fail only from the service (`frontier exited …`) | Is `claude` on the unit's `PATH` (`systemctl --user show judge-review.service -p Environment`)? If not, set a full `PATH` in `judge.env`. Is the login valid? Run `claude` once in a terminal. Does anything force another endpoint (`systemctl --user show-environment \| grep -i anthropic`, the `env` block of `~/.claude/settings.json`)? Never copy credentials into the repo, `site.env` or a unit. A failed request stays queued and is retried, up to `JUDGE_MAX_ATTEMPTS`. |
| Findings come from the local judge although `JUDGE_MODE=frontier` | Expected for `sensitive` requests or past the daily cap; the finding's notes say which. Check a path with `python3 judge/lib/config.py --explain <path>`: a secret-shaped file makes the whole request sensitive, even in the repo; more than `JUDGE_MIXED_MAX_SENSITIVE` non-infra paths do too. A worktree comes out `sensitive` only when its `.git` file does not point into the main repo's `.git/worktrees/`. |
| A frontier finding shows `${SPARK_DOMAIN}` or another `${KEY}` instead of a real value | Expected when the same placeholder also occurs literally in the input (a template line): it is ambiguous, so it is left as written; the finding's notes list it. Every other placeholder is replaced by the real value before the finding is stored. The frontier input itself (`claims-input.txt`, rejudge's `.input.txt`) and `judge-raw.txt` stay masked. A `site-mask self-check failed` error means a configured value was left in the input; nothing was sent. |
| A plan review is queued but not judged | Expected until the turn ends or `JUDGE_PLAN_DEBOUNCE_S` passes: it waits in `queue/deferred/`. The next hook event, the C6 watcher's poll or the timer releases it. To judge it at once, move it into `queue/` and run `run_judge.py <id>`. |
| A turn got no review, or chit-chat is reviewed | A turn without tool calls is reviewed only when its answer is at least `JUDGE_REVIEW_TEXT_ONLY_MIN_CHARS` long and makes a claim, or coincides with a gate decision; the same answer is never reviewed twice. Tune the `JUDGE_REVIEW_TEXT_ONLY*` settings; `JUDGE_ENQUEUE_ALWAYS=1` reviews every turn. |
| Two completion requests for one turn | The session-end audit merges into the turn's C3 request only while the runner has not started on it. If it had, a second request is written only when the session end adds new paths. |
| C3 nudged about a file the answer only mentioned | Only paths an edit verb governs are checked, and relative names only when they exist under the session cwd or its repo root. The sentence is in the request's `detail.claim_flags`; report it. |
| A command asked for approval and you think it should not have | `judge/hooks/gate.py --explain` shows the rule and the word that triggered it. Narrow the command (`--include='*.sh'`, a subdirectory) rather than approving. Gate changes need no reinstall. |
| The agent did a refused thing another way | See `evidence/<id>/refusals.jsonl`: `summary: possible-workaround` and each next call's `route`. The judge files it as R4 medium (high for a secret or a host). In a one-shot `-q` run there is no next turn for the C5 reminder, so only the review catches it. Widen `JUDGE_REFUSAL_NEXT_CALLS` if the workaround came later. |
| A finding blames the agent for a path it never touched | A request path counts only when a tool call of the session wrote it (a `write_file`/`patch` target, a `cp`/`mv`/`install`/redirect/`tee` target) or the snapshot shows it changed and a call names it. The rest is "claimed but not confirmed": listed in the manifest's `attribution.unconfirmed_paths` and the request copy's `detail.unconfirmed_paths`, kept out of `agent-diff.patch` and out of classification. Others' changes are in `others-changed.txt`. |
| A finding has a `code version: ...` note | The request (or the bundle) was written by other judge code than the one judging it, e.g. hooks still on the old checkout after a pull, or a dirty tree. A warning only. Compare `code_version` in the request with `code_versions` in the manifest; `sha` is `git rev-parse HEAD:judge`. |
| Files the agent created with `cp`/`mv`/`install` are in `others-changed.txt` | Attribution reads the copy commands from Hermes's `state.db`; if that database lost the session's rows, the copies cannot be traced. |
| A `JUDGE_INFRA_REPOS` dir has no diff | It is snapshotted from the next session start. Check the manifest's `snapshot`: `truncated` (over `JUDGE_SNAPSHOT_MAX_FILES`), hashed-only files, `skipped_roots`. |
| The judge says it cannot see the session's tool calls | Check `grep -c '\[<session>\]' evidence/<id>/hermes-log.txt`. Parallel tool calls are untagged in Hermes's log and are attributed only when no other session was active in the turn. |
| Gate bundles say `outcome: unknown` | Expected while an escalation still waits for you when the bundle is built. Otherwise check that the `post_tool_call` matcher includes `read_file` (re-run `install.sh --apply`, restart Hermes and the gateway). |
| A claim about a host service has no evidence | The collector probes units and loopback ports named in the claims or gate excerpts, at most 4. The host must be named nearby or the unit known; `JUDGE_HOST_PROBES` must not be `0`; the host must be reachable with the probe SSH settings. |
| A finding reports a hostname mismatch in a probe | Probes print the logical target (`# host: walter …`); journal lines carry the machine's own hostname, which can differ. |
| A local finding says "reply 1 was truncated" | The local judge hit `JUDGE_LOCAL_MAX_TOKENS`. The finding comes from the re-ask; the cut-off reply is in `evidence/<id>/judge-raw.txt`. Re-judge with a higher limit if it listed items the re-ask lost. |
| A code bug in the agent's diff got no R8 item | Read the finding's notes: `code review (R8): off (...)` says why (local judge, `data_class`, a gate request, no code file in the diff, `JUDGE_CODE_REVIEW=0`); no note means the bundle had no code. `validator dropped F<n>: ... R8 quote not found in agent-diff.patch` means the judge misquoted the code, and the raw reply is in `evidence/<id>/judge-raw.txt`. R8 sees the diff plus `data-files.txt`: a defect that needs a data file the agent only read is in reach only when that file is in an infra location (manifest `data_files`: `withheld_counts` `non-infra location` means it was not); code cut by the bundle budget (`[... runner omitted N of M hunk(s) ...]`) is out of reach. |
| A data file the code reads is not in `data-files.txt` | Manifest `data_files.withheld` names infra files left out and why (too big, over `JUDGE_DATA_FILES_MAX`, `.txt` not named by the diff); `withheld_counts` counts secret-shaped, scratch and non-infra files, which are never included. Check a path with `python3 judge/lib/config.py --explain <path>`. Keep a sanitized sample of such data in the repo (where the code's tests can use it too) rather than widening the infra rules. |
| An R8 item looks like a nit or a guess | Style, naming and hedged ("might", "possibly") items are dropped by the validator, at most 3 R8 items are kept, and `high` needs a shown security impact or data loss. If one still gets through, re-judge the bundle and report the item. Set `JUDGE_CODE_REVIEW=0` to turn R8 off. |
| Local findings full of `high` items that turn out false | Expected only with the cap raised. Unquoted `false` verdicts become `n/a` / `low`; local items are capped and not injected. |
| C6 alerts `n_decoded=… >= 24000 (cap n_predict=32768 …)` | A request asked for (nearly) the model's maximum output; it stops at the cap. Unload the model with the printed command only if you need the GPU. `NO output cap (n_predict=-1)` means a call bypassed the gateway's output limit. |
| A `runaway` review was judged by the local model | The request was not shaped like the watcher's (`source_event=watch`, session `watch-task<N>`, no paths, no cwd). Compare `manifest.json` `request_data_class` with `data_class`. |
| The host diff window looks wrong | Host `find -newermt` is UTC; the Hermes log zone is `JUDGE_LOG_TZ`. |
| Desktop alerts about a runaway "task 4711" while tests run | Test data from an old suite; `tests/conftest.py` now strips the display variables. A real alert names a real task and a live model. |

## Uninstall

```bash
judge/install.sh --uninstall --dry-run              # show what would be removed
judge/install.sh --uninstall                        # remove only the judge's hook entries (backup first)
judge/install.sh --uninstall --with-units --start   # also disable and remove the user units
```

Uninstall removes the managed hook entries, and the `hooks:` key itself if nothing else is left in it. It keeps `$JUDGE_REVIEW_DIR` and the consent records, and prints the `hermes hooks revoke` commands for them. To restore the exact previous file, copy back a `config.yaml.bak-judge-*`.

## Extracting to its own repo

`judge/` is built to be lifted out. Nothing under `judge/` imports or sources anything outside it, except `site.env`, which only `lib/config.py` and `lib/config.sh` read, by path. The one deliberate soft link is `verify.py` running a repo's `scripts/check-sanitized.sh` when the changed file is inside such a repo, found at run time and skipped when absent.

```bash
git clone <this repo> agent-judge && cd agent-judge
git filter-repo --subdirectory-filter judge     # judge/ becomes the repo root, history kept
cp ../<this repo>/site.env.example site.env.example   # then keep only the judge and existing-var lines
```

Afterwards point the judge at your site values with `SITE_ENV=/path/to/site.env` in the environment Hermes runs with, in the units (`judge.env`) and when running `install.sh`. Managed hook entries are recognised by their `managed_by` tag, so re-running `install.sh --apply` from the new location replaces the old entries. Approve the new command paths at the next consent prompt, and `hermes hooks revoke` the old ones.
