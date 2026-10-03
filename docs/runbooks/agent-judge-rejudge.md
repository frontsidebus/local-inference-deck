# Runbook: measure a judge change by re-judging saved bundles

Before you trust a new judge prompt, a new local model or a new severity policy, measure it: run the old and the new judge on the **same saved evidence bundles** and score both against ground truth you checked yourself. Background: [docs/agent-judge.md](../agent-judge.md), especially [Pilot results: run 1](../agent-judge.md#pilot-results-run-1). The pilot procedure that produces the bundles is [agent-judge-pilot.md](agent-judge-pilot.md).

Why bundles and not live runs: a live re-run changes the agent's behaviour, the timing and the host state, so two judges never see the same input. A saved bundle is fixed. The only variable left is the judge.

## What you need

- **Saved bundles** from a run: `$JUDGE_REVIEW_DIR/evidence/<request-id>/` plus the request (`done/<request-id>.json`). Copy them out of the review directory before you start, so a later collector run cannot rebuild them.
- **Ground truth per request**, written down before you read any finding: what the agent actually did and whether each of its claims was true, checked independently (host journal, file diffs, the gate `outcome`, live read-only commands). See [Verifying ground truth](agent-judge-pilot.md#verifying-ground-truth). Run 1's table in the design doc is an example.
- `judge/runner/rejudge.py`. <!-- TODO(area A): exact path, arguments and output layout -->

## Rules

- **Never re-judge in the live review directory.** Work on a copy, with `JUDGE_REVIEW_DIR` pointing at it. Re-judging there would overwrite the findings you are comparing against and could feed new findings to the agent through C5.
- **Respect the data boundary.** A `sensitive` bundle may only go to a local model, also when re-judging. <!-- TODO(area A): confirm rejudge.py enforces this -->
- **Fix the ground truth first.** Write the expected result for each request before you look at either judge's output. Otherwise you will grade the judge by its own answer.
- **Change one thing at a time:** the prompt, or the model, or the policy. Not two at once.

## 1. Old prompt vs. new prompt

<!-- TODO(area A): rejudge.py invocation for prompt A/B -->

Run every saved bundle through the judge twice, once with the old prompt and once with the new one, with the same model. Keep each run's findings in its own directory, named after the prompt version (for example the git commit of `judge/runner/prompt.md`).

## 2. Local model choices

The local judge is any gateway alias. Run 1 used `coder-fast`, from the same family as the worker, and it treated "cannot verify" as "false" in every scenario. The candidates to compare:

| Alias | Family | Notes |
|---|---|---|
| `big` | the worker's family (Qwen3-Coder-Next) | the default `JUDGE_LOCAL_MODEL` |
| `coder-fast` | the worker's family | run 1's local baseline; not a fair judge |
| `vision` | Gemma 4, a different family | the cross-family option |

Select the model with `JUDGE_LOCAL_MODEL=<alias>` for the run. <!-- TODO(area A): confirm the rejudge.py flag or variable -->

**Loading a model on Walter can unload others.** llama-swap swaps models in and out of GPU memory. Loading `vision`, for example, unloads the coding pair (`coder`, `coder-fast`), and a working Hermes session then stalls or reloads on its next call. Re-judge with a different local model only when no agent is working.

## 3. Score against ground truth

Label every item of every run, using the [scoring template](agent-judge-pilot.md#scoring-template): TP, FP, BG, Unverified, and FN for every ground-truth fault a run missed.

| Request | Ground truth | Run A items (id: label) | Run B items (id: label) | Notes |
|---|---|---|---|---|
| | | | | |

Then compare, per run:

| Metric | Run A | Run B |
|---|---|---|
| High FPs | | |
| Medium FPs | | |
| Precision, TP / (TP + FP) | | |
| Catch rate on manifested faults | | |
| Unverified items (should replace the old false FPs, not true findings) | | |
| Median and maximum latency (s) | | |

**What "better" means.** For a judge whose findings reach the agent, a high false positive is the most expensive error: the agent either argues with it or "fixes" correct work. A change that removes high FPs but also loses a true finding needs a human decision, not an automatic yes. Write the decision and the numbers next to the change (its commit message or PR).

**When a local model has earned trust.** The [local-judge policy](../agent-judge.md#local-judge-policy) caps local findings at `JUDGE_LOCAL_MAX_SEVERITY` and keeps them out of the agent's context (`JUDGE_INJECT_LOCAL=0`). Relax either setting only after a local model has shown no high or medium FPs on a scored set of bundles that includes clean tasks, declines and blocks (run 1's S1, S6, S7 and S8 are good cases), and record that set.
