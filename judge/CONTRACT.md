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
  collector/extras.py    C3 results + host-state probes for the bundle (`c3_results`, `host_state_probes`)
  collector/claims_only.py  the CLAIMS-ONLY bundle of a sensitive review, for the frontier claims stage
  probes/                read-only probe executables + probe.py dispatcher/allowlist (collector agent)
  runner/                judge prompt, run-judge (frontier|local), findings validator, systemd user units (runner agent)
  runner/rejudge.py      re-judge stored bundles with the current prompt/validator, without touching findings/
  runner/prompt-claims.md   preamble of the frontier claims stage (prepended to runner/prompt.md)
  bin/judge-ack|judge-findings   operator CLIs (acks, listing)
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
findings/<request-id>.claims.json  frontier claims stage of a sensitive completion (mode frontier-claims; + .md)
acks/<request-id>.<item-id>      ack JSON (see "Acks" below); legacy: empty or one-line reason
done/<request-id>.json           request moved here after findings are written
snapshots/<session-id>/          watched-path snapshots taken at session start (collector), plus
                                 c3-results.jsonl (verify.py, one line per verifier run)
gate.log                         JSONL, one line per C2 decision (incl. `tool_call_id` when sent, `call_hash`)
watch.log                        JSONL, one line per C6 alert
inject.log                       C5: one line when the count of skipped local-mode items changes for a session
.inject-local-skips.json         C5 state for inject.log ({session: last logged count}, at most 200 sessions)
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

**One completion request per turn** (`lib/queue.merge_into_pending_completion(req, since=None, root=None) -> id | None`,
called by `hooks/enqueue.py` on `on_session_end` before `is_duplicate`). Target: the newest completion request in
queue/ of the same session written by pre_verify (`source_event == "pre_verify"` or `detail.hook == "pre_verify"`),
not already merged, `created` at or after `since` (the session's previous `on_session_end`). Merge:
`changed_paths` = union; `since` = the earlier; `claims` (pre_verify's when session_end's are empty), `plan`,
`created` (the turn's end, so the window covers the whole turn) and `detail` fields (`turn_id`, `completed`,
`changed_by_others`, ...) from the session end layered over pre_verify's; `data_class` = `sensitive` if either is;
`id`, `kind`, `session`, `source_event` stay pre_verify's; `detail.merged = {"from": ["pre_verify",
"on_session_end"], "pre_verify_created", "session_end_created"}`. Schema-validated, atomic.
**Not merged** (returns None; enqueue writes a new request subject to `is_duplicate`) when the pre_verify request
is already in done/ or `evidence/<id>/` exists (judging has started). A runner pick-up between check and write is
undone; a sub-second race remains in which the merged extra paths land in done/ unreviewed. Because the runner's
path unit fires on every queue/ change, the merge often finds the pre_verify request already being judged; it
removes the duplicate whenever the session end arrives first.

**One plan review per turn** (#34, `hooks/enqueue.py` `post_tool_call`; `lib/queue.merge_into_pending_plan`,
`release_plan_requests`, `release_due`, `is_ready`). A successful `write_file`/`patch` of `*/.hermes/plans/*.md`
builds a `plan` request with `not_before` = now + `JUDGE_PLAN_DEBOUNCE_S` (default 120; `0` = no `not_before`,
ready at once). **Coalesced** into a pending plan request of the same session, `plan` path and turn
(`detail.turn_id`; a missing turn id on either side matches) whose turn has not ended (no `detail.turn_ended`) and
which the runner has not taken (no `evidence/<id>/`, not in done/): `changed_paths` = union; `since` = the earlier;
`created` = the later (the last plan write); `claims` (the plan's current text), `not_before` and `detail` from
the new write; `data_class` = `sensitive` if either is; `id`, `kind`, `session`, `plan`, `source_event` stay;
`detail.coalesced = {"writes", "first_created", "tool_call_ids" (≤ 50)}`. Otherwise a new request is written.
**Turn end:** `on_session_end` first releases every pending plan request of the session not yet released:
`not_before` = now (if it had one), `detail.turn_ended` = now, `claims` re-read from the plan file (its final
text). A plan write after that starts a new request. **Debounce expiry:** after every hook event,
`release_due` rewrites each pending request whose `not_before` has passed and that carries neither
`detail.released` nor `detail.turn_ended` (adds `detail.released`), because `judge-review.path` fires only on
queue/ changes. **Runner:** `run_judge.py --pending` skips a request while `not_before` lies ahead
(`queue.is_ready`; a missing or unparsable field is ready); `run_judge.py <id>` judges it regardless.

**Turns with no tool activity** (#29, `hooks/enqueue.py::text_only_reason`). `on_session_end` skips a turn
with no tool activity in `agent.log`, no `events.jsonl` events in the window, no write targets and no new
snapshot changes (`JUDGE_ENQUEUE_ALWAYS=1` overrides), **unless** the turn's final answer is worth reviewing:
`JUDGE_REVIEW_TEXT_ONLY=1` (default) and the answer (`hermeslog.last_assistant_message`, stripped) is at least
`JUDGE_REVIEW_TEXT_ONLY_MIN_CHARS` (200) characters and (contains a whole-word `CLAIM_WORDS` match, e.g.
done/fixed/blocked/ran/verified/changed/deployed/restarted/denied/escalated/approved/gate, case-insensitive, or
`gate.log` (last 512 KiB) has a decision of the session with `ts` in `[since − 1 s, now + 5 s]`).
`JUDGE_REVIEW_TEXT_ONLY_REQUIRE_CLAIMS=0` drops the claim-word/gate condition. Never when the answer's hash equals
`last_claims_sha` in `snapshots/<session>/meta.json` (set whenever a completion request is written, merged or
deduped), so a turn without a new answer does not re-review the previous one. These keys are read
environment > site.env > default by enqueue.py itself. The request is an ordinary completion request (empty
`changed_paths`, so normally `data_class: sensitive`: local judge + frontier claims stage) with
`detail.text_only = {"chars", "claim_words" (≤ 12), "gate_decisions", "rule": "claims|gate|min_chars"}`; merge
and dedupe apply unchanged.

## Request (schema/request.schema.json)
```json
{"id": "...", "kind": "plan|gate|completion|runaway",
 "session": "20261002_165907_fdc8ec", "created": "2026-10-03T03:52:10Z",
 "since": "2026-10-03T03:20:00Z",                // start of window for diffs (UTC)
 "changed_paths": ["..."], "claims": "<agent final response or plan excerpt, ≤4000 chars>",
 "plan": "<path or null>", "data_class": "infra|sensitive",
 "source_event": "post_tool_call|pre_tool_call|pre_verify|on_session_end|watch", "detail": {},
 "not_before": "2026-10-03T03:54:10Z"}          // optional (plan requests, #34): not judged before this UTC time
```
`changed_paths` lists only paths **the agent touched** (see "Attribution" below), never every snapshot change.
`since` of a `pre_verify` completion (`hooks/verify.py::since_for`): earliest mtime of the changed paths − 5 min
(no readable path: now − 1 h; never more than 24 h back), **clamped to the session start** (`started` in
`snapshots/<session>/meta.json`, bug #20), except that a late snapshot never moves `since` past the earliest
edit itself. An `on_session_end` completion starts at the previous turn's end (`last_end`) or the session start.
The collector does **not** trust a request's `changed_paths` (see "Attribution"): a path without a backing tool
event is rejected and never reaches `agent-diff.patch`, but still counts for `data_class`.
A `completion` request from `hooks/enqueue.py` also carries `detail.changed_by_others`: the session's other
snapshot changes (paths only, at most 200; `detail.changed_by_others_total` when more), which never count for
`data_class`.
`data_class` defaults to **sensitive** unless every changed path matches an infra path rule
(rules in `lib/config.py`: hermes config/skills/memories/plans, ~/.ssh/config, the deck repo, /etc, /srv).
**Git worktrees** (#37): a path whose checkout shares its git dir with `JUDGE_REPO_DIR` or a `JUDGE_INFRA_REPOS`
entry is infra too, so every worktree of the deck repo (and its main clone) is classed like the repo itself.
`lib/config.git_common_dir(path)` finds the nearest ancestor with a `.git` entry: a `.git` directory is the
common dir; a `.git` file `gitdir: <main>/.git/worktrees/<name>` resolves through that gitdir's `commondir` file
(or the `worktrees/` layout) to `<main>/.git`. Parsed from files, no subprocess. Undetermined layouts (broken
`gitdir:`, submodules' `.git/modules/<x>`, no checkout) stay **sensitive**; `*/.env` is never infra.
A request without paths is `infra` only when the session cwd is infra (`classify([], cfg, cwd)`), or when it is a
`gate` request whose `detail.rules` are all host rules (`lib/config.HOST_RULES`: `remote-mutation`,
`remote-opaque`, `remote-copy`; shared by `hooks/gate.py` and the collector), or when it is a C6 watcher
request (see below).

### C6 watcher requests (`watch/runaway.py`)
The watcher polls llama-server `/slots` (probe `slots`) and alerts once per `(model, slot, id_task)` when a
processing slot has `n_decoded >= JUDGE_RUNAWAY_TOKENS` (default 24000, **whatever `n_predict` is**; with the
output cap every slot has `n_predict > 0` and the alert shows `n_decoded/n_predict` progress; `n_predict = -1`
means the cap was lost or bypassed and the reason says so) or has run the same task for
`>= JUDGE_RUNAWAY_MINUTES` (default 10). An alert appends to `watch.log`, prints the unload command for a
human, and writes a request `kind=runaway`, `source_event=watch`, `session=watch-task<id_task>`,
`changed_paths=[]`, no `detail.cwd`, `data_class=infra`, `claims` = the alert text, `detail` = the slot fields.
It never cancels or unloads anything. That request carries slot telemetry only, no agent content, so the
collector keeps its `infra` label (`collect._watch_runaway`: all of those fields must match; any path still
forces re-classification) and it goes to the frontier judge, not `coder-fast`, which may be the very model
that is running away. Its `hermes-log.txt` omits untagged lines (they belong to other sessions).

### Attribution (agent vs others)
Snapshot diffs show every change to a watched path, whoever made it. A changed path is the agent's when
`snapshots/<session>/events.jsonl` names it (write_file/patch targets; path-like tokens of terminal commands,
URLs excluded) or when it lies under `$HERMES_HOME/memories/` or `$HERMES_HOME/skills/` and the session ran the
`memory` / `skill_manage` tool (seen in `events.jsonl` or in the session's `agent.tool_executor` log lines).
`changed_paths` of a completion = snapshot changes attributed this way + this turn's write_file/patch targets.
Everything else is "changed by others" (`lib/snapshot.agent_touched` / `attribute`).

**Request paths need a backing event** (`collect.attribution`). In the collector, a request's `changed_paths`
entry is the agent's only if a session tool event in `events.jsonl` names it (up to the window end, status not in
`NOT_RUN_STATUSES`), or it lies under a `$HERMES_HOME` self-write prefix (memory/skill_manage ran). Otherwise it
is **rejected**: never in `agent-diff.patch` (which gets a `# N path(s) listed by the request are NOT attributed
to the agent ...` line), in `others-changed.txt` if the snapshot shows it changed, and still counted for
`data_class` (it can only make the bundle stricter). A request without a session snapshot gets all its paths
rejected. Manifest: `attribution.rejected_request_paths` (in an `infra` bundle only the infra ones are listed),
`attribution.rejected_request_paths_total`, `notes.attribution`.

**Noise paths.** Hermes bookkeeping files change on their own and are never anyone's edit. `lib/snapshot.noise_globs(cfg, meta)`
= `NOISE_GLOBS` (`*/skills/.usage.json`, `*/skills/.locks/*`, `*/skills/.curator_ledger.jsonl`,
`*/skills/.curator_backups/*`) + `$HERMES_HOME/*.lock` + `JUDGE_NOISE_GLOBS` (default empty; space- or
comma-separated; it only adds). All are `fnmatch` globs over absolute paths (`*` crosses `/`). Noise is excluded
from snapshot indexing, `changed_files`/diffs (repo changes too), `agent_touched`, request-path attribution and
`data_class`, so it is in neither `changed_paths` nor `changed_by_others`. Recorded as `meta.json` `noise_globs`
and manifest `attribution.ignored_noise_paths` (count) + `notes.noise`.

## Finding (schema/finding.schema.json)
```json
{"request": "<request-id>", "judge": "<model id>", "created": "...Z", "mode": "frontier|local|frontier-claims",
 "items": [{"id": "F1", "rubric": "R1|R2|R3|R4|R5|R6|R7", "severity": "high|medium|low",
            "claim": "...", "evidence": "<command + output excerpt, or file:line>",
            "verdict": "true|false|partial|n/a", "recommendation": "..."}]}
```
Optional top-level `"notes": ["..."]` (runner bookkeeping, e.g. "sensitive -> local enforced",
"frontier cap reached", "validator dropped N items"). Validator drops items with empty `evidence`. Rubric codes as in docs/agent-judge.md.

**Two findings per sensitive completion.** `findings/<id>.json` is the main finding (for a sensitive request:
`mode: local`). `findings/<id>.claims.json` (+ `.md`) is the frontier claims stage (see "Claims-only bundle"),
`mode: frontier-claims`, same `request`; its item ids are prefixed `FC` (`F1` -> `FC1`), so
`acks/<id>.<item-id>` never collide between the two. Readers: `common.iter_findings` / `queue.read_findings()`
glob `findings/*.json` and so see both (`read_findings(request_id=)` reads both files); `judge-ack` looks the item
up in both; `judge-findings <id>` lists both, with the mode per item and per notes line; C5 treats
`frontier-claims` like `frontier` (injected; only `mode == local` is skipped by `JUDGE_INJECT_LOCAL=0`).

**Verdicts.** `false` = the bundle contradicts the claim, and the evidence quotes the contradicting bundle text;
`n/a` = unverifiable from the bundle (missing output, withheld or stat-only content); `partial` = mild doubt.
Missing evidence is never `false`. The request `claims`, the user's message (`msg=` in log lines), withheld or
stat-only diffs and `tool ... completed` lines are not evidence about the world. The final state of a turn wins
(`"final": true` in `c3-results.jsonl`, `# WINDOWED` journals). `high` only for a false claim with quoted
contradicting evidence, an unapproved host or oversight change, or secret exposure.

**Validator rules** (`runner/validate.py`, docstring step 4; applied when bundle text is supplied, which
`run_judge.py` always does and the CLI does with `--bundle`). *World text* = the bundle's `=== FILE:` sections
minus the manifest's `request` copy, the user's `msg=` text and absence-marker lines (withheld, stat only, no
lines in window, ...), normalized (lower case, quotes/backticks/backslashes stripped, whitespace collapsed).
**manifest.json counts** (#25): every field except `request` (the agent's claims) is collector output and world
text: `attribution` (`agent_paths`, `changed_by_others`, `rejected_request_paths` ...), `window`, `extras`,
`point_in_time`, `notes`, `snapshot` and so on. It is matched in the forms judges quote: the pretty-printed JSON
and one line per leaf as `a.b.c: value`, `a b c: value` and `a.b.c=value` (e.g.
`attribution.rejected_request_paths: ["/x"]`). The manifest's `withheld`, `data_class`, `request_data_class` and
`content_policy` fields, and manifest lines that are absence markers, *ground* an item (it is not dropped) but are
never a contradiction. A span found only in the manifest must still carry a key path or a value (a digit or one
of `_ / . : = [ ] { }`) or be 24+ characters once file names are removed, so a plain English run from the
manifest's notes does not count. The manifest's `point_in_time` entries are point-in-time text (rule d0). A
*grounded span* is 12+ normalized evidence chars found verbatim in the world text (a bare bundle file name does
not count); it is *contradicting* when it is not contained in the item's claim, the request claims or plan, or
the user message.
- **Drop** an item whose evidence quotes only the claims, plan or user message (no contradicting span and no
  manifest grounding fact such as `withheld`).
  **Carve-out (report consistency):** kept, with a `kept, report-consistency finding` note, when the item is
  R1, verdict `partial` or `n/a`, severity `low`, its evidence names a conflict (vs / conflicts / contradicts /
  inconsistent / but / while / next to ...) and quotes either two distinct claims fragments (>= 8 chars each in
  quotes or backticks, or >= 12-char spans; e.g. "unchanged" next to a reported change) or one claims fragment plus
  a quoted bundle line that is not claims text (e.g. a short log `tz` header). A `false` item, a medium/high item
  or another rubric on claims-only evidence is still dropped. **Claims stage only** (`mode=frontier-claims`):
  also kept when verdict `partial` and one quoted claims fragment of >= 20 chars holds two or more different
  numbers (an arithmetic contradiction inside one sentence, e.g. a part larger than its whole); note
  `numbers inside one claims fragment (claims-only stage)`. Other modes are unchanged.
- **`false` → `n/a` + `low`** when (b) there is no contradicting span; (c) the evidence admits absence ("no
  evidence", "cannot verify", "withheld", ...) and no contradicting span carries a failure word (error, fail,
  denied, inactive, non-zero exit, 4xx/5xx, blocked, ...); (d0) every contradicting span comes only from
  point-in-time artifacts (text starting `# POINT IN TIME`, e.g. `unit_state`, `port_listening`, or listed under
  the manifest's `point_in_time`), which show the state at collection time, not during the session; or (d)
  `c3-results.jsonl` has a `final: true, ok: true` line for a file the item names and no final failing line for
  it. Only `# WINDOWED` artifacts (e.g. `unit_journal`) and C3 `final: true` lines count as final-state evidence.
- **(d1, #32) earlier refusal, later success:** `false`, or `partial` at medium/high, → `n/a` + `low` when the
  evidence rests on a refused gate decision (`not_executed`, blocked, escalated, refused, denied) and
  `tool-calls.jsonl` has a LATER call of the same tool (the tool the item names, else any refused one) with
  `ran: true`. A low `partial` is left alone: that is how a "blocked by the gate" wording slip is filed, with the
  refusal as its evidence. Note `superseded: the <tool> call at <ts> did not run, but a later <tool> call ran ...`.
- **(g, #32) gate requests** (`kind=gate`, from the request or the manifest's request copy): an item whose claim
  is the gate request's own text (`C2 gate approve|block|pass (<rule>): ...`, or shares a 24+-char span with it)
  is **dropped** (`the claim is the gate request's own text`); any other `false` → `n/a` + `low` (a gate request
  carries no agent claim to contradict; the report is judged with the completion request).
- **`high` → `medium`** unless the verdict is `false` or the rubric is R3, R4 or R5.
- **Local-judge cap.** Items of a `mode=local` finding are capped at `JUDGE_LOCAL_MAX_SEVERITY` (`low|medium|high`,
  default and fallback `medium`). Applies to every local finding: `JUDGE_MODE=local`, sensitive bundles and
  frontier-cap fallbacks.
- Every change is a `notes` line, e.g. `"F1: verdict false->n/a, severity high->low (...)"`,
  `"F1: severity high->medium (local judge cap JUDGE_LOCAL_MAX_SEVERITY=medium; ...)"`, `"validator dropped F2: ..."`.
- `validate_finding(..., request=, max_severity=, notes_out=)`; return shape unchanged. CLI:
  `validate.py ... [--bundle FILE] [--local-max-severity LEVEL]`.

## Evidence bundle (`evidence/<request-id>/`)
**Window.** Every time-windowed artifact covers `[request.since, until]` in UTC, with
`until = min(request.created + JUDGE_WINDOW_GRACE_SECONDS, next_turn_start - 1 s)`, never before `created`
(`collect.window_info`), whenever the bundle is collected: a later turn's evidence never reaches an earlier
request's bundle, even inside the grace period. `next_turn_start` is the first
`agent.turn_context: conversation turn:` line of the session in the Hermes log at or after `created` (Hermes'
background review turns count); only when the log has none in `[since, created + grace]` (no log, older
Hermes), the earliest `since >= created` among the session's other requests in queue/ and done/. Log times have
1 s resolution, so a next turn in the same second gives `until = created`. Manifest: `window.until_basis`
(`grace` | `next_turn_log` | `next_request`) and `window.next_turn_start` (when cut). The host `find` bound moves
with the cut. Exception: `c3-results.jsonl` is windowed to the **turn end** (next turn start − 1 s, searched up to
collection time; else collection time; never before `until`) so C3 re-runs after a nudge are kept
(`extras.c3_window`). Point-in-time artifacts (read at collection time) are labelled as such in the manifest.

**Withheld content.** A withheld diff is never silent, so the judge can't read absence of content as absence of
change. A `sensitive` `agent-diff.patch` starts `# mode: CONTENT WITHHELD (data_class=sensitive): ... the file DID
change` and has one line per changed agent path (snapshot files, repo files via `git diff --numstat`, untracked
files): `# content withheld (data_class=sensitive): <path> — N lines changed (+a/-b) [added|modified|deleted]`,
or `— binary file changed` / `— snapshot skipped (...)` (`snapshot.withheld_line`). Manifest
`withheld: {artifact: reason}` covers `agent-diff.patch` (sensitive) and `others-changed.txt` (non-infra others
in an `infra` bundle); `{}` when nothing is withheld.

| File | Content | Time |
|---|---|---|
| `manifest.json` | request copy, `artifacts`, `collector_version` (2), `data_class`, `request_data_class`, `content_policy`, `collected`, `window: {since, until, grace_seconds, until_basis, next_turn_start?}`, `windowed` (list), `point_in_time: {<artifact>: {observed_at, note}}`, `attribution: {agent_paths, changed_by_others, omitted_after_window, rejected_request_paths, rejected_request_paths_total, ignored_noise_paths}`, `withheld: {<artifact>: reason}`, `snapshot: {dir_roots, truncated_roots, skipped_roots, caps, noise_globs}` (`{available: false}` without a snapshot), `extras: {available, c3_results, c3_window, host_probes}`, `notes: {<artifact or topic>: "..."}` (topics: `attribution`, `noise`, `snapshot`) | — |
| `hermes-log.txt` | agent.log/errors.log lines in the window, secrets redacted: **this session's tagged lines first**, then untagged context lines with startup/housekeeping noise dropped (none for a C6 watcher request); layout below | window |
| `gate-decisions.jsonl` | `gate.log` lines of this session with `ts` in the window, plus (for a `gate` request) the decision that created it; re-redacted; each line gains `decision_meaning` (`approve` = escalated to the human) and `outcome` (`executed` \| `not_executed` \| `unknown`) + `outcome_basis`: executed when an `events.jsonl` event (post_tool_call fires for every call; Hermes reports a denied or timed-out approval as `status="blocked"`, interrupted calls as `cancelled`/`aborted`) matches the decision by `tool_call_id`, else by tool + `call_hash`, and its status is not in `NOT_RUN_STATUSES` (the latest earlier decision of that call within 600 s); not_executed when nothing matched and the decision is settled (block, the turn ended, or 600 s passed); not_executed also as soon as an event with a `NOT_RUN_STATUSES` status (e.g. Hermes' `blocked` for a refused approval) carries the decision's `tool_call_id` (basis `post_tool_call reported status=<s> ...`); unknown without a session snapshot, when the decision or the events predate call markers, or while too recent. Always written; when empty, `notes["gate-decisions.jsonl"]` says "no gate decisions in window" | window |
| `tool-calls.jsonl` | one JSON line per tool call `post_tool_call` saw for this session with `t` in the window (bug #33; `collect.tool_calls`): `t` (UTC), `tool`, `command` (the program NAME only, `lib/toolcalls.command_word`: for `terminal` the first word, leading `VAR=value` assignments skipped, basename, kept only when it is in `READONLY_COMMANDS` (common read-only programs: `ls`, `stat`, `wc`, `cat`, `grep`, `systemctl`, `journalctl`, `sha256sum`, `git`, ...), else `(other)`; `(none)` for an empty command; never arguments; taken from the event's `command`, else from Hermes' `state.db` by `tool_call_id`, else `(unknown)`; other tools: the tool name), `gate` (`pass`: no gate.log decision matches the call by `tool_call_id`, or by tool + `call_hash` for events/decisions without ids; `escalated` / `blocked`: the matching decision; `not gated`: a tool outside the gate's matcher), `ran` (the event's status is not in `NOT_RUN_STATUSES`), `error` (status `error`), `after_refused_escalation` (an earlier `approve`/`block` decision of this session whose call never ran). Hermes' raw status is not copied: it says `blocked` for an escalation nobody approved, which reads as a gate block. At most 200 lines; when empty, `notes["tool-calls.jsonl"]` says so | window |
| `agent-diff.patch` | watched paths vs the session-start snapshot (+ repo changes since the start HEAD), **only paths attributed to the agent** (session tool events up to the window end; request `changed_paths` only with a backing event, see Attribution). Content only when `data_class=infra`; otherwise `# content withheld` lines (see above). A file modified after the window gets a `# NOTE:` line; a truncated or missing opted-in dir gets a `# NOTE: opted-in dir ...` header line | point in time (current content) |
| `others-changed.txt` | snapshot changes **not** made by the agent: `<status> <path> \| +N -M` lines, never content. In an `infra` bundle, non-infra paths are withheld (count only). Changes made after the window (by anyone) are omitted (count only) | point in time |
| `host-<name>.txt` | `# host:` header (see Probes), then UTC `find -newermt <since> ! -newermt <until>` over /etc /srv /usr/local, then `systemctl --failed` and the host clock (both at collection time) | find: window |
| `c3-results.jsonl` | the C3 verifier runs of this session from `since` to the turn end, from `snapshots/<session>/c3-results.jsonl` (format below), re-redacted; every line has `final`, `true` on the latest per `(path, check)` | window (to turn end) |
| `probes/host-<name>.txt` | read-only host-state probes chosen from the request's claims and the window's gate excerpts by `extras.host_state_probes` (`<name>` = `unit_state-<host>-<unit>`, `unit_journal-<host>-<unit>`, `port_listening-<host>-<port>`, sanitized to `[A-Za-z0-9_.-]`; at most 4 per request), re-redacted; text starting `# WINDOWED` is listed in `windowed`, the rest in `point_in_time` | unit_state, port_listening: point in time; unit_journal: window |
| `slots.json` | llama-server slots summary (`{"<model>": [slot, ...], "_collected": "...Z", "_error": "..."}`; `_`-prefixed keys are string metadata) | point in time |
| `probes/<probe>-<n>.txt` | stdout+stderr+exit code; written by `collect.py <id> --probe <name> [args]` (adds a `point_in_time` entry) | point in time |
| `probes/judge-<probe>-<n>.txt` | probes the judge requested, saved by the runner | point in time |

**`hermes-log.txt` layout** (`collect.hermes_log`, bug #19). Line 1: `# Hermes log lines for session <id> (plus
untagged context lines, after the session lines), window ... UTC; log tz ...`; line 2: `# Layout: ...`. Then, in
this order: `===== agent.log: SESSION LINES (N line(s)) =====`, `===== errors.log: SESSION LINES (...) =====`,
`===== agent.log: UNTAGGED CONTEXT (M line(s); K noise line(s) dropped: <logger> xN, ...) =====`,
`===== errors.log: UNTAGGED CONTEXT (...) =====`. SESSION LINES = every line tagged `[<session>]` (with its
continuation lines), plus the untagged parallel tool-call lines attributed to the session (bug #28, below); lines
tagged with another session never appear. UNTAGGED CONTEXT = lines without a session
tag, which may come from this or any other Hermes process; those `lib/hermeslog.is_noise` classifies as
startup/housekeeping noise are dropped and only counted: loggers `hermes_cli.plugins`,
`hermes_cli.plugin_capabilities`, `hermes_cli.mem_trim`, `hermes_cli.gateway_multiplex_mode`, `hermes_cli.main`,
`tools.registry`, `tools.tool_search`, `tools.skills_sync`, `agent.shell_hooks`, `agent.auxiliary_client`,
`agent.credential_pool`, `cron.*`, `gateway.*`, `botocore.*`, `plugins.*`, plus `JUDGE_LOG_NOISE_LOGGERS`; and the
per-process-start messages `state.db: linked SQLite ... vulnerable`, `Background MCP discovery previously exited`,
`Loaded environment variables from`, `OpenAI client created (agent_init|chat_completion_stream_request ...`.
Every other untagged line is kept, e.g. `agent.message_sanitization` "Unrepairable tool_call arguments" warnings
(Hermes does not tag them) and untagged `agent.tool_executor` lines of parallel tool calls that could not be
attributed. errors.log lines that
also appear in agent.log are counted (`also in agent.log not repeated`), not repeated. At most 3000 lines per
section (the last ones). A C6 watcher request has SESSION LINES sections only. S9 (run 2) went from 32.6K chars
with the session's first line at char ~26K to 5.2K chars with the 17 session lines on top.

**Parallel tool calls** (`lib/hermeslog.parallel_attribution`, bug #28). Hermes keeps the session tag in
thread-local state, so the calls of a concurrent tool batch, which run in worker threads, are logged UNTAGGED
(`agent.tool_executor: tool skill_view completed (0.06s, 15429 chars)`; also `failed`/`cancelled`/`abandoned`),
with no thread, task or tool_call id. Such a line is attributed to session S only when S's own turn brackets it
and nobody else was active: going back, the session-tagged lines are S's up to S's `API call #n`; going forward,
S's up to S's next `API call` or `Turn ended`; and no other session's turn (`conversation turn:` to
`Turn ended`; a turn with no end counts as busy until 30 min after its last line; a session first seen mid-turn
counts as busy from the start of the scanned log) overlaps that bracket. Otherwise the line stays untagged
context; another session's lines are never attributed. An attributed line goes into SESSION LINES with the suffix
`  # judge: untagged parallel tool call, attributed to this session by turn adjacency` (`PARALLEL_MARK`), and
counts for `session_lines`/`tool_activity`/`tools_used` (so the enqueue hook and file attribution see it). A failed
parallel call is logged twice (untagged `tool X failed`, then tagged `Tool X returned error`); exact counters pair
them. Residual risk: a Hermes process that never logs a session tag is invisible to the rule. On the real
agent.log (2026-10-02..04) all 38 untagged tool lines were attributed, each inside one session's own turn.

`data_class` of the bundle: the stricter of the request's class and the collector's own classification of the
agent-attributed paths and the rejected request paths (noise excluded). With no such paths, only a host-rule `gate` request or a C6 watcher request (shape above) keeps its own class; any other
request is classified with `classify([], cfg, cwd)`, so an `infra` label on a path-less request (forged or
buggy) comes out `sensitive` unless the cwd is infra. For `data_class=sensitive`: diffs replaced by
`# content withheld` stat lines; no file contents.
**Extras** (`collector/extras.py`, imported lazily): when absent, no `c3-results.jsonl` or host probes,
`notes` say "not installed" and `extras: {available: false}`; any exception from it becomes a note
(`not collected: ... failed (<ExcClass>)`) and the bundle is still written.

CLI: `judge/collector/collect.py <request-id>` reads the request from queue/ (or done/), writes the bundle and
`manifest.json` last, prints the evidence dir, exits 0. Exit 2 = bad id / request not found. Unreachable hosts are
recorded in `host-<name>.txt` (`UNREACHABLE: ...`), never fatal.
Snapshots (`snapshots/<session>/`, written by `hooks/enqueue.py` on `on_session_start`) cover the watched paths
**and the opted-in dirs**: `JUDGE_INFRA_REPOS` entries that are git repos join meta `repos` (HEAD + status, diffed
via git); plain dirs are copied (`snapshot.dir_roots`), skipping `.git`, `node_modules`, `__pycache__`, `.venv`,
`venv`, `.cache`, capped at `JUDGE_SNAPSHOT_MAX_FILES` (default 2000) files per root — beyond it the root is
`truncated` and **additions in it are not detected** (only M/D) — and `JUDGE_SNAPSHOT_MAX_BYTES` (default
1048576) per file (larger files are hashed, not copied: `skipped: too large`). `meta.json` gains
`dir_roots: [{root, files, truncated, too_large}]`, `skipped_roots: [{root, reason}]` (missing or not a
directory), `snapshot_caps: {max_files, max_bytes}` and `noise_globs`. `meta.json`,
`index.json` (`{abs_path: {"sha256", "size"}}`, plus `"skipped"` when a file was not copied), `files/<abs path>`
copies, `events.jsonl` (tool calls seen by post_tool_call: `{"t","tool","paths","status"}`; `paths` are the
write_file/patch targets, or the path-like tokens of a terminal command, used only for attribution, and empty
for read_file/memory/skill_manage; plus
`call_id` = Hermes `extra.tool_call_id` when sent, `call_hash` = `lib/redact.call_hash(tool, tool_input)` and
`command` = `lib/toolcalls.command_word(tool, command)` (an allowlisted program name, `(other)` or `(none)`; the
tool name for other tools; bug #33); never the command text or arguments).

## C3 results (`snapshots/<session>/c3-results.jsonl`)
`hooks/verify.py` appends one JSON line per verifier run, passing or failing (file 600, append-only):
```json
{"t": "2026-10-03T08:31:02Z", "attempt": 0, "path": "/abs/path", "check": "bash-n", "ok": false,
 "detail": "<one line, redacted, then cut to 300 chars>"}
```
- `check`: `bash-n`, `shellcheck`, `py-compile`, `json`, `yaml`, `check-sanitized` (`path` = repo root),
  `ssh-config`, `ssh-alias:<alias>` (`path` = `~/.ssh/config`), `claim` (`path` = the claimed path; `ok: false` is
  a claim mismatch, `ok: true` means changed_paths, the snapshot hash or a recent mtime confirms the change).
- `claim` covers only paths the final answer **claims** as changed (#35, `verify.py::_claimed_paths`): an edit
  verb (fixed/updated/edited/added/created/wrote/...) governs the path (same clause, before it within 60 chars
  not counting other paths and parentheticals, or after it with only linking words between: "x.sh was updated").
  A mention is not a claim: text in double quotes, a path in another clause (`;`, dashes, arrows), a sentence
  with a negation. Paths are resolved by `resolve_claim_path`: absolute and `~` paths as written; relative ones
  (bare names included) only when the path exists under the session cwd or the cwd's repo root (`.git` entry).
  Anything else (a name of a file in another checkout) is not checked; a claimed new file is only checked
  when the answer gives it as an absolute or `~` path.
- Checks that did not run (budget spent, no PyYAML, probe refused or missing) are not recorded. A shellcheck
  timeout is recorded `ok: true`, detail "timed out (not counted as a failure)".
- `detail` is redacted before it is cut, so a cut can't defeat a pattern; if `lib/redact` can't load it is `""`.
- **Retries are recorded.** When Hermes re-fires `pre_verify` after the nudge (`extra.attempt > 0`), the verifiers
  run again in record-only mode: lines are appended with that attempt, the hook prints `{}` and enqueues nothing.
  So the final state is on record and there is still at most one nudge.
- A failure to write the file goes to `hook-errors.log`; the hook's output is unchanged.

The collector copies the lines from `since` to the turn end (see Window) into the bundle and marks the latest line per `(path, check)`
with `"final": true`, so the judge can tell a failure the agent later fixed from one it left.

## Collector extras (`judge/collector/extras.py`)
Read-only, stdlib-only helpers the collector calls; if the module is missing the collector skips them and notes it.
- `c3_results(session, since, until, root) -> list[dict]`: the `c3-results.jsonl` lines with `t` in
  `[since, until]` (inclusive), oldest first, malformed lines skipped. Each line gains `final`; `true` on the
  latest per `(path, check)` (ties on `t` go to the later line in the file).
- `host_state_probes(req, cfg, runner=None, root=None, gate_lines=None) -> dict[str, str]`: never raises; on a
  malformed request returns `{"host_state_probes-error": "..."}`. Disabled by `JUDGE_HOST_PROBES=0` (default `1`).
  - Text scanned: the request's `claims`, `plan` and `detail.excerpt`, plus the `excerpt` of this session's
    gate.log lines in the window (and a gate request's own line). Window as for the bundle.
  - Units: `systemctl <verb> <unit>` / `service <unit> <verb>` (`--user` skipped), and in claim sentences with a
    state word (active, running, reloaded, restarted, up, failed, completed, ...) `<name>.service|timer|socket` or
    a well-known unit. Gate-excerpt units come first.
  - Host of a unit: the one host named in the same line or sentence (walter/covenant, `edge`, the site.env IPs,
    `JUDGE_SSH_ALIASES`); else a known-unit map (edge: nginx, oauth2-proxy, fail2ban; backend: llama-swap, docker,
    docker-user-rules, spark-backup, spark-update-check, nvidia-persistenced); else the only host named in the
    whole text; else the unit is skipped.
  - Each unit gets `unit_state <host> <unit>` + `unit_journal <host> <unit> <since> <until>`; each loopback port
    (`127.0.0.1:<p>`, `localhost:<p>`, `[::1]:<p>`) gets `port_listening <host> <p>` (same host rule, default walter).
  - **At most 4 probe runs per request**, all through `probes/probe.py` (allowlist, validation, redaction); a
    refused probe (exit 64) is dropped.
  - Keys: `unit_state-<host>-<unit>`, `unit_journal-<host>-<unit>`, `port_listening-<host>-<port>`. Each text
    starts with `# POINT IN TIME: ...` (unit_state, port_listening) or `# WINDOWED: ...` (unit_journal), then
    `# selected because: ...`.

## Claims-only bundle (`collector/claims_only.py`) and the frontier claims stage

**Policy** (owner decision 2026-10-03, docs/decisions.md #10). A `sensitive` bundle never leaves the machine. For a
sensitive **completion** (the kind whose `claims` is the agent's own final answer) the runner adds a second stage:
the local judge still reviews the full bundle (`findings/<id>.json`, capped, not injected), and the frontier judge
reviews a CLAIMS-ONLY bundle built from it (`findings/<id>.claims.json`, mode `frontier-claims`). Gate, plan and
runaway requests get no claims stage (their `claims` is synthetic or a plan, not a final answer).

**Contents: a positive allowlist.** Every value is constructed by the builder from parsed fields; nothing from the
bundle is copied through as text except the final answer:
- REVIEW REQUEST: `id`, `kind`, `data_class`, `created`, `since`, `claims`. `claims` = `request.claims` (cut to
  8000 chars), masked by `claims_only.mask_claims` (below). The final answer is free text written by an agent that
  may have read secrets and site data, so it is treated as **hostile to the data boundary** (bug #26): it is
  masked, and the whole message must still pass the self-check, which refuses rather than trusts the masking.
- `manifest.json`: `bundle_mode: "claims-only"`, `request` (the fields above minus claims), `window` (`since`,
  `until`, `grace_seconds`, `until_basis`), `timing` (`request_created`, `collected`, `log_tz` from the
  hermes-log header, `host_times: "UTC"`), `attribution_counts` (`agent_paths`, `changed_by_others`, `withheld` =
  `# content withheld` lines in `agent-diff.patch`, `rejected_request_paths`: numbers only), `path_index`
  (`file#N` -> `{"seen_in": ["claims"|"c3"], "inside": "file#M"|null}`: containment, never a name), `not_included`.
- `gate-decisions.jsonl`: per decision `ts`, `tool`, `command`, `rule`, `rules`, `decision`, `decision_meaning`,
  `outcome`. `command` is the command NAME only: for `terminal` the first word of the excerpt (env assignments
  skipped, basename, must match `[A-Za-z0-9][A-Za-z0-9._+-]{0,39}`, else `(unparsed)`); for other tools the tool
  name. `decision_meaning` is fixed text per decision; `outcome` is `executed|not_executed|unknown`. No excerpt,
  `outcome_basis`, call hash, call id or session.
- `tool-calls.jsonl` (bug #33): per line of the collector's `tool-calls.jsonl` (at most 100), re-validated
  against fixed vocabularies: `ts` (UTC), `tool`, `command` (must pass `lib/toolcalls.is_command_word`: an
  allowlisted program name, `(other)`, `(none)`, `(unknown)`, or the tool name; anything else becomes
  `(unknown)`), `gate` (`pass` | `escalated` | `blocked` | `not gated`, else `unknown`), `ran`, `error`,
  `after_refused_escalation` (booleans: anything but `true` is `false`). It tells a narrowed retry the gate passes
  by design (S8 run 4: `stat` escalated and refused, then a metadata-only `stat` passed and ran) from a call that
  went around a refusal. A bundle collected before this file existed gets `(not recorded: the bundle predates
  tool-calls.jsonl)`.
- `c3-results.jsonl`: per result `check`, `ok`, `final`, `file` (`file#N`, shared index with the claims). No
  `detail`, `path`, `t` or `attempt`.
- `tool-activity.jsonl`: a summary line (`tools: {name: {ok, error, seconds}}`, `tool_calls`,
  `parallel_tool_calls`, `api_calls`, `tokens_in`, `tokens_out`, `turns`, `other_session_lines`,
  `untagged_tool_lines` = untagged tool lines left in UNTAGGED CONTEXT, which may be this or any other session's),
  then one line per session `hermes-log.txt` event (session-tagged, or an attributed parallel tool-call line in a
  SESSION LINES section, which gets `"parallel": true`; a failed parallel call logged twice counts once), parsed
  by pattern: `tool` (`tool`, `ok`, `seconds`, `output_chars`), `api_call` (`n`, `model`,
  `tokens_in`, `tokens_out`, `latency_s`), `turn_start` (`history`), `turn_end` (`reason`, `api_calls`,
  `tool_turns`, `response_len`), each with log-local `t`. Duplicate log lines (WARNING+ lines appear in both the
  agent.log and errors.log sections) count once; other session lines are only counted; other untagged lines and
  other sessions' lines are ignored. At most 300 events (middle-cut with an `omitted` count).
- Never: file contents, diffs, paths, command arguments or output, tool error text, host or probe output, slots,
  `others-changed.txt`, snapshot data, the user's message (`msg=`), `plan`, `detail`, `changed_paths`, session id.

**Masking of the final answer** (`mask_claims`, in this order; ids are stable within one answer):
1. `lib/redact`.
2. **Sentences about secret material** (`withhold_secret_sentences`, made precise by bug #31). Only in a
   paragraph (or list) that mentions a key/keyfile/token/secret/password/passphrase/credential or a `.key`/`.pem`
   file (`32768-token context` and `16784 tokens` are not secret words), each sentence is classified
   (`classify_secret_sentence`):
   - **prefix**: it names literal leading/trailing characters: after `starts/begins/ends with`, `prefix/suffix
     is|:`, `first/last N chars are|:`, or before `prefix`, a quoted/backticked token or an unquoted one with a
     non-letter or of 1-3 letters that is not an ordinary word (`starts with s`, `begins with sk-`; not `starts
     with a letter`). Replaced whole by `[sentence disclosing a secret's prefix withheld]`.
   - **length**: a number with a char/byte/bit/digit unit (`25 chars`, `26 bytes`, `N long`) or after
     `length`/`size` (`token length: 25`), AND a secret word in the same sentence. `the first 4 chars` counts
     characters and is not a length. Replaced by `[sentence disclosing a secret's length withheld]`.
   - **doubt**: any other sentence with a detail word (`prefix`, `length`, `starts with`, `N bytes` without a
     secret word in the sentence: an offer, a refusal, a `stat` size). Kept, with every number next to a unit
     masked as `<n>` and every literal after a prefix phrase as `<chars>` (`Size: <n> bytes, mode 600`).
   The two markers are the only places the claims prompt treats as a disclosure, and they say which kind: a
   prefix is a disclosure of secret material, a length is file metadata the gate allows. Digest words are not a
   trigger: the digest value is a hex run (step 6) and `sha256: hex#1` keeps the fact that a digest of a secret
   was disclosed visible to the judge.
3. Paths -> `file#N`: absolute (`/x`), home (`~/x`, `~user/x`, `$HOME/x`) and relative paths with a directory part
   (a dotfile segment, a file extension or 2+ separators). URLs, ratios (`22.6/24.6`), `I/O`, `and/or` and a bare
   `/` are left alone. Bare file names (`config.yaml`) stay.
4. IPv4 and IPv6 addresses -> `ip#N` (IPv6 validated with `ipaddress`; times like `13:58:10` stay).
5. Site identifiers -> `host#N`: `SPARK_DOMAIN` and any subdomain of it, the domain without its TLD, every
   `SPARK_*_HOST`, `SPARK_SITE_NAME`, the `JUDGE_SSH_ALIASES`, `/etc/hostname` and this host's names (case
   insensitive). The Walter/Covenant codenames are kept (public lore). Local accounts -> `user#N`: `user:group`
   pairs (either side a known account, both sides equal like `x:x`, or after owner/owned by/chown/uid/gid) as one
   id, then the names themselves: the current user, every `/etc/passwd` account with 1000 <= uid < 65534 (`nobody`
   is public), `getpass` and home-dir basenames, `BACKEND_SSH_USER`, `EDGE_SSH_USER`, `SPARK_USERS`
   (`claims_only.site_identity`). Common words that are account names (e.g. a default `operator`) are masked too.
6. Hex runs of 16+ chars -> `hex#N` (digests, hashes, ids, key fragments); base64-like runs of 24+ chars
   (`[A-Za-z0-9+/=_-]`, letters and digits, a 9+ char piece between separators, random-looking character
   classes) -> `blob#N`. Model names (`Qwen3-Coder-30B-A3B-Instruct-Q4_K_M`), identifiers and request ids are not
   blobs; measured on random tokens, ~1% of 24-char base64url tokens and ~0.1% of 32-char ones are missed (redact
   and step 2 still apply).

**Self-check** (`claims_only.self_check(text, ident=None)`, run on the exact message, and on a JSON-unescaped
copy). It **fails closed**: any hit refuses the whole bundle. Refuses when `lib/redact` would change the text; a
`/`-prefixed or `~/`/`$HOME/` path; an IPv4 or IPv6 address; a hex run of 16+; a base64-like run of 24+; a
`user:group` pattern (as in step 5); a known local account name; a site host or domain (step 5); a `msg=` marker;
a diff hunk or header; a `# content withheld` / `# WINDOWED` / `# POINT IN TIME` marker; an unknown `=== FILE:`
header. A refused bundle is not sent: the stage is skipped, `runner.log` and the main finding's notes name the
problem KIND only (no text, no offset), and no frontier call is counted.

**Runner.** `judge_request`: local stage first (as before); then, before the main finding is written, the claims
stage when `data_class != infra`, `JUDGE_SENSITIVE_FRONTIER_CLAIMS` != `0` (default `1`), `JUDGE_MODE=frontier`
and `kind=completion`. System prompt = `runner/prompt-claims.md` + `runner/prompt.md`; no probes; one retry on an
invalid reply; validated with the claims-only bundle and the sanitized request (so `claims` is the masked text).
Each call takes one unit of `JUDGE_FRONTIER_DAILY_MAX`; at the cap the stage is skipped (never a local fallback:
the local stage already ran). A backend failure is noted, not retried, and never blocks the main finding. Files:
`findings/<id>.claims.json` + `.md`, `evidence/<id>/claims-input.txt` (exactly the message sent: the audit copy)
and `evidence/<id>/claims-raw.txt`; both evidence files are excluded from later bundles. The main finding's notes
record the outcome (`frontier-claims stage: N item(s) in findings/<id>.claims.json`, `... skipped: ...`,
`... not run: ...`, `... failed (not retried): ...`). API: `run_judge.judge_claims(request_id, request,
evidence_dir, notes, *, use_budget=True)` (raises `ClaimsSkipped` or `JudgeError`), `run_judge.claims_stage(...)`.
Audit CLI: `collector/claims_only.py <evidence-dir>` prints the message for a stored bundle (exit 1 if refused).

## Runner: bundle budget and collection timing (`runner/run_judge.py`)
- **Collection timing** (bug #21). Before running the collector for a request without a bundle,
  `wait_for_window` sleeps until `request.created + JUDGE_WINDOW_GRACE_SECONDS + 3 s` (`COLLECT_MARGIN_SECONDS`:
  log flush and 1 s log/journal timestamp resolution). It sleeps only the remainder, at most grace + margin
  (13 s by default, also for a `created` in the future); an existing bundle is never waited for. Chosen over
  re-checking outcomes at judge time because the window is already fixed (`window_info`), so waiting makes the
  one collection complete (log, gate outcomes, `unit_journal` probes, C3 lines) without rewriting a bundle or
  probing hosts twice; `judge-review.service` (`TimeoutStartSec=3600`) tolerates the pause. A gate decision
  whose call is still waiting for a human after that stays `unknown (decision too recent to tell)`, which is
  then true; a refusal Hermes already reported (`status=blocked`) is `not_executed` (see `gate-decisions.jsonl`).
- **Bundle budget** (bug #19). `JUDGE_BUNDLE_MAX_CHARS` (150000 frontier, 60000 local). `manifest.json` first,
  then files in path order. Priority content is reserved first and never head-truncated:
  `hermes-log.txt`'s session-tagged lines (with continuations; recognised by the session tag from the manifest,
  so older interleaved bundles work too) up to 50% of the budget, and `gate-decisions.jsonl` up to 20%. Only
  beyond those shares are they cut **in the middle** (first and last lines kept, one
  `[... runner omitted N session-tagged|gate decision line(s) from the middle ...]` line). Every other file, and
  `hermes-log.txt`'s untagged context, gets an equal share of the rest (at least 4000 chars): other files are
  head-truncated (`[... truncated by runner: N more chars ...]`), the context is middle-cut, and structure
  lines (headers, section titles) always stay. A non-priority file that no longer fits is listed as
  `[omitted by runner: bundle size cap]`.

- **Truncated local replies** (#30). Every local call is capped at `JUDGE_LOCAL_MAX_TOKENS` (4096).
  `call_local(messages, max_tokens=None)` records `finish_reason`/`max_tokens` of its reply in
  `run_judge.LAST_LOCAL`. When a local reply ends with `finish_reason=length`, `_judge_loop` adds a finding note
  `local judge reply N was truncated at max_tokens=M (finish_reason=length); the partial reply had K item(s) with a
  parseable severity (… high, … medium; as written by the judge, before any severity cap) | no item could be parsed
  …; it is kept in evidence/<id>/judge-raw.txt` (`truncation_note`), plus `the finding below comes from reply N
  (max_tokens=M); compare …` or `… itself truncated: items may be missing`; the same line goes to `runner.log`.
  `judge-raw.txt` keeps every reply; a truncated one's header reads `(local, <model>, TRUNCATED:
  finish_reason=length)`. The next call after a truncated reply (the validation re-ask, which also says the reply
  was cut off and asks to keep every item with shorter text) uses `JUDGE_LOCAL_RETRY_MAX_TOKENS` (default
  2 × `JUDGE_LOCAL_MAX_TOKENS`, never less than it). Frontier replies have no finish_reason and get no such note.

## Probes (`judge/probes/probe.py <name> [args...]`)
Allowlisted names with per-arg regex validation; each runs read-only with a timeout (default 20s):
`ssh_alias_test <alias>`, `port_listening <host:walter|covenant> <port>`, `http_status <https-url-on-SPARK_DOMAIN>`,
`unit_state <host> <unit>`, `file_hash <host> <abs-path>`, `render_and_diff <repo-template-path>`,
`check_sanitized <repo-path>`, `slots`, `unit_journal <host> <unit> <since> <until>`. Unknown name or bad arg →
exit 64, no execution.
- `unit_journal`: remote `sudo -n journalctl --no-pager -q --utc -o short-iso -n 300 -u <unit> --since ... --until ...`,
  falling back to plain `journalctl` when sudo needs a password. `since`/`until` must be strict
  `YYYY-MM-DDTHH:MM:SSZ`, `since <= until`, span at most 7 days. Lines the redactor would change are withheld
  whole (the count is printed); the output is redacted again as for every probe.
- **Host header** (bug #24). Remote steps on `walter`/`covenant` print one line
  `# host: <name> (<role>, via the configured ssh target from site.env; address/alias not shown). ...` and then
  `$ ssh <<name>> <remote command>` (the remote command verbatim). The logical name *is* the target; the real
  argv (from `config.host_ssh`) runs but its address, user and alias are never printed. Output lines may show the
  machine's own hostname, which can differ from the logical name; the header says that is not a mismatch.
  `host-<name>.txt` carries the same header. (`ssh_alias_test` prints its alias's `ssh -G` summary by design.)
- Unit names (`unit_state`, `unit_journal`): `^[A-Za-z0-9@._-]+\.?(service|timer|socket)?$`, starting with a letter
  or digit, at most 128 chars. systemd-escaped names (with `:` or `\`) are refused.

## Gate (C2) coverage
- **Matcher** (`install.sh`): `terminal|write_file|patch|read_file`.
- **`read_file`** (`tool_input = {"path", "offset"?, "limit"?}`; only `path` is used). A missing, empty or
  non-string path fails closed (block, exit 2), like write_file/patch. A secret-shaped path escalates
  (`approve`, rule `secret-output`; the request is a `gate` request with `detail.tool = "read_file"`, always
  `sensitive`). Every other read passes with `{}`: no log line, no request.
- **Secret-shaped** (`Gate.is_secret_file`, shared with the terminal `secret-output` rule): basename in
  `secret_output.secret_names`, OR full path in the policy key `secret_output.secret_paths` (default
  `~/.config/spark/**`, `$HERMES_HOME/.env`, `~/.ssh/id_*`, `/etc/llama-swap/api-key`, `/etc/wireguard/**`,
  `/etc/ssh/ssh_host_*_key`, `~/.git-credentials`, `~/.docker/config.json`, `~/.config/gh/hosts.yml`), minus
  `not_secret_names` (`*.pub`, `*.example`, `*.md`, `*.sh`, `*.py`, ...). A symlink's target counts too.
  `$HERMES_HOME/config.yaml` passes on purpose (settings, not credentials; see the policy's `_doc_read_file`).
- **`rule_key`** for a secret read = `judge-gate:secret-output:<sha256(tool \0 resolved-abs-path)[:12]>`: one key
  per file whatever the spelling or page, so a human's "always" covers that one file, never the whole rule.
- **Shell variables in terminal commands** (#27, local and remote, incl. `bash -c`, `eval`, function bodies,
  `$(...)`, scripts the gate peeks into): assignments to literal values (`X=v`, prefix `X=v cmd`,
  `export|local|declare|readonly|typeset X=v`; `~` / `$HOME` / `$HERMES_HOME` / `$JUDGE_REVIEW_DIR` /
  `$JUDGE_DIR` expanded; earlier known variables substituted) are tracked in order, and `$X` / `${X}` (quoted or
  not) are substituted into later words and redirect targets before every rule. A value holding a substitution,
  an unknown variable, `${X:-...}`-style expansion, an array element or `+=` is unknown; so are `for`/`select`
  loop variables, names set by `read` / `mapfile` / `readarray` / `getopts` / `printf -v`, and positional
  parameters (`$1`, `$@`; `$0` counts as known). `unset` forgets a name. Over-approximations on purpose: a prefix
  assignment stays known after its command; single-quoted `$X` is substituted too.
  Command substitutions are analyzed in order with the command that holds them (substitutions in dropped
  loop/case headers and `[[ ]]` first).
- **`secret-output`, terminal** (print commands = policy `print_commands`): a print command, `< file`, `$(<file)`,
  `openssl rsa|pkey|ec|pkcs12 -in` without `-noout`/`-pubout`, `cp|install|ln|mv <secret> /dev/stdout|-|...`,
  `python|perl|ruby|node|php -c|-e` code (or code on stdin) that reads (`open(`, `.read*(`, `read_text`, ...) a
  secret path named in the code or passed as an operand, `perl|ruby -n|-p <secret>`, `echo|printf` of a variable
  or `$(...)` that read a secret, or `<(cmd)` where cmd read one; each only when stdout is not captured. Captured =
  inside `$(...)`, stdout redirected (`>`, `>>`, `&>`; `2>` alone is not, fixed with #27), or piped into a
  `safe_sinks` command. Whole-file digests and counts (`sha256sum`, `md5sum`, `wc`, ...) pass by design; a
  digest of a byte-limited part (`head -c` / `tail -c` / `cut -c|-b` / `dd count=|skip=` piped to a hash) escalates
  (brute-forceable). A copy made in the same command (`cp|mv|install|ln <secret> dst`, or a captured secret read
  redirected to a file) is treated as the secret for the rest of that command.
- **grep/rg patterns** (#36): `grep_operands` splits patterns from files (`-e`/`--regexp` patterns or a `-f`/`--file`
  pattern file make every positional a file; before #36 `grep -f pats <secret>` took the secret for the pattern and
  passed). The pattern is never a path. A pattern matching `secret_grep_pattern` escalates as `secret-output` (when
  not quiet `-q|-l|-L|-c` and not captured) only if what is searched may hold a secret: stdin (no file, or `rg` fed by
  a pipe/redirect), any remote host, an unresolved operand, a file whose basename is not in `not_secret_names`, or a
  directory searched recursively (`grep -r|-R|-d recurse`, `rg`; no operand = cwd) that is a secret path, covers one
  (`_covers_secret`) or holds a secret-shaped file. `Gate._tree_secret` walks the tree (symlinks not followed, `.git`
  skipped) for at most `TREE_WALK_MAX` = 3000 entries and `TREE_WALK_SECONDS` = 20 ms; over budget escalates.
  `--include` / `rg -g` globs that are all in `not_secret_names` skip the walk. A secret file operand still
  escalates through the path rule whatever the pattern (`grep -r token ~/.hermes/.env`, `grep -rn x ~/.config/spark/`).
- **Syntax-only checks** (#36): `bash|sh|zsh|dash|ksh|ash [opts] -n|-o noexec FILE` (the option must come before
  FILE; `bash FILE -n` runs FILE) and `shellcheck FILE...` are not analyzed as execution of FILE. They go through
  the shared read check instead (verb "syntax-checks" / "quotes lines of"): a secret-shaped FILE or `<` stdin
  redirect escalates as `secret-output` (errors and lint output quote source lines), an unresolved one as
  `secret-output-unknown` when the line names a secret, else pass. A heredoc on stdin is not run either.
  `bash -n -c CODE` is still analyzed as before.
- **Script peeks**: a file run by path (`./x`, `$IPT …` with `IPT=/usr/sbin/iptables`) is parsed as shell only
  when it is text; a file with a NUL byte in its first 8 KiB is a binary and is skipped (#36: parsing
  iptables as shell cost ~15 ms per call, 500 ms for the pilot's firewall script).
- **`secret-output-unknown`** (approve; #27; added to older pre-rendered policies by `load_policy`): a print
  command (or the commands above) whose operand stays unresolved (`$X` unknown, `$(...)`, loop variable,
  `xargs` stdin / `-I{}`, `find -exec ... {}`, `sh -c '... "$1"' _ {}`) escalates when the same command line
  mentions a secret-shaped word anywhere (`Gate.is_secret_file` on every word: assignments, loop lists, globs such
  as `~/.config/spark/*`), or, for scripts the gate peeks into, a configured `secret_paths` match only (names like
  `site.env` in long scripts were too noisy); or when the `find` feeding it (`find R ... -exec`, `find R ... | xargs`)
  has a root R that is or contains a `secret_paths` entry (`~/.config`, `/etc`, `~`) and no `-name` filter that is
  entirely in `not_secret_names`. Otherwise it passes. The reason names the secret-shaped word, never content.
  Words of the command line that occur only inside grep/egrep/fgrep/zgrep/rg patterns, sed scripts or awk
  programs (`_pattern_args`, also inside `$(...)` and `bash -c`) are not mentions (#36): occurrence counts are
  compared, so the same word used elsewhere in the line still counts.
- **Not seen by the gate** (known limits): values built at run time (`f=$(printf '%s/%s' ~/.config spark)/k`
  without any secret-shaped word, base64-decoded names, `$IFS` tricks); copies or symlinks made in an earlier tool
  call (`cp key /tmp/x`, then `cat /tmp/x` next turn); secrets read by programs that are not print commands and
  print what they read (`vim`, a compiled tool, `git diff --no-index`); interpreter code that builds the path at
  run time; `execute_code` (outside the matcher). Each `terminal` call is judged on its own.
- **hermes CLI** (policy key `oversight_config.hermes_cli`; gate.py carries the same values as a fallback for
  older rendered policies):
  - pass: `-h`/`--help` anywhere before `--` (block flags `--yolo`/`--accept-hooks` are still checked);
    `--version`, `status`; `config` (bare) `show|get|path|env-path|check`; `hooks` (bare) `list|ls|doctor`;
    `approvals suggest` without `--apply`, `approvals test`.
  - block: `config edit`; `config set|unset` of an oversight key; `hooks revoke|remove|rm` (and legacy
    `approve|accept|allow`); `approvals suggest --apply`; `import`.
  - escalate as `secret-output`: `config get --raw` (prints credentials unmasked; passes when captured).
  - escalate as `hermes-config`: any other `config` action (`migrate`, `set|unset` of a non-oversight key,
    unknown), any other `hooks` action (e.g. `test`), `setup`, `migrate`.
- **Shell structure** (`gate.shell_structure`, terminal commands local and remote, #22): reserved words count
  only in command position. `if then elif else fi`, `while until do done`, `{ }`, `!`, `time` are dropped and
  the commands between them judged one by one; `for`/`select` headers (`for x in ...`, `for x do`,
  `for (( ... ))`) and `case WORD in` + patterns (`a|b)`, `(c)`) are dropped (arms end at `;;`, `;&`, `;;&`);
  `[[ ... ]]` is dropped whole (its `&& || < > ( )` are not shell operators); `(( ... ))` and `$(( ... ))` are
  inert, except command substitutions inside them, which are analyzed like any other; `name() {...}` /
  `function name {...}`: the body is judged where it is defined, and a later call of that name is not an
  unknown command (its own redirects still count). Redirects after `done`/`fi`/`esac`/`}` apply as written
  (remote `done > f` = `remote-mutation`). Unparseable structure is left as words, i.e. an unknown command,
  which escalates remotely. Read-only remote builtins added to the policy: `typeset`, `readonly`, `shift`,
  `break`, `continue`, `let`, `wait` (`[`, `test`, `true`, `false`, `:` were already there; a bare `X=1` never
  was a command).
- **post_tool_call matcher** (`install.sh`): `write_file|patch|terminal|memory|skill_manage|read_file`. A
  `read_file` event in `events.jsonl` has `paths: []` plus `call_id`/`call_hash`, so a gated read gets an
  `outcome` (#23); it never attributes a change to the agent.
- Hermes keys consent on `(event, command)` (`agent/shell_hooks.py` `_entry_matches`), not the matcher, so the
  matcher change needs `install.sh --apply` (re-renders `gate-policy.json`, rewrites the hooks block) and a
  restart of Hermes and the gateway, but no new consent.

## Acks (`acks/<request-id>.<item-id>`)
One JSON object, mode 600, atomic:
`{"actor": "human"|"agent", "reason": "<first line, at most 500 chars>", "ts": "<UTC Z>", "via": {...}?}`.
- `via` (written by `bin/judge-ack`; informational, forgeable, never trusted): `{"tool": "judge-ack", "uid",
  "ppid", "parent", "flag_agent", "agent_context": [...], "hermes_session"?}`.
- A legacy ack (empty or a plain-text reason) reads as `actor: "human"`, `ts` = file mtime. A JSON ack with a
  missing or invalid `actor` reads as `"agent"`.
- If any agent tool call in `snapshots/*/events.jsonl` named the ack file (write_file/patch target or terminal
  path token), the ack reads as `agent` whatever it claims (`queue.agent_wrote_ack`; `claimed_actor` is kept).
- **Status** per item: `open` (no ack) | `agent-acked` (agent ack of a `high` item: no longer injected, waits for
  a human) | `closed` (human ack, or agent ack of a non-high item). *Needs human* = a `high` item without a
  human ack. **Any** ack stops C5 re-injection.
- A human ack replaces an agent ack; an agent ack never replaces a human one (judge-ack refuses, exit 0).
- `lib/queue`: `ack(..., *, actor="human", via=None, now=None)` (alias `write_ack`), `read_ack`, `is_acked`,
  `agent_event_paths`, `agent_wrote_ack`, `ack_actor`, `closure(actor, severity)`, `item_status`, `is_closed`,
  `needs_human(status, item)`, `items_by_status`; constants `ACK_ACTORS`, `ACK_STATUSES`.
- The gate still lets the agent write under `acks/` (it has to, to ack). So a same-user agent can still forge
  a human ack (see README "Ack trust").

**`bin/judge-ack [--agent] <request-id> <item-id> "<reason>"`** (flag anywhere). Default actor `human`;
`--agent` → `agent`. It records `agent` regardless of the flag when it detects an agent context: a non-empty env
marker (`HERMES_AGENT`, `HERMES_SESSION_ID`, `HERMES_SESSION_KEY`, `AI_AGENT`, plus the comma-separated names in
the environment variable `JUDGE_ACK_AGENT_ENV`) or a `hermes`, `hermes-agent`, `run_agent.py` or `hermes_cli`
process among its /proc ancestors (a note goes to stderr). Detection can only add `agent`. Exit 0 ok, 2 unknown
finding/item, 64 usage.

**`bin/judge-findings`**: `--items` and `<request-id>` views print `== open (n) ==`,
`== agent-acked: awaiting a human (n) ==`, `== closed (n) ==`, each item with `ack: <actor> <ts>: <reason>`.
`--needs-human` = only `high` items no human has closed (implies `--items`); `--unacked` = no ack at all. The
summary line ends `open:N agent-acked:N closed:N`. `--json` items gain `status` and `ack`
(`{"actor","reason","ts"[,"claimed_actor","legacy"]}` or null); `acked` (any ack) is kept.

## C5 injection (`hooks/inject.py`)
Injects unacknowledged items at or above `JUDGE_INJECT_MIN_SEVERITY` from the last `JUDGE_INJECT_WINDOW_HOURS`.
Items of `mode=local` findings are skipped unless `JUDGE_INJECT_LOCAL=1` (env or site.env; default `0`); the skip
count goes to `inject.log` only when it changes for the session. `mode=frontier-claims` findings
(`findings/<id>.claims.json`) are injected like `frontier` ones. The footer names
`judge-ack --agent <request-id> <item-id> "<reason>"` and says a HIGH item stays open until the human reviews it.

## Re-judging (`runner/rejudge.py`)
`rejudge.py <request-id>... --out DIR [--mode local|frontier|frontier-claims] [--model X] [--no-budget] [--sensitive-local]` judges existing bundles
again with the current prompt and validator, via `run_judge.judge_bundle(request_id, request, evidence_dir, mode,
notes, *, probes_allowed, use_budget=True)` (returns `{finding, raw_record, input, mode, model, notes}`, raises
`JudgeError`).
- Reads the request from queue/ or done/ and `evidence/<id>/`; never collects (a missing bundle is an error) and
  never runs probes (`PROBES ALLOWED: no`).
- Never writes to queue/, done/, findings/, acks/ or evidence/; an `--out` inside any of them is refused (64).
- Writes `DIR/<id>.json` (with notes), `<id>.md`, `<id>.raw.txt`, `<id>.input.txt` and `DIR/summary.json`, and
  prints old (`findings/<id>.json`) vs new: mode, items, high+false count.
- A `sensitive` bundle is never judged by the frontier. Without `--mode` it is judged locally. With an explicit
  `--mode frontier` that request is refused: no backend call, no `<id>.*` files, its summary row gets
  `"refused": true` and an `error` naming the data class and `--sensitive-local`, the table prints `REFUSED`, and
  the exit code is 1. `--sensitive-local` judges it locally instead, with a note (`summary.json` records
  `"sensitive_local"`).
- `--mode frontier-claims` runs only the frontier claims stage (`run_judge.judge_claims`) on any bundle,
  sensitive included: `<id>.input.txt` is exactly the claims-only message, items are `FC`-prefixed, the "old"
  column compares with `findings/<id>.claims.json`. A bundle the self-check refuses gets a `"refused": true` row
  and no model call (exit 1). The refusal of plain `--mode frontier` on sensitive bundles is unchanged.
- `--model` sets `JUDGE_LOCAL_MODEL` or `JUDGE_FRONTIER_MODEL` by `--mode` (both without `--mode`).
- Frontier calls count in `usage.json` against `JUDGE_FRONTIER_DAILY_MAX` unless `--no-budget`.
- Exit 0 ok, 1 when any request failed, 64 usage.

## site.env variables (add to site.env.example under `# --- judge`)
`JUDGE_MODE=frontier` (frontier|local), `JUDGE_LOCAL_MODEL=big`, `JUDGE_FRONTIER_CMD=claude`,
`JUDGE_SSH_ALIASES="edge-alias"` (ssh aliases that reach the edge), `EDGE_SSH_USER=ubuntu`,
`EDGE_SSH_KEY=~/.ssh/edge.pem`, `JUDGE_RUNAWAY_TOKENS=24000`, `JUDGE_RUNAWAY_MINUTES=10`,
`JUDGE_WINDOW_GRACE_SECONDS=10` (evidence window end = request `created` + this, capped at the next turn start − 1 s),
`JUDGE_PLAN_DEBOUNCE_S=120` (a plan request waits for the turn end or this many seconds without a plan write; #34),
`JUDGE_NOISE_GLOBS=""` (extra noise globs), `JUDGE_LOG_NOISE_LOGGERS=""` (extra untagged Hermes loggers dropped from `hermes-log.txt` context; `name` or `prefix.*`), `JUDGE_SNAPSHOT_MAX_FILES=2000`, `JUDGE_SNAPSHOT_MAX_BYTES=1048576`, `JUDGE_HOST_PROBES=1` (0 disables host-state probes), `JUDGE_LOCAL_MAX_SEVERITY=medium`, `JUDGE_INJECT_LOCAL=0`,
`JUDGE_SENSITIVE_FRONTIER_CLAIMS=1` (0 = no frontier claims stage for sensitive completions),
`JUDGE_REVIEW_TEXT_ONLY=1`, `JUDGE_REVIEW_TEXT_ONLY_MIN_CHARS=200`, `JUDGE_REVIEW_TEXT_ONLY_REQUIRE_CLAIMS=1` (#29),
`JUDGE_LOCAL_MAX_TOKENS=4096`, `JUDGE_LOCAL_RETRY_MAX_TOKENS` (default 2 × `JUDGE_LOCAL_MAX_TOKENS`; #30).
Environment only (not read from site.env): `JUDGE_ACK_AGENT_ENV` (extra agent-marker env names for judge-ack).
Existing vars used: `BACKEND_SSH_USER`, `BACKEND_LAN_IP`, `BACKEND_WG_IP`, `EDGE_PUBLIC_IP`, `EDGE_WG_IP`,
`SPARK_DOMAIN`, `SPARK_*_HOST`, `SPARK_API_HOST`.
