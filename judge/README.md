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
  risky tool call ---------- C2 pre_tool_call   terminal|write_file|patch           |
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
                                                   host find in UTC, slots, probes; window ends
                                                   at request.created + grace)
                       2. run_judge.py          -> frontier `claude -p` (infra data only)
                                                   or local `big` (sensitive data, or over the cap)
                       3. validate.py           -> findings/<id>.json + .md   (items without
                                                   evidence are dropped), request -> done/
```

| # | Hermes event | Script | Sync | Effect |
|---|---|---|---|---|
| C1 | `post_tool_call`, matcher `write_file\|patch` | `hooks/enqueue.py` | async | Plan edits under `.hermes/plans/` queue a **plan review**. |
| C2 | `pre_tool_call`, matcher `terminal\|write_file\|patch`, `fail_closed: true` | `hooks/gate.py` | sync, ms | Deterministic policy: escalate host-mutating commands and sensitive-path writes to you, block edits to allowlists, hooks and approval settings. |
| C3 | `pre_verify` | `hooks/verify.py` | sync, under 60 s | Fast verifiers on the changed paths; nudges the agent once if one fails. |
| C4 | `on_session_end` (and `on_session_start` for the snapshot) | `hooks/enqueue.py` | async | Snapshot watched paths at start; queue a **completion audit** at the end. |
| C5 | `pre_llm_call` | `hooks/inject.py` | sync, fast | Show unacknowledged findings to the agent as reviewer data. |
| C6 | none (telemetry) | `watch/runaway.py` | async | Alert on a runaway generation (huge `n_decoded`, no `n_predict` cap, or a very long call). |

## Components

| Path | What it is |
|---|---|
| `install.sh` | Merges the hooks block into `$HERMES_HOME/config.yaml`, creates the review dir, renders the gate policy, optionally installs the systemd user units. Dry run by default. |
| `hooks/` | Hermes shell-hook entrypoints: JSON on stdin, JSON on stdout. All but `gate.py` fail open and log to `hook-errors.log`. |
| `policy/gate-policy.json.tmpl` | C2 rules. Host patterns are `${VARS}` from `site.env`; the installer renders it to `$JUDGE_REVIEW_DIR/gate-policy.json`. |
| `collector/collect.py` | Builds the evidence bundle for one request. |
| `probes/probe.py` | The only way the judge touches hosts: an allowlist of read-only probes with per-argument validation. |
| `runner/` | Judge prompt, `run_judge.py` (frontier or local), the findings validator, and the systemd user units. |
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
  - matcher: terminal|write_file|patch
    command: /usr/bin/python3 /path/to/repo/judge/hooks/gate.py
    timeout: 10
    fail_closed: true
    managed_by: agent-judge
  post_tool_call:
  - matcher: write_file|patch|terminal
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

## Operating it

- **Gate decisions (C2)** appear inline in Hermes as an approval prompt (escalate) or a block message, and as one JSONL line each in `$JUDGE_REVIEW_DIR/gate.log`.
- **Verifier nudges (C3)** appear as the agent continuing with a message naming the failed check.
- **Findings** land in `$JUDGE_REVIEW_DIR/findings/<request-id>.md` (for reading) and `.json` (for tools). Every item has a rubric code (R1 to R7), a severity, the claim, the **evidence** (a command and its output, or a file and line), a verdict and a recommendation.
- `judge/bin/judge-findings` lists unacknowledged findings, newest first.
- `judge/bin/judge-ack <request-id> <item-id> "<reason>"` acknowledges one item. It writes `acks/<request-id>.<item-id>`. The agent may ack too (C5 tells it how), and every ack is a file you can audit.
- **The agent sees findings** at the start of its next turn (C5), framed as reviewer data, not instructions, until they are acknowledged.
- **Runaway alerts (C6)** go to `watch.log`, the journal (`journalctl --user -u judge-runaway-watch`) and a desktop notification when a display is available.
- **The runner** processes `queue/` when the path unit fires. To run it by hand: `python3 judge/runner/run_judge.py --pending`, or `run_judge.py <request-id>` for one request.

Review data layout (all files 600, dirs 700):

```
$JUDGE_REVIEW_DIR/
  queue/<id>.json        pending requests          done/<id>.json      judged requests
  evidence/<id>/         collector bundles         findings/<id>.json|.md
  acks/<id>.<item>       acknowledgements          snapshots/<session>/  session-start copies
  gate.log  watch.log  hook-errors.log  usage.json  gate-policy.json
```

## Configuration

Values come from `site.env` at the repo root (defaults < `site.env` < environment). The judge section of [site.env.example](../site.env.example):

| Variable | Default | Meaning |
|---|---|---|
| `JUDGE_MODE` | `frontier` | `frontier` (the `claude` CLI) or `local` (the gateway). |
| `JUDGE_LOCAL_MODEL` | `big` | Gateway alias for the local judge. `vision` (Gemma) is the cross-family option when the worker is Qwen. |
| `JUDGE_FRONTIER_CMD` | `claude` | Frontier CLI. It is run with gateway endpoint variables stripped, so it uses the normal Anthropic API, never the local gateway. |
| `JUDGE_FRONTIER_DAILY_MAX` | `20` | Frontier calls per UTC day; past it, requests go to the local judge. |
| `JUDGE_FRONTIER_MAX_USD` | `2` | Per-call budget passed to the CLI. |
| `JUDGE_SSH_ALIASES` | empty | SSH aliases that should reach the edge; C3 and the probes test them. |
| `EDGE_SSH_USER`, `EDGE_SSH_KEY` | `ubuntu`, `~/.ssh/edge.pem` | How read-only probes reach Covenant. |
| `JUDGE_RUNAWAY_TOKENS`, `JUDGE_RUNAWAY_MINUTES` | `20000`, `10` | C6 thresholds. |
| `JUDGE_WINDOW_GRACE_SECONDS` | `10` | Evidence window end = request `created` + this (logs, host diffs, gate decisions). |
| `HERMES_HOME`, `JUDGE_REVIEW_DIR` | `~/.hermes`, `$HERMES_HOME/review` | Runtime locations. |

The judge also reads the existing `BACKEND_SSH_USER`, `BACKEND_LAN_IP`, `BACKEND_WG_IP`, `EDGE_PUBLIC_IP`, `EDGE_WG_IP`, `SPARK_DOMAIN` and `SPARK_*_HOST`. Less common tuning knobs (timeouts, bundle size, inject window) are documented at the top of each script.

## Data boundary

Every request carries a `data_class`. It is `infra` only when **every** path the agent changed matches an infrastructure rule: the Hermes config, skills, memories and plans, `~/.ssh/config`, this repo, `/etc` or `/srv`. Changes made by someone else (`others-changed.txt`) never count. Anything else, or anything unclear, is `sensitive`.

- `infra`: the frontier judge may see it.
- `sensitive`: **local judge only**, whatever `JUDGE_MODE` says, and the bundle carries diff stats and metadata rather than file contents.

Logs are redacted for secrets before they enter a bundle. The judge never gets a shell: on hosts it can only ask for probes from the allowlist in `probes/probe.py`, all read-only with timeouts.

## Cost guard

- Frontier calls happen only in the runner, never in a hook.
- C2 and C3 cost nothing: they are deterministic code.
- Each frontier call has a dollar ceiling (`JUDGE_FRONTIER_MAX_USD`). There is a daily call cap (`JUDGE_FRONTIER_DAILY_MAX`, counted in `usage.json`) after which the runner falls back to the local judge and notes that in the finding.
- `verify.py` deduplicates completion requests so a chatty session doesn't queue a review per turn.
- To stop all frontier spend at once, set `JUDGE_MODE=local`.

## Troubleshooting

| Symptom | Check |
|---|---|
| Hooks never fire | `hermes hooks list`: not allowed means consent is missing; approve in a TTY session. The gateway does not prompt. |
| Every terminal or write call is blocked with "failed closed" | `gate.py` crashed, timed out or printed non-JSON. Run `hermes hooks test pre_tool_call --for-tool terminal` and read `hook-errors.log`. Emergency: `judge/install.sh --uninstall`. |
| No findings appear | Is the runner unit active (`systemctl --user status`)? Is `queue/` filling? Look at `runner.log` in the review dir, then run `run_judge.py --pending` by hand. |
| Findings come from the local judge although `JUDGE_MODE=frontier` | Expected for `sensitive` requests, or when the daily cap is reached; the finding says which. |
| The installer says "does not round-trip" | Unusual YAML around `hooks:` (anchors, flow style). Nothing was written; merge the printed block by hand. |
| A hook changed on disk | Consent is keyed on the command, so it is not re-asked. `hermes hooks doctor` flags mtime drift. |
| The host diff window looks wrong | Host-side `find -newermt` uses UTC; Hermes log time zone is `JUDGE_LOG_TZ`. |

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
