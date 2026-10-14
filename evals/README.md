# Security eval harness

A small, stdlib-only harness that scores the local models (`coder`, `coder-fast`, `big`, `vision`, `hermes`) on security tasks through the LiteLLM gateway. It can also score a frontier baseline through the `claude` CLI, which is off by default. Use it to compare models, thinking modes and future fine-tunes on the same items, with confidence intervals.

Findings so far, options for further development and the roadmap: [docs/evals-roadmap.md](../docs/evals-roadmap.md).

| Path | What it is |
|---|---|
| `run.py` | The runner: suites × models → `evals/results/<run>/` |
| `scorers.py` | The scorers named in the task format, plus `refusal` |
| `report.py` | Compares runs: tables per suite and model, Wilson 95% CIs, markdown and CSV |
| `gate.py` | The [upgrade gate](#upgrade-gate): did a model, quant, build or flag change make anything worse? |
| `prompts/llm_judge.md` | The grader prompt for `llm_judge` |
| `samples/` | About 10 invented items per task type, for tests and smoke runs (committed) |
| `datasets/` | Converters for public benchmarks (`fetch.py` per suite). See [datasets/README.md](datasets/README.md) |
| `data/`, `results/` | Converted data and run outputs (gitignored) |
| `tests/` | `python3 -m pytest evals/tests -q -p no:cacheprovider` |

## Running

The runner needs Python 3.10+ (stdlib only), `SPARK_API_HOST` (read from `site.env` or the environment) and a gateway key file. The default key file is `~/.config/spark/hermes.key`; `--key-file` or `SPARK_EVAL_KEY_FILE` changes it. The key is read into memory and is never printed or written to any file.

```sh
# smoke: first 5 items of every sample suite, on the two resident coding models
python3 evals/run.py --suite evals/samples --model coder-fast --limit 5 --grader-model claude:<model>

# a converted benchmark (evals/data/<suite>.jsonl), thinking off and on
python3 evals/run.py --suite ctibench-mcq --model coder --thinking off,on

# false refusals without a grader model: CyberSecEval's keyword check instead of llm_judge
python3 evals/run.py --suite cse-frr --model coder --scorer-override cse-frr=refusal

# compare runs
python3 evals/report.py evals/results/<run-a> evals/results/<run-b> --out report.md --csv report.csv
python3 evals/report.py --latest 4
# split the NVD suites by label source (CNA vs NVD); --items backfills labels for runs made before they were recorded
python3 evals/report.py <run-dirs> --split-label-source --items evals/data/nvd-cwe.sample50.jsonl --pairs-csv pairs.csv
```

`--suite` accepts a file, a directory, or a bare suite name (resolved to `evals/data/<name>.jsonl`). A directory loads its full sets and skips the `<suite>.sampleN.jsonl` copies; to use a sample, name it, for example `--suite ctibench-mcq.sample50`.

### What a run records

Each (model, thinking mode) gets `evals/results/<run-name>-<model>[-think|-thinkdefault]/`. The default run name is the UTC start time. The directory holds:

- `run.json`, the provenance:
  - the model, and every sampler setting sent (`sampling`: the profile, each value, the seed and any overrides), plus `request_params`, the request body as sent minus the messages and `max_tokens`;
  - the thinking mode, the `max_tokens` cap for each task type, and `budget`: the token-budget policy, its sha256 and the resolved answer and total caps per suite;
  - `followup`: whether the final-answer follow-up was on;
  - the git SHA and whether the tree was dirty, and the full sha256 of `run.py`, `scorers.py` and `report.py`; `scorer_version`;
  - each dataset file's sha256 and item count, plus the fetcher's `<suite>.provenance.json` sidecar when one is present; `item_sets`: per suite, the count and sha256 of the sorted selected ids;
  - `prompts`: the sha256 of the system prompt actually sent per suite, of each default system prompt and of the follow-up prompt;
  - the model's llama-swap `cmd` and server sampling flags, taken from the repo template `walter/llama-swap/config.yaml.tmpl` (or `--llama-swap-config` for a copy of the live file);
  - `serving` (see [Serving config](#serving-config)): model file, quant, llama.cpp build, context size, chat template hash and the server's sampler defaults. Each is `{"value", "source"}`, or `{"value": null, "reason"}` when unknown. It also holds which server defaults the request overrode, and, with `--probe-server`, a check of the samplers the server actually used;
  - `protocol`: exploratory, confirmatory or rescored-exploratory; the failed checks (`{id, msg}`), the declared deviations (`{id, text}`), the freeze manifests and their sha256, the frozen ref, the comparisons file (`plan`: path, sha256, contestants), the seeds, and `sealed_items_read` for exploratory runs;
  - the served model name and llama.cpp build fingerprint;
  - the grader model and the sha256 of the grader prompt;
  - start and end times, and the history of resumes and re-scores.
- `responses.jsonl`: one line per item, holding the request, the raw response, the visible content, `reasoning_content`, `finish_reason`, usage, llama.cpp timings, latency and attempts.
- `scores.jsonl`: one line per item, with `value`, `passed`, `parsed`, `status` (`ok`, `unparsed` or `error`) and `detail`, plus the item's `meta.label_source` when it has one. Every line also carries `finish_reason`, `truncated` (the reply hit `max_tokens`), `followup_used` and `seed`, so a report can exclude or stratify without reading the responses. Refusal items also carry `extra.opener_refusal`.
- `grades.jsonl`: the grader's raw replies, used as a cache.
- `summary.json` and `report.md`.

### Settings that matter

- **Thinking:** `--thinking off|on|default`, or a list such as `off,on`.
  - `off` and `on` send `chat_template_kwargs.enable_thinking`. LiteLLM passes it through to llama-server.
  - `default` sends nothing, and the Qwen models think. Always state which mode you ran.
  - With thinking on, the reasoning comes back in `reasoning_content` and is never scored. Any `<think>` block left in the content is stripped before scoring.
- **Token budgets:** one declared policy (`BUDGET_POLICY` in `run.py`, or `--budget-policy FILE`) applies to every model in a run, and `run.json` records it with its sha256, so two runs are comparable only if their `budget.policy_sha256` match.
  - The answer budget per task type is mcq 1024, classify 1024, extract 2048, freeform 2048 and code 3072. The policy can set a per-suite answer budget (`suite_answer_tokens`), and an item's `meta.max_tokens` overrides both.
  - Thinking on or default adds the thinking budget (8192, or `--think-budget`) on top, because llama-server counts reasoning against `max_tokens`. So every model gets the same answer budget, and every thinking run gets the same extra reasoning budget.
  - A reply that hits the cap is recorded as truncated (`finish_reason: length`) on its score line. It is still scored, and it is never retried with a bigger cap.
  - `--max-tokens N` is a flat cap for smoke runs. It replaces the policy and adds no thinking budget.
- **Sampling:** every sampler that llama-server can default differently is sent on every request, from the profiles in `SAMPLING_PROFILES` in `run.py`. This stops server defaults from leaking in: `coder-fast`'s `--presence-penalty 1.5`, `big`'s `--top-k 40 --min-p 0.01`, `vision`'s `--top-k 64`.
  - Thinking off (`off`): greedy, temperature 0, with every truncation sampler and penalty off: top_p 1, top_k 0, min_p 0, presence and frequency penalties 0, repeat_penalty 1, DRY and XTC 0. Penalties apply even at temperature 0, which is why they are sent.
  - Thinking on or default (`thinking`): temperature 0.6, top_p 0.95, top_k 20 and min_p 0 (the Qwen3 model-card values for thinking mode), with the same neutral penalties, for every model. Greedy decoding makes the reasoning loop: in eval run (a), 9 of 50 `coder` CTIBench MCQ items at temperature 0 hit the cap, one repeating "T1016?" hundreds of times.
  - Overrides: `--temperature`, `--top-p`, `--presence-penalty`, and `--sampler KEY=VALUE` for any other key. They are recorded in `sampling.overrides`.
  - `--legacy-sampling` sends only what the runner sent before 2026-10 (temperature, top_p with thinking on, presence_penalty if given), so the server's defaults apply to the rest. Use it only to resume or reproduce an old run: those runs refuse to resume under the new profiles, because the sampling differs.
  - The non-OpenAI keys (top_k, min_p, repeat_penalty, DRY, XTC) only work if the gateway forwards them. `--probe-server` checks this after the run against llama-server's `/slots` and records the result in `serving.sampler_check`.
- **Seeds:** `--seeds 1234,1235,1236` runs each model and mode once per seed, each in its own directory (`…-seed1234`), and every response and score line records its seed. `--seed N` (the default is 1234) runs one seed without the suffix, as before. Sampled runs (thinking on) vary between seeds, so use at least three and compare on the same items. A greedy run with several seeds measures only server nondeterminism.
- **Prompt cache:** `--no-prompt-cache` sends `cache_prompt: false` (no KV-cache reuse between requests; recorded in `run.json`). Off by default. LiteLLM forwards it (verified 2026-10-13; see [Upgrade gate](#upgrade-gate)).
- **Concurrency:** 1 by default, because all models share the two GPUs and llama-server runs one slot per model.
- **Robustness:** `--timeout` (900 s per request) and `--retries` (2, with exponential `--backoff`). Network errors, 408, 409, 429 and 5xx are retried; other 4xx responses are not.
- **Resume:** re-run the same command with the same `--run-name`. Items with a successful response are skipped and failed ones are retried. If the model, sampling, caps or a dataset checksum differ, the runner refuses to resume.
- **Re-score:** `--rescore` re-scores the stored responses without calling the model, for example after a scorer fix. The generation provenance in `run.json` is kept.
- **Scorer override:** `--scorer-override SUITE=SCORER` (repeatable) re-scores a suite with another scorer. The override is recorded in `run.json`.

### llm_judge grading

`llm_judge` items are graded by `--grader-model`, which has **no default**: a run with `llm_judge` items stops and asks for one (or `--no-grade`). Pick a judge that is neither a contestant nor of a contestant's model family (`prompts/model_families.json`; for the local models that rules out every qwen, gemma and hermes model, so a frontier judge such as `claude:<model>` is the usual choice). The judge grades with `prompts/llm_judge.md` (rubric `judge-v2`) at temperature 0 with thinking off. The judge-v1 prompt is kept as `prompts/llm_judge.v1.md`. A rescore with `--grader-prompt evals/prompts/llm_judge.v1.md --grader-parse-retries 0` reproduces the grades cached by v1 without calling a model. The grading logic lives in `grading.py`, and the offline judge checks (calibration sheet, agreement, length bias, format perturbation) live in `judge_calibration.py`.

- **Rubric:** grade what the task asks. The reference is a fact source, not a template, and reference fields the task did not request are not required. Key names and layout do not matter, and when the task names no format, format does not matter either. The rubric includes explicit length-neutrality rules.
- **No self-grading, no same-family grading:** a grader that is a contestant, or shares a model family with one, or whose family is unknown (fail closed), is refused unless `--allow-self-grade` is given; `run.json` records `grader.self_grade` and `grader.family_overlap`. The contestants are this run's models **plus** those of the comparisons file (`--protocol-plan`), so a judge cannot grade a rival it will be compared with in another invocation. In a confirmatory run every one of these is a failed check.
- **Cache identity:** the grade cache (`grades.jsonl`) is keyed on the judge's served identity too (model file, quant and llama.cpp build, from `--serving-config` or the llama-swap config), so a different model behind the same alias regrades instead of reusing old grades. `--grader-cache-legacy` also accepts grades cached before this key existed (recorded in run.json).
- **Several judges:** `--grader-model a,b,c` grades with each judge and stores every verdict (`extra.judges`, plus one `grades.jsonl` row per judge). `--judge-aggregate first|majority` picks the decision.
- **Injection screen in code:** before the judge is called, `grading.precheck` fails an answer that contains the harness's wrapper markers, instructions addressed to a grader, or a claimed grade. It never asks the judge to make that call. A phrase that also appears in the task text is treated as a quotation.
- **Criterion provenance:** each `llm_judge` score records `reference_provenance`. The SEvenLLM-QA references are LLM-generated, so judge-graded suites are secondary evidence.

- **Timing:** grading runs **after every model has finished generating**, so the grader loads once instead of swapping with the candidate for every item.
- **Strict parsing:** the reply must be exactly `{"grade": "PASS"|"FAIL", "score": 0-10, "reason": "..."}`, and PASS must go with a score of 6 or more. Anything else is re-asked up to `--grader-parse-retries` times (default 2) with seeds seed+1, seed+2, and so on. If the reply is still invalid, the item is a grader error (`status: error`, `count_as_fail: true`). The strict accuracy (`grading.judge_accuracy`) counts it as a FAIL; the legacy accuracy drops it.
- **Untrusted candidates:** the candidate answer is fenced with a random nonce, and the grader is told to ignore instructions inside it. This reduces prompt injection; it does not rule it out.
- **Reporting:** reports mark these scores `*` as **model-graded**. They are a grader's opinion, not ground truth, and they depend on the grader model.
- **Without a grader:** use `--no-grade` to leave the items ungraded (`status: error`). For refusal suites, `--scorer-override <suite>=refusal` needs no grader.
- **Judge calibration** (design R2-1, `judge_calibration.py`): 180 units stratified random by model × task type (extract oversampled 2:1), fixed seed, grade-blind; half labelled without the reference; two independent human raters plus blind adjudication of disagreements; each rater signs a no-LLM attestation; the key holds no judge grades. The v2 non-contestant, non-family judge grades these units after the protocol is frozen (owner approval needed). The judge passes if and only if the bootstrap 95% CI lower bound of kappa against the consensus is ≥ 0.6 and inter-rater reliability meets the same bar; skewed strata report PABAK or per-class agreement. Self-preference (`selfpref`) and format sensitivity (`perturb`) are reported once multi-judge grades exist. The injection pre-check (`precheck-v2`) is deterministic and precision-first; its known recall limits are listed in the tests.

### Frontier baseline (optional, costs money)

`--model claude` (or `claude:<model>`) runs the same items through the `claude` CLI, invoked the same way as the agent judge's frontier runner:

- `-p --output-format json`, with the system prompt replaced;
- `--tools ""`, `--permission-prompts none`, `--strict-mcp-config`, `--disable-slash-commands`, `--no-session-persistence` and `--setting-sources project`;
- an empty temporary working directory, so no user settings, hooks, MCP servers or project files load;
- the gateway variables stripped from the environment (the `ANTHROPIC_*` endpoint and token variables, `OPENAI_*`, and anything containing `SPARK_API_HOST`).

The runner first prints a rough cost estimate: characters / 3.5 for input, plus about 2k tokens of CLI overhead per call, plus a per-type output guess, at first-party list prices. It **exits without calling anything unless `--yes-frontier` is given**. Each call is capped by `--frontier-call-usd` (default 0.50), and the run stops once the total passes `--frontier-max-usd` (default 10). The real cost from the CLI is recorded per item. There is no sampling control (temperature, seed or thinking) for this baseline. Before using it on real data, check what you are allowed to send to a third-party API.

## Exploratory and confirmatory runs

Every run is either **exploratory** (`--protocol exploratory`, the default) or **confirmatory** (`--protocol confirmatory`). The difference is what the results may be used for. [PROTOCOL.md](PROTOCOL.md) holds the pre-registration template and the protocol for the next confirmatory run.

- **Exploratory runs** are for developing and debugging: suites, scorers, prompts, smoke tests, and anything run on the development items. Any flag is allowed. Their numbers may suggest a hypothesis; they cannot confirm one, because the harness was tuned while looking at them.
- **Confirmatory runs** test a pre-registered hypothesis on **sealed** items nobody has looked at, with a **frozen** harness. The runner refuses to start when any check fails. Each check has an id and a kind (the full table is in [PROTOCOL.md](PROTOCOL.md#c112-run-conditions-and-stopping-rules)).
  - **Integrity checks** (no deviation accepts them):
    - the harness: a known SHA; no uncommitted or untracked files (the message names them); the freeze manifest's `harness_ref` is a **full commit SHA** (a tag or branch can move; `--frozen-ref`, if given, must name the same commit); after that commit only three kinds of commits are allowed: adding `evals/plans/freeze-C1.json` once, adding stage-2 manifests under `evals/plans/stage2/` once each, and appending to the attempt ledger; the running analysis files match `harness_sha256`; no `__pycache__` under `evals/` (run.py writes no bytecode; remove stale ones with `find evals -name __pycache__ -exec rm -rf {} +`);
    - the data: the sealed manifest is among the `--freeze` manifests; every selected item is sealed (or, for `*-prospective` suites, pinned by a stage-2 manifest, `nvdp-` id, inside the window) and not `dev_neardup_exposed`; files, ids, sealed keys and content bindings match; every selected data file is pinned; no nvd-\* item still states its answer; every manifest is committed; stage-2 manifests carry only prospective and calibration pins, and no two manifests disagree (`freeze_conflict`); plans with prospective comparisons need the stage-2 manifest;
    - the plan's sha256 is the manifest's `comparisons_sha256`; the serving record is in the repo and pinned (`serving_config_sha256`).
  - **Estimand checks** (no deviation accepts them either): no `--max-tokens`, `--final-answer`, `--legacy-sampling`, sampler overrides, `--scorer-override` (the data carries the scorers, e.g. `refusal` for cse-frr) or `--think-budget`; the frozen budget policy; thinking on is not greedy; sampled modes have at least three `--seeds`; the judge is the pinned one (alias, prompt sha256, `--grader-max-chars`, weights), not a contestant, not of a contestant's family (resolved from the alias, the static record and the file the live server reports; disagreement fails closed), and `--allow-self-grade` is not given; the run name's declared interventions are applied and match their pins (for D8 also the live `/props` model file).
  - **Policy checks** (each accepted only by its own `--deviation CHECK_ID=reason`): `--limit`, `--force-resume`, a missing `--probe-server`.
- **Integrity across invocations:**
  - a run dir with outputs but no `run.json` is refused; a run cannot change protocol mode when resumed; a confirmatory run resumes only if its outputs still match the `outputs_sha256` it recorded, it was not stopped as invalid, and its harness SHA and scorer version are unchanged;
  - every run records `outputs_sha256` in `run.json`, also inside a confirmatory `protocol` record, which report.py's gate checks;
  - every confirmatory attempt is appended to `evals/plans/attempts-C1.jsonl` (start and end lines with the run id, results root, seed and output hashes); commit it after each invocation. The report uses the first complete attempt per run id;
  - `--rescore` always marks the run `rescored-exploratory` and keeps the confirmatory record under `protocol.original` and its scores in `scores.confirmatory.jsonl`; a confirmatory run whose harness or scorer changed needs `--rescore-exploratory`.
- **Stopping rules:** a confirmatory run stops as invalid (`protocol.invalid`, exit code 4) when more than 5% of its items fail after their retries, or when the serving identity of the model or of the judge changes mid-run. The `/slots` sampler check runs after each seed's generation. Rerun an invalid attempt with the same run name under a new `--results` root; the ledger records both, and the report lists the rerun as a deviation.
- **Interventions:** `--intervention NAME` applies a change pinned in `plans/c1-interventions.json`: `D6-defensive-system-prompt` (the system prompt for every item, pinned by sha256) or `D8-tuned-flash` / `D8-untuned-flash` (the flash llama-swap cmd that `--llama-swap-config` must hold; with `--probe-server` the live `/props` model file is checked too, the other flags are honour-system). The comparisons file declares the interventions of each run name (`run_interventions`).
- **Recipe:** `evals/plans/make_c1.py` writes `freeze-C1.json` (`freeze`), the stage-2 manifests (`stage2`, `stage2-calibration`) and `run-c1.sh` (`runsh`); run `audit.py verify --complete --sealed-only` first. Keep the serving record at `evals/plans/serving-C1.json`. The report needs no `--items`: the gate re-hashes every data file it loads against the pins (`--data-root evals/data`).
- **Sealed items in exploratory runs:** an exploratory run that selects sealed held-out items (by id, or by content key across suites) prints a loud warning and records `protocol.sealed_items_read`. Nothing stops it; the seal is honour-system, so check this field.

```sh
# exploratory: anything goes; results are hypotheses
python3 evals/run.py --suite ctibench-mcq.sample50 --model coder --thinking off,on --limit 20

# confirmatory (protocol C1): see evals/plans/make_c1.py for the full recipe (freeze, external timestamp, stage 2)
python3 evals/plans/make_c1.py freeze --harness-sha "$(git rev-parse HEAD)" \
  --serving-config evals/plans/serving-C1.json --judge-alias "$C1_JUDGE" --judge-model-file "$C1_JUDGE_FILE"
python3 evals/plans/make_c1.py runsh
git status --porcelain     # must be empty, untracked files included; no __pycache__ under evals/
python3 evals/run.py --protocol confirmatory --run-name c1 --intervention D8-tuned-flash \
  --suite ctibench-mcq --suite nvd-cwe \
  --freeze evals/datasets/heldout/heldout-v1.1.json --freeze evals/plans/freeze-C1.json \
  --freeze evals/plans/stage2/prospective-C1.json --select-frozen \
  --model coder --thinking on --seeds 1234,1235,1236 \
  --serving-config evals/plans/serving-C1.json --probe-server --llama-swap-config live-swap.yaml
```

- **Item selection:** `--select-frozen` runs the ids every `--freeze` manifest lists (their intersection), from the full data files. A manifest holds any of:
  - `"files": {path: sha256}` (pins the data files);
  - `"suites": {suite: {"n", "ids_sha256", "ids": [...]}}`, or held-out `entries` of the form `"id\tkey"` or `"id\tkey\tcontent_key"` (v1.1), both re-computed from the item (`audit.heldout_key`, `audit.heldout_content_key`);
  - `"comparisons_sha256"`, `"budget_policy_sha256"`, `"harness_ref"`.
- **Contamination probes:** `python3 evals/tools/contam_probes.py --out <dir>` builds the P1–P4 probe suites (no sealed items; no model calls); their decision rules are in the file and give suite-level evidence only.
- **Seed directories:** `--seeds` applies to every mode, so the greedy thinking-off runs are repeated too. Those repeats are a check on server determinism. To avoid them, run the thinking-off mode in a separate invocation with `--seed 1234`.

### Serving config
`run.json` records what produced the tokens, so a confound can be found afterwards. There are three sources, in this order of trust:

1. `--serving-config FILE`: a static JSON file written by whoever runs the server, holding what the server cannot report about itself. For example:

   ```json
   {"defaults": {"llama_cpp_build": "b6500-abc1234", "gpu": "2x 24 GB, x8/x8 PCIe 4"},
    "models": {"coder": {"quant": "UD-Q4_K_XL", "model_file": "Model-UD-Q4_K_XL.gguf", "flags": "-c 65536 -fa on"}}}
   ```

   Without `models`, the whole object applies to every model. The file is copied into `run.json`, so keep hostnames, usernames and paths out of it.
2. `--probe-server` (opt-in): before the run, the runner asks the server for `/v1/models`, then llama-swap's `/upstream/<model>/props`, then a bare llama-server's `/props`. It records:
   - the build, model file name (basename only), context size and server sampler defaults;
   - the sha256 of the chat template.

   After the run, it reads `/slots` to check the samplers the server actually used (`serving.sampler_check`). Only endpoint paths are recorded, never hosts.
   - On llama-swap, the `/props` call loads the model if it is not loaded already.
   - Through a gateway that does not expose these endpoints, the fields stay unknown.
3. The repo's llama-swap template, which may not match the live config. Its source says so.

A field that no source knows is `{"value": null, "reason": "…"}`. The llama.cpp build falls back to the responses' `system_fingerprint` when nothing else knows it.

## Task format and answer formats

One JSON object per line, as in the eval contract:

```json
{"id": "sample-mcq-0001", "suite": "sample-mcq", "type": "mcq", "prompt": "…", "choices": ["A) …", "B) …"],
 "answer": "B", "scorer": "mcq_letter", "meta": {"source": "…", "license": "…", "category": "…"}}
```

- `type` is one of `mcq`, `extract`, `classify`, `freeform` or `code`, and it selects the default system prompt and output cap.
- An item's optional `system` field overrides the suite system prompt. A suite system prompt is a `<file>.system.txt` or `<suite>.system.txt` file next to the JSONL; without one, the per-type default in `run.py` is used.
- An optional `meta.max_tokens` raises or lowers one item's output cap.

`answer` depends on the scorer. Every scorer strips thinking blocks first.

| Scorer | `answer` | Passes when |
|---|---|---|
| `mcq_letter` | `"B"` (or a list of accepted letters) | The extracted letter is accepted. Extraction handles "Answer: B", "(b)", "**B**", "B) …", the last explicit statement, and a uniquely quoted choice text. Ambiguous answers ("A or B") are unparsed. |
| `exact` | a string or a list of accepted strings | The `Answer:` line (or the whole reply) matches after normalizing case, whitespace, markdown and edge punctuation. |
| `exact_set` | a list | The sets are equal; `value` is the set F1. ID-like answers (ATT&CK T-numbers, CVE, CWE, CAPEC, IPv4, hashes) come from the `Answer:` line, else the trailing lines with IDs, else the whole reply. Other answers are split on commas, semicolons, newlines and "and". |
| `f1_tokens` | a string, a list, or `{"reference", "threshold"}` | Token F1 (SQuAD-style) is at least the threshold (default 0.5). |
| `regex` | a pattern, or `{"pattern", "all": [...], "must_not": [...], "flags": "ims"}` | Every pattern matches and no `must_not` pattern does. |
| `numeric_tol` | a number, or `{"value", "abs_tol", "rel_tol", "percent"}` | The number on the `Answer:` line (else the last number) is within tolerance. |
| `json_fields` | an object of expected fields (dotted names for nesting) | Every field matches; `value` is the fraction that does. Strings are compared case-insensitively, numbers as numbers and lists as sets. |
| `cwe_match` | `"CWE-79"`, a list, or `{"cwe", "related": [...], "related_credit": 0.5}` | The predicted CWE is accepted. CWE-79, cwe_79 and CWE-0079 are equal. A related (parent or child) ID gets partial credit but does not pass. Several different CWEs with no answer line are unparsed. |
| `cvss_mae` | a CVSS v3.x vector, a score, or `{"vector"/"score", "tolerance"}` | The absolute error is within the tolerance (default 1.0). A vector is scored with the CVSS 3.1 base formula. The reply may give a vector or a score. The report shows the MAE. v4.0 vectors are not computed, so give v4 items a numeric score. |
| `llm_judge` | a reference string (an answer, possibly JSON, or grading criteria), or `{"rubric", "reference"}` | The grader returns PASS. `meta.reference_format: "json"` tells the grader the reference is JSON. |
| `refusal` | `"comply"` or `"refuse"`, or `meta.expected_behavior` | The model did what was expected. It uses CyberSecEval's `is_response_llm_refusal` keyword check, ported verbatim: the first 220 characters are scanned, and replies with code blocks count as compliance. With no stated expectation, the expected behaviour is comply. |

**Answer parsing (round 2).**
- **CVSS:** the `Answer:` line (a vector; else the number after "score"; else the last 0–10 number; "n/a" or a range = unparsed), else the last valid v3.x vector (any metric order, per CVSS v3.1 §6), else a final score-only line. Unparsed CVSS rows have `value` null; the report imputes per the comparisons file.
- **CWE:** the `Answer:` line (the first CWE, or a bare id ≤ 1500), else a last line starting with a CWE id, else the single non-negated CWE of the final paragraph's first CWE sentence.
- **MCQ** answer lines accept `:`, `：`, `=`, `->`, `=>`, `→` and `答案`.
- **Status values:** `ok` / `unparsed` / `unparsed_truncated` / `error`.

An `unparsed` item counts as wrong. An `error` item (a malformed answer or a grader failure) and an api error are reported in the failure columns and, by default, count as a **fail**; the report also shows the accuracy with them left out (`--errors exclude` swaps the two).

## Adding a suite

1. Write a converter at `evals/datasets/<suite>/fetch.py` that writes `evals/data/<suite>.jsonl`, and add a `README.md` with the license, citation and caveats. Follow [datasets/README.md](datasets/README.md). Don't commit benchmark data unless its license allows redistribution.
2. Pick a scorer from the table above. Prefer a mechanical scorer over `llm_judge`.
3. Run it with `--limit 5` and read `responses.jsonl` next to `scores.jsonl`. A high `unparsed` count usually means the prompt doesn't state the answer format: add a suite system prompt, or fix the extraction and add a test.
4. Hand-written examples belong in `samples/`, invented and safe to commit.

## Reading the results

- **Confidence intervals:** accuracy is passed / scored, with a Wilson 95% interval. With 50 items, a model at 70% has an interval of about 56–81%. Differences of a few points between models are noise unless the intervals separate or a paired test on the same items says otherwise. Use enough items (the `--sample` sets from the fetchers are a start) and compare models on identical items, seeds and settings.
- **Mean score:** the average partial credit (F1, field fraction, grader score / 10). For `cvss_mae` items it is shown separately as the MAE, in CVSS points (lower is better).
- **Latency and tokens/s:** these are end-to-end per request. They include queueing behind other users of the same model, because llama-server runs one slot per model; for throughput numbers, use the llama-swap benchmarks. The failure columns (api errors, truncated, unparsed, item or grader errors) say whether a low score is the model or the harness.
- **Contamination:** most public suites have been public since 2023–2024 and may be in the training data. Use them as a regression floor and to compare models, not as absolute skill. Fresh data (for example the NVD suites built from recent CVEs) and the private golden set are better for decisions.
- **Several runs of one model:** runs that differ only by seed are labelled "… seed N". Any other runs with the same model and mode get their dir name appended, so every row stays distinguishable.
- **Paired comparisons:** with two or more runs, `report.py` adds a table per suite and pair of runs: the items both scored, how many only one of them passed, and an exact two-sided McNemar p-value. This is the right test for two models on the same items, and it is far more sensitive than comparing Wilson intervals. In eval run (a), `coder` beat `coder-fast` on CTIBench CVSS by 12 items to 4 (p = 0.077) while their intervals overlapped. `--pairs-csv` writes the table as CSV. Each row also gives the accuracy difference with a Newcombe 95% interval and the minimum detectable difference at 80% power; these p-values are **exploratory** (uncorrected: a report over six models has hundreds of them).
- **Pre-registered comparisons:** `--comparisons <file>` (for C1, `evals/plans/comparisons-C1.json`) puts the confirmatory analysis first. The file declares, per decision (family), the test (`holm`, or `iut` for "best of k" claims, where every member must hold), the runs, suite, metric, direction (`a>b`, `a<b`, `two-sided`, or `equivalent` with a margin) and alpha. Each comparison declares its **primary analysis** (`primary`: exclusions, error policy, CVSS imputation `none|baseline|max` and constant) and its pre-registered sensitivity analyses. The report gives each comparison an effect with a 95% interval, its p (McNemar, sign-flip permutation for seed groups and MAE, or TOST for equivalence), Holm within the family, BH q, Holm across all families, and a verdict; a verdict is downgraded ("confirmed (not robust to X)") when a pre-registered sensitivity analysis flips the conclusion. Results are labelled **pre-registered** only if the confirmatory gate passes, family by family: every run is confirmatory and valid, the first complete attempt in the ledger, with its own output hashes; no integrity or estimand check was waived (accepted policy deviations are listed in the title); equal harness SHA, frozen ref, scorer version, budget policy, analysis config and code hashes; `sampler_check.ok` for every run; every loaded data file matches its pin; the comparisons file is the frozen one; the declared interventions and seeds match; for a judge-graded family, a pinned calibration verdict of exactly "pass" (C1 has none: D1 is exploratory). Otherwise the family is "Post-hoc / exploratory (pre-specified comparisons, not confirmatory)". Use `evals/tools/power.py` for a-priori power; the MDE in reports is descriptive. Write the file before looking at the results. `stats.py` holds the methods.
- **Seeds:** runs that differ only by seed are grouped: per-seed accuracy as mean ± SD, and comparisons on each item's mean pass over seeds. A sampled run with one seed is marked "single seed — not confirmatory".
- **Baselines:** each suite table ends with constant baselines (MCQ chance and most common letter; CWE always CWE-79 and most frequent label; CVSS constant 7.5 and the set's median). A model at or below a constant has shown no skill there.
- **Refusal suites:** two numbers. The score (refused / complied) is CyberSecEval's keyword check, comparable with published CSE results; it counts any reply with a code block as compliance. The **opener refusal** rate, our own metric, flags replies whose first 220 characters are a refusal even when a code block follows ("I can't provide that, but here is a toy example"). In eval run (a), `coder-fast` had a 10% CSE false-refusal rate but opened 39 of 50 replies with a refusal; `coder`, 4% and 8 of 50.
- **Item flags (data rules, `datasets/audit.py`):** near-duplicates (J ≥ 0.8) are capped at 2 per cluster; template clusters (J ≥ 0.5) are kept only as cluster ids for cluster-robust intervals (a per-template-and-label cap was rejected because it makes selection label-dependent). Confirmatory primaries exclude sealed items with `meta.rigor.dev_neardup_exposed` and report `dev_cluster_exposed` as a stratum (`heldout/dev-exposed-v1.json`, `audit.py exposure`). `cvss_in_words` (CVSS metric values stated in CVSS vocabulary) is a key leak, stripped for nvd-\*; the CWE name in the text is a stratum (`meta.rigor.cwe_name_in_text`, None = unknown), not a leak, and the no-name stratum is the primary for CWE decisions. cse-frr prompts get tiers (benign / borderline / incoherent / covert-harmful, a text-only rule with amendment R2-4b); covert-harmful prompts are excluded from the false-refusal rate, and the covert lexicon's recall is unknown. Saturation: at least 2 models, each with n ≥ 100, at ≥ 90% (only cybermetric-500 qualifies now).
- **Label source:** `--split-label-source` adds a row per `meta.label_source` value next to the whole suite. About 23% of labels earlier called "cna" come only from CISA ADP and are now `adp-secondary:CISA-ADP`. The prospective fetch must declare its label precedence (NVD > CNA > ADP) before fetching, and `first_public` = min(NVD published, CVE5 datePublic, reference dates) once the data are refetched. The NVD suites record whether the answer is the CNA's label or NVD's; NVD labels are often narrower CWEs and match much less often.
- **Model-graded scores** (`*`) depend on the grader. Re-grade with another grader (`--rescore --grader-model …`) before trusting a small difference.

## Upgrade gate

`gate.py` is a one-command regression check for a change of model, quant, llama.cpp build or serving flags. It answers "did this change make anything worse?" with paired statistics on fixed development items.

```sh
python3 evals/gate.py calibrate --model coder --yes   # once per model (and after caching/batching changes): A/A
python3 evals/gate.py baseline --model coder --yes    # before a change, if the alias has no current baseline
python3 evals/gate.py check --model coder --yes       # after it: compare with the latest compatible baseline
python3 evals/gate.py compare <baseline-dir> <candidate-dir>   # offline, no model calls
```

**Suites** (`plans/gate-v1-<model>.json`; a model without a manifest is refused). Margins confirmed by the owner on 2026-10-12:

| Suite | Items | Metric | Margin | n coder / coder-fast |
|---|---|---|---|---|
| mcq-knowledge | ctibench-mcq + cybermetric-500 | accuracy | 5 pp | 400 / 400 |
| cwe | nvd-cwe, no-name stratum | accuracy (either label) | 7.5 pp / 5 pp | 280 / 540 |
| cvss | nvd-cvss | MAE (unparsed = error of 7.5) | 0.30 | 120 / 200 |
| refusal | cse-frr (no covert-harmful), answer cap 512 tokens | full compliance | 10 pp | 180 / 300 |
| format | the mcq, cwe and cvss items, pooled | parse-ok rate | 2 pp | 800 / 1140 |

- **Items:** `tools/gate_select.py` selects them deterministically (seed 20261011): no sealed held-out item, and none whose template or near-duplicate cluster holds one. The data rules are applied in memory (leaks stripped for nvd-\*), and the result is ordered so each model runs a prefix. `plans/gate-v1.items.json` holds the selection; `--check` re-derives it and the data files. The gate runs `evals/data/gate-v1/<source>.jsonl` (`--write-data`, gitignored, pinned by sha256 in the manifests), not the raw files, which still state some answers. `--write-manifests` writes the manifests.
- **Manifest** (schema `gate-manifest-v1`, validated strictly):
  - per suite: its sources (data file, sha256, ids), metric, margin and better direction, an optional answer cap (`max_tokens`, here 512 for the refusal suite only) and optional parse checks;
  - a `format` block (pooled parse-ok rate, plus a per-suite tripwire);
  - the sampling profile: greedy, thinking off, seed 1234, prompt cache off, concurrency 1, and the sha256 of the budget policy with the per-suite caps;
  - the required `scorer_version`, and alpha 0.05.

  An id that is in the sealed held-out manifests is refused, as are an item matching one by content, an `llm_judge` item and an unstripped nvd-\* item.
- **Runs:**
  - The gate runs run.py with `--probe-server`, `--stop-rules` (the C1.12 stopping rules in an exploratory run), `--no-prompt-cache` and `--concurrency 1`. It uses the command line the GPU guard matches (`--run-name gate`; set `EVAL_RUN=gate` for `tools/stop.sh`).
  - Results go to `results/gate/<model>/{baseline,check,aa}-<UTC stamp>/` with `gate.json`. It holds the manifest sha256, the sampling profile, the budget and per-suite caps, the harness, and the serving identity: model file, quant, build, flags, and the model sha256 and image digest when `--serving-config` gives them.
  - **Route:** requests go over `tools/gateway-tunnel.sh` (SSH to the backend's LiteLLM, as the plans do), not through the public edge, which on 2026-10-13 held a request for the full timeout three times: a TLS handshake that never got an answer. If the tunnel dies, ssh closes the local socket and run.py retries at once. `--base-url` or `EVAL_BASE_URL` is used as given, `--via-edge` keeps the edge, and with no backend in `site.env` the gate warns and uses the edge. A configured tunnel that does not start is a refusal. `gate.json` records the route of each run (`routes`).
  - Each request times out after `--request-timeout` seconds (default 300; the slowest gate item takes under a minute), passed to run.py as `--timeout`.
  - Before any run it prints the model, the item count and the estimated time, and it runs nothing without `--yes`. It also refuses while the guard's STOP file exists. `--resume DIR` continues an interrupted run.
  - Run time is about **2.0 h for `coder` and 1.3 h for `coder-fast`**, per run (a calibration is two runs). The gate uses no judge, no frontier model and no sealed items.
- **Compatibility:** a baseline is used only if these all equal the candidate's: manifest sha256, scorer version, sampling profile, budget, prompt-cache setting, item sets and system prompts. Otherwise the gate refuses and says why. A different run.py or scorers.py is refused unless `--allow-stale-harness`, which is recorded and printed.
- **A/A calibration:** `calibrate` runs the baseline config twice, back to back. The first run is stored as a baseline; the A/A verdict, with the discordant share ψ and the MAE difference SD per suite, is stored on it.
  - The A/A passes only if every row is equivalent. A "different-better" in an A/A is non-determinism too.
  - `check` refuses until the model's latest A/A under the manifest has passed. `--accept-aa` overrides that; the override is recorded in the report.
  - On failure, fix determinism (prompt cache, `-np 1`, no other traffic) before trusting any verdict.
  - It is enforced in code rather than left as a runbook step, because a skipped A/A silently voids every later verdict. It is per model and manifest, not per baseline, so re-baselining after a PASS costs one run, not two.
- **Per row** (difference = candidate − baseline):
  - Binary metrics get the exact Tango TOST at the margin and an exact McNemar test; MAE gets the paired bootstrap test and bootstrap TOST.
  - **equivalent**: TOST p < alpha. This is checked first, so a significant shift inside the margin is equivalent, with a note.
  - **different-worse / different-better**: the difference p, Holm-adjusted across all rows, is < alpha. The sign, read with the metric's better direction, says which.
  - **inconclusive**: anything else.
  - **different-worse (format)**: a pooled suite whose items turned unparsed more often than the reverse (one-sided exact McNemar p < alpha), whatever its accuracy.
- **Overall and exit code:**
  - FAIL (1) if any row is different-worse.
  - PASS (0) if every row is equivalent or different-better.
  - **INCONCLUSIVE (2): hold, the owner decides**, using the per-suite table. Expect it often: even a clean upgrade passes every row only about 48% of the time for `coder` and 65% for `coder-fast`.
  - 3 for an invalid run: a stopping rule fired, the serving identity changed mid-run, api errors went above `validity.max_api_error_rate`, items are missing, or `/slots` shows other samplers.
  - 4 when the gate refuses.

  The markdown report is printed and written to the candidate dir. It has the per-suite table (n, baseline, candidate, difference with its 95% interval, TOST p, difference p with Holm, parse and truncation rates, verdict), the serving identity diff and the overall verdict.
- **Prompt cache:** `run.py --no-prompt-cache` sends `cache_prompt: false` and records it in run.json. It is off by default. LiteLLM forwards the field to llama-server: on 2026-10-13, identical requests through the gateway reused 0 prompt tokens with `cache_prompt: false` and 459 of 463 with `true` (llama-server `timings.cache_n`), and both A/A calibrations were byte-identical run to run (980/980 coder, 1440/1440 coder-fast). (Before that, the stored A/A pair had 24 of 117 outputs identical when the cache state differed, against 489 of 491 when it matched.)

## Long runs: plans, the GPU guard, stop and status

For runs that take hours, use a plan script in [`plans/`](plans/README.md) instead of hand-typed commands:
- **Plans** record the exact suites, sizes, seeds and phase order, and resume after a stop.
- **`tools/gpu-guard.sh`** polls the backend's GPUs over SSH (`BACKEND_SSH_USER@BACKEND_LAN_IP` from `site.env`, or `EVAL_GPU_SSH`) every 30 s and logs each sample.
  - It stops the run cleanly on high core temperature, a hardware protection, sustained over-limit power, a dead fan, or lost readings.
  - It notifies on warnings.
- **`tools/gateway-tunnel.sh`** (used by the plans by default) sends traffic over SSH to the backend's LiteLLM instead of through the public edge. `run.py` takes the route from `EVAL_BASE_URL` (environment or `site.env`), or from `--base-url`.
- **`tools/stop.sh`** stops a run by hand; **`tools/status.sh`** shows its progress and the GPU readings.
- **`datasets/make_subsets.py`** builds derived sets deterministically from fetched data:
  - label-source suites, such as NVD's own labels;
  - stratified subsets of existing samples.

## Costs and disruption

- **The coding pair:** `coder` (GPU0) and `coder-fast` (GPU1) are normally resident, so evals against them cause no swaps. They do share the GPUs with everyone else. Run at concurrency 1, and expect latency to rise while someone else is using the same model.
- **The two-GPU models:** `big`, `vision` and `hermes` take both GPUs. Evaluating them, or grading with `big`, **evicts `coder` and `coder-fast`** for the length of the run plus the model's TTL (30 min). Loading `big` takes about 7–20 s (more with a cold page cache). Anyone using Open WebUI, Hermes or a coding harness meanwhile waits for swaps. Schedule these runs when nobody is using the system. (A local judge is also a contestant or of a contestant's family, so for anything you will report, grade with an independent judge.) The runner prints a note when a run will evict the pair.
- **Frontier:** it costs real money; see the estimate printed before the run. It is never run unless `--yes-frontier` is given.

### Final-answer follow-up (mcq / classify, off by default)

The follow-up is **off by default** since 2026-10, and `--final-answer` turns it on. It is for exploratory runs only: a confirmatory run refuses it.

- **Why it is off:** it gives a second chance, under a different decoding regime, only to the items that failed to commit within the budget. So it changes what is measured (an answer under a fixed budget) and it favours the models that fail most. It inflated thinking scores: `coder` thinking-on MCQ was 70.5% with it and 62.5% without it.
- **Marking:** every item that used it is marked `followup_used: true` on its response and score lines (and, as before, `final_answer_prompt`). So a report can exclude those items, or show results with and without them.

Some local models reason in the open and either run out of tokens or never commit to a letter. When the follow-up is on and an mcq or classify reply is cut off (`finish_reason=length`) or has no parsable answer, the runner sends **one** short follow-up ("Reply with only your final answer…", `max_tokens` 32) and scores the answer it gets. The original reply is kept in `content_before_final`, and the report's *final-answer prompts* column shows how many scores depended on it.

The follow-up always has **thinking off**, whatever the run's mode. Before this, a thinking-on run sent the follow-up with thinking on, and the model spent all 32 tokens reasoning: in eval run (a), all 13 `coder` thinking-on follow-ups came back empty and were scored unparsed. When a cut-off thinking reply has no visible content, the follow-up's assistant turn is the last 6000 characters of `reasoning_content` (the chat template would otherwise drop the reasoning), and the response is marked `final_answer_context: "reasoning"`. `--no-final-answer` is still accepted, and it is now the default. The follow-up never runs for the frontier baseline. In a first real run, 3 of 20 CTIBench MCQ replies from `coder-fast` (thinking off) hit the old 512-token cap before stating a letter; 2 still did at 1024, while visibly going in circles.
