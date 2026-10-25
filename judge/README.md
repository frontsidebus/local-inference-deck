# judge/ has moved: agent-judge

The agent judge (Hermes shell hooks, evidence collector, review runner, units, tests) now lives in its own repo,
**agent-judge** (plan step 2). Its design doc and runbooks moved with it: `docs/agent-judge.md`,
`docs/decisions.md` (#10, #11) and `docs/runbooks/agent-judge-{pilot,rejudge}.md`.

- **Checkout path:** `~/src/deck/agent-judge` (the path in [deck.lock](../deck.lock)). The Hermes hook commands
  and `JUDGE_INFRA_REPOS` name it, so it must not move.
- **Install from that checkout:** `judge/install.sh --apply --with-units --start`, then approve the hooks in an
  interactive `hermes chat`. This repo no longer holds the hooks; nothing here is run by Hermes.
- **Configuration stays in this repo's site env:** `JUDGE_REPO_DIR` = this deck checkout, `JUDGE_INFRA_REPOS` =
  the agent-judge and sec-evals checkouts ([site.env.example](../site.env.example), section `# --- judge`).
- **code_version is unchanged by the move:** it is the git tree id of `judge/`, and agent-judge keeps `judge/` at
  the same prefix, so its first commit has the same `judge/` tree as this repo had before the removal.

The seams between the two repos are contracts C7-C10 ([contracts/README.md](../contracts/README.md)). The deck
keeps byte-identical copies of the judge-owned ones under `contracts/vendored/agent-judge/`.
