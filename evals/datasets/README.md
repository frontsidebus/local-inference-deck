# Security eval datasets

Each `<dir>/fetch.py` does four things:
1. downloads a public benchmark from its official source;
2. verifies the pinned sha256;
3. converts the data to the task format in the eval contract;
4. writes `evals/data/<suite>.jsonl`, plus a `<suite>.provenance.json` sidecar recording the source URLs, checksums, license, fetch date and output checksum.

`evals/data/` is gitignored. **No benchmark item text is committed.** What is committed are item ids, content hashes and a few labels: the test fixture `evals/tests/fixtures/rigor_items.jsonl` (20 annotated items: ids, gold answers and rigor metadata, with the prompt text removed) and the held-out manifests in `heldout/` (ids and keys). Most of these licenses are non-commercial and share-alike, or they don't state a license at all, so the fetchers regenerate the data byte-for-byte. Small invented fixtures live in `<dir>/fixtures/`.

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

## Rigor layer (`audit.py`, `--rigor`)

Off by default, so the pinned outputs stay byte-identical. Every fetcher takes `--rigor`, which adds `meta.rigor` to each item, and, with `--sample N`, also writes `<suite>.dedup.sample<N>.jsonl` from the dedup pool. `make_subsets.py label|subset --rigor` does the same for derived sets. The rules read item text and metadata only; the thresholds were fixed before anything was measured, and the reasons are in the `audit.py` docstring.

| Field in `meta.rigor` | Rule |
|---|---|
| `near_dup_cluster`, `template_cluster`, `*_size`, `dedupe_keep` | word 5-gram Jaccard ≥ 0.8 (near-duplicate, Lee et al. 2022) and ≥ 0.5 (template, Broder et al. 1997), leader clustering, MinHash LSH plus exact verification. The pool keeps ≤ `--dedupe-cap` (2) per **near-duplicate** cluster. Template clusters are not capped, because siblings are often distinct tasks with different labels; use `template_cluster` as the cluster id for cluster-robust CIs |
| `has_signal`, `lexicon_signal`, `boilerplate` | vendor risk-matrix sentences and version/upgrade filler are removed; `has_signal` = at least 5 informative tokens remain; `lexicon_signal` = a weakness term (CWE tasks) or impact term (CVSS tasks) is present (secondary, low recall) |
| `leak`, `leak_action`, `leak_stripped`, `leak_remaining` | key leaks only: CWE ids; CVSS vector fragments, scores, severity words, verbal-vector boilerplate, and `cvss_in_words` (a metric value in CVSS vocabulary, e.g. "high impact on confidentiality", "scope is changed", and vendor prose rendered from a vector: "confidentiality (high)", "high complexity attack", "via local access", "privileged user combined with", "attack requirements are present"; a bare "unauthenticated user" is evidence, not a leak); MCQ key text quoted in the stem. `--leak default` strips leaking sentences for `nvd-*` (never emptying a description) and flags public benchmarks. `leak` is what the text had; `leak_remaining` is what is left after stripping |
| `cwe_name_in_text`, `other_cwe_name_in_text` | strata, not leaks. The first is True when a name phrase of the gold CWE is in the text; the second when a phrase of some *other* CWE is. Phrases come from MITRE's catalogue ([`cwe_names.json`](cwe_names.json), built by [`build_cwe_names.py`](build_cwe_names.py) from `cwec_v4.20.xml`, with version and sha256 recorded): the name, its parenthetical, and the alternate terms, plus hand synonyms. None only for ids that are not in the catalogue. Name-in-text measures name→ID lookup. "Other" counts only Weakness entries (not Categories or Views), and single-token parentheticals ("PHP", "API", CERT chapter codes) are not phrases |
| `cwe_mapping_usage`, `cwe_abstraction` | MITRE's mapping guidance for the gold CWE (`Allowed` / `Allowed-with-Review` / `Discouraged` / `Prohibited`, from `Mapping_Notes/Usage`) and its abstraction (Base, Class, Variant, Pillar, Category …), both from `cwe_names.json` |
| `split`, `template_has_sealed`, `template_straddles_split`, `baseline_train_ok`, `cna_cluster` | `split` is `dev` (used in development), `sealed` or `other`. `baseline_train_ok` = not sealed, and no sealed item in the same template cluster: the rows a text baseline may train on. `cna_cluster` is the record's CNA, for cluster-bootstrap sensitivity. `audit.py baseline-split <files>` writes these as rows |
| `format_example_replaced` (nvd) | the answer-format example in our own prompts was a possible gold label (CWE-79 is the modal label; the 9.8 vector is the modal vector). Rigor builds show `CWE-1000`, a View that MITRE marks Prohibited for mapping, and a zero-impact vector, which occurs 0 times as a gold label. Public benchmark prompts are unchanged |
| `late_published_cve`, `late_cve_id` | the CVE id year is before the publish year; `late_cve_id` is the stricter stratum, more than one year before |
| `dev_exposed`, `dev_cluster_exposed`, `dev_neardup_exposed` | the item, a template sibling, or a near-duplicate is in a development file ([`heldout/dev-exposed-v1.json`](heldout/dev-exposed-v1.json), keys only). Sealed near-duplicate-exposed items should stay out of confirmatory primaries |
| `scorer` (cse-frr) | under `--rigor`, cse-frr items carry `scorer: refusal` (the three refusal tiers that D6 uses); `meta.legacy_scorer` keeps `llm_judge`. |
| `frr_tier`, `frr_exclude` (cse-frr) | incoherent > covert-harmful > borderline > benign, from fixed lexicons. Covert-harmful prompts (detection evasion, concealment) are excluded from the false-refusal rate |
| `first_public_date`, `first_public_basis` (nvd) | min(NVD published, CVE5 `datePublic`, reference dates). The current data has only NVD published (`pending refetch`) |
| `label_source`, `label_date` | `nvd-primary` / `cna-secondary:<cna>` / `adp-secondary:CISA-ADP` per NVD item, else the benchmark key's provenance. `meta.labels` keeps every CVSS metric and CWE weakness with role (nvd/cna/adp), source and NVD's verbatim `type`. The pinned picker treats every non-NVD source as "cna", so about 23% of the "CNA" labels are really CISA-ADP |
| `public_date`, `benchmark_release`, `post_cutoff`, `late_published_cve` | first-public date for NVD items (r4; `post_cutoff` is None for a `late_cve_id` item whose first-public date is still the NVD publish date) or benchmark release; `post_cutoff[model]` is True / False / None, using [`model_cutoffs.json`](model_cutoffs.json). Upper bounds (HF repo creation) can only give True or None |
| `heldout`, `heldout_key` | sealed held-out split, below |
| `suite_at_ceiling` | from [`suite_status.json`](suite_status.json): at least 2 models with n ≥ 100, all ≥ 90% (otherwise `insufficient` or `discriminating`) |
| `key_override` | only with `--key-overrides key_overrides.json` (empty by default; each entry needs a human justification and an external source, and model agreement is rejected as a reason) |

**Held-out split** ([`heldout/heldout-v1.json`](heldout/heldout-v1.json)): an item is sealed if it is in no file already used for development (every `*.sample*.jsonl`, every dataset a stored run read, and every committed `*.jsonl` under `evals/`, such as test fixtures and samples) and `int(sha256("lid-heldout-v1|"+key)[:8],16) % 100 < 30`. The key is the CVE id for CVE items, so a CVE is sealed in every suite at once; otherwise it is `suite|sha256(normalized content)`. The file stores only ids, keys and a commitment hash, no data. `python3 evals/datasets/audit.py verify-seal` checks the commitment against `evals/data`. Sealed items are never run during harness development. The dedup pools exclude them, and they are kept for one pre-registered confirmatory run. There is also a prospective split: NVD CVEs published 2026-10-01..31, to be fetched only for that run.

**Content binding** ([`heldout/heldout-v1.1.json`](heldout/heldout-v1.1.json)): the same membership as v1, plus a content key per sealed item, `key|sha256(canon(prompt) US json(answer))`, computed on the leak-stripped files. These content keys are unsalted sha256 of prompt and answer, so anyone holding the public prompt can confirm a candidate answer against them; this is acceptable because the source benchmarks and NVD records are public anyway, and no such key is published for prospective items. The manifest lists which sealed items changed text through stripping. `audit.heldout_content_key(item)` is the function the runner uses. `audit.py verify <files> [--complete] [--sealed-only]` exits 1 on any of these problems, matching `run.check_freeze`:
- a sealed item whose text or answer was rewritten, or whose file is unstripped;
- ids that were swapped, or a sealed item moved under another id;
- with `--complete`, sealed items missing;
- with `--sealed-only`, non-sealed extras. `audit.py seal-v11 --rigor-dir <annotated dir>` rebuilds the manifest and refuses any change of membership.

**Annotated run files:** `audit.py regen --rigor-dir <annotated dir> --out-dir <dir>` writes annotated copies of every dataset file a stored run read, under the same names. That includes the `*sample*`, `*.sysctx` and derived `nvdlab` files, whose items keep their own suite and id.

**Prospective arm** (`nvd-recent/fetch.py`):
- `--prospective` fetches CVEs published 2026-10-01..31 into suites `nvd-cwe-prospective` / `nvd-cvss-prospective`, with ids `nvdp-cwe-NNNN` / `nvdp-cvss-NNNN`. It is never pinned.
- `--label-precedence legacy|nvd,cna,adp|nvd,adp,cna` sets which source's label wins. `legacy` (the default) is the pinned behaviour: NVD first, then every other source together, with CISA ADP counted as "cna".
- `--label-mode nvd-only` keeps only the records NVD labelled itself. On the August window that is 82 CWE and 783 CVSS items.

`python3 evals/datasets/audit.py exposure` rewrites the dev-exposed key list from the sample files, stored runs and committed item files (test fixtures). It also prints any sealed key that has since been exposed, which should be 0.

`audit.py report --dir <annotated dir> [--results-dir evals/results --key-audit-out <scratch file>]` prints per-suite stats, the saturation flags, and the list of items every model answers with the same wrong answer. That list is for a human to adjudicate. Keys are never changed because models agree.

## Suite status (saturation)

From [`suite_status.json`](suite_status.json), rule R3-5. Each model is `insufficient` if it has fewer than 100 scored items, otherwise at `ceiling` (pass rate ≥ 0.90) or `below`. A suite is `at_ceiling` only when there are at least 2 models and every model is sufficient and at ceiling. It is `at_ceiling_among_sufficient` when every sufficient model is at ceiling but some models are insufficient. An equivalence claim on a saturated suite says nothing about the models. Rates come from the stored thinking-off runs of 2026-10-11.

| Suite | Status | Pass rate per model |
|---|---|---|
| `cse-frr` | not_at_ceiling | coder 0.96 (n=202), coder-fast 0.90 (n=202), big 0.99 (n=100), hermes 1.00 (n=100), vision 0.79 (n=100), local/qwen3.8-flash-next 0.91 (n=100) |
| `ctibench-ate` | insufficient | coder 0.03 (n=60, insufficient), coder-fast 0.07 (n=60, insufficient), big 0.05 (n=60, insufficient), hermes 0.03 (n=60, insufficient), vision 0.10 (n=60, insufficient), local/qwen3.8-flash-next 0.05 (n=60, insufficient) |
| `ctibench-mcq` | not_at_ceiling | coder 0.68 (n=218), coder-fast 0.69 (n=218), big 0.67 (n=100), hermes 0.72 (n=100), vision 0.74 (n=100), local/qwen3.8-flash-next 0.66 (n=100) |
| `ctibench-rcm` | not_at_ceiling | coder 0.71 (n=200), coder-fast 0.68 (n=200), big 0.70 (n=100), hermes 0.66 (n=98, insufficient), vision 0.72 (n=100), local/qwen3.8-flash-next 0.75 (n=100) |
| `ctibench-vsp` | not_at_ceiling | coder 0.59 (n=200), coder-fast 0.54 (n=200), big 0.55 (n=100), hermes 0.70 (n=100), vision 0.72 (n=100), local/qwen3.8-flash-next 0.73 (n=100) |
| `cybermetric-500` | at_ceiling | coder 0.96 (n=500), coder-fast 0.96 (n=500), big 0.92 (n=100), hermes 0.95 (n=100), vision 1.00 (n=100), local/qwen3.8-flash-next 0.96 (n=100) |
| `nvd-cvss` | not_at_ceiling | coder 0.47 (n=238), coder-fast 0.44 (n=238), big 0.43 (n=100), hermes 0.51 (n=100), vision 0.47 (n=100), local/qwen3.8-flash-next 0.54 (n=100) |
| `nvd-cvss-nvdlab` | not_at_ceiling | coder 0.61 (n=247), coder-fast 0.53 (n=247), big 0.48 (n=100), hermes 0.47 (n=100), vision 0.59 (n=100), local/qwen3.8-flash-next 0.60 (n=100) |
| `nvd-cwe` | not_at_ceiling | coder 0.62 (n=248), coder-fast 0.55 (n=248), big 0.50 (n=100), hermes 0.46 (n=100), vision 0.55 (n=100), local/qwen3.8-flash-next 0.61 (n=100) |
| `nvd-cwe-nvdlab` | not_at_ceiling | coder 0.26 (n=130), coder-fast 0.35 (n=130), big 0.24 (n=82, insufficient), hermes 0.21 (n=82, insufficient), vision 0.23 (n=82, insufficient), local/qwen3.8-flash-next 0.24 (n=82, insufficient) |
| `secqa-v1` | insufficient | coder 1.00 (n=50, insufficient), coder-fast 1.00 (n=50, insufficient) |
| `secqa-v2` | insufficient | coder 0.98 (n=50, insufficient), coder-fast 1.00 (n=50, insufficient) |
| `sevenllm-mcq` | insufficient | coder 0.98 (n=50, insufficient), coder-fast 0.98 (n=50, insufficient), big 0.94 (n=50, insufficient), hermes 0.98 (n=50, insufficient), vision 0.98 (n=50, insufficient), local/qwen3.8-flash-next 1.00 (n=50, insufficient) |
| `sevenllm-qa` | not_at_ceiling | coder 0.85 (n=100), coder-fast 0.75 (n=100), big 0.85 (n=100), hermes 0.75 (n=100), vision 0.70 (n=99, insufficient), local/qwen3.8-flash-next 0.75 (n=100) |

Proposal: retire `cybermetric-500` from the headline tables and keep it as a regression floor. `secqa-*`, `sevenllm-mcq` and `ctibench-ate` cannot be judged: they have too few items per model, and sevenllm-mcq has only 50 items in total.
