#!/usr/bin/env python3
"""CyberMetric (github.com/cybermetric/CyberMetric) -> evals/data/cybermetric-<size>.jsonl."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _common as c  # noqa: E402

COMMIT = "294662b03be73a9c7c73918f687882c1ba637c47"
BASE = f"https://raw.githubusercontent.com/cybermetric/CyberMetric/{COMMIT}"
# The GitHub repo has no LICENSE file; the first author's Hugging Face mirror
# (tihanyin/CyberMetric) declares apache-2.0. See README.md.
LICENSE = "Apache-2.0 (declared on the authors' HF mirror; no LICENSE file in the GitHub repo)"
SOURCE = "CyberMetric"
FILES = {
    "80": "ffdda528c5f344871ad0a9ecef5c64ccf9964d68c3047ac0adaac520b2fc7dc6",
    "500": "b0dcf5cb0b51792e3134bec8cecadfe9c5ecc543fb5abfcece99f041066701a5",
    "2000": "28129b8d5cae82a80ba76bb31eb029e1238d1e348e6d0e85f44526583e27cd03",
    "10000": "bd4ff2a96a930211ebd2429639bc386833604c1de783806941541be6b6367ec1",
}


def convert(doc: dict, size: str) -> list[dict]:
    suite = f"cybermetric-{size}"
    qs = doc["questions"]
    out = []
    for i, q in enumerate(qs, 1):
        letters = sorted(q["answers"])
        out.append({
            "id": c.make_id(suite, i, len(qs)),
            "suite": suite, "type": "mcq",
            "prompt": q["question"].strip(),
            "choices": c.letter_choices((k, q["answers"][k]) for k in letters),
            "answer": q["solution"].strip(), "scorer": "mcq_letter",
            "meta": {"source": SOURCE, "license": LICENSE},
        })
    return out


def main(argv: list[str] | None = None) -> int:
    p = c.base_parser(__doc__)
    p.add_argument("--size", default="80,500", help="comma list of 80,500,2000,10000 (default 80,500)")
    args = p.parse_args(argv)
    cache = Path(args.cache_dir) / "cybermetric"
    for size in args.size.split(","):
        name = f"CyberMetric-{size}-v1.json"
        url = f"{BASE}/{name}"
        path = c.download(url, cache / name, FILES[size], args.offline)
        items = convert(json.loads(path.read_text(encoding="utf-8")), size)
        c.write_outputs(f"cybermetric-{size}", items, args,
                        [{"url": url, "sha256": FILES[size], "revision": COMMIT}], LICENSE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
