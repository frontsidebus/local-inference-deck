# Runbook: measure a judge change by re-judging saved bundles

Before you trust a new judge prompt, a new local model or a new severity policy, measure it: run the old and the new judge on the **same saved evidence bundles** and score both against ground truth you checked yourself. Background: [docs/agent-judge.md](../agent-judge.md), especially [Measured results](../agent-judge.md#7-measured-results). The pilot procedure that produces the bundles is [agent-judge-pilot.md](agent-judge-pilot.md).

Why bundles and not live runs: a live re-run changes the agent's behaviour, the timing and the host state, so two judges never see the same input. A saved bundle is fixed. The only variable left is the judge.

## What you need

- **Saved bundles** from a run: `$JUDGE_REVIEW_DIR/evidence/<request-id>/` and the request in `queue/` or `done/`. `rejudge.py` never collects, so a missing bundle is an error, not a fresh collection.
- **Bundles for a code change can be rebuilt offline** when the originals are not good enough (for example after a collector fix): copy the session snapshot, the request and `gate.log` into a private review root (with paths rewritten to a checkout of the build branch at the task's commit), and run `collector/collect.py` with `JUDGE_HOST_PROBES=0` and that `JUDGE_REVIEW_DIR`. That is how the pilot-2 bundles were rebuilt after #43–#45. Audit the rebuilt bundle before any frontier call.
- **Ground truth per request**, written down before you read any finding: what the agent actually did and whether each of its claims was true, checked independently (host journal, file diffs, the gate `outcome`, live read-only commands). See [Verifying ground truth](agent-judge-pilot.md#verifying-ground-truth). Run 1's table in the design doc is an example.

## The tool

```bash
judge/runner/rejudge.py <request-id>... --out DIR [--mode local|frontier|frontier-claims] [--model ALIAS] [--no-budget] [--sensitive-local]
```

- It reads requests and bundles from `$JUDGE_REVIEW_DIR` and **writes only to `DIR`**: `<id>.json` (with the validator's notes), `<id>.md`, `<id>.raw.txt` (the model's raw reply), `<id>.input.txt` (exactly what was sent), and `summary.json`. It never writes to `queue/`, `done/`, `findings/`, `acks/` or `evidence/`, and refuses an `--out` inside them. So it can point at the live review directory without changing what the agent sees.
- It prints a table comparing each new finding with the existing `findings/<id>.json`: mode, items, and the count of high `false` items.
- It sends `PROBES ALLOWED: no`, so every run sees the bundle exactly as saved.
- **The data boundary holds.** A `sensitive` bundle never goes to the frontier judge. Without `--mode` it is judged locally. With `--mode frontier` it is **refused**: no model call, `REFUSED` in the table with the reason, `"refused": true` in `summary.json`, and exit 1 (the other requests are still judged). It is not silently judged locally instead, because the default local model `big` unloads the coding models on Walter. If you do want those requests judged locally, add `--sensitive-local` (and usually `--model`); each such finding carries a note. For a mixed set of ids, the cleaner route is two runs: `--mode frontier` on the `infra` ids, `--mode local` on the rest.
- **The claims stage** of a `sensitive` completion can be measured with `--mode frontier-claims`: it builds the claims-only bundle (final answer, gate/C3/tool metadata; no contents, diffs, paths or user messages), self-checks it and sends only that to the frontier judge, exactly as the live runner does. `<id>.input.txt` is the exact text that left the machine; read one before trusting a new builder. A bundle the self-check refuses is reported `REFUSED` and not sent. Compare with the live `findings/<id>.claims.json`.
- `--model` sets the local or frontier model according to `--mode` (both when `--mode` is absent).
- Frontier calls count against `JUDGE_FRONTIER_DAILY_MAX` unless you pass `--no-budget`. Each still costs up to `JUDGE_FRONTIER_MAX_USD`.
- Exit codes: 0 ok, 1 when any request failed, 64 for usage errors.

## Rules

- **Fix the ground truth first.** Write the expected result for each request before you look at either judge's output. Otherwise you will grade the judge by its own answer.
- **Change one thing at a time:** the prompt, or the model, or the policy. Not two at once.
- **One output directory per run**, named after what you changed (for example `rj-<prompt commit>-<model>`). Keep them; they are the record.

## 1. Old prompt vs. new prompt

`rejudge.py` uses the prompt and validator of the checkout it runs from. To compare two versions, run it from two checkouts (for example a second `git worktree` at the old commit) on the same request ids and the same model:

```bash
ids="<id1> <id2> ..."
(cd <old-checkout> && judge/runner/rejudge.py $ids --mode local --model big --out ~/rj/old-big)
(cd <new-checkout> && judge/runner/rejudge.py $ids --mode local --model big --out ~/rj/new-big)
```

Both read the same `$JUDGE_REVIEW_DIR`, so both see identical bundles. Use a real path for `--out`, outside the review directory.

Some changes need no model call at all. Validator changes can be checked against the stored raw replies: `judge/runner/validate.py <finding.json> --bundle $JUDGE_REVIEW_DIR/evidence/<id> --local-max-severity medium`. That is how run 1's stored outputs were re-checked.

## 2. Local model choices

The local judge is any gateway alias. Run 1 used `coder-fast`, from the same family as the worker, and it treated "cannot verify" as "false" in every scenario. The candidates to compare:

| Alias | Family | Notes |
|---|---|---|
| `big` | the worker's family (Qwen3-Coder-Next) | the default `JUDGE_LOCAL_MODEL` |
| `coder-fast` | the worker's family | deployed for sensitive reviews only, capped at medium and not injected; not a fair judge (run 1) |
| `vision` | Gemma 4, a different family | the recommended candidate; not yet measured as a judge |

```bash
judge/runner/rejudge.py $ids --mode local --model vision --out ~/rj/new-vision
judge/runner/rejudge.py $ids --mode local --model big    --out ~/rj/new-big
```

For `infra` requests, add a frontier run with the same prompt as the reference (`--mode frontier`).

**Loading a model on Walter can unload others.** llama-swap swaps models in and out of GPU memory. Loading `vision`, for example, unloads the coding pair (`coder`, `coder-fast`), and a working Hermes session then stalls or reloads on its next call. Re-judge with a different local model only when no agent is working.

## 3. Score against ground truth

Label every item of every run (read `<id>.json` and its `notes`; an item the validator dropped or downgraded counts as the validator's decision, and note it), using the [scoring template](agent-judge-pilot.md#scoring-template): TP, FP, BG, Unverified, and FN for every ground-truth fault a run missed.

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

**When a local model has earned trust.** The [local-judge policy](../agent-judge.md#runner-and-judges) caps local findings at `JUDGE_LOCAL_MAX_SEVERITY` and keeps them out of the agent's context (`JUDGE_INJECT_LOCAL=0`). Relax either setting only after a local model has shown no high or medium FPs on a scored set of bundles that includes clean tasks, declines and blocks (run 1's S1, S6, S7 and S8 are good cases), and record that set.
