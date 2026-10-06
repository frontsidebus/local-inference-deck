# CTIBench (`ctibench-mcq`, `ctibench-rcm`, `ctibench-vsp`, `ctibench-ate`)

Cyber threat intelligence tasks from the CTIBench paper.

- **Source:** https://huggingface.co/datasets/AI4Sec/cti-bench, pinned to revision `9237e1636ee3e168fbe5ebdcc1c571de0525e568`; the TSVs come straight from the HF `resolve/` URLs.
- **License:** the dataset card front matter says `license: cc-by-nc-sa-4.0`. (The card's prose field reads "License: [More Information Needed]". We go by the front matter.)
- **Paper:** https://arxiv.org/abs/2406.07599

```bibtex
@misc{alam2024ctibench,
  title={CTIBench: A Benchmark for Evaluating LLMs in Cyber Threat Intelligence},
  author={Md Tanvirul Alam and Dipkamal Bhushal and Le Nguyen and Nidhi Rastogi},
  year={2024}, eprint={2406.07599}, archivePrefix={arXiv}, primaryClass={cs.CR}
}
```

## Subtasks

| Suite | Items | Type | Scorer | What it measures | Prompt |
|---|---|---|---|---|---|
| `ctibench-mcq` | 2500 | mcq | `mcq_letter` | CTI knowledge: ATT&CK, CWE, CAPEC, NIST/STIX/TAXII/Diamond Model | the question, plus `choices` (the official prompt wrapper is dropped so all MCQ suites render the same way) |
| `ctibench-rcm` | 1000 | classify | `cwe_match` | map a 2024 NVD CVE description to its CWE | the official `Prompt` column, verbatim |
| `ctibench-vsp` | 1000 | extract | `cvss_mae` | predict the CVSS v3.1 vector from a CVE description; `answer` is the vector string | the official `Prompt`, verbatim |
| `ctibench-ate` | 60 | extract | `exact_set` | extract ATT&CK technique IDs from a software description (Enterprise 47, Mobile 13); `answer` is the sorted, de-duplicated list of `Txxxx` IDs | the official `Prompt`, verbatim (it embeds the full technique list, about 8k chars) |

`meta.category`:
- MCQ: the source, e.g. `attack.mitre.org/techniques` (1578), `cwe.mitre.org/data` (543), `capec.mitre.org/data` (217), or `doc:<document>` (162) for the questions generated from standards documents.
- ATE: the ATT&CK platform.

`--sample N` stratifies on `meta.category`.

## Pinned files (sha256)

| File | sha256 |
|---|---|
| cti-mcq.tsv | `11e0af8db2dae9706c6e79901d2390bcdf9746e8feccbd9ba10682df649bcc6a` |
| cti-rcm.tsv | `ed7d7fa79fc912c627f96e229eaa38b3c4b6be813d834d434580be6d4703d700` |
| cti-vsp.tsv | `43d4ceefe042236a7d0885cb8506bf6537426cf2359eddb2450dd151864a498b` |
| cti-ate.tsv | `79e5354a5c6aae0c979389786853e768201f8d129fb0f0ca5af392555f238f28` |

## Excluded
- **CTI-TAA** (threat-actor attribution, 50 items): the public TSV has no ground-truth column, and the paper grades it by hand or with an LLM, using "correct / plausible" categories. It can't be scored reproducibly, so it is skipped.
- **cti-rcm-2021**: an older variant, not converted. Add it to `FILES` if you want it.

## Caveats
- **Contamination:** the set has been public since mid-2024. The MCQ items are built from ATT&CK, CWE and CAPEC pages that are almost certainly in pretraining data, and the RCM/VSP CVEs are from 2024. Use `nvd-cwe` and `nvd-cvss` for a fresher signal.
- **Label noise:**
  - MCQ items were GPT-generated. One source label is lowercase (`b`; it is normalized to `B`), and 5 items have an empty option text (rows 57, 1031, 1310 and 1408 have an empty D; row 2236 has an empty C). They are kept as they are.
  - The MCQ answer distribution is skewed (C 928, B 813, D 385, A 374), so a model that always answers "C" scores 37%. Compare against that floor, not 25%.
- RCM labels are NVD's CWE assignments, which are themselves sometimes debatable. The distribution is dominated by CWE-79 (229 of 1000).
- VSP uses NVD's v3.1 vectors as ground truth. How `cvss_mae` turns a vector into a base score is the harness's choice.
- **License:** non-commercial, share-alike. The converted data is **not committed** (it is gitignored); `fetch.py` regenerates it.

## Run
```
python3 evals/datasets/ctibench/fetch.py [--tasks mcq,rcm,vsp,ate] [--sample 50] [--offline]
```
