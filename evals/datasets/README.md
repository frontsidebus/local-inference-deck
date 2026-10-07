# Security eval datasets

Each `<dir>/fetch.py` does four things:
1. downloads a public benchmark from its official source;
2. verifies the pinned sha256;
3. converts the data to the task format in the eval contract;
4. writes `evals/data/<suite>.jsonl`, plus a `<suite>.provenance.json` sidecar recording the source URLs, checksums, license, fetch date and output checksum.

`evals/data/` is gitignored. **No benchmark data is committed.** Most of these licenses are non-commercial and share-alike, or they don't state a license at all, so the fetchers regenerate the data byte-for-byte. Small invented fixtures live in `<dir>/fixtures/`.

```
python3 evals/datasets/ctibench/fetch.py --sample 50     # writes the full set + a 50-item stratified sample
python3 -m pytest evals/datasets/tests -q -p no:cacheprovider
```

All fetchers share `_common.py`. They need only the stdlib (urllib, json, csv) and take the same options:
- `--sample N`: also writes `<suite>.sample<N>.jsonl`. The sample is stratified on `meta.category`, uses a fixed seed (`--seed`, default 20261005) and keeps source order.
- `--offline`: uses only the download cache in `evals/data/.cache/`.
- `--out-dir`, `--cache-dir`.

## Index

| Suite | Dir | Type | Items | License | Scorer | What it measures |
|---|---|---|---|---|---|---|
| `ctibench-mcq` | [ctibench](ctibench/) | mcq | 2500 | CC-BY-NC-SA-4.0 | `mcq_letter` | CTI knowledge (ATT&CK, CWE, CAPEC, CTI standards) |
| `ctibench-rcm` | [ctibench](ctibench/) | classify | 1000 | CC-BY-NC-SA-4.0 | `cwe_match` | CVE description → CWE (2024 CVEs) |
| `ctibench-vsp` | [ctibench](ctibench/) | extract | 1000 | CC-BY-NC-SA-4.0 | `cvss_mae` | CVE description → CVSS v3.1 vector |
| `ctibench-ate` | [ctibench](ctibench/) | extract | 60 | CC-BY-NC-SA-4.0 | `exact_set` | ATT&CK technique IDs from CTI text |
| `cybermetric-80` / `-500` / `-2000` / `-10000` | [cybermetric](cybermetric/) | mcq | 80 / 500 / 2000 / 10180 | Apache-2.0 (from the authors' HF card; no LICENSE file in the repo) | `mcq_letter` | general cybersecurity knowledge |
| `secqa-v1` / `-v2` | [secqa](secqa/) | mcq | 110 / 100 | CC-BY-NC-SA-4.0 | `mcq_letter` | textbook security concepts (v2 is harder) |
| `sevenllm-mcq` | [sevenllm](sevenllm/) | mcq | 50 (en) | Apache-2.0 | `mcq_letter` | reading comprehension over incident reports |
| `sevenllm-qa` | [sevenllm](sevenllm/) | extract / freeform | 600 (en) | Apache-2.0 | `llm_judge` | CTI extraction (IOCs, malware features) and analysis or summary writing |
| `cse-frr` | [cse-frr](cse-frr/) | freeform | 750 | MIT | `llm_judge` | **false refusal** on benign, borderline security requests |
| `nvd-cwe` | [nvd-recent](nvd-recent/) | classify | 9199 | CVE ToU / NVD | `cwe_match` | CVE → CWE on **fresh** CVEs (published Aug 2026) |
| `nvd-cvss` | [nvd-recent](nvd-recent/) | extract | 9552 | CVE ToU / NVD | `cvss_mae` | CVE → CVSS v3.1 on fresh CVEs |

The counts are from the real fetch on 2026-10-05. SEvenLLM also has 650 Chinese items, available with `--lang zh|all`.

A suggested first pass: run `--sample 50` (or 100) for each suite. That is about 700 to 1300 calls in total. Then run the full sets of the cheaper MCQ suites.

## Derived sets (`make_subsets.py`)

`make_subsets.py` builds sets from data that has already been fetched. It needs no network, and the same input and seed give byte-identical files.
- **`label`:** every item with one `meta.label_source`, as its own suite. For example, `--suite nvd-cwe --label nvd` gives `nvd-cwe-nvdlab`: the CVEs whose CWE comes from NVD's own analysis rather than the CNA. Ids are renamed to match, and the original is kept in `meta.source_id`, so the set can run next to its parent suite.
- **`subset`:** a stratified subset of an existing file, for example `--from ctibench-mcq.sample200 --n 100` gives `ctibench-mcq.sample200in100`. Use it to run a slower model on a subset of the same items, which keeps paired comparisons valid.

Both use the fetchers' stratified sampler, which is proportional per `meta.category` and keeps the source order. [`../plans/run-b-day1.sh`](../plans/run-b-day1.sh) `prepare` shows real use.

## Evaluated and not converted

| Candidate | Why not |
|---|---|
| CTIBench **TAA** | no ground-truth column in the public TSV; the paper grades it by hand or with an LLM using "plausible" labels |
| **SecEval** (XuanwuAI, CC-BY-NC-SA-4.0, 2189 items) | redundant with CyberMetric and SecQA (also GPT-4-generated knowledge MCQs); 927 items have **multiple** correct letters, and the contract has no multi-select MCQ scorer (`exact_set` over letters would work, but the harness would have to parse letter sets); 7 items have an empty answer. Easy to add later. |
| CyberSecEval **MITRE** (attack compliance) | offensive prompts; scoring grades how useful the attack help was, through an LLM expansion and judge step, which means generating offensive content. Skipped on safety grounds. Only the benign FRR half is converted. |
| CyberSecEval **crwd_meta** (CyberSOCEval malware analysis, threat-intel reasoning) | depends on Hybrid Analysis sandbox reports and CrowdStrike PDFs that are not in the repo |
| CyberSecEval instruct / autocomplete / interpreter / canary_exploit / uplift | need the Insecure Code Detector, sandboxes or an attack judge; out of scope for an offline text harness |
| **PrimeVul** (code vulnerability detection, MIT) | the official data is on Google Drive only (no stable stdlib-fetchable URL); the HF copies are unofficial re-uploads. **This leaves the main gap: there is no code-vuln or log-triage suite yet.** |

## Common caveats
- **Contamination:** every suite except `nvd-*` has been public since 2023–2024, and some are bundled in popular harnesses. Treat those scores as a regression floor and a way to compare models, not as absolute skill.
- **Prompting:**
  - MCQ suites carry only the question and `choices`, so the harness controls the template and answer extraction for all of them in the same way.
  - CTIBench RCM, VSP and ATE keep the official prompts, which ask for the answer on the last line.
- **Scorer gaps** against the contract:
  - no `refusal` scorer (used `llm_judge` for `cse-frr`);
  - no multi-select MCQ scorer (SecEval not converted);
  - SEvenLLM's free-form JSON references fit `llm_judge` better than `json_fields`.
