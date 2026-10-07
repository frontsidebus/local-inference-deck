# Security eval harness

A small, stdlib-only harness that scores the local models (`coder`, `coder-fast`, `big`, `vision`, `hermes`) on security tasks through the LiteLLM gateway. It can also score a frontier baseline through the `claude` CLI, which is off by default. Use it to compare models, thinking modes and future fine-tunes on the same items, with confidence intervals.

| Path | What it is |
|---|---|
| `run.py` | The runner: suites × models → `evals/results/<run>/` |
| `scorers.py` | The scorers named in the task format, plus `refusal` |
| `report.py` | Compares runs: tables per suite and model, Wilson 95% CIs, markdown and CSV |
| `prompts/llm_judge.md` | The grader prompt for `llm_judge` |
| `samples/` | About 10 invented items per task type, for tests and smoke runs (committed) |
| `datasets/` | Converters for public benchmarks (`fetch.py` per suite). See [datasets/README.md](datasets/README.md) |
| `data/`, `results/` | Converted data and run outputs (gitignored) |
| `tests/` | `python3 -m pytest evals/tests -q -p no:cacheprovider` |

## Running

The runner needs Python 3.10+ (stdlib only), `SPARK_API_HOST` (read from `site.env` or the environment) and a gateway key file. The default key file is `~/.config/spark/hermes.key`; `--key-file` or `SPARK_EVAL_KEY_FILE` changes it. The key is read into memory and is never printed or written to any file.

```sh
# smoke: first 5 items of every sample suite, on the two resident coding models
python3 evals/run.py --suite evals/samples --model coder-fast --model coder --limit 5 --grader-model coder

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
  - the model, and the sampling settings sent (temperature and top_p, which default by thinking mode (see below); seed, default 1234; optional presence_penalty);
  - the thinking mode, and the `max_tokens` cap for each task type;
  - the git SHA and whether the tree was dirty;
  - each dataset file's sha256 and item count, plus the fetcher's `<suite>.provenance.json` sidecar when one is present;
  - the model's llama-swap `cmd` and server sampling flags, taken from the repo template `walter/llama-swap/config.yaml.tmpl` (or `--llama-swap-config` for a copy of the live file);
  - the served model name and llama.cpp build fingerprint;
  - the grader model and the sha256 of the grader prompt;
  - start and end times, and the history of resumes and re-scores.
- `responses.jsonl`: one line per item, holding the request, the raw response, the visible content, `reasoning_content`, `finish_reason`, usage, llama.cpp timings, latency and attempts.
- `scores.jsonl`: one line per item, with `value`, `passed`, `parsed`, `status` (`ok`, `unparsed` or `error`) and `detail`, plus the item's `meta.label_source` when it has one. Refusal items also carry `extra.opener_refusal`.
- `grades.jsonl`: the grader's raw replies, used as a cache.
- `summary.json` and `report.md`.

### Settings that matter

- **Thinking:** `--thinking off|on|default`, or a list such as `off,on`.
  - `off` and `on` send `chat_template_kwargs.enable_thinking`. LiteLLM passes it through to llama-server.
  - `default` sends nothing, and the Qwen models think. Always state which mode you ran.
  - With thinking on, the reasoning comes back in `reasoning_content` and is never scored. Any `<think>` block left in the content is stripped before scoring.
- **Output caps:** the `max_tokens` per task type with thinking off is mcq 512, classify 512, extract 2048, freeform 2048 and code 3072. Thinking on or default adds `--think-budget` (8192), because reasoning counts against `max_tokens`. A reply that hits the cap is counted as truncated; it is still scored.
- **Sampling:** defaults depend on the thinking mode.
  - Thinking off: temperature 0 (greedy, repeatable), and top_p unset.
  - Thinking on or default: temperature 0.6 and top_p 0.95, as Qwen recommends. Greedy decoding makes the reasoning loop: in eval run (a), 9 of 50 `coder` CTIBench MCQ items at temperature 0 hit the cap, one repeating "T1016?" hundreds of times. Sampled runs vary, so use several `--seed` values and compare on the same items.
  - `--temperature` and `--top-p` override both defaults.
  - The model's other llama-swap defaults still apply unless you override them. For example, `coder-fast` has `--presence-penalty 1.5`; `run.json` records the server flags.
  - A thinking run made before these defaults (at temperature 0) is not resumed at the new ones: the runner refuses and names the sampling difference. Pass `--temperature 0` to resume it as it was.
- **Concurrency:** 1 by default, because all models share the two GPUs and llama-server runs one slot per model.
- **Robustness:** `--timeout` (900 s per request) and `--retries` (2, with exponential `--backoff`). Network errors, 408, 409, 429 and 5xx are retried; other 4xx responses are not.
- **Resume:** re-run the same command with the same `--run-name`. Items with a successful response are skipped and failed ones are retried. If the model, sampling, caps or a dataset checksum differ, the runner refuses to resume.
- **Re-score:** `--rescore` re-scores the stored responses without calling the model, for example after a scorer fix. The generation provenance in `run.json` is kept.
- **Scorer override:** `--scorer-override SUITE=SCORER` (repeatable) re-scores a suite with another scorer. The override is recorded in `run.json`.

### llm_judge grading

`llm_judge` items are graded by `--grader-model`, which defaults to the local `big`, using `prompts/llm_judge.md` at temperature 0 with thinking off.

- **Timing:** grading runs **after every model has finished generating**, so the grader loads once instead of swapping with the candidate for every item.
- **Strict parsing:** the reply must be exactly `{"grade": "PASS"|"FAIL", "score": 0-10, "reason": "..."}`, and PASS must go with a score of 6 or more. Anything else is a grader error. Grader errors are counted separately and kept out of accuracy.
- **Untrusted candidates:** the candidate answer is fenced with a random nonce, and the grader is told to ignore instructions inside it. This reduces prompt injection; it does not rule it out.
- **Reporting:** reports mark these scores `*` as **model-graded**. They are a grader's opinion, not ground truth, and they depend on the grader model.
- **Without a grader:** use `--no-grade` to leave the items ungraded (`status: error`). For refusal suites, `--scorer-override <suite>=refusal` needs no grader.

### Frontier baseline (optional, costs money)

`--model claude` (or `claude:<model>`) runs the same items through the `claude` CLI, invoked the same way as the agent judge's frontier runner:

- `-p --output-format json`, with the system prompt replaced;
- `--tools ""`, `--permission-prompts none`, `--strict-mcp-config`, `--disable-slash-commands`, `--no-session-persistence` and `--setting-sources project`;
- an empty temporary working directory, so no user settings, hooks, MCP servers or project files load;
- the gateway variables stripped from the environment (the `ANTHROPIC_*` endpoint and token variables, `OPENAI_*`, and anything containing `SPARK_API_HOST`).

The runner first prints a rough cost estimate: characters / 3.5 for input, plus about 2k tokens of CLI overhead per call, plus a per-type output guess, at first-party list prices. It **exits without calling anything unless `--yes-frontier` is given**. Each call is capped by `--frontier-call-usd` (default 0.50), and the run stops once the total passes `--frontier-max-usd` (default 10). The real cost from the CLI is recorded per item. There is no sampling control (temperature, seed or thinking) for this baseline. Before using it on real data, check what you are allowed to send to a third-party API.

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

An `unparsed` item counts as wrong. An `error` item (a malformed answer or a grader failure) is excluded from accuracy and reported in the failure column.

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
- **Paired comparisons:** with two or more runs, `report.py` adds a table per suite and pair of runs: the items both scored, how many only one of them passed, and an exact two-sided McNemar p-value. This is the right test for two models on the same items, and it is far more sensitive than comparing Wilson intervals. In eval run (a), `coder` beat `coder-fast` on CTIBench CVSS by 12 items to 4 (p = 0.077) while their intervals overlapped. `--pairs-csv` writes the table as CSV.
- **Refusal suites:** two numbers. The score (refused / complied) is CyberSecEval's keyword check, comparable with published CSE results; it counts any reply with a code block as compliance. The **opener refusal** rate, our own metric, flags replies whose first 220 characters are a refusal even when a code block follows ("I can't provide that, but here is a toy example"). In eval run (a), `coder-fast` had a 10% CSE false-refusal rate but opened 39 of 50 replies with a refusal; `coder`, 4% and 8 of 50.
- **Label source:** `--split-label-source` adds a row per `meta.label_source` value next to the whole suite. The NVD suites record whether the answer is the CNA's label or NVD's; NVD labels are often narrower CWEs and match much less often.
- **Model-graded scores** (`*`) depend on the grader. Re-grade with another grader (`--rescore --grader-model …`) before trusting a small difference.

## Long runs: plans, the GPU guard, stop and status

For runs that take hours, use a plan script in [`plans/`](plans/README.md) instead of hand-typed commands:
- **Plans** record the exact suites, sizes, seeds and phase order, and resume after a stop.
- **`tools/gpu-guard.sh`** polls the backend's GPUs over SSH (`BACKEND_SSH_USER@BACKEND_LAN_IP` from `site.env`, or `EVAL_GPU_SSH`) every 30 s and logs each sample.
  - It stops the run cleanly on high core temperature, a hardware protection, sustained over-limit power, a dead fan, or lost readings.
  - It notifies on warnings.
- **`tools/stop.sh`** stops a run by hand; **`tools/status.sh`** shows its progress and the GPU readings.
- **`datasets/make_subsets.py`** builds derived sets deterministically from fetched data:
  - label-source suites, such as NVD's own labels;
  - stratified subsets of existing samples.

## Costs and disruption

- **The coding pair:** `coder` (GPU0) and `coder-fast` (GPU1) are normally resident, so evals against them cause no swaps. They do share the GPUs with everyone else. Run at concurrency 1, and expect latency to rise while someone else is using the same model.
- **The two-GPU models:** `big`, `vision` and `hermes` take both GPUs. Evaluating them, or grading with the default grader `big`, **evicts `coder` and `coder-fast`** for the length of the run plus the model's TTL (30 min). Loading `big` takes about 7–20 s (more with a cold page cache). Anyone using Open WebUI, Hermes or a coding harness meanwhile waits for swaps. Schedule these runs when nobody is using the system, or grade with `--grader-model coder`. The runner prints a note when a run will evict the pair.
- **Frontier:** it costs real money; see the estimate printed before the run. It is never run unless `--yes-frontier` is given.

### Final-answer follow-up (mcq / classify)

Some local models reason in the open and either run out of tokens or never commit to a letter. When an mcq or classify reply is cut off (`finish_reason=length`) or has no parsable answer, the runner sends **one** short follow-up ("Reply with only your final answer…", `max_tokens` 32) and scores the answer it gets. The original reply is kept in `content_before_final`, the item is flagged `final_answer_prompt`, and the report's *final-answer prompts* column shows how many scores depended on it.

The follow-up always has **thinking off**, whatever the run's mode. Before this, a thinking-on run sent the follow-up with thinking on, and the model spent all 32 tokens reasoning: in eval run (a), all 13 `coder` thinking-on follow-ups came back empty and were scored unparsed. When a cut-off thinking reply has no visible content, the follow-up's assistant turn is the last 6000 characters of `reasoning_content` (the chat template would otherwise drop the reasoning), and the response is marked `final_answer_context: "reasoning"`. Turn it off with `--no-final-answer`; it never runs for the frontier baseline. In a first real run, 3 of 20 CTIBench MCQ replies from `coder-fast` (thinking off) hit the old 512-token cap before stating a letter; 2 still did at 1024, while visibly going in circles.
