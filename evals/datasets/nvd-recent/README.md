# Recent NVD CVEs (`nvd-cwe`, `nvd-cvss`)

A fresh, contamination-resistant version of CTIBench RCM and VSP. The CVEs were **published 2026-08-01 to 2026-08-31**, which is after the training cutoff of most models we run. Each one is labelled with its CWE (`nvd-cwe`) and its CVSS v3.1 vector (`nvd-cvss`).

- **Source:** NVD CVE API 2.0, `https://services.nvd.nist.gov/rest/json/cves/2.0?pubStartDate=…&pubEndDate=…`.
  - The API is fetched in pages of 2000, sleeping 6.5 s between requests (the public rate limit is 5 requests per 30 s).
  - `NVD_API_KEY` is optional. It is read from the environment and never printed.
- **License / terms:**
  - CVE records fall under the **CVE Program Terms of Use** (https://www.cve.org/Legal/TermsOfUse; text checked in `CVEProject/cve-website`, `src/views/Legal/TermsOfUse.vue`): "MITRE hereby grants you a perpetual, worldwide, non-exclusive, no-charge, royalty-free, irrevocable copyright license to reproduce, prepare derivative works of, publicly display, publicly perform, sublicense, and distribute Common Vulnerabilities and Exposures (CVE™). Any copy you make for such purposes is authorized provided that you reproduce MITRE's copyright designation and this license in any such copy."
  - The NVD enrichment (NVD's CWE and CVSS) is NIST's work. NVD's terms-of-use page renders client-side and could not be quoted from a fetch; check https://nvd.nist.gov/developers/terms-of-use before redistributing.
  - Data is **not committed**.
- **Citation:** NIST National Vulnerability Database, https://nvd.nist.gov/. There is no paper; the task design follows CTIBench RCM and VSP (arXiv:2406.07599).

## Determinism
The API is live, so raw bytes change as NVD and CNAs update records.
- Raw pages are cached in `evals/data/.cache/nvd-recent/`, and their sha256 values are recorded in the provenance sidecar. Reruns from the cache (`--offline`) are exact.
- `PINNED` in `fetch.py` holds the sha256 of the **converted** JSONL for the default window. The values come from the first fetch, on 2026-10-05:
  - `nvd-cwe`: `c5c2c88b726d618a1ec5ec5d2afa03ad65d993256aacfe18ab7341e085d3538f`
  - `nvd-cvss`: `2b3ed302f66de97a0b1b90c08e4d3940e6a584d07971a3696fc4bde50e3f7628`
- A fresh fetch whose output differs exits with status 2. `--accept-drift` writes it anyway, and the provenance file records `drift_accepted`.
  - To compare models, keep one cache, or one converted file, for the whole comparison.
- `--start` and `--end` choose another window (at most 120 days). Other windows are unpinned.

## Items (window 2026-08-01..31; 12,719 CVEs published)

| Suite | Items | Type | Scorer | Label source |
|---|---|---|---|---|
| `nvd-cwe` | 9199 | classify | `cwe_match` | NVD 82, CNA 9117 |
| `nvd-cvss` | 9552 | extract | `cvss_mae` | NVD 783, CNA 8769 |

The prompts are our own wording, modelled on CTIBench's (CTIBench's text is CC-BY-NC-SA, so it is not copied). Each one asks for the answer alone on the last line.

`meta.category` is the CWE (for `nvd-cwe`) or the attack vector (for `nvd-cvss`), so `--sample N` keeps the label distribution. **Use `--sample`.** The full sets are large for a local model.

### Selection rules (in `convert()`)
- Drop records with `vulnStatus == Rejected` (461 in the window), and records whose English description is under 40 characters.
- **CWE:** if NVD (`nvd@nist.gov`) lists a `CWE-*`, that label wins. Otherwise the CNA's label is used.
  - Records with more than one distinct CWE from the chosen source are skipped as ambiguous.
  - `NVD-CWE-noinfo` and `NVD-CWE-Other` are not labels.
- **CVSS:** the same precedence over `cvssMetricV31`. Records with only CVSS v4.0 or v3.0 are skipped (`cvss_mae` is a v3.1 scorer).
- **Leak filter:** skip a CWE item whose description names its own CWE ID, and a CVSS item whose description contains a vector (`CVSS:3` or `AV:`).

## Caveats
- **Label noise is high:**
  - 99% of the CWE labels and 92% of the CVSS vectors come from CNAs, because NVD's analysis backlog left most of 2026 "Deferred" or "Awaiting Analysis".
  - CNA labels are inconsistent. One vendor CNA's quarterly bulk filing accounts for 765 of the 1067 CWE-284 labels, which makes CWE-284 the most common label, ahead of CWE-79 (696).
  - Expect lower agreement than on CTIBench RCM, where all labels were NVD-analysed.
  - `meta.label_source` lets you score the NVD-labelled subset on its own.
- **Contamination:**
  - Fresh by publication date. But about 3% of the records are older CVE IDs (2019–2025) published late, and the issues behind them may be public elsewhere.
  - A model with web or tool access could look the CVE up. The harness should not give it tools for this suite.
- **The distribution is skewed** toward web and plugin bugs (XSS, missing authorization, SQLi, path traversal), the same as NVD as a whole.

## Run
```
python3 evals/datasets/nvd-recent/fetch.py [--sample 100] [--offline] [--accept-drift] [--start … --end …]
```
