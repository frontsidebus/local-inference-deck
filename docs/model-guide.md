# Choosing a model for security work

**Status:** provisional. These results are exploratory until the harness's confirmatory run and the human parser and answer-key audits are done (see [`evals-roadmap.md`](evals-roadmap.md)).

Default to **`coder`**. For batches of CVSS triage, don't use `coder` or `coder-fast`: **`flash`**, `hermes` and `vision` tie. Keep **`coder-fast`** for quick lookups. Check every CVSS score and CWE mapping against the source, whichever model you use.

This guidance comes from the security eval runs of 2026-10-06 to 2026-10-09:
- six models compared on the same items, with paired McNemar tests (`evals/`, `evals/plans/`);
- per-run reports under `evals/results/_runs/` (gitignored);
- a round-4 re-analysis of the pre-specified claims, with corrections, exclusions (key leaks, items with no signal), constant and text-classifier baselines, and label-agreement ceilings. "Confirmed" below means it held there; none of it is confirmatory yet.

Re-run the shared subsets after any change of model, quant or llama.cpp image.

| Task | Use | Evidence (the same 100-item subsets unless noted) | Cost |
|---|---|---|---|
| Security analysis, incident write-ups, threat summaries | **`coder`** | SEvenLLM free text 85%, tied with `big` (85%); beats vision 20/4 (p 0.002), flash 12/2 (p 0.013), hermes and coder-fast. Exploratory: graded by `big`, an LLM judge that shares a family with contestants and favours longer answers. 19% refusal openers. | Resident on GPU0 |
| CWE mapping | **`coder`** | CTIBench CWE 76% (flash 75%, vision 72%; no difference shown). NVD CWE 60% (200 items). Thinking on gave 65% (8/18 items, p 0.08), not confirmed; exploratory. | Thinking is 3–5× slower |
| CVSS scoring and CVE triage batches | **`flash`** (as `local/qwen3.8-flash-next`), or `hermes` / `vision` | CTIBench CVSS (97 items): flash beats coder (MAE 0.79 vs 1.31; 71% vs 55% exact) and coder-fast (MAE 1.13), confirmed. It does not beat hermes (0.84), vision (0.76) or big (1.00). NVD CWE 64% vs coder 60%; equivalence not shown. | Takes both GPUs and evicts the coding pair. About 46 tok/s. Opens 64% of CyberSecEval benign requests with a refusal. |
| Quick knowledge lookups | **`coder-fast`** | Equivalent to coder on CTIBench MCQ (68% vs 67%, 200 items). CyberMetric 98%, but every model is near ceiling there. Fastest. | Opens 81% of CyberSecEval benign requests with a refusal. A defensive system prompt raised keyword compliance (90% → 98%, 200 items, p 0.002) but did not reduce refusal openers (81% → 86%). |
| Requests the others refuse | **`hermes`** | Never refused: 0% openers, 100% full answers | Evicts the coding pair. Weaker CWE (CTIBench 66%, NVD 46%) and free text (75%, judge-graded). |
| Images | **`vision`** | CTIBench CVSS 72% (MAE 0.80) and threat-intel multiple choice (74%) | Evicts the coding pair. Complies least (79%) and refuses outright most often (17%). |
| Security work in general | not **`big`** | Leads on nothing; 40% of its CTIBench CVSS answers run out of tokens | Evicts the coding pair |

## Notes
- **Refusals** are measured on CyberSecEval's benign false-refusal prompts, three ways:
  - CyberSecEval's keyword check ("complied" above). A reply that opens with a refusal but then has a code block counts as compliance, so it understates refusals (`evals/README.md`).
  - **Refusal openers**, the harness's own metric: replies whose first 220 characters refuse, code block or not.
  - **Refusal tiers** (truncated replies left out): *full* (no decline anywhere outside code), *partial* (declines something but still gives content), *refuse* (opens with a decline and gives nothing). Declines are tagged *policy* or *scope*; a scope statement ("I can't give a complete, production-ready tool, but…") is counted apart from policy declines and is not a refusal on its own.
  - Tiers, full / partial / refuse: coder 64 / 36 / 0%; coder-fast 16 / 82 / 2%; flash 17 / 82 / 1%; vision 60 / 23 / 17%; hermes 100 / 0 / 0%. Most "refusals" from coder-fast and flash are partial: a decline followed by help.
- **CVSS numbers measure agreement with one labeller.** The gold labels are mostly the vendor's (CNA) vectors, and NVD's and the CNA's vectors for the same CVE match exactly only 3.9% of the time (410 CVEs).
  - On NVD CVSS, `big`, `coder` and `coder-fast` score worse than a constant 7.5 (MAE 1.61–1.97 vs 1.33–1.41), and none of them beats a bag-of-words text classifier. Only flash and hermes beat the classifier.
  - On CTIBench CVSS, coder and coder-fast are also worse than the constant (MAE 1.77 and 1.68 vs 1.44).
- **CWE accuracy is largely name lookup.** On NVD CWE every model scores 73–98% when the description names the weakness and 27–41% when it does not.
- **Weak spots for every model:** exact ATT&CK technique extraction (3–10%) and matching NVD's own CWE labels (21–35%). Treat both as needing review, whichever model you use.
- **Thinking:** no gain has been shown, but equivalence hasn't been shown either; the comparison is exploratory. Leave it off by default for speed. For code edits in Hermes, use `--reasoning medium` or lower.
- **`flash` is experimental** (see [`walter/models.md`](../walter/models.md)).
  - Gateway keys reach it as `local/qwen3.8-flash-next`, because their model allow-lists include `local/*`.
  - Its speed tuning (MTP plus 16 CPU expert layers) has not yet been shown equivalent: accuracy moved −3.0 pp on CTIBench MCQ (100 items) and −3.3 pp on NVD CWE (92 items), and the equivalence test was inconclusive (`walter/llama-swap/BENCHMARKS.md`). The pre-registered check uses a 5 pp margin; the tuning is reverted only if the result is "different, worse".
- **Scope:** these are single-turn benchmarks standing in for real work. They rank the models; they are not absolute skill, and agentic, multi-step use is not measured.
