"""make_subsets.py on invented items (no network)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

DS = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("make_subsets", DS / "make_subsets.py")
ms = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ms)


def item(i: int, label: str, cat: str) -> dict:
    return {"id": f"nvd-cwe-{i:04d}", "suite": "nvd-cwe", "type": "classify", "prompt": f"CVE text {i}",
            "answer": "CWE-79", "scorer": "cwe_match", "meta": {"source": "test", "license": "CC0-1.0", "category": cat, "label_source": label}}


def write(d: Path, name: str, items: list[dict]) -> None:
    (d / f"{name}.jsonl").write_text("".join(json.dumps(i) + "\n" for i in items))


def read(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text().splitlines()]


def test_label_subset_renames_suite_and_ids_and_keeps_the_source_id(tmp_path):
    items = [item(i, "nvd" if i % 3 == 0 else "cna", "CWE-79" if i % 2 else "CWE-89") for i in range(1, 31)]
    write(tmp_path, "nvd-cwe", items)
    assert ms.main(["--data-dir", str(tmp_path), "label", "--suite", "nvd-cwe", "--label", "nvd", "--n", "4",
                    "--seed", "7"]) == 0
    out = read(tmp_path / "nvd-cwe-nvdlab.sample4.jsonl")
    assert len(out) == 4
    assert all(o["suite"] == "nvd-cwe-nvdlab" and o["meta"]["label_source"] == "nvd" for o in out)
    assert all(o["id"] == "nvd-cwe-nvdlab-" + o["meta"]["source_id"].rsplit("-", 1)[1] for o in out)
    assert len({o["id"] for o in out}) == 4
    # deterministic: the same seed gives the same bytes; n above the pool keeps every labelled item
    first = (tmp_path / "nvd-cwe-nvdlab.sample4.jsonl").read_bytes()
    ms.main(["--data-dir", str(tmp_path), "label", "--suite", "nvd-cwe", "--label", "nvd", "--n", "4", "--seed", "7"])
    assert (tmp_path / "nvd-cwe-nvdlab.sample4.jsonl").read_bytes() == first
    ms.main(["--data-dir", str(tmp_path), "label", "--suite", "nvd-cwe", "--label", "nvd", "--n", "200"])
    assert len(read(tmp_path / "nvd-cwe-nvdlab.sample200.jsonl")) == 10
    # the source file is untouched
    assert read(tmp_path / "nvd-cwe.jsonl") == items


def test_label_subset_with_no_match_fails(tmp_path):
    write(tmp_path, "nvd-cwe", [item(1, "cna", "CWE-79")])
    assert ms.main(["--data-dir", str(tmp_path), "label", "--suite", "nvd-cwe", "--label", "nvd", "--n", "5"]) == 1


def test_subset_names_and_is_a_subset(tmp_path):
    items = [item(i, "cna", f"CWE-{i % 4}") for i in range(1, 41)]
    write(tmp_path, "nvd-cwe.sample40", items)
    write(tmp_path, "cybermetric-500", items)
    assert ms.main(["--data-dir", str(tmp_path), "subset", "--from", "nvd-cwe.sample40", "--n", "10"]) == 0
    assert ms.main(["--data-dir", str(tmp_path), "subset", "--from", "cybermetric-500", "--n", "10"]) == 0
    sub = read(tmp_path / "nvd-cwe.sample40in10.jsonl")
    assert len(sub) == 10 and all(s in items for s in sub)
    assert (tmp_path / "cybermetric-500.sample40in10.jsonl").exists()
    # stratified: every category is represented, in source order
    assert {s["meta"]["category"] for s in sub} == {f"CWE-{k}" for k in range(4)}
    ids = [s["id"] for s in sub]
    assert ids == sorted(ids)
