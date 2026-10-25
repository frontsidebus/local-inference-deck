# Security eval harness: moved to sec-evals

The eval harness (`run.py`, the scorers, the upgrade gate, the C1 protocol, the plans, the dataset converters and
their tests) and `docs/evals-roadmap.md` moved to their own repo, **sec-evals** (plan step 3). The history came
along (`git filter-repo --path evals/ --path docs/evals-roadmap.md`) and the `evals/` prefix is unchanged, so
harness hashes, pid files and the GPU guard's command line are the same as before.

- **Checkout:** `~/src/deck/sec-evals` (the `deck.lock` path), or any path named by `$SEC_EVALS_DIR`. It is a
  working checkout: the attempts ledger is committed there after each confirmatory run.
- **Site values:** `bin/deck install evals` links the checkout's gitignored `site.env` to the deck's site.env.
  `bin/deck check` verifies the link.
- **Running:** `bin/deck evals run …` and `bin/deck evals gate …` from this repo. deck runs
  `python3 -B evals/{run,gate}.py` from the sec-evals root with `--site-env` and this checkout's
  `walter/llama-swap/config.yaml.tmpl` as `--llama-swap-config`.
- **Model swaps:** add the sec-evals checkout to `SPARK_EVAL_REPOS` (colon-separated) so `spark-model` sees its
  live runs ([walter/models/README.md](../walter/models/README.md)).
- **Docs:** the harness README, `PROTOCOL.md` and the roadmap live in the sec-evals repo.

Provenance references in sec-evals (for example commit ids in `evals/plans/c1-interventions.json` and
`PROTOCOL.md`, and `walter/...` template paths) are commits and paths in this repo.

## Upgrade gate

Moved with the harness: see the "Upgrade gate" section of the sec-evals README. The deck's entry point is
`bin/deck evals gate <calibrate|baseline|check> --model <alias> --yes`; the steps for a serving change are in
[docs/runbooks/upgrade.md](../docs/runbooks/upgrade.md).

Old checkouts of this repo may still hold the gitignored `evals/data/` and `evals/results/`. They stay ignored here
(`.gitignore`) and must never be committed; they are the state a rollback to `pre-split-3` runs gates with.
