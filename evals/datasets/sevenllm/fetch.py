#!/usr/bin/env python3
"""SEvenLLM-Bench (Multilingual-Multimodal-NLP/SEVENLLM-Dataset, test.jsonl)
-> evals/data/sevenllm-mcq.jsonl and sevenllm-qa.jsonl (English by default).

The source mixes English and Chinese items; --lang picks en (default), zh or all.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _common as c  # noqa: E402

REVISION = "1de23ce55cadc984d3f3a7b52c4035a68c6cd5b0"
URL = f"https://huggingface.co/datasets/Multilingual-Multimodal-NLP/SEVENLLM-Dataset/resolve/{REVISION}/test.jsonl"
SHA256 = "08a27277386b1f205ed0814d379bb0db9bdce8f39bde099020a1f8044d404d7b"
LICENSE = "Apache-2.0"
SOURCE = "SEvenLLM-Bench"
_CJK = re.compile("[一-鿿]")


def lang_of(row: dict) -> str:
    text = json.dumps([row.get("instruction"), row.get("input")], ensure_ascii=False)
    return "zh" if _CJK.search(text) else "en"


def convert(lines: list[str], lang: str = "en") -> dict[str, list[dict]]:
    rows = [json.loads(l) for l in lines if l.strip()]
    rows = [r for r in rows if lang == "all" or lang_of(r) == lang]
    mcq_rows = [r for r in rows if isinstance(r["instruction"], dict)]
    qa_rows = [r for r in rows if not isinstance(r["instruction"], dict)]

    def meta(r: dict) -> dict:
        return {"source": SOURCE, "license": LICENSE, "category": r["category"],
                "lang": lang_of(r), "source_id": r["id"]}

    mcq = []
    for i, r in enumerate(mcq_rows, 1):
        ins = r["instruction"]
        letters = sorted(ins["choice"])
        mcq.append({
            "id": c.make_id("sevenllm-mcq", i, len(mcq_rows)),
            "suite": "sevenllm-mcq", "type": "mcq",
            "prompt": f"{r['input'].strip()}\n\n{ins['question'].strip()}",
            "choices": c.letter_choices((k, ins["choice"][k]) for k in letters),
            "answer": str(r["output"]).strip(), "scorer": "mcq_letter",
            "meta": meta(r),
        })
    qa = []
    for i, r in enumerate(qa_rows, 1):
        structured = not isinstance(r["output"], str)
        ref = json.dumps(r["output"], ensure_ascii=False) if structured else r["output"].strip()
        qa.append({
            "id": c.make_id("sevenllm-qa", i, len(qa_rows)),
            "suite": "sevenllm-qa", "type": "extract" if structured else "freeform",
            "prompt": f"{r['instruction'].strip()}\n\n{r['input'].strip()}",
            "answer": ref, "scorer": "llm_judge",
            "meta": {**meta(r), "reference_format": "json" if structured else "text"},
        })
    return {"sevenllm-mcq": mcq, "sevenllm-qa": qa}


def main(argv: list[str] | None = None) -> int:
    p = c.base_parser(__doc__)
    p.add_argument("--lang", choices=["en", "zh", "all"], default="en")
    args = p.parse_args(argv)
    path = c.download(URL, Path(args.cache_dir) / "sevenllm" / "test.jsonl", SHA256, args.offline)
    suites = convert(path.read_text(encoding="utf-8").splitlines(), args.lang)
    for suite, items in suites.items():
        c.write_outputs(suite, items, args,
                        [{"url": URL, "sha256": SHA256, "revision": REVISION}], LICENSE,
                        {"lang": args.lang})
    return 0


if __name__ == "__main__":
    sys.exit(main())
