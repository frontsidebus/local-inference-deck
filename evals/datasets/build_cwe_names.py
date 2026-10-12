#!/usr/bin/env python3
"""Build evals/datasets/cwe_names.json (CWE id -> name phrases) from the public MITRE CWE XML catalogue.
Stdlib only, offline once the XML is downloaded.

    curl -sSLO https://cwe.mitre.org/data/xml/cwec_latest.xml.zip && unzip cwec_latest.xml.zip
    python3 evals/datasets/build_cwe_names.py cwec_v4.20.xml

Phrases for each non-deprecated Weakness and Category (rule R3-1, fixed before measuring):
  (a) the Name with any parenthetical removed;
  (b) the content of each parenthetical, split on " or " and ",";
  (c) every Alternate_Term, split on " / ".
A phrase is kept if it has at least 2 word tokens, or if it is a single all-caps acronym of 3+ letters in the
source (XSS, CSRF, SSRF ...). Phrases are stored normalized (audit._plain form, without the padding spaces).
The hand synonyms in audit.CWE_NAMES are merged in at load time, not here.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit  # noqa: E402

NS = "{http://cwe.mitre.org/cwe-7}"
OUT = Path(__file__).resolve().parent / "cwe_names.json"


def phrases_of(name: str, alt_terms: list[str]) -> list[str]:
    raw = [re.sub(r"\([^)]*\)", " ", name)]
    for par in re.findall(r"\(([^)]*)\)", name):
        # R4-1b: a single-token parenthetical is a platform / language / chapter code ("PHP", "API", "IDS")
        raw += [p for p in re.split(r"\s+or\s+|,", par.strip(" '\"")) if len(re.findall(r"[A-Za-z0-9]+", p)) >= 2]
    for t in alt_terms:
        raw += t.split(" / ")
    out = []
    for p in raw:
        p = p.strip(" '\"")
        if not p:
            continue
        words = re.findall(r"[A-Za-z0-9]+", p)
        if len(words) >= 2 or (len(words) == 1 and re.fullmatch(r"[A-Z]{3,}", words[0])):
            norm = audit._plain(p).strip()
            if norm and norm not in out:
                out.append(norm)
    return out


def build(xml_path: Path) -> dict:
    root = ET.parse(xml_path).getroot()
    names: dict[str, dict] = {}
    for tag in ("Weakness", "Category", "View"):
        for w in root.iter(NS + tag):
            if w.get("Status") == "Deprecated":
                continue
            terms = [t.text or "" for t in w.iter(NS + "Term")]
            mn = w.find(NS + "Mapping_Notes")
            usage = mn.find(NS + "Usage").text if mn is not None and mn.find(NS + "Usage") is not None else None
            names[f"CWE-{w.get('ID')}"] = {
                "name": w.get("Name"), "kind": tag.lower(),
                "abstraction": w.get("Abstraction") or tag, "usage": usage,  # R4-3
                "phrases": phrases_of(w.get("Name") or "", terms) if tag != "View" else []}
    return {"source": "MITRE CWE XML catalogue (https://cwe.mitre.org/data/xml/cwec_latest.xml.zip)",
            "catalog_version": root.get("Version"), "catalog_date": root.get("Date"),
            "catalog_sha256": hashlib.sha256(xml_path.read_bytes()).hexdigest(),
            "license": "CWE Terms of Use (MITRE; free to use, copy and distribute with attribution)",
            "rule": "R3-1/R4-1: name minus parentheticals, parenthetical contents of >=2 tokens, alternate terms; "
                    ">=2 tokens or an all-caps acronym of >=3 letters. usage = Mapping_Notes/Usage (R4-3)",
            "cwes": dict(sorted(names.items(), key=lambda kv: int(kv[0][4:])))}


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    doc = build(Path(argv[0]))
    OUT.write_text(json.dumps(doc, indent=0, ensure_ascii=False) + "\n")
    print(f"{OUT.name}: {len(doc['cwes'])} entries from CWE {doc['catalog_version']} ({doc['catalog_date']})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
