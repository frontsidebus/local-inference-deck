# Eval harness: where it stands and where it could go

**As of:** 2026-10-11 · **Status:** options under consideration. Nothing below is committed work.

This page sets out what the security eval harness (`evals/`) has shown so far, and the uses it could be developed for. It gives the further work each use needs, so future effort goes to the uses whose results will actually be acted on.

---

## Where it stands

**The harness.** The October 2026 runs compared six local models on public security benchmarks (`docs/model-guide.md`). Four rounds of adversarial review followed. They looked for:
- data bias and label contamination;
- leakage between development and test items;
- overfitting of the analysis to results already seen;
- validation bias.

Each finding was fixed and turned into a regression test. The hardened harness (about 790 tests) is still being landed as a series of PRs: scoring, data, stats/report, judge, then protocol/run. It adds:
- **Scoring:** final-answer-only parsing; CVSS 3.1 scores computed from the vector; CWE one-hop credit; refusal tiers that ignore scope statements.
- **Data:** near-duplicate and template clustering; stripping of leaked labels; label-source roles (NVD, CNA, ADP); a sealed held-out split; prospective (post-cutoff) items.
- **Statistics:**
  - Corrections: exact paired tests, Holm/BH over the declared family, size-correct equivalence tests.
  - Robustness: pre-registered sensitivity analyses, and strata that can never flip a verdict.
  - References: baselines (constant, majority letter, longest option, a dev-trained text classifier) and a label-agreement ceiling.
- **Judge:** an independence check by model family; a calibration protocol against human ratings; a self-preference test.
- **Confirmatory mode:** a run is accepted only if it is frozen, sealed, hashed and probed against the live server. Anything else is reported as post-hoc, with the reasons listed.

**What it found.** These findings are the main result so far. They are provisional until the deferred human audits (parser and answer key) are done.

| Finding | Evidence | What it means |
|---|---|---|
| NVD's own CVSS vector and the vendor's (CNA) vector for the same CVE match exactly only **3.9%** of the time | n = 410 CVEs; mean score gap 1.22 | CVSS "accuracy" mostly measures agreement with one labeller's habits, not ground truth |
| CWE accuracy depends on whether the description names the weakness: **73–98%** when it does, **27–41%** when it doesn't | `nvd-cwe`, 200 items, all six models | Much of the apparent CWE skill is name lookup |
| On `nvd-cvss`, `big`, `coder` and `coder-fast` have higher score error than a **constant 7.5**, and none of them beats a bag-of-words classifier | MAE 1.61–1.97 vs 1.33–1.41 for the constant on the same items | Don't trust these models' CVSS scores on their own |
| The LLM judge used for free-text grading favours longer answers | judge review, rounds 1–2 | Judge-graded rankings (SEvenLLM) are exploratory only |
| Several statements in the current model guide are not supported once corrections and baselines are applied | round-4 re-analysis | Model-guide corrections are a separate, pending PR |

**What it doesn't cover:**
- agentic, multi-step work;
- vendor-recommended sampler settings;
- prompt paraphrases;
- refusal categories beyond the one CyberSecEval category used.

---

## Options

Each option lists its value and the work still needed beyond landing the harness PRs. Effort is rough. GPU days are watched daytime runs with the GPU guard on, and they evict the resident coding pair.

### 1. Routing for this stack (the original goal)

- **Value:** high, and mostly delivered.
- **Remaining work:** small.
  - Land the harness PRs.
  - Correct `docs/model-guide.md` (the CVSS tie, the CWE claim, the SEvenLLM caveat, the refusal tiers).
  - About an hour of agent work plus review. No GPU.

### 2. Upgrade gate: does a model, quant, llama.cpp build or tuning change make anything worse?

- **Value:** high and recurring. Models, quants and images change often (see `docs/runbooks/upgrade.md`). This is where the harness pays for itself.
- **Remaining work:** moderate.
  - A one-command regression check: a fixed sealed subset, the frozen scorer, and equivalence tests at a pre-set margin (5 pp, as decided for the flash-tuning comparison).
  - Its output is one of "equivalent", "different" or "inconclusive".
  - Wire it into the upgrade runbook.
  - About a day of agent work, then 1–2 GPU hours per upgrade.

### 3. Vetting local models for vulnerability triage (CVE enrichment, CWE mapping, CVSS)

- **Value:** high for any team that wants to use local models on CVE data. The label-noise and baseline findings are what such a team needs before trusting a model.
- **Remaining work:** moderate to large. Roughly 1–2 calendar weeks:
  - a fresh prospective NVD fetch;
  - the signed freeze tag;
  - one confirmatory run;
  - the two approved robustness arms (MCQ option shuffle, 2× token budget);
  - the human answer-key audit.

  That comes to about 2 GPU days and several hours of human labelling.

### 4. Methods write-up or paper

- **Value:** medium to high. The negative findings are useful to anyone benchmarking LLMs on security data:
  - label disagreement;
  - name lookup;
  - models losing to trivial baselines;
  - judge length bias.
- **Remaining work:** large. Everything in option 3, plus:
  - an external review (one non-Claude model and one human) before the signed tag;
  - the deferred parser and key audits;
  - a datasheet and data-licence review;
  - the writing.

  Weeks to months part-time. Human labelling and writing are the bottleneck, not code.
- **Framing:** methods-first. The harness and its negative findings are the contribution, and the model results are a case study.

### 5. A reusable open-source harness

- **Value:** medium, and only if others adopt it.
- **Remaining work:** large and ongoing.
  - Decouple it from this stack's gateway and model names.
  - Package it, add a suite plugin interface, and set up CI and user docs.
  - Then maintain it.

---

## Recommended order

1. **Now:**
   - Land the harness as the split PRs.
   - Correct the model guide.

   Both are cheap and clearly worth it. The current guide overstates some results.
2. **Next:** build the upgrade gate (option 2). It is the best value for the effort.
3. **Only if wanted:** options 3 and 4. They are real opportunities, but they need human time for labelling, review and writing. The work already done is not a reason to start them.

**Not recommended:** more red-team rounds. The returns have diminished. The remaining risks need humans (audits, external review, an external timestamp), not more code.

## Decisions already taken (2026-10-11)

These apply to whichever option goes ahead:
- **Freeze:** a signed public tag before any confirmatory run.
- **Exploratory only:**
  - the judge-graded written-analysis comparison;
  - the thinking-on/off comparison. Thinking stays off by default.
- **Robustness arms:** MCQ option shuffle and a 2× token budget are approved. Vendor samplers and paraphrases are disclosed as limitations.
- **Equivalence margin:** 5 pp. The flash tuning is reverted only if it is shown "different, worse".
- **Human audits:** deferred. Results stay provisional until they are done.
- **Deviations:** an accepted deviation that changes the estimand makes that comparison family post-hoc.
