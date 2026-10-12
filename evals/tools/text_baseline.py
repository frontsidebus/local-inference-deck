#!/usr/bin/env python3
"""A dev-trained text-classifier baseline for the CWE and CVSS suites (multinomial Naive Bayes, stdlib only).

    python3 evals/tools/text_baseline.py --items <annotated nvd-cwe.jsonl> [--items ...] --out nb-preds.jsonl
    python3 evals/tools/text_baseline.py ... --dev-only     # train on meta.rigor.train_ok_dev_only items only

What it is for: a model shows CWE/CVSS *skill* only if it beats a cheap text classifier trained on development items
(the description words plus the CNA), not just a constant. report.py reads the predictions (``--text-baseline``),
shows the classifier as a baseline row, and adds a "beats the text baseline" table per run.

Rules (fixed a priori, model-blind; DATA provides the split fields):
- Training items: ``meta.rigor.train_ok`` (not sealed, and no sealed item in the same template cluster), or
  ``train_ok_dev_only`` with ``--dev-only``. Without those fields: ``meta.rigor.split != "sealed"`` and the template
  cluster has no sealed item.
- Features: lower-cased word tokens (2+ characters) of the CVE description (the text after "CVE description:", else
  the whole prompt) plus one ``cna=<CNA>`` token. Laplace smoothing 1. Ties break on the label string.
- An item that is itself a training item is predicted by 5-fold cross-fitting over template clusters (folds by
  sha256 of the cluster id), so it is never in its own training set and neither is any item of its template. Other
  items (sealed) are predicted by the model trained on every training item.
- CWE: one classifier over gold CWE ids. CVSS: one classifier per base metric (AV, AC, PR, UI, S, C, I, A) on the
  gold vector; the predicted vector is scored with the CVSS 3.1 formula.
Output: one JSON line per item: id, suite, pred (CWE id or vector), pred_score (CVSS), fit ("cross-fit k" | "full"),
train_n.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

EVALS = Path(__file__).resolve().parent.parent
if str(EVALS) not in sys.path:
    sys.path.insert(0, str(EVALS))

K_FOLDS = 5
METRICS = ("AV", "AC", "PR", "UI", "S", "C", "I", "A")
_TOK = re.compile(r"[a-z0-9][a-z0-9_.+-]*[a-z0-9]")


def description(item: Dict[str, Any]) -> str:
    p = str(item.get("prompt") or "")
    m = re.search(r"CVE description:\s*(.*)", p, re.S | re.I)
    return m.group(1) if m else p


def cna_of(item: Dict[str, Any]) -> str:
    meta = item.get("meta") or {}
    rg = meta.get("rigor") or {}
    return str(rg.get("cna_cluster") or (meta.get("labels") or {}).get("cna") or "?")


def features(item: Dict[str, Any]) -> List[str]:
    return _TOK.findall(description(item).lower()) + [f"cna={cna_of(item)}"]


def _norm_cwe(s: Any) -> Optional[str]:
    m = re.search(r"CWE[\s_-]*0*(\d{1,5})", str(s), re.I)
    return f"CWE-{int(m.group(1))}" if m else None


def _vector_metrics(v: Any) -> Optional[Dict[str, str]]:
    m = re.search(r"AV:([NALP])/AC:([LH])/PR:([NLH])/UI:([NR])/S:([UC])/C:([HLN])/I:([HLN])/A:([HLN])", str(v), re.I)
    return dict(zip(METRICS, (x.upper() for x in m.groups()))) if m else None


class NB:
    """Multinomial Naive Bayes with Laplace smoothing."""

    def __init__(self) -> None:
        self.doc = Counter()
        self.tok: Dict[str, Counter] = defaultdict(Counter)
        self.tot = Counter()
        self.vocab: set = set()

    def add(self, toks: List[str], label: str) -> None:
        self.doc[label] += 1
        self.tok[label].update(toks)
        self.tot[label] += len(toks)
        self.vocab.update(toks)

    def predict(self, toks: List[str]) -> Optional[str]:
        if not self.doc:
            return None
        n = sum(self.doc.values())
        v = len(self.vocab) or 1
        best = None
        for lab in sorted(self.doc):
            lp = math.log(self.doc[lab] / n)
            denom = self.tot[lab] + v
            c = self.tok[lab]
            for t in toks:
                lp += math.log((c.get(t, 0) + 1) / denom)
            if best is None or lp > best[0]:
                best = (lp, lab)
        return best[1]


def train_ok(item: Dict[str, Any], sealed_clusters: set, dev_only: bool) -> bool:
    rg = (item.get("meta") or {}).get("rigor") or {}
    key = "train_ok_dev_only" if dev_only else "train_ok"
    if key in rg or "baseline_train_ok" in rg:
        return bool(rg.get(key, rg.get("baseline_train_ok")))
    if rg.get("split") == "sealed" or rg.get("heldout"):
        return False
    if dev_only and rg.get("split") not in ("dev",):
        return False
    return str(rg.get("template_cluster")) not in sealed_clusters


def _fold(item: Dict[str, Any]) -> int:
    c = str(((item.get("meta") or {}).get("rigor") or {}).get("template_cluster") or item.get("id"))
    return int(hashlib.sha256(c.encode()).hexdigest()[:8], 16) % K_FOLDS


def _labels(item: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """{'cwe': id} or {metric: value} for one item's gold."""
    if item.get("scorer") == "cwe_match":
        ans = item.get("answer")
        ans = ans.get("cwe") if isinstance(ans, dict) else ans
        g = _norm_cwe(ans[0] if isinstance(ans, list) and ans else ans)
        return {"cwe": g} if g else None
    if item.get("scorer") == "cvss_mae":
        ans = item.get("answer")
        ans = ans.get("vector") if isinstance(ans, dict) else ans
        return _vector_metrics(ans)
    return None


def predict_suite(items: List[Dict[str, Any]], dev_only: bool = False) -> List[Dict[str, Any]]:
    items = sorted(items, key=lambda x: x["id"])
    sealed_clusters = {str(((it.get("meta") or {}).get("rigor") or {}).get("template_cluster"))
                       for it in items if ((it.get("meta") or {}).get("rigor") or {}).get("split") == "sealed"
                       or ((it.get("meta") or {}).get("rigor") or {}).get("heldout")}
    rows = [(it, _labels(it), features(it)) for it in items]
    train = [(it, lab, f) for it, lab, f in rows if lab and train_ok(it, sealed_clusters, dev_only)]
    keys = sorted({k for _, lab, _ in train for k in lab})

    def fit(subset) -> Dict[str, NB]:
        models = {k: NB() for k in keys}
        for _, lab, f in subset:
            for k in keys:
                if k in lab:
                    models[k].add(f, lab[k])
        return models
    full = fit(train)
    folds = {k: fit([t for t in train if _fold(t[0]) != k]) for k in range(K_FOLDS)}
    train_ids = {t[0]["id"] for t in train}
    out = []
    for it, _, f in rows:
        if it["id"] in train_ids:
            k = _fold(it)
            models, how, n = folds[k], f"cross-fit {k}", sum(1 for t in train if _fold(t[0]) != k)
        else:
            models, how, n = full, "full", len(train)
        pred = {k: m.predict(f) for k, m in models.items()}
        rec: Dict[str, Any] = {"id": it["id"], "suite": it.get("suite"), "fit": how, "train_n": n}
        if "cwe" in pred:
            rec["pred"] = pred["cwe"]
        elif all(pred.get(m) for m in METRICS):
            vec = "CVSS:3.1/" + "/".join(f"{m}:{pred[m]}" for m in METRICS)
            rec["pred"] = vec
            try:
                import scorers
                rec["pred_score"] = scorers.cvss31_base(vec)
            except Exception:
                rec["pred_score"] = None
        out.append(rec)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--items", action="append", required=True, help="annotated suite file(s) (full pools)")
    ap.add_argument("--out", required=True, help="predictions JSONL")
    ap.add_argument("--dev-only", action="store_true", help="train on train_ok_dev_only items only")
    a = ap.parse_args(argv)
    by: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    for p in a.items:
        for line in Path(p).read_text(encoding="utf-8").splitlines():
            if line.strip():
                it = json.loads(line)
                if it.get("scorer") in ("cwe_match", "cvss_mae"):
                    by[it["suite"]][it["id"]] = it
    rows = []
    for suite in sorted(by):
        rows += predict_suite(list(by[suite].values()), a.dev_only)
    Path(a.out).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    print(json.dumps({s: sum(1 for r in rows if r["suite"] == s) for s in sorted(by)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
