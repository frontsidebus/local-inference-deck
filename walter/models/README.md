# walter/models/ — lab models (spark-model)

Lab models are arbitrary GGUF models from allow-listed Hugging Face orgs (abliterated, uncensored, specialised
research models), kept next to the core models on Walter's `/models` disk. Phase 1 ships the lifecycle core:
`spark-model search | fit | pull | ls | rm | review | render | status`, the site-local registry and the renderer.
**Phase 1 never changes what llama-swap serves**: a pulled model is registered with `enabled: false`, and with no
enabled lab model the rendered llama-swap config is byte for byte the repo template. Enabling, load/unload/restore
and the LiteLLM `lab/<id>` entries are Phase 2.

<!-- ======================================== lifecycle (feat/lab-lifecycle) ======================================== -->

## Components

| piece | where | what |
|---|---|---|
| `clients/bin/spark-model` | workstation (`~/.local/bin/spark-model`, a launcher written by `clients/install.sh`) | `search`/`fit` run locally (`lib/cli_fit.py` of the checkout); every other verb runs the agent over SSH |
| `spark-model-agent` | `/usr/local/sbin/spark-model-agent` (root 0755) | the **only writer** of lab state: registry, `gguf/lab/`, `quarantine/`, the rendered config |
| `lib/registry.py` | `/usr/local/lib/spark-model/` | registry schema, atomic save, `render(base, registry)` |
| `lib/fetch.py` | same | pull: plan, download (resume), verify, promote; removal |
| `lib/gguf.py`, `lib/hf.py`, `lib/fit.py`, `lib/cli_fit.py` | same | bounded GGUF header parser, Hub client, fit calculator (see "Fit calculator") |
| `agent.env.tmpl` | `/etc/spark-model/agent.env` (root 0644) | `MODELS_DIR`, `LAB_HF_ORGS`, `LAB_MODELS_QUOTA_GB`, `LAB_FREE_RESERVE_GB`, `LLAMA_SWAP_URL` from `site.env` |
| `sudoers-spark-model.tmpl` | `/etc/sudoers.d/spark-model` (root 0440, `visudo -c` checked) | the operator's one rule (see "Access") |
| `registry.example.yaml` | (repo only) | the schema by example; the real `/models/registry.yaml` is site-local and never committed |

On Walter:

```
/models/registry.yaml              site-local source of truth (root 0644, written atomically by the agent)
/models/quarantine/<id>/           downloads in progress / failed pulls (root 0700; never mounted)
/models/gguf/lab/<id>/<file>.gguf  promoted lab files (dirs 0755, files 0444, root): inside the read-only model mount
/etc/llama-swap/config.base.yaml   the repo template, rendered by deploy.sh (root:llamaswap 0640)
/etc/llama-swap/config.yaml        base + enabled lab models + their matrix sets (what llama-swap reads)
/etc/llama-swap/config.yaml.prev   the previous config.yaml, kept by every change
/etc/spark-hf/token                optional Hugging Face read token (root 0400)
```

Files of a model are stored flat in `lab/<id>/` under their base names (the Hub path is kept in the registry), so a
split GGUF's shards sit together and `-m` points at shard 1.

## Access: one sudoers line

`/etc/sudoers.d/spark-model`:

```
Defaults!/usr/local/sbin/spark-model-agent env_reset, !setenv
${BACKEND_SSH_USER} ALL=(root) NOPASSWD: /usr/local/sbin/spark-model-agent
```

The workstation runs `ssh -o BatchMode=yes $BACKEND_SSH_USER@$BACKEND_LAN_IP sudo -n /usr/local/sbin/spark-model-agent <verb> --json ...`.
Why a sudoers line and not an SSH forced command: the operator already logs in with its normal key, a forced command
would need a second key and an `authorized_keys` entry that this repo does not manage, and the agent must also be
callable on Walter itself (deploy.sh, manual runbooks). The reasoning that makes the line safe:

- the agent is the only writer of lab state; the operator gets exactly this program as root, nothing else;
- `env_reset` (sudo's default) and no `SETENV`: the caller cannot pass `SPARK_MODEL_*` (path overrides for tests and
  deploy.sh) or `PYTHON*` variables. The agent also ignores `SPARK_MODEL_*` whenever sudo started it directly
  (`SUDO_COMMAND`), and runs as `python3 -IB` (isolated: no `PYTHONPATH`, no user site, no script-dir import);
- its argv is validated: ids by `^[a-z0-9][a-z0-9.-]{0,38}[a-z0-9]$`, repos by `org/name[@rev]`, `--file` only as
  a glob matched against the Hub's file list, `--ctx` as a bounded integer; no abbreviations, no path arguments;
- every file operation is relative to a directory fd opened with `O_NOFOLLOW` and checked (owned by root, not group-
  or world-writable; quarantine 0700), because `/models` and `/models/gguf` belong to the operator user;
- the registry is treated as untrusted input (it sits in the operator-owned `/models`): it is opened without
  following symlinks, validated strictly on every load, and nothing in it reaches a command line except through
  the whitelists above, so even a hand-edited registry cannot inject shell, paths or flags;
- pulls are GGUF-only from allow-listed orgs, pinned to a commit, sha256-verified, header-parsed with hard caps.

## Registry (`/models/registry.yaml`)

Schema `spark-model-registry-v1`; [`registry.example.yaml`](registry.example.yaml) shows every field. Validation is
strict (`registry.validate`): unknown keys, bad ids, a revision that is not 40-hex, hashes that are not 64-hex,
non-`.gguf` or `..` paths, duplicate ids or file names, a missing `role: model` file, out-of-range numbers, and
anything not on a whitelist is an error, and every problem is reported at once with its location
(`models[2] (id=foo): sampling.temp must be a number 0.0..5.0`). Defaults: `n_max` = min(8192, ctx), `image`,
`split: layer`, `ttl: 900`, `enabled: false`, `pinned: false`.

- `placement`: `gpu0` | `gpu1` (`-sm none`, one device) | `both` (`split: layer` -> `-sm layer -ts 1,1`;
  `split: tensor` -> `${tp}` + `-sm tensor`). `cpu_experts` (both only): an `-ot "<regex>=CPU"` regex (regex
  characters only), which also switches `${mem}` to `${mem-moe}`.
- `sampling`: `temp top_p top_k min_p typical_p presence_penalty frequency_penalty repeat_penalty`, bounded.
- `extra_args`: whitelist only: `--ctx-checkpoints N`, `--cache-reuse N`, `--spec-type draft-mtp`,
  `--spec-draft-n-max N`, `-devd CUDA0|CUDA1`, `-ngld N`, `--reasoning-format none|auto|deepseek|deepseek-legacy`,
  `--reasoning-budget N`, `--chat-template-file /models/gguf/lab/<id>/<name>.jinja`, `--swa-full`.
- `license: {id, flag, gated}`, `template: {source, sha256, bytes, flags, reviewed}`, `fit: {vram_gb, speed_class,
  placement, warnings}` are written by `pull`. An `enabled` model with a flagged, unreviewed template is invalid.

## Renderer and deploy integration

`registry.render(base_text, registry)` returns the whole llama-swap config:

- **no enabled lab model: the base text, unchanged** (tested against the real template rendered with
  `site.env.example`);
- otherwise the base text with insertions only (no YAML round trip, so comments and formatting stay): one entry per
  enabled model after the last core model, matrix vars after the last core var, matrix sets after the last core
  set. A lab entry is
  `${run} ${mem} --gpus "\"device=N\"" ${image}` / `-m /models/gguf/lab/<id>/<file>` / `${common} -c <ctx> -n <n_max>
  <placement flags>` / sampling / extra args, with `cmdStop: ${stop}`, `proxy`, `ttl`, `unlisted: true`,
  `metadata: {lab: true, class: ...}` and `capabilities`. It goes through the same hardened `${run}` macro and
  read-only `/models/gguf` mount as the core models (a test runs the core hardening checks on lab entries);
- the matrix: lab vars `l1..lN` (registry order, skipping names the base uses) are appended; core vars, evict costs
  and sets are unchanged and stay first, so the core `coding` set keeps winning ties. One set
  `lab-gpu-pairs: "(c | <gpu0 labs>) & (f | <gpu1 labs>)"` covers every GPU0/GPU1 pair (it also contains the core pair,
  harmlessly); each `both` model gets its own set `lab-<id>: "lN"`. No lab model is left in no set.
- the result is re-parsed and checked (`check_config`): every set parses with v260's grammar and names known
  vars/models/sets, every var a known model, no duplicate ids/aliases, every `${macro}` of a lab entry resolves,
  and everything that is not a lab insertion equals the base.

`spark-model-agent render` prints the diff against `/etc/llama-swap/config.yaml`; `render --apply` installs it:
only when the content differs (llama-swap reloads on any mtime change), after `llama-swap -validate` (with a
placeholder `LLAMA_SWAP_API_KEY`, never the real key), atomically (temp + rename) and keeping `config.yaml.prev`.
It refuses while a lab model is in llama-swap's `/running` (`--force` overrides and says so).

**deploy.sh** installs the rendered template as `/etc/llama-swap/config.base.yaml`, then runs the staged agent's
`render --apply` against `$MODELS_DIR/registry.yaml`. Consequences:

- a plain deploy can never drop lab entries: config.yaml is always rendered from base + registry, never copied from
  the template;
- with no registry (or nothing enabled) the result equals the template, which equals today's config.yaml, so the
  agent writes nothing and llama-swap does not reload; a template change re-renders with the lab entries kept;
- an invalid registry fails the deploy and leaves config.yaml as it was (`--dry-run` reports `new` for a config
  that would change, without writing).

## llama-swap v260: what was verified (source at tag v260, commit fcefa7b)

| fact | where |
|---|---|
| The matrix DSL has `&`, `|`, parentheses and `+set` references; identifiers are letters, digits, `_ - .` | `internal/matrix/dsl.go:53-75` (tokens), `:91-93` (ident chars), `:137-198` (or/and/atom grammar) |
| Var keys must match `^[a-zA-Z0-9.-]{1,32}$` and name a known model; evict_costs keys must be known vars/models | `internal/config/matrix.go:11`, `:66-86` |
| Every set identifier must resolve to a var or a model id; `+ref` must name a set | `internal/matrix/program.go:71`, `:76` |
| Set order is kept for tie-breaking (topological, then definition order) | `internal/matrix/doc.go:17-20`, `program.go:186-216` |
| A model in no set can still run, but alone: the solver evicts everything that is running | `internal/matrix/program.go:238-272`, `internal/router/matrix.go:32-33` |
| The matrix is validated when the config loads (a bad set fails the load or reload) | `internal/config/load.go:228-231` |
| `unlisted` (hidden from `/v1/models`) and `metadata` (free-form map, macros expanded) are model keys | `internal/config/model_config.go:123`, `:142`; `internal/server/api.go:199` |
| `-validate` exists: loads the config (and `-config-dir`) and exits 0/1 without serving | `llama-swap.go:107-115`, `:126`, `:139-142` |
| `${env.X}` fails the load when X is unset (hence the placeholder key for -validate) | `internal/config/macros.go:482-484` |
| `--watch-config` polls every 2 s and reloads on any mtime or size change (so identical content is never rewritten) | `internal/watcher/watcher.go:17`, `:75-85` |
| `GET /running` lists running models, behind the API key | `internal/server/server.go:380`, `api.go:348-367` |

The rendered config with lab models (gpu0, gpu1, both with tensor split and CPU experts, mmproj and draft files) also
passes the real v260 binary's `-validate` (`LLAMA_SWAP_BIN=<v260 binary> pytest walter/tests/test_spark_model_registry.py`).

## Pull

`spark-model pull <org/name>[@rev] --id <id> --placement gpu0|gpu1|both [--file GLOB]... [--ctx N] [--class C] [--yes]`

Without `--yes` it prints the plan and writes nothing: files with sizes and sha256, total, quota after, free space,
licence and flag, chat-template size and flags, fit estimate, warnings and refusals. With `--yes`:

1. **allow-list**: the org must be in `LAB_HF_ORGS` (site.env, exact names, space-separated; there is no
   `--allow-org`: the owner edits site.env and redeploys). Empty = every pull is refused.
2. **pin**: the revision is resolved to its 40-hex commit once; every request after that uses the sha.
3. **files**: GGUF only (safetensors/.bin/.pt are never downloaded). Without `--file` the repo must hold exactly one
   model (one file or one shard set); otherwise the plan lists the files to pick from. `--file` globs are matched
   against the Hub's list, never used as paths. Every file needs an LFS sha256 (else refused). Roles: names with
   `mmproj` -> mmproj, names starting `mtp-`/`draft-` -> draft, the rest model; a shard set must be complete.
4. **headers before download**: every model shard's GGUF header is read by HTTP Range and parsed with the bounded
   parser (`gguf.parse_header`, 256 MiB cap); `split.count` must equal the number of shards and `split.no` run 0..n-1.
5. **quota and space**: lab bytes in the registry + this pull <= `LAB_MODELS_QUOTA_GB` (decimal GB, default 1500),
   and free space on `MODELS_DIR` >= pull x 1.05 + `LAB_FREE_RESERVE_GB` (default 300). `rm --suggest` lists what
   could go.
6. **download** into `/models/quarantine/<id>/` (root 0700, never mounted) as `<name>.part`, resuming with HTTP
   Range after a dropped connection (5 retries) or an interrupted pull (re-run the same command); a server that
   ignores Range restarts the file. sha256 and size are checked against the Hub's LFS values; a mismatch keeps the
   file as `<name>.bad`.
7. **verify**: GGUF magic, bounded header parse and "the file is long enough for its tensors" for every file;
   shard count again.
8. **promote**: files 0444 into `gguf/lab/.<id>.incoming/`, which is renamed to `gguf/lab/<id>/` (0755).
9. **register**: the entry is appended with `enabled: false`; `fit` is filled from `fit.estimate` when available.

A failure keeps the quarantine copy and says why; `ls` lists quarantine leftovers and orphan `lab/` dirs.

**HF token** (optional; the allow-listed repos are normally public): a fine-grained read-only token in
`/etc/spark-hf/token`, root 0400: `sudo install -d -m 0700 /etc/spark-hf && sudo install -m 0400 /dev/stdin
/etc/spark-hf/token` (paste, Ctrl-D). It is read by `hf.py` from its path, sent only as an `Authorization` header to
Hub hosts (redirects to CDNs never get it), and never printed, logged or stored in the registry.

**Licence flag** (`license.flag`): `noncommercial` (NC/research terms), `custom` (model-specific licences such as
llama*, gemma, openrail), `other` (licence `other` or none), `gated` (an OSI-style licence behind a gate), else
null. Printed at pull and in `ls`.

**Class**: `--class uncensored|abliterated|specialized|base`; without it the class is taken from the repo name
(`abliterat*` / `uncensor*`) or the pull is refused. `tools_allowed` is false for uncensored/abliterated.

### Chat template flags

The template is `tokenizer.chat_template` from the header; its sha256 and size are recorded. Owner decision 6: only
**flagged** templates need review (`spark-model review <id>` prints it and records `reviewed: true`; Phase 2's
enable refuses flagged + unreviewed). The heuristics are deliberately simple:

| flag | when |
|---|---|
| `missing` | no template in the header |
| `large` | more than 32 KB |
| `large-range` | a `range(N)` with a literal N above 1000 |
| `raise` | `raise_exception` (or `raise`): rendering can be aborted on chosen inputs |
| `dunder` | `__attribute__` access inside a tag (sandbox-escape attempts) |
| `system-text` | literal text outside the messages loop longer than 200 characters, or containing "you are", "always", "never", "ignore", "must" (an injected system prompt) |

## ls, rm, review, render, status

- `ls`: lab entries (placement, class, size, ctx, flags: enabled, pinned, licence, template, missing files), lab
  bytes vs quota, free space, orphan dirs, quarantine leftovers, and the core models (read-only).
- `rm <id>`: plan; `rm <id> --yes`: removes the registry entry, then `gguf/lab/<id>/` (fd-relative, no symlinks
  followed, refuses subdirectories). Only lab entries; core files are never touched. Pinned entries, enabled
  entries and a model running in llama-swap need `--force` (printed). `rm --suggest`: unpinned entries by least
  recent use (`last_used`, else `added`); nothing is deleted.
- `review <id>`: prints the template from the model file (checked against the registry sha256) and records
  `reviewed: true`.
- `render`: the diff; from the workstation it is dry only in Phase 1 (deploy.sh applies).
- `status`: paths, settings, whether config.yaml equals base + registry, lab models in `/running`.

All verbs take `--json` (the client always uses it; the human text is in its `"text"` field).

## Guardrails

- **Live eval/gate runs** (client side): `spark-model rm --yes` refuses while an eval or gate run is live on the
  workstation: a process running `evals/run.py` or `evals/tools/gpu-guard.sh`, or a `*.pid` file under
  `evals/results/_runs/*/` whose process is alive (this checkout plus `SPARK_EVAL_REPOS`, colon-separated).
  `--force` overrides and prints what it overrode. (`render --apply` is not offered by the client in Phase 1.)
- **Server side**: `rm` and `render --apply` refuse while a lab model is in llama-swap's `/running`.
- One registry writer at a time (`flock` on `/run/spark-model-agent.lock`; a pull holds it for its whole
  download); read-only verbs never wait.

## Runbook

First time:

1. `site.env`: set `LAB_HF_ORGS` (exact org names), optionally `LAB_MODELS_QUOTA_GB` / `LAB_FREE_RESERVE_GB`.
2. Deploy Walter (`sudo walter/deploy.sh`): installs the agent, lib, sudoers rule, `agent.env`, `quarantine/` and
   `gguf/lab/`, and renders config.yaml (unchanged: no registry yet).
3. Optional HF token (above). Workstation: `clients/install.sh` (installs the `spark-model` launcher).

A pull:

```bash
spark-model search <words>                                   # fit badges (fit calculator)
spark-model fit someorg/Some-Model-GGUF --file '*Q4_K_M*'     # VRAM / placement / speed class
spark-model pull someorg/Some-Model-GGUF --file '*Q4_K_M*' --id some-model-8b --placement gpu1   # plan
spark-model pull someorg/Some-Model-GGUF --file '*Q4_K_M*' --id some-model-8b --placement gpu1 --yes
spark-model ls
spark-model review some-model-8b                             # only if its template is flagged
```

Verify: `sudo spark-model-agent status` says config in sync; `sha256sum /models/gguf/lab/<id>/*` matches the
registry; `sudo -l -U <operator>` shows only the agent rule from this file.

Troubleshooting: `org ... is not in LAB_HF_ORGS` -> edit site.env, redeploy. `quota:` / `free space:` -> `spark-model
rm --suggest`. `sha256 mismatch` -> the upstream file changed or the transfer was corrupted: delete
`/models/quarantine/<id>/` and pull again. A deploy that fails in `render --apply` -> fix the registry
(`sudo spark-model-agent status` prints every problem), or move it aside to deploy without lab entries.

Rollback: `sudo cp -p /etc/llama-swap/config.yaml.prev /etc/llama-swap/config.yaml` (llama-swap reloads); the
registry is plain YAML (back it up with the rest of `/models`).

Tests: `python3 -B -m pytest -q walter/tests/test_spark_model_{registry,fetch,agent,client}.py walter/tests/test_deploy.py`
(all paths under tmp_path, a fake Hub on 127.0.0.1, no network, no root).

<!-- ====================================== end lifecycle (feat/lab-lifecycle) ====================================== -->
