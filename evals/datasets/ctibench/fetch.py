#!/usr/bin/env python3
"""CTIBench (AI4Sec/cti-bench) -> evals/data/ctibench-{mcq,rcm,vsp,ate}.jsonl.

CTI-TAA is skipped: the public TSV has no ground-truth column.
"""
from __future__ import annotations

import csv
import io
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _common as c  # noqa: E402

REVISION = "9237e1636ee3e168fbe5ebdcc1c571de0525e568"
BASE = f"https://huggingface.co/datasets/AI4Sec/cti-bench/resolve/{REVISION}"
LICENSE = "CC-BY-NC-SA-4.0"
SOURCE = "CTIBench"
FILES = {
    "mcq": ("cti-mcq.tsv", "11e0af8db2dae9706c6e79901d2390bcdf9746e8feccbd9ba10682df649bcc6a"),
    "rcm": ("cti-rcm.tsv", "ed7d7fa79fc912c627f96e229eaa38b3c4b6be813d834d434580be6d4703d700"),
    "vsp": ("cti-vsp.tsv", "43d4ceefe042236a7d0885cb8506bf6537426cf2359eddb2450dd151864a498b"),
    "ate": ("cti-ate.tsv", "79e5354a5c6aae0c979389786853e768201f8d129fb0f0ca5af392555f238f28"),
}


def read_tsv(text: str) -> list[dict]:
    csv.field_size_limit(1 << 30)
    return list(csv.DictReader(io.StringIO(text), delimiter="\t"))


def _meta(category: str | None = None, **kw) -> dict:
    m = {"source": SOURCE, "license": LICENSE}
    if category is not None:
        m["category"] = category
    m.update(kw)
    return m


def _mcq_category(url: str) -> str:
    u = urlparse(url)
    seg = [s for s in u.path.split("/") if s]
    if u.netloc:  # attack.mitre.org/techniques, cwe.mitre.org/data, capec.mitre.org/data
        return f"{u.netloc}/{seg[0]}" if seg else u.netloc
    # GPT-generated items cite a source document, e.g. "/NIST CTI sharing_part2.txt"
    name = seg[-1] if seg else "unknown"
    return "doc:" + re.sub(r"(_part\d+)?\.txt$", "", name)


def convert_mcq(rows: list[dict]) -> list[dict]:
    out = []
    for i, r in enumerate(rows, 1):
        out.append({
            "id": c.make_id("ctibench-mcq", i, len(rows)),
            "suite": "ctibench-mcq", "type": "mcq",
            "prompt": r["Question"].strip(),
            "choices": c.letter_choices((k, r[f"Option {k}"]) for k in "ABCD"),
            "answer": r["GT"].strip().upper(), "scorer": "mcq_letter",
            "meta": _meta(_mcq_category(r["URL"]), url=r["URL"]),
        })
    return out


def convert_rcm(rows: list[dict]) -> list[dict]:
    out = []
    for i, r in enumerate(rows, 1):
        out.append({
            "id": c.make_id("ctibench-rcm", i, len(rows)),
            "suite": "ctibench-rcm", "type": "classify",
            "prompt": r["Prompt"].strip(),
            "answer": r["GT"].strip(), "scorer": "cwe_match",
            "meta": _meta(url=r["URL"], cve=r["URL"].rstrip("/").rsplit("/", 1)[-1]),
        })
    return out


def convert_vsp(rows: list[dict]) -> list[dict]:
    out = []
    for i, r in enumerate(rows, 1):
        out.append({
            "id": c.make_id("ctibench-vsp", i, len(rows)),
            "suite": "ctibench-vsp", "type": "extract",
            "prompt": r["Prompt"].strip(),
            "answer": r["GT"].strip(), "scorer": "cvss_mae",
            "meta": _meta(url=r["URL"], cve=r["URL"].rstrip("/").rsplit("/", 1)[-1]),
        })
    return out


def convert_ate(rows: list[dict]) -> list[dict]:
    out = []
    for i, r in enumerate(rows, 1):
        ids = sorted({t.strip() for t in r["GT"].split(",") if t.strip()})
        out.append({
            "id": c.make_id("ctibench-ate", i, len(rows)),
            "suite": "ctibench-ate", "type": "extract",
            "prompt": r["Prompt"].strip(),
            "answer": ids, "scorer": "exact_set",
            "meta": _meta(r["Platform"].strip(), url=r["URL"]),
        })
    return out


CONVERTERS = {"mcq": convert_mcq, "rcm": convert_rcm, "vsp": convert_vsp, "ate": convert_ate}


def main(argv: list[str] | None = None) -> int:
    p = c.base_parser(__doc__)
    p.add_argument("--tasks", default="mcq,rcm,vsp,ate", help="comma list of subtasks")
    args = p.parse_args(argv)
    cache = Path(args.cache_dir) / "ctibench"
    for task in args.tasks.split(","):
        name, sha = FILES[task]
        url = f"{BASE}/{name}"
        path = c.download(url, cache / name, sha, args.offline)
        items = CONVERTERS[task](read_tsv(path.read_text(encoding="utf-8")))
        c.write_outputs(f"ctibench-{task}", items, args,
                        [{"url": url, "sha256": sha, "revision": REVISION}], LICENSE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
