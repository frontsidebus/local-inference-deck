#!/usr/bin/env python3
"""SecQA (zefang-liu/secqa) test splits -> evals/data/secqa-v1.jsonl, secqa-v2.jsonl.

The dev (5 items) and val splits are not converted; dev is meant for few-shot
exemplars and the harness runs zero-shot.
"""
from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _common as c  # noqa: E402

REVISION = "d00a07484283be5602e2bae36dbefdaaf555a9fb"
BASE = f"https://huggingface.co/datasets/zefang-liu/secqa/resolve/{REVISION}/data"
LICENSE = "CC-BY-NC-SA-4.0"
SOURCE = "SecQA"
FILES = {
    "v1": ("secqa_v1_test.csv", "9a333f23d89d0d3d6e883e5ab3be474327d4f57d2684c3d1ada787d510738bfd"),
    "v2": ("secqa_v2_test.csv", "b04f92a17b278e9765fa262103fa4127905fbae46d9eaf54f5444f986fc9c722"),
}


def convert(text: str, version: str) -> list[dict]:
    suite = f"secqa-{version}"
    rows = list(csv.DictReader(io.StringIO(text)))
    out = []
    for i, r in enumerate(rows, 1):
        out.append({
            "id": c.make_id(suite, i, len(rows)),
            "suite": suite, "type": "mcq",
            "prompt": r["Question"].strip(),
            "choices": c.letter_choices((k, r[k]) for k in "ABCD"),
            "answer": r["Answer"].strip(), "scorer": "mcq_letter",
            "meta": {"source": SOURCE, "license": LICENSE,
                     "explanation": r.get("Explanation", "").strip()},
        })
    return out


def main(argv: list[str] | None = None) -> int:
    p = c.base_parser(__doc__)
    p.add_argument("--versions", default="v1,v2")
    args = p.parse_args(argv)
    cache = Path(args.cache_dir) / "secqa"
    for v in args.versions.split(","):
        name, sha = FILES[v]
        url = f"{BASE}/{name}"
        path = c.download(url, cache / name, sha, args.offline)
        items = convert(path.read_text(encoding="utf-8"), v)
        c.write_outputs(f"secqa-{v}", items, args,
                        [{"url": url, "sha256": sha, "revision": REVISION}], LICENSE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
