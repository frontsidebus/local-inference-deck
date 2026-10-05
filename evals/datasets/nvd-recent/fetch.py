#!/usr/bin/env python3
"""Recent CVEs from the NVD CVE API 2.0 -> evals/data/nvd-cwe.jsonl, nvd-cvss.jsonl.

A contamination-resistant counterpart to CTIBench RCM/VSP: CVEs published in a
fixed, recent window (default 2026-08-01..2026-08-31), labelled with the CWE and
CVSS v3.1 vector from NVD when NVD has analysed the record, otherwise from the
CNA. See README.md for the label-noise caveats.

The NVD API is a live service, so raw bytes are not stable. The pin is on the
normalized output (the two JSONL files) for the default window; raw API pages
are cached under evals/data/.cache/nvd-recent/ so reruns are offline and exact.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _common as c  # noqa: E402

API = "https://services.nvd.nist.gov/rest/json/cves/2.0"
DEFAULT_START = "2026-08-01T00:00:00.000"
DEFAULT_END = "2026-08-31T23:59:59.999"
PAGE = 2000
# sha256 of the converted JSONL for the default window (first real fetch: 2026-10-05).
PINNED = {
    "nvd-cwe": "c5c2c88b726d618a1ec5ec5d2afa03ad65d993256aacfe18ab7341e085d3538f",
    "nvd-cvss": "2b3ed302f66de97a0b1b90c08e4d3940e6a584d07971a3696fc4bde50e3f7628",
}
LICENSE = ("CVE Program Terms of Use (MITRE copyright license to reproduce and distribute CVE); "
           "NVD enrichment is a US-government work")
SOURCE = "NVD CVE API 2.0"
NVD = "nvd@nist.gov"

CWE_PROMPT = (
    "Analyze the following CVE description and map it to the most appropriate CWE. "
    "Give a brief justification. The last line of your response must contain only the "
    "CWE ID (for example CWE-79).\n\nCVE description: {desc}"
)
CVSS_PROMPT = (
    "Analyze the following CVE description and determine its CVSS v3.1 base metrics: "
    "AV, AC, PR, UI, S, C, I and A. Valid values: AV = N, A, L or P; AC = L or H; "
    "PR = N, L or H; UI = N or R; S = U or C; C, I and A = H, L or N. Briefly justify "
    "each metric. The last line of your response must contain only the CVSS v3.1 vector "
    "string (for example CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H).\n\n"
    "CVE description: {desc}"
)


def _desc(cve: dict) -> str:
    for d in cve.get("descriptions", []):
        if d.get("lang") == "en":
            return " ".join(d["value"].split())
    return ""


def pick_cwe(cve: dict) -> tuple[str, str] | None:
    """(CWE id, label source) or None. NVD's own label wins; a CNA label is used
    only when NVD has none. Ambiguous (more than one CWE) records are skipped."""
    nvd, cna = set(), set()
    for w in cve.get("weaknesses", []):
        vals = {d["value"] for d in w.get("description", []) if d.get("value", "").startswith("CWE-")}
        (nvd if w.get("source") == NVD else cna).update(vals)
    if nvd:
        return (nvd.pop(), "nvd") if len(nvd) == 1 else None
    if len(cna) == 1:
        return cna.pop(), "cna"
    return None


def pick_cvss(cve: dict) -> tuple[str, str] | None:
    ms = cve.get("metrics", {}).get("cvssMetricV31", [])
    nvd = {m["cvssData"]["vectorString"] for m in ms if m.get("source") == NVD}
    cna = {m["cvssData"]["vectorString"] for m in ms if m.get("source") != NVD}
    if nvd:
        return (nvd.pop(), "nvd") if len(nvd) == 1 else None
    if len(cna) == 1:
        return cna.pop(), "cna"
    return None


def convert(vulns: list[dict]) -> dict[str, list[dict]]:
    cves = sorted((v["cve"] for v in vulns), key=lambda x: x["id"])
    cwe_rows, cvss_rows = [], []
    for cve in cves:
        if cve.get("vulnStatus") == "Rejected":
            continue
        desc = _desc(cve)
        if len(desc) < 40:
            continue
        base = {"source": SOURCE, "license": LICENSE, "cve": cve["id"],
                "published": cve.get("published", "")[:10], "vuln_status": cve.get("vulnStatus", "")}
        cw = pick_cwe(cve)
        # Skip descriptions that state the answer outright.
        if cw and cw[0].lower() not in desc.lower():
            cwe_rows.append((cve, desc, cw, base))
        cv = pick_cvss(cve)
        if cv and "CVSS:3" not in desc and "AV:" not in desc:
            cvss_rows.append((cve, desc, cv, base))
    out_cwe = [{
        "id": c.make_id("nvd-cwe", i, len(cwe_rows)), "suite": "nvd-cwe", "type": "classify",
        "prompt": CWE_PROMPT.format(desc=desc), "answer": cw[0], "scorer": "cwe_match",
        "meta": {**base, "category": cw[0], "label_source": cw[1]},
    } for i, (cve, desc, cw, base) in enumerate(cwe_rows, 1)]
    out_cvss = [{
        "id": c.make_id("nvd-cvss", i, len(cvss_rows)), "suite": "nvd-cvss", "type": "extract",
        "prompt": CVSS_PROMPT.format(desc=desc), "answer": cv[0], "scorer": "cvss_mae",
        "meta": {**base, "category": cv[0].split("/")[1], "label_source": cv[1]},
    } for i, (cve, desc, cv, base) in enumerate(cvss_rows, 1)]
    return {"nvd-cwe": out_cwe, "nvd-cvss": out_cvss}


def fetch_window(start: str, end: str, cache: Path, offline: bool, refresh: bool) -> tuple[list[dict], list[dict]]:
    """Return (vulnerabilities, page records). Pages are cached by window + index."""
    cache.mkdir(parents=True, exist_ok=True)
    tag = f"{start[:10]}_{end[:10]}"
    headers = {}
    key = os.environ.get("NVD_API_KEY")  # optional; never printed
    if key:
        headers["apiKey"] = key
    vulns, pages, idx, total = [], [], 0, None
    while total is None or idx < total:
        path = cache / f"cves-{tag}-{idx:06d}.json"
        q = urllib.parse.urlencode({"pubStartDate": start, "pubEndDate": end,
                                    "resultsPerPage": PAGE, "startIndex": idx})
        url = f"{API}?{q}"
        if path.exists() and not refresh:
            raw = path.read_bytes()
        else:
            if offline:
                raise FileNotFoundError(f"--offline and no cached page {path}")
            if pages:
                time.sleep(6.5 if not key else 0.7)  # NVD public rate limit: 5 req / 30 s
            raw = c.http_get(url, timeout=180, headers=headers)
            path.write_bytes(raw)
        doc = json.loads(raw)
        total = doc["totalResults"]
        vulns.extend(doc["vulnerabilities"])
        pages.append({"url": url, "sha256": c.sha256_bytes(raw), "cached_as": path.name})
        idx += PAGE
        if not doc["vulnerabilities"]:
            break
    return vulns, pages


def main(argv: list[str] | None = None) -> int:
    p = c.base_parser(__doc__)
    p.add_argument("--start", default=DEFAULT_START, help="pubStartDate (NVD format)")
    p.add_argument("--end", default=DEFAULT_END, help="pubEndDate (NVD format; window <= 120 days)")
    p.add_argument("--refresh", action="store_true", help="re-download pages even if cached")
    p.add_argument("--accept-drift", action="store_true",
                   help="write output even if it no longer matches the pinned checksum")
    args = p.parse_args(argv)
    vulns, pages = fetch_window(args.start, args.end, Path(args.cache_dir) / "nvd-recent",
                                args.offline, args.refresh)
    pinned_window = (args.start, args.end) == (DEFAULT_START, DEFAULT_END)
    suites = convert(vulns)
    drift = {}
    for suite, items in suites.items():
        got = c.sha256_bytes(c.to_jsonl(items))
        if pinned_window and got != PINNED[suite]:
            drift[suite] = got
    if drift and not args.accept_drift:
        for s, got in drift.items():
            print(f"{s}: converted output sha256 {got} != pinned {PINNED[s]}", file=sys.stderr)
        print("Upstream NVD data changed (or the cache is from another fetch). Rerun with "
              "--accept-drift to use it, and record the new checksum.", file=sys.stderr)
        return 2
    for suite, items in suites.items():
        c.write_outputs(suite, items, args, pages, LICENSE,
                        {"window": {"start": args.start, "end": args.end},
                         "cves_in_window": len(vulns), "pinned": pinned_window,
                         "drift_accepted": suite in drift})
    return 0


if __name__ == "__main__":
    sys.exit(main())
