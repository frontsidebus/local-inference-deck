# Choosing a model for security work

Default to **`coder`**. Use **`flash`** for batches of CVSS or CWE triage. Keep **`coder-fast`** for quick lookups.

This guidance comes from the security eval runs of 2026-10-06 to 2026-10-09:
- six models compared on the same items, with paired McNemar tests (`evals/`, `evals/plans/`);
- per-run reports under `evals/results/_runs/` (gitignored).

Re-run the shared subsets after any change of model, quant or llama.cpp image.

| Task | Use | Evidence (the same 100-item subsets unless noted) | Cost |
|---|---|---|---|
| Security analysis, incident write-ups, threat summaries | **`coder`** | Best on SEvenLLM free text, 85% (graded by `big`); beats vision 20/4 (p 0.002) and flash 12/2 (p 0.013). Refuses rarely (23% refusal openers). | Resident on GPU0 |
| CWE mapping where accuracy matters | **`coder`**, thinking on | CTIBench CWE 76%; NVD CWE 60% thinking off, 65% thinking on (200 items, 8/18 items, p 0.08) | Thinking is 3–5× slower |
| CVSS scoring and CVE triage batches | **`flash`**, as `local/qwen3.8-flash-next` | Lowest CVSS error of the six: CTIBench 73%, MAE 0.75 (coder 59%, 1.27; 17/3 items, p 0.003). NVD CWE 64%, matching coder. | Takes both GPUs and evicts the coding pair. About 46 tok/s. Opens 64% of benign defensive requests with a refusal. |
| Quick knowledge lookups | **`coder-fast`** | Ties on multiple-choice knowledge (CyberMetric 98%, CTIBench MCQ 65%) and is the fastest. | Opens 84% of benign defensive coding requests with a refusal. A defensive system prompt raised full compliance (90% → 98%) but left the refusal openers unchanged (81% → 86%, 200 items, p 0.11). |
| Requests the others refuse | **`hermes`** | Never refused: 0% openers, 100% complied | Evicts the coding pair. Weaker CWE (65%) and free text (75%). |
| Images | **`vision`** | Strong CVSS (72%) and threat-intel multiple choice (74%) | Evicts the coding pair. Refuses most often of the six (79% complied). |
| Security work in general | not **`big`** | Leads on nothing; 40% of its CVSS answers run out of tokens | Evicts the coding pair |

## Notes
- **Refusal openers** are the eval harness's own metric. It counts replies whose first 220 characters refuse, even when a code block follows. CyberSecEval's keyword check counts those as compliance, so its numbers understate refusals (`evals/README.md`).
- **Weak spots for every model:** exact ATT&CK technique extraction (3–10%) and matching NVD's own CWE labels (21–35%). Treat both as needing review, whichever model you use.
- **Thinking:** apart from coder's CWE mapping, it showed no gain larger than seed-to-seed noise. Leave it off by default for speed. For code edits in Hermes, use `--reasoning medium` or lower.
- **`flash` is experimental** (see [`walter/models.md`](../walter/models.md)).
  - Gateway keys reach it as `local/qwen3.8-flash-next`, because their model allow-lists include `local/*`.
  - Its speed tuning (MTP plus 16 CPU expert layers) did not measurably change accuracy (`walter/llama-swap/BENCHMARKS.md`).
- **Scope:** these are single-turn benchmarks standing in for real work. They rank the models; they are not absolute skill, and agentic, multi-step use is not measured.
