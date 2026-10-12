#!/usr/bin/env python3
"""Build evals/scoring_data/cwe-childof.json from the public MITRE CWE XML catalog (stdlib only, offline).

    curl -sSLO https://cwe.mitre.org/data/xml/cwec_latest.xml.zip && unzip cwec_latest.xml.zip
    python3 evals/tools/build_cwe_hierarchy.py cwec_v4.20.xml

Keeps only ChildOf relations of Weakness entries in view 1000 (Research Concepts, the full hierarchy) and view 1003
(Weaknesses for Simplified Mapping of Published Vulnerabilities, the view NVD labels use). Deprecated entries are
dropped. The output records the catalog version, date and sha256 so the mapping is reproducible. It is used only
by scorers.cwe_match for the separate ``cwe_related_match`` field (one hop: the predicted CWE is a direct parent or
child of the gold CWE); it never changes ``passed``.
"""
from __future__ import annotations

import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

NS = "{http://cwe.mitre.org/cwe-7}"
VIEWS = ("1000", "1003")
OUT = Path(__file__).resolve().parent.parent / "scoring_data" / "cwe-childof.json"


def build(xml_path: Path) -> dict:
    root = ET.parse(xml_path).getroot()
    child_of: dict = {}
    for w in root.iter(NS + "Weakness"):
        if w.get("Status") == "Deprecated":
            continue
        cid = int(w.get("ID"))
        for r in w.iter(NS + "Related_Weakness"):
            if r.get("Nature") == "ChildOf" and r.get("View_ID") in VIEWS:
                child_of.setdefault(cid, set()).add(int(r.get("CWE_ID")))
    return {
        "source": "MITRE CWE XML catalog (https://cwe.mitre.org/data/xml/cwec_latest.xml.zip)",
        "catalog_version": root.get("Version"),
        "catalog_date": root.get("Date"),
        "catalog_sha256": hashlib.sha256(xml_path.read_bytes()).hexdigest(),
        "relation": "ChildOf",
        "views": list(VIEWS),
        "license": "CWE Terms of Use (MITRE; free to use, copy and distribute with attribution)",
        "child_of": {str(k): sorted(v) for k, v in sorted(child_of.items())},
    }


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    data = build(Path(argv[0]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, separators=(",", ":"), sort_keys=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(data['child_of'])} CWEs with parents, catalog v{data['catalog_version']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
