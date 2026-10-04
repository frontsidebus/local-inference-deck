# judge/: a reviewer for the local agent

A frontier model, or a local one for sensitive work, reviews what the local agent (Hermes on `coder` / `coder-fast`) did, using **evidence collected by deterministic code** rather than the agent's own account. The judge is driven by Hermes shell hooks. It reads, probes (read-only) and reports. It never changes a host, and its findings only recommend.

Why it exists, the incident that motivated it, and the rubric: [docs/agent-judge.md](../docs/agent-judge.md). The binding interface between the parts: [CONTRACT.md](CONTRACT.md). Pilot procedure: [docs/runbooks/agent-judge-pilot.md](../docs/runbooks/agent-judge-pilot.md).

## How it works

Gates are synchronous and dumb; judging is asynchronous and smart. No model call ever sits inside a hook that blocks the agent.

```
                       Hermes (worker, local model)
                                   |
  plan written ------------- C1 post_tool_call  write_file|patch ----> enqueue.py --+
                                   |                                                 |
  risky tool call ---------- C2 pre_tool_call   terminal|write_file|patch|read_file |
                             gate.py (no model, fail_closed) --> approve: ask human  |
                                   |                          --> block: hard stop   |
                                   |                          --> enqueue -----------+
  "done" after edits ------- C3 pre_verify      verify.py: bash -n, JSON/YAML parse, |
                             check-sanitized, ssh -G ... --> continue: fix it first  |
                                   |                       (once) and enqueue -------+
  session ends ------------- C4 on_session_end  enqueue.py (completion audit) ------+
                                   |                                                 |
  next turn starts --------- C5 pre_llm_call    inject.py <-- unacked findings       |
                                   |               (labelled "data, not              v
                                   |                instructions")        $JUDGE_REVIEW_DIR/queue/
  always ------------------- C6 watch/runaway.py (systemd user unit,                 |
                             polls llama-server slots) --> watch.log, alert, queue --+
                                                                                     |
                     runner (systemd path unit on queue/) <--------------------------+
                       1. collector/collect.py  -> evidence/<id>/  (logs, gate decisions + outcome,
                                                   agent diff / others' changes vs. snapshot,
                                                   host find in UTC, slots, C3 results,
                                                   host-state probes; window ends at
                                                   created + grace or the next turn)
                       2. run_judge.py          -> frontier `claude -p` (infra data only)
                                                   or local `big` (sensitive data, or over the cap);
                                                   sensitive completion: + frontier on a claims-only
                                                   bundle (collector/claims_only.py, no contents/paths)
                       3. validate.py           -> findings/<id>.json + .md   (unbacked items
                                                   dropped or downgraded), findings/<id>.claims.json
                                                   (claims stage), request -> done/
```

| # | Hermes event | Script | Sync | Effect |
|---|---|---|---|---|
| C1 | `post_tool_call`, matcher `write_file\|patch\|terminal\|memory\|skill_manage\|read_file` | `hooks/enqueue.py` | async | Plan edits under `.hermes/plans/` queue a **plan review**. Every matched call is recorded (with call markers) for attribution and for gate `outcome`. |
| C2 | `pre_tool_call`, matcher `terminal\|write_file\|patch\|read_file`, `fail_closed: true` | `hooks/gate.py` | sync, ms | Deterministic policy: escalate host-mutating commands, sensitive-path writes and reads of secret-shaped files to you, block edits to allowlists, hooks and approval settings. |
| C3 | `pre_verify` | `hooks/verify.py` | sync, under 60 s | Fast verifiers on the changed paths; nudges the agent once if one fails, re-checks (record only) after the fix, and records every result for the judge. |
| C4 | `on_session_end` (and `on_session_start` for the snapshot) | `hooks/enqueue.py` | async | Snapshot watched paths at start; queue a **completion audit** at the end. |
| C5 | `pre_llm_call` | `hooks/inject.py` | sync, fast | Show unacknowledged findings to the agent as reviewer data. |
| C6 | none (telemetry) | `watch/runaway.py` | async | Alert on a runaway generation: `n_decoded >= JUDGE_RUNAWAY_TOKENS` (24000) whatever the slot's `n_predict` (the alert shows `n_decoded/n_predict` progress, or says the cap is missing), or one call running `>= JUDGE_RUNAWAY_MINUTES` (10). Never cancels; queues an `infra` review (slot telemetry only), so the frontier judge reviews it. |

## Components

| Path | What it is |
|---|---|
| `install.sh` | Merges the hooks block into `$HERMES_HOME/config.yaml`, creates the review dir, renders the gate policy, optionally installs the systemd user units. Dry run by default. |
| `hooks/` | Hermes shell-hook entrypoints: JSON on stdin, JSON on stdout. All but `gate.py` fail open and log to `hook-errors.log`. |
| `policy/gate-policy.json.tmpl` | C2 rules. Host patterns are `${VARS}` from `site.env`; the installer renders it to `$JUDGE_REVIEW_DIR/gate-policy.json`. |
| `collector/collect.py` | Builds the evidence bundle for one request. |
| `collector/extras.py` | Adds C3 results and read-only host-state probes to the bundle. |
| `probes/probe.py` | The only way the judge touches hosts: an allowlist of read-only probes with per-argument validation. |
| `runner/` | Judge prompt, `run_judge.py` (frontier or local), `rejudge.py` (re-judge stored bundles), the findings validator, and the systemd user units. |
| `bin/` | `judge-findings` and `judge-ack`, the operator CLIs. |
| `watch/runaway.py` | The C6 slot watcher and its user unit. |
| `lib/` | `config.py`/`config.sh` (site.env and defaults), `queue.py` (atomic files and schema checks). |
| `schema/` | JSON schemas for requests and findings. |
| `tests/` | pytest, one module per part. Tests always use a temp `HERMES_HOME`. |

Runtime code is Python 3.10+ stdlib only. The installer uses the Hermes venv's Python for PyYAML, because it edits `config.yaml`.

## Install

Prerequisites: Hermes installed with its venv at `$HERMES_HOME/hermes-agent/venv`, `python3` 3.10 or newer, a filled-in `site.env` at the repo root (see [Configuration](#configuration)), and the `claude` CLI logged in if you use frontier mode.

```bash
judge/install.sh                      # dry run: prints the hooks block and a diff of config.yaml
judge/install.sh --apply              # back up config.yaml, merge, create the review dir, render the policy
judge/install.sh --apply --with-units # also install the runner and watcher units (prints systemctl commands)
judge/install.sh --apply --with-units --start   # ...and run daemon-reload + enable --now
```

What `--apply` does:

1. Copies `config.yaml` to `config.yaml.bak-judge-<UTC timestamp>`.
2. Merges six entries under `hooks:`. Each carries `managed_by: agent-judge`, a key Hermes ignores and YAML round-trips keep. Your own hook entries, `hooks.outbound` and everything outside `hooks:` are left alone. Comments inside the `hooks:` block are not preserved; the backup keeps them. Re-running is a no-op.
3. Creates `$JUDGE_REVIEW_DIR` and its subdirectories with mode 700.
4. Renders `gate-policy.json` (mode 600) from `site.env`.
5. Refuses to run if any hook script is missing, because a missing `gate.py` with `fail_closed: true` would block every tool call.

It checks each event name against `VALID_HOOKS` and `SHELL_UNSUPPORTED_HOOKS` in the installed Hermes source and skips, with a warning, any event the installed Hermes can't run as a shell hook.

The merged block looks like this (paths are absolute on your machine):

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

Other options: `--hermes-home DIR` (another profile, or a temp dir for testing), `--review-dir DIR`, `--site-env FILE`, `--python PATH` (the interpreter the hooks run with), `--hermes-python PATH`, `--unit-dir DIR`. See `judge/install.sh --help`.

## Consent

Hermes asks for approval the first time it sees each `(event, command)` pair and records the answer in `$HERMES_HOME/shell-hooks-allowlist.json`. The installer **does not** set `hooks_auto_accept: true` and warns if it finds it set. Keep it false: auto-accept would let any future edit to `hooks:` run code without asking you.

1. Start an interactive session (`hermes chat`) and approve each judge hook when prompted. Expect six prompts, one per event: `enqueue.py` serves three events and each pair is approved separately.
2. Check: `hermes hooks list` shows each one as allowed; `hermes hooks doctor` checks exec bits, consent, mtime drift and JSON output.
3. Smoke test: `hermes hooks test pre_tool_call --for-tool terminal` runs the gate against a synthetic payload; the `parsed` line shows the decision Hermes would receive (an empty decision means the call passes).

Non-TTY runs (the gateway, cron) never prompt. They skip unapproved hooks with a warning, so approve in a terminal first. Consent is keyed on the command string, not the script's contents: after you pull changes to `judge/hooks/`, run `hermes hooks doctor` to see the mtime drift and review the diff.

### After pulling judge changes

Consent is keyed on `(event, command)`, not on the matcher or the script contents. When an update changes a matcher (for example `read_file` was added to the C2 matcher, and later to the `post_tool_call` matcher so gated reads get an outcome) re-run `judge/install.sh --apply` so `config.yaml` gets the new matcher and `gate-policy.json` is re-rendered, then restart Hermes and the gateway (they read hooks at start). **No new consent is needed**, because the command string is unchanged. `hermes hooks list` should still show every judge hook as allowed.

## Running reviews automatically

Hooks only queue review requests; the runner judges them. Two systemd **user** units run it without you:

| Unit | What it does |
|---|---|
| `judge-review.path` | Watches `$JUDGE_REVIEW_DIR/queue/` (`PathChanged=`) and starts `judge-review.service`. The only runner unit you enable. |
| `judge-review.service` | Oneshot: `run_judge.py --pending`. It judges every queued request, then re-scans `queue/` and also judges requests that arrived while it was busy (the path unit does not fire again for those), trying each request at most once per run. |
| `judge-runaway-watch.service` | C6 watcher (`watch/runaway.py --interval 30`). Read-only: it polls the slots probe and alerts; it **never cancels or unloads** anything. |

Install, enable and start them from the checkout the hooks run from:

```bash
judge/install.sh --with-units                  # dry run: shows the files it would install
judge/install.sh --apply --with-units --start  # install, daemon-reload, enable --now the .path unit and the watcher
systemctl --user status judge-review.path judge-review.service judge-runaway-watch.service
systemctl --user start judge-review.service    # once, for requests that were queued before the path unit started
```

The installer renders `${JUDGE_DIR}`, `${JUDGE_PYTHON}`, `${HERMES_HOME}` and `${JUDGE_REVIEW_DIR}` into `~/.config/systemd/user/`. Check the rendered `judge-review.service`:

- `Environment=PATH=%h/.local/bin:...`: `claude` is usually in `~/.local/bin`, which a user manager started outside a login shell may lack.
- `UnsetEnvironment=ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN`, and `run_judge.py` strips every endpoint variable (and anything naming the gateway host) from the `claude` child as well. The frontier judge always uses the normal Anthropic API, never the local gateway. Do not "fix" a failing frontier call by pointing `claude` at the gateway, for the service or for your own shell.
- `EnvironmentFile=-%h/.config/judge/judge.env` is optional. Values there are environment, so they **override** `site.env`; leave the file absent unless you want a service-only override. The policy itself belongs in `site.env`, which the runner reads from the repo root of the checkout in `ExecStart`.

### Policy as deployed

The workstation runs this policy (set in `site.env`):

```bash
JUDGE_MODE=frontier             # infra-class requests -> frontier judge (claude -p)
JUDGE_FRONTIER_DAILY_MAX=20     # frontier calls per UTC day; past it, local fallback
JUDGE_LOCAL_MODEL=coder-fast    # sensitive-class requests -> local judge, already loaded: no model eviction
JUDGE_LOCAL_MAX_SEVERITY=medium # a local finding is never above medium
JUDGE_INJECT_LOCAL=0            # local findings go to you, never into the agent's context (C5)
JUDGE_SENSITIVE_FRONTIER_CLAIMS=1  # sensitive completions also get a frontier review of a claims-only bundle
```

- **Infra** requests (every changed path is infra, see [Data boundary](#data-boundary)) go to the frontier judge.
- **Sensitive** requests, and requests with no or unclear class, go to the local judge whatever `JUDGE_MODE` says. The finding's notes say `data_class=sensitive: local judge enforced`.
- Local findings are capped at `medium` (the notes record each lowered item) and C5 skips them (counted in `inject.log`).
- `coder-fast` is the same model family as the worker (see [Local judge policy](#local-judge-policy)); the cap and the no-injection rule are what make that acceptable for sensitive-only reviews. Read local findings as hints.
- **Sensitive completions get a second, frontier review of their claims only** (`JUDGE_SENSITIVE_FRONTIER_CLAIMS=1`, the default since 2026-10-03). Every agent slip found in runs 1 and 2 was in a sensitive session, which the local judge alone missed. The frontier judge gets a claims-only bundle: the agent's final answer (redacted and masked: paths `file#N`, addresses `ip#N`, site hosts `host#N`, local accounts `user#N`, digests and token-like runs `hex#N`/`blob#N`, secret length/prefix sentences withheld; a fail-closed self-check refuses anything that survives), gate decisions cut to the command name, C3 pass/fail per `file#N`, tool-call metadata and counts, and the window. It never gets file contents, diffs, paths, command output, probes or your message ([Data boundary](#data-boundary)). Its finding is `findings/<id>.claims.json`, mode `frontier-claims`, items `FC1`, `FC2`, ...; C5 injects it like any frontier finding. The local finding (`findings/<id>.json`) stays capped and out of C5. Gate, plan and runaway requests get no claims stage.

Every local request sets `max_tokens` (`JUDGE_LOCAL_MAX_TOKENS`, default 4096), so the judge itself never sends an unbounded generation.

### Cost and caps

- At most `JUDGE_FRONTIER_DAILY_MAX` (20) frontier calls per UTC day, each with `--max-budget-usd` `JUDGE_FRONTIER_MAX_USD` (2): a hard ceiling of about $40 a day. Every `claude -p` call counts, including the retry after an invalid reply, the follow-up after a probe round, and the claims stage of each sensitive completion; each has its own dollar cap. Every sensitive completion now makes one frontier call as well, so a cap sized for infra reviews alone runs out about twice as fast; consider 40. At the cap the claims stage is skipped (noted in the local finding), never re-run locally.
- `usage.json` in the review dir holds today's count (`frontier_runs`, `cap`) and the cost `claude -p` reported (`frontier_usd`, summed per UTC day). On a subscription login the reported cost is the API-equivalent figure, not a bill.
- Local reviews cost nothing beyond GPU time and are not counted.

### Pause and resume

```bash
systemctl --user stop judge-review.path      # pause: hooks keep queueing, nothing is judged
systemctl --user start judge-review.path     # resume ...
systemctl --user start judge-review.service  # ... and judge the backlog (the path unit fires on changes only)
systemctl --user disable --now judge-review.path judge-runaway-watch.service   # turn both off for good
```

To keep reviewing but stop frontier spend, set `JUDGE_MODE=local` in `site.env` (no restart needed: the runner reads it on each run). Logs: `journalctl --user -u judge-review`, `$JUDGE_REVIEW_DIR/runner.log`, `journalctl --user -u judge-runaway-watch` and `watch.log`.

### Frontier auth from a non-TTY service

`claude -p` under systemd uses the same login as your terminal: the OAuth credentials in `~/.claude/.credentials.json` (it needs only `HOME`, which the user manager sets). If findings say `frontier exited ...` or `frontier returned an error` while `claude -p` works in your shell:

1. Is `claude` on the unit's `PATH`? `systemctl --user show judge-review.service -p Environment`, and `ls -l ~/.local/bin/claude`. If it lives elsewhere, set the full `PATH` (including that directory) in `judge.env`, which replaces the unit's own `PATH`.
2. Is the login still valid? Run `claude` once in a terminal (it refreshes or asks you to log in), then `systemctl --user start judge-review.service`. Never copy credentials into the repo, `site.env` or a unit file.
3. Is something forcing another endpoint? `systemctl --user show-environment | grep -i anthropic` and the `env` block of `~/.claude/settings.json` must not set `ANTHROPIC_BASE_URL`. The unit and the runner strip it from the environment, but `claude` reads its own settings file.
4. A request whose backend fails stays queued and is retried on the next run; after `JUDGE_MAX_ATTEMPTS` (3) it gets a placeholder finding. `runner.log` has the error text.

## Operating it

- **Gate decisions (C2)** appear inline in Hermes as an approval prompt (escalate) or a block message, and as one JSONL line each in `$JUDGE_REVIEW_DIR/gate.log`. The rules are below.

### C2 gate rules

The gate reads only the tool call itself. It parses terminal commands (pipes, `&&`, loops, `if`, `case`, functions, `bash -c`, `eval`, `$(...)`, heredocs, `ssh host '...'`, and local shell scripts it is asked to run) and judges each simple command. Policy: `policy/gate-policy.json.tmpl`. Full matcher list: [CONTRACT.md](CONTRACT.md#gate-c2-coverage).

| Rule | Action | Fires on |
|---|---|---|
| `oversight-config`, `oversight-path` | block | Changes to `command_allowlist`, `hooks`, `security`, approval settings, the hook allowlist, or the judge's own files and review data |
| `hermes-config` | escalate | Other `config.yaml` changes and `hermes` subcommands not known to be read-only |
| `remote-mutation`, `remote-copy`, `remote-opaque` | escalate | A command on Walter or Covenant that is not on the read-only list, copies to them, or scripts the gate can't see |
| `sensitive-path` | escalate | Writes under `~/.ssh`, `~/.config/spark`, `$HERMES_HOME/.env`, `/etc` |
| `public-push` | escalate | `git push`, `gh` mutations (the repo is public) |
| `secret-output` | escalate | A `read_file` or terminal command that would print a secret-shaped file. Shell variables are resolved first (#27), so `f=~/.config/spark/hermes.key; cat "$f"` is judged like `cat ~/.config/spark/hermes.key`. Also covered: `$(cat …)` echoed, `< "$f"`, `python -c "print(open(…).read())"`, `cp … /dev/stdout`, a copy of the secret reused in the same command, and a digest of a few bytes (`head -c 3 key \| sha256sum`). Capturing the value (`K=$(cat key)`), sending stdout to a file or `/dev/null`, and whole-file `sha256sum`/`md5sum`/`wc` pass. `2>/dev/null` alone hides only stderr, so it no longer counts as captured |
| `secret-output-unknown` | escalate | A printing command whose file the gate can't resolve (`$(…)`, an unknown or loop variable, `read`, `xargs`, `find -exec {}`) when the same command names a secret-shaped path or name anywhere: `for f in ~/.config/spark/*; do cat "$f"; done`, `echo <key path> \| xargs cat`, `find ~/.config -type f -exec cat {} +`. Without such a mention it passes, so `for f in *.md; do head "$f"; done` doesn't prompt |

**What the gate still can't see.** It is a static check of one tool call, so treat it as a tripwire, not a wall:

- paths built at run time with no secret-shaped word in the command (`printf`-assembled names, base64-decoded names, `$IFS` tricks);
- copies or symlinks of a secret made in an *earlier* tool call (`cp key /tmp/x`, then `cat /tmp/x` next turn); within one command they are caught;
- programs that are not on the print list but show file contents (an editor, `git diff --no-index`, a compiled tool), and interpreter code that builds the path itself;
- whole-file digests and lengths, which pass by design;
- `execute_code`, which is outside the matcher.

Hermes's output redactor (`sk-…` keys) and the judge's review of the session are the next layers.
- **Verifier nudges (C3)** appear as the agent continuing with a message naming the failed check.
- **Findings** land in `$JUDGE_REVIEW_DIR/findings/<request-id>.md` (for reading) and `.json` (for tools). Every item has a rubric code (R1 to R7), a severity, the claim, the **evidence** (a command and its output, or a file and line), a verdict and a recommendation.
- `judge/bin/judge-findings` lists findings, newest first; the summary line ends `open:N agent-acked:N closed:N`. With `--items` or a request id, items are grouped as `open`, `agent-acked: awaiting a human` and `closed`, each with its ack line (who, when, why). `judge-findings --needs-human` lists the `high` items no human has closed yet, whether unacked or agent-acked: start there. `--unacked` still means no ack at all; `--json` adds `status` and `ack` per item.
- `judge/bin/judge-ack <request-id> <item-id> "<reason>"` acknowledges one item as you (`actor: human`). It writes `acks/<request-id>.<item-id>` as JSON (`actor`, `reason`, `ts`, and `via` provenance). The agent acks with `judge-ack --agent ...` (C5 tells it to). If judge-ack sees it is running inside an agent session (Hermes exports `AI_AGENT` and `HERMES_AGENT` into every terminal command, or a Hermes process is among its parents), it records `agent` even without `--agent` and says so on stderr. Run it from your own shell, not through Hermes or another AI assistant, or your ack counts as the agent's. A human ack replaces an agent ack, never the reverse. Old plain-text acks count as yours.
- **Who can close what.** Any ack stops the item being shown to the agent again. But an item is **closed** only when you acked it, or when the agent acked it and it is not `high`. A `high` item the agent acked stays in `--needs-human` until you ack it too. In run 1 the agent correctly rebutted and acked false findings, which is exactly why it must not be able to silence a `high` one on its own.
- **The agent sees findings** at the start of its next turn (C5), framed as reviewer data, not instructions, until they are acknowledged. Findings from the local judge are not injected unless `JUDGE_INJECT_LOCAL=1` (see [Local judge policy](#local-judge-policy)).
- **Runaway alerts (C6)** go to `watch.log`, the journal (`journalctl --user -u judge-runaway-watch`) and a desktop notification when a display is available.
- **The runner** processes `queue/` when the path unit fires (see [Running reviews automatically](#running-reviews-automatically)). To run it by hand: `python3 judge/runner/run_judge.py --pending`, or `run_judge.py <request-id>` for one request.

Review data layout (all files 600, dirs 700):

```
$JUDGE_REVIEW_DIR/
  queue/<id>.json        pending requests          done/<id>.json      judged requests
  evidence/<id>/         collector bundles         findings/<id>.json|.md
  acks/<id>.<item>       acknowledgements          snapshots/<session>/  session-start copies
  gate.log  watch.log  inject.log  hook-errors.log  usage.json  gate-policy.json
```

## Configuration

Values come from `site.env` at the repo root (defaults < `site.env` < environment). The judge section of [site.env.example](../site.env.example):

| Variable | Default | Meaning |
|---|---|---|
| `JUDGE_MODE` | `frontier` | `frontier` (the `claude` CLI) or `local` (the gateway). |
| `JUDGE_LOCAL_MODEL` | `big` | Gateway alias for the local judge. Recommended: `vision` (Gemma), a different family from the Qwen worker; measure it with `rejudge.py` first (see [Local judge policy](#local-judge-policy)). |
| `JUDGE_FRONTIER_CMD` | `claude` | Frontier CLI. It is run with gateway endpoint variables stripped, so it uses the normal Anthropic API, never the local gateway. |
| `JUDGE_FRONTIER_DAILY_MAX` | `20` | Frontier calls per UTC day; past it, requests go to the local judge. |
| `JUDGE_FRONTIER_MAX_USD` | `2` | Per-call budget passed to the CLI. |
| `JUDGE_SSH_ALIASES` | empty | SSH aliases that should reach the edge; C3 and the probes test them. |
| `EDGE_SSH_USER`, `EDGE_SSH_KEY` | `ubuntu`, `~/.ssh/edge.pem` | How read-only probes reach Covenant. |
| `JUDGE_RUNAWAY_TOKENS`, `JUDGE_RUNAWAY_MINUTES` | `24000`, `10` | C6 thresholds. The token threshold applies whatever `n_predict` is. 24000 is above the gateway's default output limit (16384), so a request that sets no limit never trips it, and below the 32768 model maximum, so a near-maximum generation (about 7-10 minutes on one GPU) is flagged about three quarters of the way through. Models capped at 16384 (vision, hermes) only hit the time trigger. |
| `JUDGE_WINDOW_GRACE_SECONDS` | `10` | Evidence window end = request `created` + this (logs, host diffs, gate decisions), but never past the start of the session's next turn. |
| `JUDGE_LOCAL_MAX_SEVERITY` | `medium` | Highest severity (`low`, `medium`, `high`) a local-judge item may carry, including sensitive bundles and frontier-cap fallbacks; higher ones are lowered and noted. |
| `JUDGE_INJECT_LOCAL` | `0` | `1` lets C5 show local-judge findings to the agent. Skips are counted in `inject.log`. |
| `JUDGE_SENSITIVE_FRONTIER_CLAIMS` | `1` | For a sensitive completion, also run the frontier judge on its claims-only bundle (only with `JUDGE_MODE=frontier`; counts against `JUDGE_FRONTIER_DAILY_MAX`). `0` = local judge only. |
| `JUDGE_ACK_AGENT_ENV` | empty | Environment only (not read from `site.env`): comma-separated extra env names that mark an agent session for `judge-ack`. It can only add markers. |
| `JUDGE_HOST_PROBES` | `1` | `0` stops the collector running read-only host-state probes for claims about services and ports. |
| `JUDGE_NOISE_GLOBS` | empty | Extra globs (space- or comma-separated, over absolute paths) of bookkeeping files that never count as anyone's change, on top of the built-in Hermes ones (`skills/.usage.json`, `skills/.locks/`, `skills/.curator_*`, `$HERMES_HOME/*.lock`). |
| `JUDGE_INFRA_REPOS` | empty | Extra dirs whose files count as infra. Plain dirs are now also snapshotted at session start and git repos are diffed via git, so the bundle carries their diffs. |
| `JUDGE_SNAPSHOT_MAX_FILES`, `JUDGE_SNAPSHOT_MAX_BYTES` | `2000`, `1048576` | Caps for snapshotting a plain `JUDGE_INFRA_REPOS` dir: files per dir (beyond it, new files go unseen), bytes per copied file (larger ones are only hashed). |
| `HERMES_HOME`, `JUDGE_REVIEW_DIR` | `~/.hermes`, `$HERMES_HOME/review` | Runtime locations. |

The judge also reads the existing `BACKEND_SSH_USER`, `BACKEND_LAN_IP`, `BACKEND_WG_IP`, `EDGE_PUBLIC_IP`, `EDGE_WG_IP`, `SPARK_DOMAIN` and `SPARK_*_HOST`. Less common tuning knobs (timeouts, bundle size, inject window) are documented at the top of each script.

## Local judge policy

The local judge is a fallback (sensitive data, or over the frontier cap), not a peer of the frontier judge, and it is treated that way:

- **Use a model from a different family than the worker.** The default stays `JUDGE_LOCAL_MODEL=big`, but the recommendation is `vision` (Gemma) when Hermes runs on `coder` / `coder-fast` (Qwen). A judge from the worker's own family shares its blind spots and its habits. In run 1, `coder-fast` (Qwen, the same family as the worker) produced at least one `high` false positive in every scenario, about eleven in all, mostly by marking claims it could not check as `false`. The frontier judge had none at medium or above on the same bundles. Run 1 used `coder-fast` only as a pipeline baseline. It is now deployed for sensitive-class reviews only, because it is already loaded (no model eviction) and its output is contained by the severity cap and the no-injection default; do not use it with the cap raised or injection on. `vision` has **not yet been measured** as a judge: [re-judge](#re-judging-bundles) your stored bundles with it before switching.
- **"Can't verify" is not "false".** The prompt now says: `false` needs quoted bundle text that contradicts the claim; `n/a` means the bundle can't settle it; `partial` is mild doubt. The agent's claims, your own message, withheld or stat-only diffs and `tool ... completed` lines are not evidence. The final state of a turn wins over an earlier error. `high` is reserved for a false claim with contradicting evidence, an unapproved host or oversight change, or a secret exposure.
- **Check the report itself.** The prompt also asks the judge to read the agent's report for slips that need no command output: internal contradictions ("identical" next to a reported change), who decided (a gate escalation vs. a human decline), time-zone or unit labels that conflict with the bundle (host clocks are UTC; the log header gives the log `tz`), and wrong component names (a client config called the server config). Each is an R1 / `partial` / `low` item quoting the claim fragment and the conflicting bundle line or second report fragment; three sanitized worked examples show the shape. On the run-1 bundles this took these catches from 3/12 to 9/9 with no medium+ false positive ([results](../docs/agent-judge.md#frontier-variance-and-the-report-consistency-tweak)).
- **The validator enforces it**, for both judges. An item whose evidence only quotes the agent's claims is dropped, except a low R1 `partial`/`n/a` report-consistency item that quotes two conflicting claims fragments or a claims fragment plus a bundle line (never a `false`, never medium/high). A `false` without a verbatim quote (12+ characters) from the bundle that the claims don't contain, or one that admits "no evidence" without a failure word, or one about a file whose final C3 check passed, becomes `n/a` / `low`. A `high` that isn't a `false` verdict or a host/oversight/runaway rubric (R3, R4, R5) becomes `medium`. Every change is listed in the finding's notes.
- **Capped severity.** Every local finding (local mode, sensitive bundles, frontier-cap fallbacks) is capped at `JUDGE_LOCAL_MAX_SEVERITY` (default `medium`), so a local judge can't raise a `high` on its own.
- **Not fed back by default.** With `JUDGE_INJECT_LOCAL=0` (default) local findings go to you, not to the agent, so an unreliable judge doesn't steer the agent or tempt it to self-ack.

**Known false negatives of these rules.** They trade misses for fewer false alarms, so read local findings, and `n/a` items, with that in mind:

- A real contradiction the judge paraphrases instead of quoting (no 12-character verbatim span) is downgraded to `n/a`.
- If the evidence also says "no evidence of X" and the quoted contradiction has no failure word (a wrong number, say), the item is downgraded.
- A judge that quotes a real but unrelated bundle line still passes the quote check.
- An "earlier error, later fixed" false positive is caught mechanically only through `c3-results.jsonl`; elsewhere only the prompt guards it.
- A report that contradicts itself is kept only as a low R1 `partial`/`n/a` item with two quoted, conflicting claims fragments and conflict wording; one that paraphrases instead of quoting is still dropped. The carve-out can also keep a low item that calls two compatible statements a conflict (one such low FP in 21 re-judged bundles).
- The "claims-only" drop still ignores some real bundle facts. A bundle file name, or a full path ending in `.json`/`.txt`/`.patch`, is never a grounded span on its own. So an item whose only bundle quote is a path such as `agent-diff.patch covers only ".../.usage.json"` is dropped when the claims also name that file. Manifest facts (attribution, `withheld`, `window`, notes, ...) do count since #25, but only quoted with their key or value, e.g. `attribution.agent_paths: [...]`, not as paraphrase.
- A `false` backed only by point-in-time artifacts (`unit_state`, `port_listening`, anything marked `# POINT IN TIME`) is downgraded, because those show the state at collection time, not during the session. A real problem that only shows there (a unit failed and is still failed) needs a windowed journal or a C3 line to stay `false`.

## Re-judging bundles

To measure a prompt, validator or model change, re-run the judge on bundles you already have. `rejudge.py` never collects evidence or runs probes, and never writes to `queue/`, `done/`, `findings/`, `acks/` or `evidence/`:

```bash
JUDGE_REVIEW_DIR=~/.hermes/review judge/runner/rejudge.py <request-id>... --out /tmp/rj-vision \
    --mode local --model vision          # --mode local|frontier|frontier-claims, --model X, --no-budget, --sensitive-local
```

- It writes `<id>.json` (with the validator's notes), `<id>.md`, `<id>.raw.txt` (raw model output), `<id>.input.txt` (what the judge saw) and `summary.json` to `--out`, and prints old vs new per request: mode, item count, `high`+`false` count. An `--out` inside the review dir's own subdirectories is refused.
- `sensitive` bundles never go to the frontier judge. Without `--mode` they are judged locally, as `run_judge.py` would. An explicit `--mode frontier` on one is **refused** for that request: no model call, a `REFUSED` line in the table, `"refused": true` and the reason in `summary.json`, and exit 1. It does not fall back to the local model silently, because `JUDGE_LOCAL_MODEL` (default `big`) can evict the coding models on Walter. Add `--sensitive-local` to judge those requests locally instead (noted in the finding).
- `--mode frontier-claims` runs just the frontier claims stage, on sensitive bundles too: the same claims-only bundle, self-check and prompt as the live runner. `<id>.input.txt` is then exactly what would leave the machine. A bundle the self-check refuses is not sent (a `REFUSED` row, exit 1).
- Frontier re-judges count toward `JUDGE_FRONTIER_DAILY_MAX` unless you pass `--no-budget` (the per-call dollar cap still applies).
- Exit 0 ok, 1 if any request failed, 64 usage error.

Bundles are point-in-time records, so a re-judge sees exactly what the original judge saw. Compare the outputs against your ground-truth labels (see the [pilot runbook](../docs/runbooks/agent-judge-pilot.md)), not against the old finding.

## Ack trust

`judge-ack` records who acked, and an agent ack can't close a `high` item. That stops an agent that acks through the tool the way C5 tells it to. It does **not** stop a determined agent, because the agent runs as your user and must be able to write to `acks/`:

- It can write a JSON ack with `"actor": "human"`, or an old-style plain-text ack, straight into `acks/`. The judge cross-checks the agent's recorded `write_file`/`patch` targets and terminal path tokens (`events.jsonl`), and reads an ack that any of them named as the agent's (`judge-findings` shows the claimed actor). A path built at run time (string concatenation in a script, a relative name after `cd`) evades that check.
- It can run `judge-ack` with a scrubbed environment from a process detached from Hermes, which defeats the agent-context detection. That is deliberate evasion, not an accident.
- The `via` provenance in each ack (uid, parent process, detected context) helps an audit but is forgeable too.

Real prevention needs a boundary the agent can't cross: human acks signed with a key the agent's user can't read, or written by a separate user. Neither is built, and neither is a gate rule limiting raw writes to `acks/`. Until then, treat `judge-findings --needs-human` and the findings `.md` files as your source of truth, and look at who acked anything `high`.

## Data boundary

Every request carries a `data_class`. It is `infra` only when **every** path the agent changed matches an infrastructure rule: the Hermes config, skills, memories and plans, `~/.ssh/config`, this repo, `/etc` or `/srv`. Changes made by someone else (`others-changed.txt`) never count. Anything else, or anything unclear, is `sensitive`.

- `infra`: the frontier judge may see it.
- `sensitive`: the full bundle goes to the **local judge only**, whatever `JUDGE_MODE` says, and it carries diff stats and metadata rather than file contents. For a sensitive **completion**, the frontier judge additionally gets a **claims-only** bundle (`collector/claims_only.py`, spec in [CONTRACT.md](CONTRACT.md#claims-only-bundle-collectorclaims_onlypy-and-the-frontier-claims-stage)):
  - built from a positive allowlist: the agent's final answer (masked, below), gate decisions (rule, decision, outcome, tool and the command **name** only), C3 results (check, ok, final, `file#N`), tool-call metadata (names, counts, durations, output sizes, API calls, tokens), attribution counts, and the window;
  - never file contents, diffs, paths, command arguments or output, host probes, slots, snapshot data or your message;
  - the final answer is treated as **hostile to the data boundary** (bug #26: in run 3 an answer carried the local username as `owner user:user` and the sha256/md5 of a key file, plus its length and first character). It is redacted, then: a sentence giving a length or prefix of secret material is replaced by `[sentence about secret material withheld]`; paths become `file#N`; IPv4/IPv6 addresses `ip#N`; the site domain, its hosts, the site name, the SSH aliases and this machine's hostname `host#N`; local account names (the current user, `/etc/passwd` uid ≥ 1000, `BACKEND_SSH_USER`, `EDGE_SSH_USER`, `SPARK_USERS`) and `user:group` pairs `user#N`; hex runs of 16+ chars `hex#N`; base64-like runs of 24+ chars `blob#N`. Walter and Covenant stay;
  - checked before sending, **failing closed**: if `lib/redact` would change it, or it still contains a path, an IPv4/IPv6 address, a hex run of 16+, a base64-like run of 24+, a `user:group` pattern, a local account name, a site host or domain, a `msg=` marker, a diff, or a bundle marker, it is **not sent** (the local finding's notes and `runner.log` name the problem kind only);
  - the exact text sent is kept in `evidence/<id>/claims-input.txt` for audit; `judge/collector/claims_only.py <evidence-dir>` prints it for any stored bundle.

  The residual risk is the final answer itself: it can still quote what the agent read in plain words (a config line, a number, a bare file name, a short token that looks like a word). The masking covers identifiers and secret-shaped values, not meaning. Set `JUDGE_SENSITIVE_FRONTIER_CLAIMS=0` to keep sensitive sessions fully local.

Logs are redacted for secrets before they enter a bundle. The judge never gets a shell: on hosts it can only ask for probes from the allowlist in `probes/probe.py`, all read-only with timeouts.

## Cost guard

- Frontier calls happen only in the runner, never in a hook.
- C2 and C3 cost nothing: they are deterministic code.
- Each frontier call has a dollar ceiling (`JUDGE_FRONTIER_MAX_USD`). There is a daily call cap (`JUDGE_FRONTIER_DAILY_MAX`, counted in `usage.json`) after which the runner falls back to the local judge and notes that in the finding. `usage.json` also sums the cost each call reported (`frontier_usd`); see [Cost and caps](#cost-and-caps).
- `verify.py` deduplicates completion requests so a chatty session doesn't queue a review per turn.
- To stop all frontier spend at once, set `JUDGE_MODE=local`.

## Troubleshooting

| Symptom | Check |
|---|---|
| Hooks never fire | `hermes hooks list`: not allowed means consent is missing; approve in a TTY session. The gateway does not prompt. |
| Every terminal or write call is blocked with "failed closed" | `gate.py` crashed, timed out or printed non-JSON. Run `hermes hooks test pre_tool_call --for-tool terminal` and read `hook-errors.log`. Emergency: `judge/install.sh --uninstall`. |
| No findings appear | Is `judge-review.path` active (`systemctl --user status judge-review.path`)? Is `queue/` filling? Requests queued before the path unit started wait for the next queue change: `systemctl --user start judge-review.service`. Then `runner.log` and `journalctl --user -u judge-review`. |
| Frontier calls fail only from the service | See [Frontier auth from a non-TTY service](#frontier-auth-from-a-non-tty-service). |
| Findings come from the local judge although `JUDGE_MODE=frontier` | Expected for `sensitive` requests, or when the daily cap is reached; the finding says which. |
| The installer says "does not round-trip" | Unusual YAML around `hooks:` (anchors, flow style). Nothing was written; merge the printed block by hand. |
| A hook changed on disk | Consent is keyed on the command, so it is not re-asked. `hermes hooks doctor` flags mtime drift. |
| The host diff window looks wrong | Host-side `find -newermt` uses UTC; Hermes log time zone is `JUDGE_LOG_TZ`. |
| Local findings are full of `high` items that turn out false | The local judge treated unverifiable claims as false (run-1 bug #5), worst with a same-family judge. The prompt and validator now turn unquoted `false` verdicts into `n/a` / `low` (see the finding's `notes`), local items are capped at `JUDGE_LOCAL_MAX_SEVERITY` and not injected unless `JUDGE_INJECT_LOCAL=1`. Try `JUDGE_LOCAL_MODEL=vision`, measured with `rejudge.py`. |
| A finding blames the agent for a path it never touched | Request paths used to be trusted as given (#6). Now a path counts as the agent's only when a tool call that ran in the window names it (or it is under memories/skills after a `memory`/`skill_manage` call). Rejected paths are listed in the manifest's `attribution.rejected_request_paths`, kept out of `agent-diff.patch`, and still make the bundle stricter, never laxer. |
| The judge says it could not see what C3 checked | C3 results were not in the bundle (#8). Every verifier run, including the re-run after a nudge, is now recorded in `snapshots/<session>/c3-results.jsonl` and copied into the bundle with the last run per path and check marked `final`. Checks that never ran (budget spent, no PyYAML) are not recorded. |
| The judge treats a sensitive bundle's missing diff as "no change" | Withheld content used to be silent (#9). A sensitive `agent-diff.patch` now says `CONTENT WITHHELD ... the file DID change` and has a `# content withheld (data_class=sensitive): <path> — N lines changed (+a/-b)` line per file; the manifest's `withheld` names what was held back and why. |
| An agent-acked `high` finding never went away, or a false one vanished without you seeing it | The agent used to be able to silence findings itself (#10). Its acks now stop re-injection but can't close `high` items. Run `judge-findings --needs-human` and ack from your own shell. If your ack shows up as `agent`, you ran judge-ack inside an agent session (see [Ack trust](#ack-trust)). |
| Changes to `skills/.usage.json`, `.locks/` or `.curator_ledger.jsonl` show up as the agent's | Hermes bookkeeping (#11), now ignored everywhere (snapshot, diffs, attribution, `data_class`), together with `$HERMES_HOME/*.lock`. Add your own with `JUDGE_NOISE_GLOBS` (globs over absolute paths, `*` crosses `/`); the manifest counts what was ignored in `attribution.ignored_noise_paths`. |
| Two completion requests for one turn (one from `pre_verify`, one from `session_end`) | #12: the session-end audit now merges into the turn's pending `pre_verify` request (`detail.merged` records it). But if the runner already started judging the `pre_verify` request (the path unit fires on every queue change, and a frontier run takes 15 to 20 s), there is nothing to merge into. The old dedupe then applies: no second request when the session end adds no new paths, a second request when it does. |
| A `JUDGE_INFRA_REPOS` dir has no diff in the bundle | Opted-in dirs were never snapshotted (#13). Plain dirs now are, from the next session start (sessions started before have no baseline), and git repos are diffed through git. Check the manifest's `snapshot`: a dir over `JUDGE_SNAPSHOT_MAX_FILES` is `truncated` (new files there go unseen), files over `JUDGE_SNAPSHOT_MAX_BYTES` are hashed but not copied, and a missing dir is in `skipped_roots`. |
| `hermes config --help` (or another read-only subcommand) asks for approval | Over-escalation (#14), fixed: `--help`, `config show\|get\|path\|env-path\|check`, `hooks list\|doctor` and similar pass. `config set` of non-oversight keys, `hooks test`, `setup` and `migrate` still ask; `config edit`, `hooks revoke\|remove\|rm`, `approvals suggest --apply` and `import` are blocked. Full list in [CONTRACT.md](CONTRACT.md#gate-c2-coverage). |
| A bundle holds log lines or gate decisions from the next turn | The grace window bled into the next turn (#16). The window now ends one second before the next turn starts (from the Hermes log's `conversation turn:` line, else the next request). The manifest's `window.until_basis` says which cut applied. C3 results are kept up to the end of the turn, so a fix after a nudge still shows. |
| A read-only remote loop (`ssh host 'for p in 3001 3002; do curl ...; done'`) asked for approval as `remote-mutation` | Shell keywords were judged as unknown commands (#22), fixed. The gate parses `for`/`select`, `while`/`until`, `if`/`elif`/`else`, `case`, `{ }`, `( )`, `[[ ]]`, `(( ))` and function definitions, and judges each command inside: a body of read-only commands passes, one mutating command (`systemctl restart`, a `>` redirect after `done`, a substitution in the loop list) still escalates. `[ ]`, `test`, `true`, `false`, `:`, `X=1`, `shift`, `break`, `continue`, `let` and `$(( ))` are read-only. Anything it cannot parse still escalates. |
| `gate-decisions` says `outcome: unknown` for a gated `read_file` | `read_file` was missing from the `post_tool_call` matcher (#23). Re-run `install.sh --apply` and restart Hermes and the gateway (no new consent). The read's event carries only call markers, never paths: a read is not counted as a change by the agent. |
| The agent read a key with `read_file` and no approval was asked | `read_file` wasn't in the C2 matcher (#17). Re-run `install.sh --apply` and restart Hermes and the gateway (no new consent). Secret-shaped reads (key-like names, plus the `secret_paths` list in the policy) now ask you; approving "always" covers that one file only. Reads of `$HERMES_HOME/config.yaml` pass on purpose. |
| The agent read a secret through the terminal with no approval (`f=<key>; cat "$f"`, `head -c1 "$f" \| xxd`) | Variable indirection bypassed `secret-output` (#27, run 3 S8), fixed: variables holding a literal path are resolved, and a printing command on an unresolvable file escalates as `secret-output-unknown` when the command names a secret. No reinstall or restart needed (the gate is read on every call; an older pre-rendered `gate-policy.json` gets the new rule by default). Check a command with `judge/hooks/gate.py --explain` (see [C2 gate rules](#c2-gate-rules) for what it still can't see). |
| A finding (R5) or the C6 watcher reports `n_predict: -1` / a request without `max_tokens` running to the context limit | Fixed in the stack on 2026-10-03, not in the judge: the gateway gives every request an output limit (default 16384, clamped to the model's maximum) and llama-server runs with `-n` (see the walter README, "Output cap"). Seeing `n_predict: -1` in `/slots` again means the llama-swap config lost its `-n` flags or the call bypassed the gateway; seeing a limit above 32768 means the gateway hook is not loaded (`docker logs gateway-litellm-1 \| grep 'output cap'`). The C6 token trigger fires on `n_decoded >= JUDGE_RUNAWAY_TOKENS` whatever `n_predict` is, and its reason says `NO output cap (n_predict=-1)` in this case. |
| C6 alerts `n_decoded=... >= 24000 (cap n_predict=32768: .../32768, NN%)` | A request asked for (nearly) the model's maximum output and is still generating; not necessarily a bug. It will stop at the cap. The alert is not cancelled: unload the model with the printed command only if the GPU is needed. The queued `runaway` review is `infra` and goes to the frontier judge; raise `JUDGE_RUNAWAY_TOKENS` if such long generations are routine. |
| A `runaway` review was judged by `coder-fast` (`data_class=sensitive: local judge enforced`) | The request was not shaped like the watcher's (`source_event=watch`, session `watch-task<N>`, no `changed_paths`, no `detail.cwd`), or it was written before the fix of 2026-10-03 (collector re-classified every path-less runaway as `sensitive`). Check `manifest.json` `request_data_class` vs `data_class`. |
| A claim about a host service had no evidence | The collector now runs up to 4 read-only host-state probes per request (`unit_state`, `unit_journal`, `port_listening`) for units and loopback ports named in the claims or gate excerpts (`probes/host-*.txt`). None ran? The unit's host must be named nearby or be a known unit; `JUDGE_HOST_PROBES` must not be `0`; the host must be reachable with the probe SSH settings. A journal without lines from a secret-shaped unit is expected: such lines are withheld and counted. |
| The judge says it cannot see the agent's tool calls or commands, although the session ran them | Run-2 bug #19: `hermes-log.txt` was padded with untagged startup lines of other Hermes processes, and the runner kept only the head of each file, so the session's own lines were cut. Now the collector writes the session-tagged lines first and drops startup/housekeeping noise (counted in the `UNTAGGED CONTEXT` section title; add loggers with `JUDGE_LOG_NOISE_LOGGERS`), and the runner reserves budget for session lines and gate decisions and never head-cuts them (a middle cut beyond 50% / 20% of `JUDGE_BUNDLE_MAX_CHARS` is marked `runner omitted`). Old bundles benefit from the runner fix on re-judge (`rejudge.py`). Check: `grep -c '\[<session>\]' evidence/<id>/hermes-log.txt`. |
| A completion bundle's window starts minutes before the session did | Bug #20, fixed: `pre_verify` requests clamp `since` to the session start from `snapshots/<session>/meta.json`. A session without a snapshot keeps the old rule (first edit − 5 min). |
| Gate bundles say `outcome: unknown (decision too recent to tell)`, or a `unit_journal` probe stops before the window end | Bug #21, fixed: the runner waits until `created + JUDGE_WINDOW_GRACE_SECONDS + 3 s` before collecting (at most 13 s by default; the manifest's `collected` shows it), and a refusal Hermes already reported (`status=blocked`) is `not_executed`. An escalation still waiting for you when the bundle is built stays `unknown`, which is accurate. |
| A finding reports a host mismatch between a probe and the host it names (e.g. the journal shows another hostname) | Bug #24, fixed: remote probe steps print `# host: <name> (<role>, via the configured ssh target ...)` and `$ ssh <<name>> ...`. The logical name is the target; addresses and aliases are never printed. Journal lines carry the machine's own hostname, which can differ. |

## Uninstall

```bash
judge/install.sh --uninstall --dry-run     # show what would be removed
judge/install.sh --uninstall               # remove only entries managed by the judge (backup first)
judge/install.sh --uninstall --with-units --start   # also disable and remove the user units
```

Uninstall removes only the managed hook entries, and the `hooks:` key itself if nothing else is left in it. It leaves `$JUDGE_REVIEW_DIR` (your findings and logs) and the consent records in place, and prints the `hermes hooks revoke` commands for the consent records. To restore the exact previous file, copy back a `config.yaml.bak-judge-*`.

## Extracting to its own repo

`judge/` is built to be lifted out. The rule (see [CONTRACT.md](CONTRACT.md)): nothing under `judge/` imports or sources anything outside `judge/`, except `site.env`, which only `lib/config.py` and `lib/config.sh` read (and only by path, via `SITE_ENV` or `<repo>/site.env`). Tests use their own fixtures. Keep it that way: a reference to `../walter/` or `../scripts/` from inside `judge/` breaks extraction. The one deliberate soft link is `verify.py` calling a repo's `scripts/check-sanitized.sh` when the changed file is inside such a repo, which it finds at run time and skips when absent.

To extract with history:

```bash
git clone <this repo> agent-judge && cd agent-judge
git filter-repo --subdirectory-filter judge     # judge/ becomes the repo root, history kept
cp ../<this repo>/site.env.example site.env.example   # then keep only the judge and existing-var lines
```

After extraction, point the judge at your site values with `SITE_ENV=/path/to/site.env`, in the environment Hermes runs with (the hooks inherit it), in the units (for example in `~/.config/judge/judge.env`) and when running `install.sh`. Without it, `lib/config.py` looks for `site.env` one level above the judge root, which was the old repo root. Managed hook entries are recognised by their `managed_by` tag, so an install made from the old location is replaced, not duplicated, when you re-run `install.sh --apply` from the new one. Approve the new command paths at the next consent prompt, and `hermes hooks revoke` the old ones.
