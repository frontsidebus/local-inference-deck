#!/usr/bin/env python3
"""Derived eval sets, built from data the fetchers already wrote to evals/data/ (no network). Deterministic:
the same inputs and seed give byte-identical files, so a plan's items can be rebuilt from the repo.

    # every item of one label source, as its own suite (e.g. NVD's own CWE labels vs the CNA's)
    python3 evals/datasets/make_subsets.py label --suite nvd-cwe --label nvd --n 200 --seed 20261007
      -> evals/data/nvd-cwe-nvdlab.sample200.jsonl   (suite nvd-cwe-nvdlab, ids nvd-cwe-nvdlab-NNNN)

    # a stratified subset of an existing file (e.g. a smaller set for a slow model, on the same items)
    python3 evals/datasets/make_subsets.py subset --from ctibench-mcq.sample200 --n 100 --seed 20261007
      -> evals/data/ctibench-mcq.sample200in100.jsonl
    python3 evals/datasets/make_subsets.py subset --from cybermetric-500 --n 100 --seed 20261007
      -> evals/data/cybermetric-500.sample500in100.jsonl

`label` renames the suite to ``<suite>-<label>lab`` and the ids to match (the original id is kept in
``meta.source_id``), so the set can run next to its parent suite without duplicate ids. Both commands sample with
the fetchers' stratified sampler (proportional per ``meta.category``, source order kept). If n is at least the
number of items, every item is kept (the file name still says sample<n>, as the fetchers do).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as c  # noqa: E402


def read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def label_subset(items: list[dict], suite: str, label: str, n: int, seed: int) -> list[dict]:
    new_suite = f"{suite}-{label}lab"
    picked = [dict(it, suite=new_suite) for it in items
              if (it.get("meta") or {}).get("label_source") == label]
    out = []
    for it in c.stratified_sample(picked, n, seed=seed):
        it = dict(it, meta=dict(it.get("meta") or {}))
        it["meta"]["source_id"] = it["id"]
        it["id"] = f"{new_suite}-{it['id'].rsplit('-', 1)[1]}"
        out.append(it)
    return out


def subset_name(source: str, total: int, n: int) -> str:
    """ctibench-mcq.sample200 -> ctibench-mcq.sample200in100; cybermetric-500 (500 items) -> ...sample500in100."""
    if re.search(r"\.sample\d+$", source):
        return f"{source}in{n}"
    return f"{source}.sample{total}in{n}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=str(c.DATA_DIR))
    sub = ap.add_subparsers(dest="cmd", required=True)
    lab = sub.add_parser("label", help="every item with one meta.label_source, as its own suite")
    lab.add_argument("--suite", required=True, help="source suite (reads <data-dir>/<suite>.jsonl)")
    lab.add_argument("--label", required=True, help="meta.label_source value, e.g. nvd")
    sb = sub.add_parser("subset", help="a stratified subset of an existing file")
    sb.add_argument("--from", dest="source", required=True, help="file stem in <data-dir>, e.g. ctibench-mcq.sample200")
    for p in (lab, sb):
        p.add_argument("--n", type=int, required=True)
        p.add_argument("--seed", type=int, default=c.DEFAULT_SEED)
    a = ap.parse_args(argv)
    d = Path(a.data_dir)
    if a.cmd == "label":
        src = d / f"{a.suite}.jsonl"
        out_items = label_subset(read(src), a.suite, a.label, a.n, a.seed)
        if not out_items:
            print(f"{src.name}: no items with label_source={a.label!r}", file=sys.stderr)
            return 1
        out = d / f"{a.suite}-{a.label}lab.sample{a.n}.jsonl"
    else:
        src = d / f"{a.source}.jsonl"
        items = read(src)
        out_items = c.stratified_sample(items, a.n, seed=a.seed)
        out = d / f"{subset_name(a.source, len(items), a.n)}.jsonl"
    c.validate(out_items)
    data = c.to_jsonl(out_items)
    out.write_bytes(data)
    print(f"{out.name}: {len(out_items)} items  sha256={c.sha256_bytes(data)}  (from {src.name}, seed {a.seed})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
