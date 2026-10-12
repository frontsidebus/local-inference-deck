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
# Prospective confirmatory arm (PROTOCOL): fetch only after the window has closed and settled.
PROSPECTIVE_START = "2026-10-01T00:00:00.000"
PROSPECTIVE_END = "2026-10-31T23:59:59.999"
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


# Label precedence (R3-4). Each tier is a set of source roles; the first tier with any label wins, and a tier
# with more than one distinct value makes the record ambiguous (skipped). "legacy" is the pinned behaviour:
# NVD, then every other source lumped together (CNA and CISA ADP).
PRECEDENCE = {
    "legacy": (("nvd",), ("cna", "adp")),
    "nvd,cna,adp": (("nvd",), ("cna",), ("adp",)),
    "nvd,adp,cna": (("nvd",), ("adp",), ("cna",)),
    "nvd-only": (("nvd",),),
}


def _pick(entries: list[tuple[str, str]], precedence: str) -> tuple[str, str] | None:
    """entries = [(role, value)]. Returns (value, label source) or None."""
    for tier in PRECEDENCE[precedence]:
        vals = {v for r, v in entries if r in tier}
        if vals:
            if len(vals) != 1:
                return None
            # legacy keeps its historical names: "nvd", or "cna" for every other source (ADP included)
            src = ("nvd" if tier == ("nvd",) else "cna") if precedence == "legacy" else tier[0]
            return vals.pop(), src
    return None


def pick_cwe(cve: dict, precedence: str = "legacy") -> tuple[str, str] | None:
    """(CWE id, label source) or None. With the default (legacy) precedence NVD's own label wins and any other
    source's label is used only when NVD has none. Ambiguous (more than one CWE) records are skipped."""
    entries = [(source_role(w.get("source", ""), cve), d["value"]) for w in cve.get("weaknesses", [])
               for d in w.get("description", []) if d.get("value", "").startswith("CWE-")]
    return _pick(entries, precedence)


def pick_cvss(cve: dict, precedence: str = "legacy") -> tuple[str, str] | None:
    entries = [(source_role(m.get("source", ""), cve), m["cvssData"]["vectorString"])
               for m in cve.get("metrics", {}).get("cvssMetricV31", [])]
    return _pick(entries, precedence)


ADP_CISA = "134c704f-9b21-4f2e-91b3-4a467353bcc0"  # CISA ADP (Vulnrichment)


def source_role(source: str, cve: dict) -> str:
    """nvd = NVD's own analysis; adp = CISA ADP; cna = anything else (normally the record's sourceIdentifier)."""
    if source == NVD:
        return "nvd"
    if source == ADP_CISA:
        return "adp"
    return "cna"


def label_fields(cve: dict, cve5: dict | None = None, ref_dates: dict | None = None) -> dict:
    """Both label sources of a record, kept apart (NVD primary vs CNA secondary), plus per-entry provenance
    (role, source, NVD type Primary/Secondary) for every CVSS metric and CWE weakness, and the first-public date.
    Added to meta.labels under --rigor (not in the pinned output). The legacy cwe_cna/cvss_cna lists keep the
    pinned picker's meaning: every non-NVD source (so they include ADP entries; see *_adp)."""
    import audit  # noqa: PLC0415  (lazy: audit imports _common)
    cwe_nvd, cwe_cna, cwe_adp = set(), set(), set()
    weaknesses = []
    for w in cve.get("weaknesses", []):
        vals = sorted({d["value"] for d in w.get("description", []) if d.get("value", "").startswith("CWE-")})
        role = source_role(w.get("source", ""), cve)
        weaknesses.append({"role": role, "source": w.get("source", ""), "type": w.get("type"), "cwes": vals})
        (cwe_nvd if role == "nvd" else cwe_cna).update(vals)
        if role == "adp":
            cwe_adp.update(vals)
    metrics = []
    for key, ms in sorted(cve.get("metrics", {}).items()):
        if not key.startswith("cvssMetric"):
            continue
        for m in ms:
            metrics.append({"role": source_role(m.get("source", ""), cve), "source": m.get("source", ""),
                            "type": m.get("type"), "version": (m.get("cvssData") or {}).get("version"),
                            "vector": (m.get("cvssData") or {}).get("vectorString")})
    v31 = [m for m in metrics if m["version"] == "3.1"]
    date, basis = audit.first_public_date(cve.get("published"), cve5, ref_dates)
    if cve5 is None and not ref_dates:
        basis += " (pending refetch: CVE5 datePublic and reference dates not fetched)"
    return {"cna": cve.get("sourceIdentifier", ""),
            "last_modified": cve.get("lastModified", "")[:10],
            "cwe_nvd": sorted(cwe_nvd), "cwe_cna": sorted(cwe_cna), "cwe_adp": sorted(cwe_adp),
            "cvss_nvd": sorted({m["vector"] for m in v31 if m["role"] == "nvd"}),
            "cvss_cna": sorted({m["vector"] for m in v31 if m["role"] != "nvd"}),
            "cvss_adp": sorted({m["vector"] for m in v31 if m["role"] == "adp"}),
            "metrics": metrics, "weaknesses": weaknesses,
            "first_public": {"date": date, "basis": basis}}


def add_label_fields(suites: dict[str, list[dict]], vulns: list[dict]) -> dict[str, list[dict]]:
    by_id = {v["cve"]["id"]: v["cve"] for v in vulns}
    return {s: [dict(it, meta={**it["meta"], "labels": label_fields(by_id[it["meta"]["cve"]])}) for it in items]
            for s, items in suites.items()}


def convert(vulns: list[dict], precedence: str = "legacy", prospective: bool = False) -> dict[str, list[dict]]:
    """Defaults give the pinned output. prospective=True names the suites nvd-cwe-prospective /
    nvd-cvss-prospective and the ids nvdp-cwe-NNNN / nvdp-cvss-NNNN (R3-4)."""
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
        cw = pick_cwe(cve, precedence)
        # Skip descriptions that state the answer outright.
        if cw and cw[0].lower() not in desc.lower():
            cwe_rows.append((cve, desc, cw, base))
        cv = pick_cvss(cve, precedence)
        if cv and "CVSS:3" not in desc and "AV:" not in desc:
            cvss_rows.append((cve, desc, cv, base))
    s_cwe, s_cvss = ("nvd-cwe-prospective", "nvd-cvss-prospective") if prospective else ("nvd-cwe", "nvd-cvss")
    p_cwe, p_cvss = ("nvdp-cwe", "nvdp-cvss") if prospective else ("nvd-cwe", "nvd-cvss")
    out_cwe = [{
        "id": c.make_id(p_cwe, i, len(cwe_rows)), "suite": s_cwe, "type": "classify",
        "prompt": CWE_PROMPT.format(desc=desc), "answer": cw[0], "scorer": "cwe_match",
        "meta": {**base, "category": cw[0], "label_source": cw[1]},
    } for i, (cve, desc, cw, base) in enumerate(cwe_rows, 1)]
    out_cvss = [{
        "id": c.make_id(p_cvss, i, len(cvss_rows)), "suite": s_cvss, "type": "extract",
        "prompt": CVSS_PROMPT.format(desc=desc), "answer": cv[0], "scorer": "cvss_mae",
        "meta": {**base, "category": cv[0].split("/")[1], "label_source": cv[1]},
    } for i, (cve, desc, cv, base) in enumerate(cvss_rows, 1)]
    return {s_cwe: out_cwe, s_cvss: out_cvss}


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
    p.add_argument("--prospective", action="store_true",
                   help=f"the sealed prospective arm: window {PROSPECTIVE_START[:10]}..{PROSPECTIVE_END[:10]} unless "
                        "--start/--end are given, suites nvd-*-prospective, ids nvdp-*; never pinned")
    p.add_argument("--label-precedence", choices=[k for k in PRECEDENCE if k != "nvd-only"], default="legacy",
                   help="which source's label wins (legacy = NVD, then any other source; the pinned behaviour)")
    p.add_argument("--label-mode", choices=["any", "nvd-only"], default="any",
                   help="nvd-only keeps only records NVD itself labelled")
    args = p.parse_args(argv)
    precedence = "nvd-only" if args.label_mode == "nvd-only" else args.label_precedence
    if args.prospective and (args.start, args.end) == (DEFAULT_START, DEFAULT_END):
        args.start, args.end = PROSPECTIVE_START, PROSPECTIVE_END
    vulns, pages = fetch_window(args.start, args.end, Path(args.cache_dir) / "nvd-recent",
                                args.offline, args.refresh)
    pinned_window = ((args.start, args.end) == (DEFAULT_START, DEFAULT_END) and not args.prospective
                     and precedence == "legacy")
    suites = convert(vulns, precedence, args.prospective)
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
    if args.rigor:
        suites = add_label_fields(suites, vulns)
    for suite, items in suites.items():
        c.write_outputs(suite, items, args, pages, LICENSE,
                        {"window": {"start": args.start, "end": args.end},
                         "cves_in_window": len(vulns), "pinned": pinned_window,
                         "drift_accepted": suite in drift, "label_precedence": precedence,
                         "prospective": args.prospective})
    return 0


if __name__ == "__main__":
    sys.exit(main())
