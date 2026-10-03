# judge/ — interface contract

Component that lets a frontier (or local) model judge a local agent (Hermes) from evidence.
Design rationale: `docs/agent-judge.md`. This file is the **binding contract** between the parts; change it
deliberately and update every part that depends on it.

## Ground rules
- **Self-contained.** Nothing in `judge/` imports or sources files outside `judge/` except `site.env`
  (read via `judge/lib/config.py` / `judge/lib/config.sh`). This keeps it extractable into its own repo.
- **Python 3.10+ stdlib only** for runtime code (Hermes' venv is NOT assumed). Tests use pytest.
  Bash allowed for thin wrappers; must pass `bash -n` (shellcheck if available).
- **Public repo**: follow `/CONVENTIONS.md`; no real hosts/IPs/names/secrets; run `scripts/check-sanitized.sh`.
- **Never touch the live Hermes profile in tests or development.** Use a temp `HERMES_HOME` and a temp
  `JUDGE_REVIEW_DIR`. Live install is a separate, human-approved step (`judge/install.sh --apply`).
- **Read-only toward hosts.** Nothing in judge/ changes Walter or Covenant. Probes are read-only by construction.

## Layout
```
judge/
  CONTRACT.md            this file
  README.md              overview, install, operate, uninstall (docs agent)
  install.sh             renders hook config; --dry-run default, --apply merges into $HERMES_HOME/config.yaml
  lib/config.py|sh       load site.env + judge defaults (shared helper; owned by the collector agent)
  lib/queue.py           atomic read/write of requests/findings/acks + schema validation (collector agent)
  schema/request.schema.json  schema/finding.schema.json   (collector agent)
  hooks/                 Hermes shell-hook entrypoints (stdin JSON → stdout JSON)
    gate.py              C2 pre_tool_call  (gate agent)
    verify.py            C3 pre_verify     (verify agent)
    enqueue.py           C1 post_tool_call on plans + C4 on_session_end → write request (collector agent)
    inject.py            C5 pre_llm_call   (runner agent)
  policy/gate-policy.json.tmpl   C2 rules (JSON: stdlib has no YAML), host patterns as ${VARS} (gate agent)
  collector/collect.py   build evidence bundle for a request (collector agent)
  probes/                read-only probe executables + probe.py dispatcher/allowlist (collector agent)
  runner/                judge prompt, run-judge (frontier|local), findings validator, systemd user units (runner agent)
  watch/runaway.py       C6 llama-server slot watcher (verify agent)
  tests/                 pytest; one test module per part; fixtures under tests/fixtures/
```

## Runtime directories (env-overridable)
- `HERMES_HOME` (default `~/.hermes`)
- `JUDGE_REVIEW_DIR` (default `$HERMES_HOME/review`):
```
queue/<request-id>.json          pending review requests
evidence/<request-id>/           collector output (see bundle below)
findings/<request-id>.json       judge output (validated)
findings/<request-id>.md         human-readable rendering of the same
acks/<request-id>.<item-id>      empty or one-line reason; presence = acknowledged
done/<request-id>.json           request moved here after findings are written
snapshots/<session-id>/          watched-path snapshots taken at session start (collector)
gate.log                         JSONL, one line per C2 decision
watch.log                        JSONL, one line per C6 alert
```
Writes are atomic (write tmp + rename). Files are mode 600, dirs 700.

## IDs
`<request-id>` = `<UTC yyyymmddThhmmssZ>-<session-id short 6>-<kind>`, e.g. `20261003T035210Z-fdc8ec-completion`.
"short 6" = the last 6 alphanumeric characters of the session id (`lib/queue.session_short`). A request with an
empty `session` is global (not tied to one Hermes session); its short id is `nosess`.
If an id is already taken (queue/ or done/), `lib/queue.new_request_id` bumps the timestamp by one second.
Completion dedupe (`lib/queue.is_duplicate`, used by verify.py and enqueue.py): a completion request is not written
when a completion request of the same session exists with `created` within `JUDGE_COMPLETION_DEDUPE_SECONDS`
(default 900) and a superset of its `changed_paths` (empty is a subset).

## Request (schema/request.schema.json)
```json
{"id": "...", "kind": "plan|gate|completion|runaway",
 "session": "20261002_165907_fdc8ec", "created": "2026-10-03T03:52:10Z",
 "since": "2026-10-03T03:20:00Z",                // start of window for diffs (UTC)
 "changed_paths": ["..."], "claims": "<agent final response or plan excerpt, ≤4000 chars>",
 "plan": "<path or null>", "data_class": "infra|sensitive",
 "source_event": "post_tool_call|pre_tool_call|pre_verify|on_session_end|watch", "detail": {}}
```
`data_class` defaults to **sensitive** unless every changed path matches an infra path rule
(rules in `lib/config.py`: hermes config/skills/memories/plans, ~/.ssh/config, the deck repo, /etc, /srv).

## Finding (schema/finding.schema.json)
```json
{"request": "<request-id>", "judge": "<model id>", "created": "...Z", "mode": "frontier|local",
 "items": [{"id": "F1", "rubric": "R1|R2|R3|R4|R5|R6|R7", "severity": "high|medium|low",
            "claim": "...", "evidence": "<command + output excerpt, or file:line>",
            "verdict": "true|false|partial|n/a", "recommendation": "..."}]}
```
Optional top-level `"notes": ["..."]` (runner bookkeeping, e.g. "sensitive -> local enforced",
"frontier cap reached", "validator dropped N items"). Validator drops items with empty `evidence`. Rubric codes as in docs/agent-judge.md.

## Evidence bundle (`evidence/<request-id>/`)
`manifest.json` (request copy + list of artifacts + collector version + data_class),
`hermes-log.txt` (session-window lines from agent.log/errors.log, secrets redacted),
`local-diff.patch` (watched paths vs snapshot), `host-<name>.txt` (UTC `find -newermt` + failed units per host),
`slots.json` (llama-server slots summary: `{"<model>": [slot, ...], "_collected": "...Z", "_error": "..."}`;
`_`-prefixed keys are string metadata), `probes/<probe>-<n>.txt` (stdout+stderr+exit code; written by
`collect.py <id> --probe <name> [args]`), `probes/judge-<probe>-<n>.txt` (probes the judge requested, saved by the runner).
For `data_class=sensitive` (request's class, or re-classification of `changed_paths`; the stricter wins): diffs
replaced by `git diff --stat`-style summaries; no file contents.

CLI: `judge/collector/collect.py <request-id>` reads the request from queue/ (or done/), writes the bundle and
`manifest.json` last, prints the evidence dir, exits 0. Exit 2 = bad id / request not found. Unreachable hosts are
recorded in `host-<name>.txt` (`UNREACHABLE: ...`), never fatal.
Snapshots (`snapshots/<session>/`, written by `hooks/enqueue.py` on `on_session_start`): `meta.json`,
`index.json` (`{abs_path: {"sha256", "size"}}`, plus `"skipped"` when a file was not copied), `files/<abs path>`
copies, `events.jsonl` (tool calls seen by post_tool_call).

## Probes (`judge/probes/probe.py <name> [args...]`)
Allowlisted names with per-arg regex validation; each runs read-only with a timeout (default 20s):
`ssh_alias_test <alias>`, `port_listening <host:walter|covenant> <port>`, `http_status <https-url-on-SPARK_DOMAIN>`,
`unit_state <host> <unit>`, `file_hash <host> <abs-path>`, `render_and_diff <repo-template-path>`,
`check_sanitized <repo-path>`, `slots`. Unknown name or bad arg → exit 64, no execution.

## Hermes shell-hook wire format (from Hermes docs; verify against ~/.hermes/hermes-agent source)
stdin: `{"hook_event_name", "tool_name", "tool_input", "session_id", "cwd", "profile", "extra"}`.
stdout: `pre_tool_call` → `{"action":"block","message":...}` | `{"action":"approve","message":...,"rule_key":...}` | `{}`;
`pre_verify` → `{"action":"continue","message":...}` | `{}` (gate on `extra.attempt`);
`pre_llm_call` → `{"context": "..."}` | `{}`. Exit code 2 = block (pre_tool_call). Hooks must never crash: wrap,
log to `$JUDGE_REVIEW_DIR/hook-errors.log`, and emit `{}` — EXCEPT gate.py which is installed `fail_closed: true`.

## site.env variables (add to site.env.example under `# --- judge`)
`JUDGE_MODE=frontier` (frontier|local), `JUDGE_LOCAL_MODEL=big`, `JUDGE_FRONTIER_CMD=claude`,
`JUDGE_SSH_ALIASES="edge-alias"` (ssh aliases that reach the edge), `EDGE_SSH_USER=ubuntu`,
`EDGE_SSH_KEY=~/.ssh/edge.pem`, `JUDGE_RUNAWAY_TOKENS=20000`, `JUDGE_RUNAWAY_MINUTES=10`.
Existing vars used: `BACKEND_SSH_USER`, `BACKEND_LAN_IP`, `BACKEND_WG_IP`, `EDGE_PUBLIC_IP`, `EDGE_WG_IP`,
`SPARK_DOMAIN`, `SPARK_*_HOST`, `SPARK_API_HOST`.
