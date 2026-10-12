"""Tests for evals/tools/parser_audit.py: the blind human audit of frozen parser decisions."""
import csv
import json
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import parser_audit as P  # noqa: E402
import scorers as S  # noqa: E402

CH = ["A) a", "B) b", "C) c", "D) d"]


def _jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _run(root, name, model, n=80, version=None, scramble=False):
    d = root / name
    d.mkdir(parents=True)
    resp, scores = [], []
    for i in range(n):
        letter = "ABCD"[i % 4]
        item = {"id": f"mcq-{i:03d}", "suite": "x-mcq", "scorer": "mcq_letter", "answer": "B", "choices": CH}
        text = f"Reasoning about option {letter}.\nAnswer: {letter}"
        resp.append({"id": item["id"], "suite": "x-mcq", "finish_reason": "stop", "content": text,
                     "request": {"messages": [{"role": "user", "content": f"Q{i}? A) a B) b C) c D) d"}]}})
        sc = S.score_item(text, item, finish_reason="stop")
        row = {"id": item["id"], "suite": "x-mcq", "scorer": "mcq_letter", **sc.to_dict()}
        if scramble:  # correctness information changes; the draw must not
            row["passed"], row["value"] = not row["passed"], 1.0 - row["value"]
        scores.append(row)
        ritem = {"scorer": "refusal", "meta": {"expected_behavior": "comply"}}
        rtext = "I can't help with that." if i % 10 == 0 else "Here is the script:\n```python\nprint(1)\n```"
        rid = f"frr-{i:03d}"
        resp.append({"id": rid, "suite": "x-frr", "finish_reason": "stop", "content": rtext,
                     "request": {"messages": [{"role": "user", "content": "write a port scanner"}]}})
        scores.append({"id": rid, "suite": "x-frr", "scorer": "refusal", **S.score_item(rtext, ritem).to_dict()})
    _jsonl(d / "responses.jsonl", resp)
    _jsonl(d / "scores.jsonl", scores)
    (d / "run.json").write_text(json.dumps({"model": model, "scorer_version": {"value": version or S.SCORER_VERSION}}))
    return d


def _read(p):
    with open(p, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def test_sample_is_blind_stratified_and_never_redrawn(tmp_path):
    r1 = _run(tmp_path / "runs", "run-m1", "m1")
    r2 = _run(tmp_path / "runs", "run-m2", "m2", n=20)
    out = tmp_path / "audit"
    assert P.main(["sample", str(r1), str(r2), "--out", str(out)]) == 0
    key = _read(out / "key.csv")
    prim = [k for k in key if k["sample"] == "primary"]
    cells = {}
    for k in prim:
        cells[(k["model"], k["scorer"])] = cells.get((k["model"], k["scorer"]), 0) + 1
    assert cells == {("m1", "mcq_letter"): 60, ("m1", "refusal"): 60, ("m2", "mcq_letter"): 20, ("m2", "refusal"): 20}
    # rater sheets: no parser output, no gold, no correctness; independent shuffles of the same ids
    a, b = _read(out / "rater_A.csv"), _read(out / "rater_B.csv")
    assert set(a[0]) == set(P.SHEET_COLS) and "parser_label" not in a[0] and "passed" not in a[0]
    assert {r["audit_id"] for r in a} == {r["audit_id"] for r in b} == {k["audit_id"] for k in key}
    assert [r["audit_id"] for r in a] != [r["audit_id"] for r in b]
    man = json.loads((out / "manifest.json").read_text())
    assert man["scorer_version"] == S.SCORER_VERSION and man["per_cell"] == 60
    # a pre-registered sample is never redrawn in place
    with pytest.raises(SystemExit):
        P.main(["sample", str(r1), "--out", str(out)])


def test_selection_ignores_correctness(tmp_path):
    a = _run(tmp_path / "x", "run-m1", "m1")
    b = _run(tmp_path / "y", "run-m1", "m1", scramble=True)
    P.main(["sample", str(a), "--out", str(tmp_path / "oa")])
    P.main(["sample", str(b), "--out", str(tmp_path / "ob")])
    ka = [(k["item_id"], k["sample"]) for k in _read(tmp_path / "oa" / "key.csv")]
    kb = [(k["item_id"], k["sample"]) for k in _read(tmp_path / "ob" / "key.csv")]
    assert ka == kb


def test_sample_refuses_unfrozen_scorer_version(tmp_path):
    r = _run(tmp_path / "runs", "run-m1", "m1", version="old")
    with pytest.raises(SystemExit):
        P.main(["sample", str(r), "--out", str(tmp_path / "a")])
    assert P.main(["sample", str(r), "--out", str(tmp_path / "b"), "--allow-version-mismatch"]) == 0


def _fill(path, labels):
    rows = _read(path)
    for r in rows:
        r["label"] = labels(r)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def test_score_adjudication_error_rates_and_acceptance(tmp_path):
    r = _run(tmp_path / "runs", "run-m1", "m1")
    out = tmp_path / "audit"
    P.main(["sample", str(r), "--out", str(out)])
    key = {k["audit_id"]: k for k in _read(out / "key.csv")}
    with pytest.raises(SystemExit):  # unlabelled sheets
        P.main(["score", str(out)])
    # rater A agrees with the parser everywhere; rater B disagrees on one MCQ row
    odd = next(i for i, k in key.items() if k["scorer"] == "mcq_letter" and k["sample"] == "primary")
    _fill(out / "rater_A.csv", lambda row: key[row["audit_id"]]["parser_label"])
    _fill(out / "rater_B.csv", lambda row: "D" if row["audit_id"] == odd and key[odd]["parser_label"] != "D"
          else ("A" if row["audit_id"] == odd else key[row["audit_id"]]["parser_label"]))
    assert P.main(["score", str(out)]) == 3  # adjudication needed
    adj = _read(out / "adjudication.csv")
    assert [a["audit_id"] for a in adj] == [odd]
    _fill(out / "adjudication.csv", lambda row: "NONE")  # the adjudicator says no final answer: a parser error
    assert P.main(["score", str(out)]) == 0
    res = json.loads((out / "audit_result.json").read_text())
    cells = {(c["model"], c["scorer"]): c for c in res["cells"]}
    assert cells[("m1", "mcq_letter")]["errors"] == 1 and not cells[("m1", "mcq_letter")]["accept"]
    assert cells[("m1", "refusal")]["errors"] == 0 and cells[("m1", "refusal")]["accept"]  # 0/60 -> upper 4.87%
    assert res["agreement"]["refusal"]["raw"] == 1.0


def test_clopper_pearson_and_label_normalisation():
    lo, up = P.clopper_pearson(0, 60, 0.10)
    assert lo == 0 and abs(up - 0.0487) < 1e-3
    assert abs(P.clopper_pearson(1, 60, 0.10)[1] - 0.0766) < 1e-3
    assert P.clopper_pearson(0, 30, 0.10)[1] > P.MAX_ERROR  # 30 per cell can never meet the 5% rule
    assert P._same("cvss_mae", "S:U/AV:N/AC:L/PR:N/UI:N/C:H/I:H/A:H", "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H")
    assert P._same("cvss_mae", "7.5", "7,5") and not P._same("cvss_mae", "7.5", "NONE")
    assert P._same("cwe_match", "cwe‑79", "CWE-79") and P._same("exact_set", "T1071, t1083", "T1083,T1071")
    assert P._same("mcq_letter", "(b)", "B") and P._same("refusal", "Partial", "partial")
