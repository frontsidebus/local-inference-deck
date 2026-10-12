#!/usr/bin/env python3
"""Offline checks of the LLM judge (stdlib only; no model calls unless ``perturb --grade --yes-call-model``).

Subcommands:

  sheet       Calibration design R2-1 (pre-registered; see the constants below). Stratified random by model x task
              type, with extract oversampled 2:1 (20 + 10 per model). Selection is blind to grades. Within each
              stratum, half the units have the reference withheld. Writes two rater sheets with independent
              shuffles and their own row ids, a key with no judge grades, instructions, and attestation templates.
  adjudicate  A blind sheet of the units on which raters A and B disagree, for an adjudicator.
  agree       Inter-rater agreement and consensus labels. Given v2 judge runs (--judge-runs), it adds judge-vs-
              consensus agreement: kappa with a bootstrap CI, PABAK, per-class agreement, confusion and error
              direction, per model, type and reference condition, plus the pre-registered verdict (kappa lower 95%
              CI >= 0.6, attestations present). It refuses judge runs graded with another prompt or by a judge in a
              contestant's family.
  length      Length-bias analysis on stored grades: a logistic regression of pass on within-item centred log
              length with model and task-type fixed effects, per-model slopes, length-matched discordant pairs,
              and pass rate by length quintile (PROTOCOL C1.9). CIs come from an item bootstrap.
  selfpref    Self-preference once multi-judge grades exist: the log-odds that a judge passes answers from its own
              family, controlling for judge leniency, answer-model quality, length and reference overlap, plus a
              per-judge difference-in-differences.
  perturb     Format-sensitivity harness: re-grade a seeded random sample with the answers reformatted (JSON -> prose,
              Markdown stripped). Without --grade it only writes the plan.

A within-item length effect is confounded with real completeness, because a longer answer may simply cover more of
what was asked. Only human labels (``agree``) can tell the two apart.

Examples:
  python3 evals/judge_calibration.py sheet --runs evals/results/*-qa-* --data evals/data/sevenllm-qa.jsonl \\
      --out-dir /tmp/calib
  python3 evals/judge_calibration.py adjudicate --key K --sheet-a A --sheet-b B --data D --out adj.csv
  python3 evals/judge_calibration.py agree --key K --sheet-a A --sheet-b B --adjudication adj.csv \\
      --attest-a attestation_A.txt --attest-b attestation_B.txt --judge-runs <v2-regraded run dirs>
  python3 evals/judge_calibration.py length --runs evals/results/*-qa-*
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

EVALS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EVALS_DIR))
import grading as G  # noqa: E402
import report as R  # noqa: E402
import scorers as S  # noqa: E402

DEFAULT_SEED = 20261011
LABELS = ("PASS", "FAIL")


# ------------------------------------------------------------------------------------------- loading
def load_items(paths: Sequence[Path]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for p in paths:
        for it in R.read_jsonl(Path(p)):
            if "id" in it:
                out[it["id"]] = it
    return out


def load_rows(run_dirs: Sequence[Path], suite: Optional[str] = None) -> List[Dict[str, Any]]:
    """One row per (run, llm_judge item) with a response that is not an API error. A grader error is kept, with
    passed=False and judge_status='error' (strict accounting: never dropped)."""
    rows = []
    for d in run_dirs:
        d = Path(d)
        rj = json.loads((d / "run.json").read_text(encoding="utf-8")) if (d / "run.json").exists() else {}
        model = rj.get("model") or d.name
        resp = R.latest_by_id(R.read_jsonl(d / "responses.jsonl"))
        scores = R.latest_by_id(R.read_jsonl(d / "scores.jsonl"))
        for rid, r in sorted(resp.items()):
            sc = scores.get(rid)
            if r.get("error") or not sc or sc.get("scorer") != "llm_judge":
                continue
            if suite and r.get("suite") != suite:
                continue
            cand = S.strip_thinking(r.get("content") or "")
            st = sc.get("status", "ok")
            judge_score = round(float(sc.get("value", 0)) * 10) if st == "ok" else None
            rows.append({"key": f"{d.name}/{rid}", "run": d.name, "model": model, "id": rid,
                         "suite": r.get("suite"), "type": r.get("type") or "?", "candidate": cand,
                         "chars": len(cand), "passed": bool(sc.get("passed")) and st == "ok",
                         "judge_status": st, "judge_grade": sc.get("parsed") if st == "ok" else "ERROR",
                         "judge_score": judge_score})
    return rows


# ------------------------------------------------------------------------------------------- calibration design
# Pre-registered (AMENDMENTS.md R2-1); constants, not tuned.
KAPPA_LOWER_CI_THRESHOLD = 0.6      # judge passes calibration iff the lower 95% CI of kappa vs consensus >= this
SKEW_PREVALENCE = 0.10              # a stratum whose human pass rate is < this or > 1 - this is "skewed": no kappa
# Per model. R3-2 sizing (size_calibration, kappa 0.75, power 0.80): extract needs 240 units (40/model); freeform
# needs ~1000 at prevalence 0.95, more than the stored population (6 x 49), so it is capped at all 49 per model and the
# shortfall is recorded (calibration_power_r3.json): the freeform gate is underpowered unless new items are added.
CALIB_ALLOCATION = {"extract": 40, "freeform": 49}
CALIB_BOOT = 2000
RATERS = ("A", "B")
ATTEST_TEMPLATE = """Human-label attestation for rater {r}

I labelled every row of rater_{r}.csv myself. I am a human. I did not use an LLM or any other model to
suggest, check or fill in labels, and I did not open calibration_key.csv or see model names or judge grades
before I finished.

name_or_initials:
date:
human_labels_no_llm (yes/no):
"""


def stratified_sample(rows: Sequence[Dict[str, Any]], n: int, seed: int,
                      strata: Tuple[str, ...] = ("model", "type")) -> List[Dict[str, Any]]:
    """Equal allocation across strata (remainder to random strata); simple random sampling within each.
    Blind to grades: only the stratum fields define the strata."""
    keys = sorted({tuple(r[s] for s in strata) for r in rows})
    rng = random.Random(seed)
    base, rem = divmod(n, len(keys))
    extra = set(rng.sample(keys, rem)) if rem else set()
    return allocated_sample(rows, {k: base + (k in extra) for k in keys}, seed, strata)


def allocated_sample(rows: Sequence[Dict[str, Any]], alloc: Dict[Tuple, int], seed: int,
                     strata: Tuple[str, ...] = ("model", "type")) -> List[Dict[str, Any]]:
    groups: Dict[Tuple, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[tuple(r[s] for s in strata)].append(r)
    rng = random.Random(f"{seed}:sample")
    out = []
    for k in sorted(groups):
        pool = sorted(groups[k], key=lambda r: r["key"])
        take = min(len(pool), alloc.get(k, 0))
        for r in rng.sample(pool, take):
            out.append({**r, "stratum": "|".join(map(str, k)), "stratum_N": len(pool), "stratum_n": take})
    return out


def unit_id(r: Dict[str, Any]) -> str:
    return hashlib.sha256(f"{r['run']}/{r['id']}".encode()).hexdigest()[:12]


def calibration_sample(rows: Sequence[Dict[str, Any]], seed: int,
                       allocation: Optional[Dict[str, int]] = None) -> List[Dict[str, Any]]:
    """R2-1 units (per model, allocation[type] units of each task type) with the R3-1 reference condition: per
    ITEM, a seeded random half of the distinct sampled items of each type is withheld, so every unit of an item has
    the same condition on both sheets. Uses only model, type, item id and the seed."""
    allocation = allocation or CALIB_ALLOCATION
    models = sorted({r["model"] for r in rows})
    alloc = {(m, t): n for m in models for t, n in allocation.items()}
    sample = allocated_sample(rows, alloc, seed)
    items_by_type: Dict[str, set] = defaultdict(set)
    for r in sample:
        r["unit_id"] = unit_id(r)
        items_by_type[r["type"]].add(r["id"])
    rng = random.Random(f"{seed}:refcond-item")
    hidden: set = set()
    for t in sorted(items_by_type):
        its = sorted(items_by_type[t])
        hidden |= set(rng.sample(its, len(its) // 2))
    for r in sample:
        r["ref_shown"] = r["id"] not in hidden
    check_condition_per_item(sample)
    return sample


def check_condition_per_item(units: Iterable[Dict[str, Any]]) -> None:
    """R3-1 invariant: no item appears under both reference conditions."""
    cond: Dict[str, set] = defaultdict(set)
    for r in units:
        cond[r.get("id") or r.get("item_id")].add(r["ref_shown"])
    mixed = sorted(i for i, c in cond.items() if len(c) > 1)
    if mixed:
        raise ValueError(f"items under both reference conditions: {mixed[:5]}")


SHEET_COLS = ["row", "item_ref", "task_type", "reference_shown", "question", "reference", "candidate",
              "human_grade", "human_score", "human_note"]
KEY_COLS = ["unit_id", "row_A", "row_B", "item_id", "model", "run", "task_type", "stratum", "stratum_N",
            "stratum_n", "ref_shown", "chars"]
ADJ_COLS = ["adj_row", "unit_id", "item_ref", "task_type", "reference_shown", "question", "reference", "candidate",
            "label_A", "label_B", "note_A", "note_B", "final_grade", "adjudicator_note"]
WITHHELD = "(withheld for this row: grade from the task and the source text in the question alone)"


def rubric_text(prompt_path: Path) -> str:
    """The grading rules from the judge prompt (everything before the template sections), for the labeller."""
    t = prompt_path.read_text(encoding="utf-8")
    return t.split("Reply with exactly one JSON object")[0].strip()


def _reference(it: Dict[str, Any]) -> str:
    try:
        spec = S.llm_judge_spec(it)
    except S.ItemError:
        return ""
    return (spec["rubric"] + "\n\n" if spec["rubric"] else "") + spec["reference"]


def item_ref(item_id: str) -> str:
    """An opaque per-item reference for the rater sheets (the public dataset id is kept only in the key). The
    question text is itself public, so this only removes the trivial lookup."""
    return "q" + hashlib.sha256(f"calib-item:{item_id}".encode()).hexdigest()[:10]


def _sheet_row(r: Dict[str, Any], it: Dict[str, Any], row: str) -> Dict[str, Any]:
    return {"row": row, "item_ref": item_ref(r["id"]), "task_type": r["type"], "reference_shown": "yes" if r["ref_shown"] else "no",
            "question": it.get("prompt", ""), "reference": _reference(it) if r["ref_shown"] else WITHHELD,
            "candidate": r["candidate"], "human_grade": "", "human_score": "", "human_note": ""}


def write_calibration(sample: Sequence[Dict[str, Any]], items: Dict[str, Dict[str, Any]], out_dir: Path,
                      prompt_path: Path, seed: int) -> Dict[str, Path]:
    """Two rater sheets (independent shuffles, own row ids), the key (no judge grades), instructions and
    attestation templates."""
    out_dir.mkdir(parents=True, exist_ok=True)
    rows_of: Dict[str, Dict[str, str]] = defaultdict(dict)
    paths: Dict[str, Path] = {}
    for rater in RATERS:
        order = sorted(sample, key=lambda r: r["unit_id"])
        random.Random(f"{seed}:shuffle:{rater}").shuffle(order)
        p = out_dir / f"rater_{rater}.csv"
        with p.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, SHEET_COLS)
            w.writeheader()
            for i, r in enumerate(order, 1):
                row = f"{rater}{i:03d}"
                rows_of[r["unit_id"]][rater] = row
                w.writerow(_sheet_row(r, items.get(r["id"]) or {}, row))
        paths[f"sheet_{rater}"] = p
        a = out_dir / f"attestation_{rater}.txt"
        a.write_text(ATTEST_TEMPLATE.format(r=rater), encoding="utf-8")
        paths[f"attestation_{rater}"] = a
    key = out_dir / "calibration_key.csv"
    with key.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, KEY_COLS)
        w.writeheader()
        for r in sorted(sample, key=lambda r: r["unit_id"]):
            w.writerow({"unit_id": r["unit_id"], "row_A": rows_of[r["unit_id"]]["A"],
                        "row_B": rows_of[r["unit_id"]]["B"], "item_id": r["id"], "model": r["model"],
                        "run": r["run"], "task_type": r["type"], "stratum": r["stratum"],
                        "stratum_N": r["stratum_N"], "stratum_n": r["stratum_n"],
                        "ref_shown": "yes" if r["ref_shown"] else "no", "chars": r["chars"]})
    paths["key"] = key
    instr = out_dir / "calibration_instructions.txt"
    n_hidden = sum(not r["ref_shown"] for r in sample)
    instr.write_text(
        "Blind calibration of the LLM judge (designs R2-1, R3-1, R3-2).\n\n"
        f"{len(sample)} answers, stratified random by model x task type, seed {seed}.\n"
        "Two raters label independently: rater A uses rater_A.csv, rater B uses rater_B.csv. The rows are the\n"
        "same answers in different orders. Do not discuss labels until both sheets are done.\n"
        "Do not open calibration_key.csv: the person running the analysis uses it. The sheets carry no model names\n"
        "and no judge grades; no judge grades exist yet.\n"
        f"{n_hidden} rows have the reference withheld (reference_shown = no). Grade them from the task and the\n"
        "source text in the question alone. The condition is per question: a question whose reference is withheld\n"
        "never shows it on any row.\n"
        "Fill human_grade with PASS or FAIL (human_score 0-10 and human_note are optional). When done, fill in\n"
        "attestation_<A|B>.txt. Disagreements go to an adjudicator (judge_calibration.py adjudicate).\n"
        f"Pre-registered pass rule, for each task type and pooled: the lower 95% CI of the judge-vs-consensus kappa\n"
        f">= {KAPPA_LOWER_CI_THRESHOLD}; in a skewed stratum (human pass rate under 10% or over 90%) the lower CI of\n"
        f"the minority-class specific agreement >= {KAPPA_LOWER_CI_THRESHOLD} (with at least {MIN_MINORITY} minority units).\n"
        "Attestations: two different raters, each with a different name, the statement kept, an ISO date, and\n"
        "human_labels_no_llm: yes.\n\n"
        "The rules (the same ones the judge gets). Where a reference is shown, it was written by an LLM:\n"
        "use it to check facts, and trust the source text over it.\n\n" + rubric_text(prompt_path) + "\n",
        encoding="utf-8")
    paths["instructions"] = instr
    return paths


def _labels(sheet: Path) -> Dict[str, Dict[str, str]]:
    out = {}
    for r in csv.DictReader(sheet.open(encoding="utf-8")):
        g = (r.get("human_grade") or "").strip().upper()
        out[r["row"]] = {"grade": g if g in LABELS else "", "note": r.get("human_note") or ""}
    return out


def _key(key: Path) -> List[Dict[str, str]]:
    return list(csv.DictReader(key.open(encoding="utf-8")))


def write_adjudication(key: Path, sheet_a: Path, sheet_b: Path, items: Dict[str, Dict[str, Any]], out: Path,
                       seed: int = DEFAULT_SEED) -> int:
    """Units that both raters labelled and labelled differently, shuffled, blind to model and judge."""
    la, lb = _labels(sheet_a), _labels(sheet_b)
    cand_a = {r["row"]: r.get("candidate", "") for r in csv.DictReader(sheet_a.open(encoding="utf-8"))}
    rows = []
    for k in _key(key):
        a, b = la.get(k["row_A"], {}), lb.get(k["row_B"], {})
        if a.get("grade") and b.get("grade") and a["grade"] != b["grade"]:
            rows.append((k, a, b))
    random.Random(f"{seed}:adjudicate").shuffle(rows)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, ADJ_COLS)
        w.writeheader()
        for i, (k, a, b) in enumerate(rows, 1):
            it = items.get(k["item_id"]) or {}
            shown = k["ref_shown"] == "yes"
            w.writerow({"adj_row": i, "unit_id": k["unit_id"], "item_ref": item_ref(k["item_id"]),
                        "task_type": k["task_type"],
                        "reference_shown": k["ref_shown"], "question": it.get("prompt", ""),
                        "reference": _reference(it) if shown else WITHHELD, "candidate": cand_a.get(k["row_A"], ""),
                        "label_A": a["grade"], "label_B": b["grade"], "note_A": a["note"], "note_B": b["note"],
                        "final_grade": "", "adjudicator_note": ""})
    return len(rows)


# ------------------------------------------------------------------------------------------- agreement statistics
def cohen_kappa(pairs: Sequence[Tuple[str, str]], labels: Sequence[str] = LABELS) -> Optional[float]:
    n = len(pairs)
    if not n:
        return None
    po = sum(a == b for a, b in pairs) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[l] * cb[l] for l in labels) / (n * n)
    if pe >= 1.0:
        return 1.0 if po == 1.0 else 0.0
    return (po - pe) / (1 - pe)


def confusion(pairs: Sequence[Tuple[str, str]]) -> Dict[str, int]:
    """pairs: (first, second), e.g. (judge, human). Keys first_second."""
    c = Counter(f"{j}_{h}" for j, h in pairs)
    return {f"{j}_{h}": c.get(f"{j}_{h}", 0) for j in LABELS for h in LABELS}


def pabak(pairs: Sequence[Tuple[str, str]]) -> Optional[float]:
    """Prevalence- and bias-adjusted kappa for two categories: 2 * observed agreement - 1."""
    if not pairs:
        return None
    return 2 * sum(a == b for a, b in pairs) / len(pairs) - 1


def per_class_agreement(pairs: Sequence[Tuple[str, str]]) -> Dict[str, Optional[float]]:
    """Positive (PASS) agreement 2a/(2a+b+c) and negative (FAIL) agreement 2d/(2d+b+c)."""
    cm = confusion(pairs)
    a, d = cm["PASS_PASS"], cm["FAIL_FAIL"]
    bc = cm["PASS_FAIL"] + cm["FAIL_PASS"]
    return {"positive": 2 * a / (2 * a + bc) if 2 * a + bc else None,
            "negative": 2 * d / (2 * d + bc) if 2 * d + bc else None}


def _resampler(pairs: Sequence[Tuple[str, str]], clusters: Optional[Sequence[str]], rng: random.Random):
    """R4-2: a function returning one bootstrap resample. With clusters (item ids aligned with pairs), whole items
    are resampled with all their units; without, units (library use only)."""
    if clusters is None:
        return lambda: [pairs[rng.randrange(len(pairs))] for _ in pairs]
    groups: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for p_, c in zip(pairs, clusters):
        groups[c].append(p_)
    keys = sorted(groups)
    return lambda: [p_ for _ in keys for p_ in groups[keys[rng.randrange(len(keys))]]]


def kappa_ci(pairs: Sequence[Tuple[str, str]], n_boot: int = CALIB_BOOT,
             seed: int = DEFAULT_SEED, clusters: Optional[Sequence[str]] = None) -> Optional[List[float]]:
    """Percentile bootstrap 95% CI; item-cluster bootstrap when clusters are given (R4-2)."""
    if len(pairs) < 2 or (clusters is not None and len(set(clusters)) < 2):
        return None
    rng = random.Random(f"{seed}:kappa")
    draw = _resampler(pairs, clusters, rng)
    ks = []
    for _ in range(n_boot):
        k = cohen_kappa(draw())
        if k is not None:
            ks.append(k)
    return [_pct(ks, 0.025), _pct(ks, 0.975)]


D1_SCOPES = ("all", "extract_only")
REQUIRED_GATES = {"all": ("extract", "freeform", "pooled", "ref_withheld"),
                  "extract_only": ("extract", "ref_withheld_extract")}   # R4-2; extract_only is an OWNER-DECISION
MIN_MINORITY = 5   # R3-2: a skewed stratum with fewer minority-class units is not evaluable (a failed gate)


def minority_agreement(pairs: Sequence[Tuple[str, str]]) -> Tuple[Optional[str], Optional[float]]:
    """(minority class of the second rater/human, its specific agreement)."""
    if not pairs:
        return None, None
    hp = sum(h == "PASS" for _, h in pairs) / len(pairs)
    cls = "PASS" if hp < 0.5 else "FAIL"
    pc = per_class_agreement(pairs)
    return cls, pc["positive" if cls == "PASS" else "negative"]


def stratum_gate(pairs: Sequence[Tuple[str, str]], n_boot: int = CALIB_BOOT, seed: int = DEFAULT_SEED,
                 clusters: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """R3-2 for one stratum of (judge, human) pairs. Non-skewed: lower 95% CI of kappa >= threshold. Skewed:
    lower 95% CI of the minority-class specific agreement >= threshold, and not evaluable with fewer than
    MIN_MINORITY minority units."""
    n = len(pairs)
    if not n:
        return {"status": "not_evaluable", "reason": "no units", "n": 0}
    hp = sum(h == "PASS" for _, h in pairs) / n
    skewed = hp < SKEW_PREVALENCE or hp > 1 - SKEW_PREVALENCE
    if not skewed:
        ci = kappa_ci(pairs, n_boot, seed, clusters)
        lo = ci[0] if ci else None
        return {"status": "pass" if lo is not None and lo >= KAPPA_LOWER_CI_THRESHOLD else "fail",
                "measure": "kappa", "estimate": cohen_kappa(pairs), "ci95": ci, "n": n, "human_pass_rate": hp,
                "n_clusters": len(set(clusters)) if clusters is not None else None}
    cls, est = minority_agreement(pairs)
    n_min = sum(h == cls for _, h in pairs)
    if n_min < MIN_MINORITY:
        return {"status": "not_evaluable", "reason": f"skewed with {n_min} minority ({cls}) units < {MIN_MINORITY}",
                "measure": f"{cls.lower()}_specific_agreement", "n": n, "human_pass_rate": hp}
    rng = random.Random(f"{seed}:minority")
    draw = _resampler(pairs, clusters, rng)
    bs = []
    for _ in range(n_boot):
        pc = per_class_agreement(draw())
        v = pc["positive" if cls == "PASS" else "negative"]
        if v is not None:
            bs.append(v)
    ci = [_pct(bs, 0.025), _pct(bs, 0.975)] if bs else None
    lo = ci[0] if ci else None
    return {"status": "pass" if lo is not None and lo >= KAPPA_LOWER_CI_THRESHOLD else "fail",
            "measure": f"{cls.lower()}_specific_agreement", "estimate": est, "ci95": ci, "n": n,
            "n_minority": n_min, "human_pass_rate": hp,
            "n_clusters": len(set(clusters)) if clusters is not None else None}


def gate_power(kappa: float, prevalence: float, n: int, reps: int = 400, n_boot: int = 500,
               seed: int = DEFAULT_SEED, judge_prevalence: Optional[float] = None) -> float:
    """P(stratum_gate passes) at n units when the true judge-vs-human kappa is ``kappa`` with human pass
    prevalence ``prevalence`` (judge prevalence the same unless given). Exact simulation of the gate above:
    resampling pairs is multinomial resampling of the 2x2 table, which numpy does fast (numpy needed here)."""
    import numpy as np
    pj, ph = judge_prevalence if judge_prevalence is not None else prevalence, prevalence
    pe = pj * ph + (1 - pj) * (1 - ph)
    po = kappa * (1 - pe) + pe
    p11 = (po - 1 + pj + ph) / 2
    probs = np.array([p11, pj - p11, ph - p11, 1 - pj - ph + p11])   # PP, judgeP/humanF, judgeF/humanP, FF
    if (probs < -1e-12).any():
        raise ValueError("kappa not attainable with these prevalences")
    probs = np.clip(probs, 0, None) / np.clip(probs, 0, None).sum()
    rng = np.random.default_rng(abs(hash((kappa, prevalence, n, seed))) % (2 ** 32))
    thr = KAPPA_LOWER_CI_THRESHOLD

    def stats(c):  # c: (..., 4) counts -> kappa, positive and negative specific agreement
        a, b, cc, d = (c[..., i].astype(float) for i in range(4))
        m = a + b + cc + d
        po_ = (a + d) / m
        pe_ = ((a + b) * (a + cc) + (cc + d) * (b + d)) / (m * m)
        with np.errstate(divide="ignore", invalid="ignore"):
            k = np.where(pe_ >= 1, np.where(po_ == 1, 1.0, 0.0), (po_ - pe_) / (1 - pe_))
            pos = np.where(2 * a + b + cc > 0, 2 * a / (2 * a + b + cc), np.nan)
            neg = np.where(2 * d + b + cc > 0, 2 * d / (2 * d + b + cc), np.nan)
        return k, pos, neg
    passes = 0
    for _ in range(reps):
        c = rng.multinomial(n, probs)
        hp = (c[0] + c[2]) / n
        boots = rng.multinomial(n, c / n, size=n_boot)
        k, pos, neg = stats(boots)
        if SKEW_PREVALENCE <= hp <= 1 - SKEW_PREVALENCE:
            lo = np.percentile(k, 2.5)
        else:
            minority_pass = hp < 0.5
            n_min = c[0] + c[2] if minority_pass else c[1] + c[3]
            if n_min < MIN_MINORITY:
                continue
            v = pos if minority_pass else neg
            v = v[~np.isnan(v)]
            if not len(v):
                continue
            lo = np.percentile(v, 2.5)
        passes += lo >= thr
    return passes / reps


def _np_stats(c):
    """c: (..., 4) counts [PP, judgeP/humanF, judgeF/humanP, FF] -> kappa, positive and negative agreement."""
    import numpy as np
    a, b, cc, d = (c[..., i].astype(float) for i in range(4))
    m = a + b + cc + d
    with np.errstate(divide="ignore", invalid="ignore"):
        po_ = (a + d) / m
        pe_ = ((a + b) * (a + cc) + (cc + d) * (b + d)) / (m * m)
        k = np.where(pe_ >= 1, np.where(po_ == 1, 1.0, 0.0), (po_ - pe_) / (1 - pe_))
        pos = np.where(2 * a + b + cc > 0, 2 * a / (2 * a + b + cc), np.nan)
        neg = np.where(2 * d + b + cc > 0, 2 * d / (2 * d + b + cc), np.nan)
    return k, pos, neg


def _np_gate(item_counts, rng, n_boot: int) -> bool:
    """stratum_gate with the item-cluster bootstrap, vectorised. item_counts: (n_items, 4)."""
    import numpy as np
    tot = item_counts.sum(0)
    n = tot.sum()
    if n == 0 or len(item_counts) < 2:
        return False
    hp = (tot[0] + tot[2]) / n
    idx = rng.integers(0, len(item_counts), size=(n_boot, len(item_counts)))
    k, pos, neg = _np_stats(item_counts[idx].sum(1))
    if SKEW_PREVALENCE <= hp <= 1 - SKEW_PREVALENCE:
        return bool(np.percentile(k, 2.5) >= KAPPA_LOWER_CI_THRESHOLD)
    minority_pass = hp < 0.5
    n_min = tot[0] + tot[2] if minority_pass else tot[1] + tot[3]
    if n_min < MIN_MINORITY:
        return False
    v = pos if minority_pass else neg
    v = v[~np.isnan(v)]
    return bool(len(v)) and bool(np.percentile(v, 2.5) >= KAPPA_LOWER_CI_THRESHOLD)


def _accuracy_for_kappa(kappa: float, ph: float) -> float:
    """Judge = human label flipped with probability 1 - m (symmetric): solve m for the population kappa."""
    lo, hi = 0.5, 1.0
    for _ in range(60):
        m = (lo + hi) / 2
        pj = ph * m + (1 - ph) * (1 - m)
        pe = pj * ph + (1 - pj) * (1 - ph)
        if (m - pe) / (1 - pe) < kappa:
            lo = m
        else:
            hi = m
    return (lo + hi) / 2


CALIB_DESIGN = {"extract": {"items": 51, "per_model": 40, "prevalence": 0.58},
                "freeform": {"items": 49, "per_model": 49, "prevalence": 0.95}}


def joint_gate_power(kappa: float = 0.75, conc: Optional[float] = 3.0, design: Optional[Dict[str, Dict]] = None,
                     n_models: int = 6, reps: int = 500, n_boot: int = 1000, seed: int = DEFAULT_SEED) -> Dict[str, Any]:
    """R4-2: P(every required gate passes) under item clustering, by simulating the whole calibration design.
    Item-level judge accuracy ~ Beta(m*conc, (1-m)*conc) (conc None = iid), m solved for the population kappa;
    each of n_models models answers per_model of a stratum's items; the reference is withheld for a random half of
    each stratum's items; every gate uses the item-cluster bootstrap. Returns per-gate and joint pass rates for both
    d1 scopes, with Monte Carlo standard errors. numpy needed; single-threaded numpy is enough."""
    import numpy as np
    design = design or CALIB_DESIGN
    rng = np.random.default_rng([seed, int(kappa * 1000), int((conc or 0) * 1000)])
    names = list(REQUIRED_GATES["all"]) + ["ref_withheld_extract"]
    hits = {k: 0 for k in names}
    joint = {sc: 0 for sc in D1_SCOPES}
    for _ in range(reps):
        per_item: Dict[str, Any] = {}
        withheld: Dict[str, Any] = {}
        for st, d in design.items():
            m = _accuracy_for_kappa(kappa, d["prevalence"])
            ni = d["items"]
            acc = rng.beta(m * conc, (1 - m) * conc, ni) if conc else np.full(ni, m)
            units = np.zeros(ni, int)
            for _m in range(n_models):
                units[rng.choice(ni, size=min(d["per_model"], ni), replace=False)] += 1
            counts = np.zeros((ni, 4), int)
            for i in range(ni):
                h = rng.random(units[i]) < d["prevalence"]
                j = np.where(rng.random(units[i]) < acc[i], h, ~h)
                counts[i] = [(j & h).sum(), (j & ~h).sum(), (~j & h).sum(), (~j & ~h).sum()]
            per_item[st] = counts[units > 0]
            w = np.zeros(ni, bool)
            w[rng.choice(ni, size=ni // 2, replace=False)] = True
            withheld[st] = counts[(units > 0) & w]
        res = {"extract": _np_gate(per_item["extract"], rng, n_boot),
               "freeform": _np_gate(per_item["freeform"], rng, n_boot),
               "pooled": _np_gate(np.vstack([per_item["extract"], per_item["freeform"]]), rng, n_boot),
               "ref_withheld": _np_gate(np.vstack([withheld["extract"], withheld["freeform"]]), rng, n_boot),
               "ref_withheld_extract": _np_gate(withheld["extract"], rng, n_boot)}
        for k in names:
            hits[k] += res[k]
        for sc in D1_SCOPES:
            joint[sc] += all(res[g] for g in REQUIRED_GATES[sc])

    def pr(x):
        p_ = x / reps
        return {"p": p_, "mc_se": math.sqrt(max(p_ * (1 - p_), 1e-12) / reps)}
    return {"kappa": kappa, "conc": conc, "design": design, "n_models": n_models, "reps": reps, "n_boot": n_boot,
            "gates": {k: pr(v) for k, v in hits.items()}, "joint": {sc: pr(v) for sc, v in joint.items()}}


def size_calibration(kappa: float = 0.75, prevalences: Optional[Dict[str, float]] = None,
                     grid: Sequence[int] = (60, 90, 120, 150, 180, 240, 300, 360, 480, 600),
                     target: float = 0.80, reps: int = 400, n_boot: int = 500, seed: int = DEFAULT_SEED) -> Dict[str, Any]:
    """R3-2: the smallest n per stratum with gate power >= target."""
    prevalences = prevalences or {"extract": 0.58, "freeform": 0.95}
    out: Dict[str, Any] = {"kappa": kappa, "target_power": target, "reps": reps, "n_boot": n_boot, "strata": {}}
    for st, p in prevalences.items():
        curve, chosen = {}, None
        for n in grid:
            pw = gate_power(kappa, p, n, reps, n_boot, seed)
            curve[n] = pw
            if pw >= target:
                chosen = n
                break
        out["strata"][st] = {"prevalence": p, "power_curve": curve, "n": chosen}
    return out


def agreement_block(pairs: Sequence[Tuple[str, str]], n_boot: int = CALIB_BOOT, seed: int = DEFAULT_SEED,
                    weights: Optional[Sequence[float]] = None,
                    clusters: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """pairs: (rater1, rater2) or (judge, human). Kappa is withheld for skewed strata (R2-1)."""
    n = len(pairs)
    cm = confusion(pairs)
    second_pass = sum(h == "PASS" for _, h in pairs) / n if n else None
    skewed = n > 0 and (second_pass < SKEW_PREVALENCE or second_pass > 1 - SKEW_PREVALENCE)
    out: Dict[str, Any] = {
        "n": n, "accuracy": (cm["PASS_PASS"] + cm["FAIL_FAIL"]) / n if n else None,
        "confusion_first_x_second": cm, "first_pass_only": cm["PASS_FAIL"], "second_pass_only": cm["FAIL_PASS"],
        "first_pass_rate": sum(j == "PASS" for j, _ in pairs) / n if n else None,
        "second_pass_rate": second_pass, "pabak": pabak(pairs), "per_class": per_class_agreement(pairs),
        "skewed": skewed}
    if not skewed:
        out["kappa"] = cohen_kappa(pairs)
        out["kappa_ci95"] = kappa_ci(pairs, n_boot, seed, clusters)
        out["ci_method"] = "item-cluster bootstrap" if clusters is not None else "unit bootstrap"
    if weights and n and sum(weights):
        out["weighted_accuracy"] = sum(w for w, (a, b) in zip(weights, pairs) if a == b) / sum(weights)
    return out


ATTEST_STATEMENT = "I did not use an LLM"
ATTEST_FIELDS = ("name_or_initials", "date", "human_labels_no_llm")
# R4-4: a fixed table of Cyrillic and Greek letters that look like Latin ones
_CONFUSABLES = str.maketrans({
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c", "т": "t", "у": "y",
    "х": "x", "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "ɡ": "g", "ӏ": "l",
    "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t", "υ": "u", "χ": "x",
    "ζ": "z", "η": "n", "μ": "u"})


def normalize_name(name: str) -> str:
    """R4-4: NFKD, drop combining marks, map confusables to Latin, casefold, keep [a-z0-9]."""
    import unicodedata
    t = unicodedata.normalize("NFKD", name or "")
    t = "".join(ch for ch in t if not unicodedata.combining(ch)).casefold().translate(_CONFUSABLES)
    return re.sub(r"[^a-z0-9]", "", t)


def name_initials(name: str) -> str:
    import unicodedata
    toks = [t for t in re.split(r"[\s\W_]+", unicodedata.normalize("NFKD", name or "")) if t]
    return normalize_name("".join(t[0] for t in toks))


def _template_lines(rater: str) -> List[str]:
    return [" ".join(x.split()) for x in ATTEST_TEMPLATE.format(r=rater).splitlines() if x.strip()]


def parse_attestation(path: Optional[Path], rater: Optional[str] = None,
                      today: Optional[str] = None) -> Dict[str, Any]:
    """R3-4 + R4-4. Valid only when the file is exactly the template's statement block plus the three fields (no
    other non-empty lines), each field exactly once, a name with >= 2 letters/digits, a real ISO date not after
    today, and human_labels_no_llm exactly "yes" (one trailing period allowed)."""
    import datetime as _dt
    if not path or not Path(path).exists():
        return {"ok": False, "name": None, "problems": ["missing attestation file"]}
    text = Path(path).read_text(encoding="utf-8")
    problems = []
    if ATTEST_STATEMENT.casefold() not in " ".join(text.split()).casefold():
        problems.append("statement text missing")
    seen: Dict[str, List[str]] = defaultdict(list)
    allowed = set()
    for r in ([rater] if rater else list(RATERS)):
        allowed |= {l for l in _template_lines(r) if ":" not in l or not any(l.startswith(f) for f in ATTEST_FIELDS)}
    extra = []
    for line in text.splitlines():
        if not line.strip():
            continue
        k, sep, v = line.partition(":")
        key = re.sub(r"\s*\(.*\)\s*$", "", k.strip().lower())
        if sep and key in ATTEST_FIELDS:
            seen[key].append(v.strip())
        elif " ".join(line.split()) not in allowed:
            extra.append(line.strip()[:80])
    if extra:
        problems.append(f"text outside the template: {extra[:2]}")
    for f in ATTEST_FIELDS:
        if len(seen[f]) != 1:
            problems.append(f"field {f} appears {len(seen[f])} times (must be exactly once)")
    name = seen["name_or_initials"][0] if len(seen["name_or_initials"]) == 1 else None
    if name is not None and len(normalize_name(name)) < 2:
        problems.append("name_or_initials needs at least 2 letters or digits")
    if len(seen["date"]) == 1:
        try:
            d = _dt.date.fromisoformat(seen["date"][0])
            if d > (_dt.date.fromisoformat(today) if today else _dt.date.today()):
                problems.append("date is in the future")
        except ValueError:
            problems.append("date must be a real date, YYYY-MM-DD")
    if len(seen["human_labels_no_llm"]) == 1 and not re.fullmatch(r"(?i)yes\.?", seen["human_labels_no_llm"][0]):
        problems.append('human_labels_no_llm must be exactly "yes"')
    return {"ok": not problems, "name": name, "problems": problems}


def attestation_ok(path: Optional[Path]) -> bool:
    return parse_attestation(path)["ok"]


def attestations_valid(attest: Optional[Dict[str, Optional[Path]]], today: Optional[str] = None) -> Dict[str, Any]:
    """Both raters valid, different files, and different people: normalised names differ and neither name is the
    other's initials (R4-4). Honour-system: this detects carelessness, not a determined forger."""
    res = {r: parse_attestation((attest or {}).get(r), r, today) for r in RATERS}
    problems = [f"{r}: {p}" for r in RATERS for p in res[r]["problems"]]
    pa, pb = (attest or {}).get("A"), (attest or {}).get("B")
    if pa and pb and Path(pa).resolve() == Path(pb).resolve():
        problems.append("A and B use the same attestation file")
    na, nb = res["A"]["name"], res["B"]["name"]
    if na and nb:
        a, b = normalize_name(na), normalize_name(nb)
        if a == b:
            problems.append("A and B attest with the same name: two distinct raters are required")
        elif a in (name_initials(nb),) or b in (name_initials(na),):
            problems.append("one rater's name is the other's initials: two distinct raters are required")
    return {"ok": not problems, "problems": problems, "names": {r: res[r]["name"] for r in RATERS}}


def load_judge_grades(run_dirs: Sequence[Path], contestants: Iterable[str], allow_nonv2: bool = False,
                      prompt_path: Path = EVALS_DIR / "prompts" / "llm_judge.md") -> Tuple[Dict[Tuple[str, str], str], Dict[str, Any]]:
    """(run name, item id) -> PASS|FAIL from v2 judge runs. A grader error counts as FAIL (strict accounting).
    Refuses (ValueError) runs graded with another prompt or by a judge that overlaps a contestant, unless
    allow_nonv2, which is recorded."""
    want = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
    contestants = sorted(set(contestants))
    grades: Dict[Tuple[str, str], str] = {}
    info: Dict[str, Any] = {"prompt_sha256": want, "runs": {}, "allow_nonv2": allow_nonv2, "problems": []}
    for d in run_dirs:
        d = Path(d)
        rj = json.loads((d / "run.json").read_text(encoding="utf-8")) if (d / "run.json").exists() else {}
        g = rj.get("grader") or {}
        judges = g.get("models") or ([g["model"]] if g.get("model") else [])
        problems = []
        if g.get("prompt_sha256") != want:
            problems.append(f"{d.name}: grader prompt sha {g.get('prompt_sha256')} is not the current v2 prompt")
        idents = {k: v for k, v in (g.get("served_identity") or {}).items() if isinstance(v, dict)}
        ov = G.family_overlap(judges, contestants, identities=idents) if judges else [{"reason": "no grader recorded"}]
        if ov:
            problems.append(f"{d.name}: judge not independent of the contestants: {ov}")
        sp = d / "scores.jsonl"
        info["runs"][d.name] = {"judges": judges, "prompt_sha256": g.get("prompt_sha256"), "problems": problems,
                                "served_identity": idents,
                                "scores_sha256": hashlib.sha256(sp.read_bytes()).hexdigest() if sp.exists() else None}
        info["problems"] += problems
        for sc in R.read_jsonl(d / "scores.jsonl"):
            if sc.get("scorer") != "llm_judge":
                continue
            ok = sc.get("status", "ok") == "ok" and sc.get("parsed") in LABELS
            grades[(d.name, sc["id"])] = sc["parsed"] if ok else "FAIL"
    if info["problems"] and not allow_nonv2:
        raise ValueError("; ".join(info["problems"]))
    return grades, info


def agreement(key: Path, sheet_a: Path, sheet_b: Path, adjudication: Optional[Path] = None,
              judge_grades: Optional[Dict[Tuple[str, str], str]] = None, attest: Optional[Dict[str, Path]] = None,
              n_boot: int = CALIB_BOOT, seed: int = DEFAULT_SEED, d1_scope: str = "all",
              today: Optional[str] = None) -> Dict[str, Any]:
    """Inter-rater agreement, consensus labels, and (when v2 judge grades are given) judge-vs-consensus agreement
    with the pre-registered verdict. Missing labels and unadjudicated disagreements are listed, never dropped
    silently. All intervals use the item-cluster bootstrap (R4-2). d1_scope: 'all' (gates: extract, freeform, pooled,
    ref_withheld) or 'extract_only' (gates: extract, ref_withheld within extract; OWNER-DECISION)."""
    if d1_scope not in D1_SCOPES:
        raise ValueError(f"d1_scope must be one of {D1_SCOPES}")
    keyrows = _key(key)
    la, lb = _labels(sheet_a), _labels(sheet_b)
    adj = {}
    if adjudication and Path(adjudication).exists():
        for r in csv.DictReader(Path(adjudication).open(encoding="utf-8")):
            g = (r.get("final_grade") or "").strip().upper()
            if g in LABELS:
                adj[r["unit_id"]] = g
    units, unlabelled, unadjudicated = [], [], []
    for k in keyrows:
        a, b = la.get(k["row_A"], {}).get("grade"), lb.get(k["row_B"], {}).get("grade")
        if not a or not b:
            unlabelled.append(k["unit_id"])
            continue
        cons = a if a == b else adj.get(k["unit_id"])
        if cons is None:
            unadjudicated.append(k["unit_id"])
        units.append({**k, "A": a, "B": b, "consensus": cons,
                      "weight": float(k["stratum_N"]) / float(k["stratum_n"])})
    attv = attestations_valid(attest, today)
    att = {"ok": attv["ok"], "problems": attv["problems"]}
    out: Dict[str, Any] = {
        "design": {"n_units": len(keyrows), "threshold_kappa_lower_ci": KAPPA_LOWER_CI_THRESHOLD,
                   "skew_prevalence": SKEW_PREVALENCE, "min_minority": MIN_MINORITY, "bootstrap": n_boot,
                   "ci_method": "item-cluster percentile bootstrap", "seed": seed, "d1_scope": d1_scope},
        "unlabelled_units": unlabelled, "unadjudicated_units": unadjudicated, "attestations": att,
        "inter_rater": agreement_block([(u["A"], u["B"]) for u in units], n_boot, seed,
                                       clusters=[u["item_id"] for u in units])}
    inter = out["inter_rater"]
    for dim, field in (("by_ref_shown", "ref_shown"), ("by_type", "task_type")):
        out.setdefault("inter_rater_" + dim, {})
        for v in sorted({u[field] for u in units}):
            sub = [u for u in units if u[field] == v]
            out["inter_rater_" + dim][v] = agreement_block([(u["A"], u["B"]) for u in sub], n_boot, seed,
                                                           clusters=[u["item_id"] for u in sub])
    if judge_grades is None:
        out["verdict"] = "pending: no v2 judge grades yet (OWNER-APPROVAL run)"
        return out
    rows, missing = [], []
    for u in units:
        if u["consensus"] is None:
            continue
        j = judge_grades.get((u["run"], u["item_id"]))
        if j is None:
            missing.append(u["unit_id"])
            continue
        rows.append({**u, "judge": j})
    out["judge_missing_units"] = missing

    def block(rs):
        return agreement_block([(r["judge"], r["consensus"]) for r in rs], n_boot, seed, [r["weight"] for r in rs],
                               clusters=[r["item_id"] for r in rs])
    out["judge_vs_consensus"] = block(rows)
    for dim, field in (("by_model", "model"), ("by_type", "task_type"), ("by_ref_shown", "ref_shown")):
        out[dim] = {v: block([r for r in rows if r[field] == v]) for v in sorted({r[field] for r in rows})}
    lenient = [r for r in rows if r["judge"] == "PASS" and r["consensus"] == "FAIL"]
    strict = [r for r in rows if r["judge"] == "FAIL" and r["consensus"] == "PASS"]

    def mean_log(rs):
        return sum(math.log1p(int(r["chars"])) for r in rs) / len(rs) if rs else None
    out["length_vs_errors"] = {"mean_log_chars_lenient_errors": mean_log(lenient),
                               "mean_log_chars_strict_errors": mean_log(strict),
                               "mean_log_chars_agreements": mean_log([r for r in rows if r["judge"] == r["consensus"]])}
    # R3-2 / R4-2: one gate per required stratum, item-cluster bootstrap
    def gate(rs):
        return stratum_gate([(r["judge"], r["consensus"]) for r in rs], n_boot, seed, [r["item_id"] for r in rs])
    strata = {"pooled": rows, "ref_withheld": [r for r in rows if r["ref_shown"] == "no"]}
    for t in sorted({k["task_type"] for k in keyrows}):
        strata[t] = [r for r in rows if r["task_type"] == t]
    strata["ref_withheld_extract"] = [r for r in rows if r["ref_shown"] == "no" and r["task_type"] == "extract"]
    all_gates = {k: gate(v) for k, v in strata.items()}
    required = REQUIRED_GATES[d1_scope]
    gates = {k: all_gates.get(k) or {"status": "not_evaluable", "reason": "no units", "n": 0} for k in required}
    out["gates"] = gates
    out["gates_reported_not_required"] = {k: v for k, v in all_gates.items() if k not in required}
    ir_lo = (inter.get("kappa_ci95") or [None])[0]
    if unlabelled or unadjudicated or missing or not att["ok"]:
        verdict = "incomplete"
    elif ir_lo is None or ir_lo < KAPPA_LOWER_CI_THRESHOLD:
        verdict = "inconclusive: inter-rater kappa lower CI below threshold (human labels unreliable)"
    elif all(g["status"] == "pass" for g in gates.values()):
        verdict = "pass"
    else:
        verdict = "fail: " + ", ".join(f"{k}={g['status']}" for k, g in gates.items() if g["status"] != "pass")
    out["verdict"] = verdict
    return out


# ------------------------------------------------------------------------------------------- statistics
def _solve(A: List[List[float]], b: List[float]) -> List[float]:
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-12:
            raise ZeroDivisionError("singular matrix")
        M[c], M[p] = M[p], M[c]
        for r in range(n):
            if r != c and M[r][c]:
                f = M[r][c] / M[c][c]
                M[r] = [x - f * y for x, y in zip(M[r], M[c])]
    return [M[i][n] / M[i][i] for i in range(n)]


def _inv(A: List[List[float]]) -> List[List[float]]:
    n = len(A)
    cols = [_solve(A, [1.0 if i == j else 0.0 for i in range(n)]) for j in range(n)]
    return [[cols[j][i] for j in range(n)] for i in range(n)]


def logistic_fit(X: List[List[float]], y: List[float], ridge: float = 0.01, iters: int = 50) -> Dict[str, Any]:
    """Newton-Raphson logistic regression. A small ridge (not on column 0, the intercept) keeps the fit finite
    under separation, for example a model that passes every freeform item. Wald SEs come from the penalised
    information matrix."""
    p = len(X[0])
    beta = [0.0] * p
    converged = False
    H: List[List[float]] = []
    for _ in range(iters):
        grad = [0.0] * p
        H = [[0.0] * p for _ in range(p)]
        for xi, yi in zip(X, y):
            eta = sum(b * x for b, x in zip(beta, xi))
            mu = 1 / (1 + math.exp(-max(-35.0, min(35.0, eta))))
            w = mu * (1 - mu)
            r = yi - mu
            for a in range(p):
                if xi[a] == 0.0:
                    continue
                grad[a] += xi[a] * r
                wa = w * xi[a]
                Ha = H[a]
                for c in range(p):
                    if xi[c] != 0.0:
                        Ha[c] += wa * xi[c]
        for a in range(1, p):
            grad[a] -= ridge * beta[a]
            H[a][a] += ridge
        try:
            step = _solve(H, grad)
        except ZeroDivisionError:
            break
        beta = [b + s for b, s in zip(beta, step)]
        if max(abs(s) for s in step) < 1e-8:
            converged = True
            break
    try:
        cov = _inv(H)
        se = [math.sqrt(max(cov[i][i], 0.0)) for i in range(p)]
    except ZeroDivisionError:
        se = [float("nan")] * p
    return {"coef": beta, "se": se, "converged": converged}


def _design(rows: Sequence[Dict[str, Any]], models: Sequence[str], types: Sequence[str],
            item_mean: Dict[str, float]) -> List[List[float]]:
    """[1, centred log length, item mean log length, model dummies (first is baseline), type dummies]."""
    X = []
    for r in rows:
        ll = math.log1p(r["chars"])
        x = [1.0, ll - item_mean[r["id"]], item_mean[r["id"]]]
        x += [1.0 if r["model"] == m else 0.0 for m in models[1:]]
        x += [1.0 if r["type"] == t else 0.0 for t in types[1:]]
        X.append(x)
    return X


def _item_means(rows: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    acc: Dict[str, List[float]] = defaultdict(list)
    for r in rows:
        acc[r["id"]].append(math.log1p(r["chars"]))
    return {k: sum(v) / len(v) for k, v in acc.items()}


def _pooled_beta(rows: Sequence[Dict[str, Any]]) -> Optional[Tuple[float, float]]:
    if not rows or len({r["passed"] for r in rows}) < 2:
        return None
    models = sorted({r["model"] for r in rows})
    types = sorted({r["type"] for r in rows})
    fit = logistic_fit(_design(rows, models, types, _item_means(rows)), [1.0 if r["passed"] else 0.0 for r in rows])
    return fit["coef"][1], fit["se"][1]


def _model_beta(rows: Sequence[Dict[str, Any]], model: str, item_mean: Dict[str, float]) -> Optional[float]:
    rs = [r for r in rows if r["model"] == model]
    if len({r["passed"] for r in rs}) < 2:
        return None
    types = sorted({r["type"] for r in rs})
    X = [[1.0, math.log1p(r["chars"]) - item_mean[r["id"]]] + [1.0 if r["type"] == t else 0.0 for t in types[1:]]
         for r in rs]
    return logistic_fit(X, [1.0 if r["passed"] else 0.0 for r in rs])["coef"][1]


def _discordant(rows: Sequence[Dict[str, Any]], model: Optional[str] = None) -> Tuple[int, int]:
    """(longer passes, shorter passes) over same-item pairs of different models whose grades differ. With model,
    only pairs that include it."""
    by_item: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_item[r["id"]].append(r)
    lw = sw = 0
    for rs in by_item.values():
        for i in range(len(rs)):
            for j in range(i + 1, len(rs)):
                a, b = rs[i], rs[j]
                if a["passed"] == b["passed"] or a["chars"] == b["chars"]:
                    continue
                if model and model not in (a["model"], b["model"]):
                    continue
                longer = a if a["chars"] > b["chars"] else b
                if longer["passed"]:
                    lw += 1
                else:
                    sw += 1
    return lw, sw


def _pct(xs: List[float], q: float) -> Optional[float]:
    xs = sorted(x for x in xs if x is not None and not math.isnan(x))
    if not xs:
        return None
    k = (len(xs) - 1) * q
    f, c = math.floor(k), math.ceil(k)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def length_bias(rows: Sequence[Dict[str, Any]], n_boot: int = 200, seed: int = DEFAULT_SEED) -> Dict[str, Any]:
    """Length analysis on stored grades (see the module docstring). The bootstrap resamples items (all of an
    item's rows together), so the CIs respect the within-item pairing."""
    rows = list(rows)
    models = sorted({r["model"] for r in rows})
    types = sorted({r["type"] for r in rows})
    item_mean = _item_means(rows)
    by_item: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_item[r["id"]].append(r)
    ids = sorted(by_item)

    def stats(rs):
        im = _item_means(rs)
        out = {"pooled": _pooled_beta(rs)}
        for t in types:
            out[f"type:{t}"] = _pooled_beta([r for r in rs if r["type"] == t])
        for m in models:
            out[f"model:{m}"] = _model_beta(rs, m, im)
            lw, sw = _discordant(rs, m)
            out[f"disc:{m}"] = lw / (lw + sw) if lw + sw else None
        lw, sw = _discordant(rs)
        out["disc:all"] = lw / (lw + sw) if lw + sw else None
        return out

    point = stats(rows)
    rng = random.Random(seed)
    boots: Dict[str, List[float]] = defaultdict(list)
    for _ in range(n_boot):
        rs = []
        for k, iid in enumerate(rng.choices(ids, k=len(ids))):
            rs += [{**r, "id": f"{iid}#{k}"} for r in by_item[iid]]
        for name, v in stats(rs).items():
            if v is None:
                continue
            boots[name].append(v[0] if isinstance(v, tuple) else v)

    def ci(name):
        b = boots.get(name, [])
        return [_pct(b, 0.025), _pct(b, 0.975)] if b else None

    res: Dict[str, Any] = {"n_rows": len(rows), "n_items": len(ids), "bootstrap": n_boot, "seed": seed,
                           "length_unit": "log(1 + visible answer characters), centred within item",
                           "pooled": {}, "by_type": {}, "by_model": {}}
    for key, dest in [("pooled", res["pooled"])] + [(f"type:{t}", res["by_type"].setdefault(t, {})) for t in types]:
        v = point[key]
        sub = [r for r in rows if key == "pooled" or r["type"] == key.split(":", 1)[1]]
        dest.update({"coef_centred_loglen": v[0] if v else None, "se": v[1] if v else None, "ci95": ci(key),
                     "n": len(sub), "pass_rate": sum(r["passed"] for r in sub) / len(sub) if sub else None,
                     "odds_ratio_per_doubling": math.exp(v[0] * math.log(2)) if v else None})
    lw, sw = _discordant(rows)
    res["pooled"]["discordant_pairs"] = {"longer_passes": lw, "shorter_passes": sw,
                                         "p_longer_wins": point["disc:all"], "ci95": ci("disc:all"),
                                         "binom_p": G.mcnemar_exact(lw, sw)}
    for m in models:
        rs = [r for r in rows if r["model"] == m]
        lw, sw = _discordant(rows, m)
        cl = [math.log1p(r["chars"]) - item_mean[r["id"]] for r in rs]
        res["by_model"][m] = {
            "n": len(rs), "pass_rate": sum(r["passed"] for r in rs) / len(rs) if rs else None,
            "median_chars": sorted(r["chars"] for r in rs)[len(rs) // 2] if rs else None,
            "mean_centred_loglen": sum(cl) / len(cl) if cl else None,
            "coef_centred_loglen": point[f"model:{m}"], "ci95": ci(f"model:{m}"),
            "discordant_longer_passes": lw, "discordant_shorter_passes": sw,
            "p_longer_wins": point[f"disc:{m}"], "p_longer_wins_ci95": ci(f"disc:{m}")}
    return res


# ------------------------------------------------------------------------------------------- length quintiles
def length_quintiles(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """PROTOCOL C1.9: the judge's pass rate within answer-length quintiles (cut points over all rows), overall and
    per model."""
    xs = sorted(r["chars"] for r in rows)
    if not xs:
        return {}
    cuts = [_pct([float(x) for x in xs], q) for q in (0.2, 0.4, 0.6, 0.8)]

    def q_of(c):
        return sum(c > x for x in cuts)
    out: Dict[str, Any] = {"cut_points_chars": cuts, "overall": {}, "by_model": {}}
    for scope, rs in [("overall", rows)] + [(m, [r for r in rows if r["model"] == m])
                                            for m in sorted({r["model"] for r in rows})]:
        cells = {}
        for q in range(5):
            sub = [r for r in rs if q_of(r["chars"]) == q]
            cells[f"Q{q + 1}"] = {"n": len(sub), "pass_rate": sum(r["passed"] for r in sub) / len(sub) if sub else None}
        if scope == "overall":
            out["overall"] = cells
        else:
            out["by_model"][scope] = cells
    return out


# ------------------------------------------------------------------------------------------- self-preference
def load_judge_votes(run_dirs: Sequence[Path], items: Dict[str, Dict[str, Any]],
                     suite: Optional[str] = None) -> List[Dict[str, Any]]:
    """One row per (item, answer model, judge) vote with a valid grade, from multi-judge score rows
    (extra.judges) or single-judge rows (extra.grader.model). Adds length and reference token-F1."""
    out = []
    for d in run_dirs:
        d = Path(d)
        rj = json.loads((d / "run.json").read_text(encoding="utf-8")) if (d / "run.json").exists() else {}
        model = rj.get("model") or d.name
        resp = R.latest_by_id(R.read_jsonl(d / "responses.jsonl"))
        for sc in R.latest_by_id(R.read_jsonl(d / "scores.jsonl")).values():
            if sc.get("scorer") != "llm_judge" or (suite and sc.get("suite") != suite):
                continue
            ex = sc.get("extra") or {}
            votes = ex.get("judges")
            if not votes and sc.get("status") == "ok" and (ex.get("grader") or {}).get("model"):
                votes = [{"model": ex["grader"]["model"], "status": "ok", "grade": sc.get("parsed")}]
            r = resp.get(sc["id"]) or {}
            it = items.get(sc["id"]) or {}
            cand = S.strip_thinking(r.get("content") or "")
            try:
                ref = S.llm_judge_spec(it)["reference"] if it else ""
            except S.ItemError:
                ref = ""
            f1 = S.token_f1(cand, ref) if ref else 0.0
            for v in votes or []:
                if v.get("status") != "ok" or v.get("grade") not in LABELS:
                    continue
                out.append({"id": sc["id"], "model": model, "judge": v["model"], "passed": v["grade"] == "PASS",
                            "chars": len(cand), "ref_f1": f1, "type": r.get("type") or "?"})
    return out


def _own(judge: str, model: str, mode: str) -> bool:
    """own-family (or exact self) indicator. Unknown families are refused (R3-3): never silently 'not own'."""
    if judge.strip().casefold() == model.strip().casefold():
        return True
    if mode == "self":
        return False
    jf, mf = G.model_families(judge), G.model_families(model)
    if jf is None or mf is None:
        raise ValueError(f"unknown model family for {judge if jf is None else model!r}: add it to model_families.json")
    return bool(jf & mf)


SELFPREF_RIDGE = 1e-3   # tiny, for numerical stability only
SELFPREF_MIN_VOTES_PER_ITEM = 12   # R4-5: validated design floor (3 judges x 4 answer models)


def _selfpref_fit(rows: Sequence[Dict[str, Any]], mode: str, quality_proxy: bool = False) -> Optional[Dict[str, Any]]:
    """R3-3/R4-5: logistic regression of PASS on item fixed effects, judge FE, answer-model FE, judge x centred log
    length, judge x centred reference F1, and own. With quality_proxy, also judge x (leave-judge-out mean grade of
    the same answer, centred within item): a judge-independent quality proxy that absorbs judges differing in
    discrimination. Items whose votes are all PASS or all FAIL are dropped (no within-item information). The item
    FE are eliminated with a block (Schur-complement) Newton step, so only a small dense system is solved.
    numpy required. Judge x answer-family slopes are collinear with own and are not estimable (disclosed)."""
    import numpy as np
    by_item: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_item[r["id"]].append(r)
    keep = [r for rs in by_item.values() if len({x["passed"] for x in rs}) > 1 for r in rs]
    judges = sorted({r["judge"] for r in keep})
    models = sorted({r["model"] for r in keep})
    items = sorted({r["id"] for r in keep})
    own = np.array([_own(r["judge"], r["model"], mode) for r in keep], float)
    if len(judges) < 2 or not own.any() or own.all():
        return None
    pos = {it: k for k, it in enumerate(items)}
    ix = np.array([pos[r["id"]] for r in keep])
    ll = np.array([math.log1p(r["chars"]) for r in keep])
    f1 = np.array([r["ref_f1"] for r in keep], float)
    ni = len(items)

    def centre(v):
        s_ = np.zeros(ni)
        c_ = np.zeros(ni)
        np.add.at(s_, ix, v)
        np.add.at(c_, ix, 1.0)
        return v - (s_ / c_)[ix]
    llc, f1c = centre(ll), centre(f1)
    cols = [own]
    names = ["own"]
    for jn in judges[1:]:
        cols.append(np.array([r["judge"] == jn for r in keep], float))
        names.append(f"judge:{jn}")
    for mn in models[1:]:
        cols.append(np.array([r["model"] == mn for r in keep], float))
        names.append(f"model:{mn}")
    jmask = {jn: np.array([r["judge"] == jn for r in keep], float) for jn in judges}
    for jn in judges:
        cols.append(jmask[jn] * llc)
        names.append(f"len:{jn}")
        cols.append(jmask[jn] * f1c)
        names.append(f"f1:{jn}")
    if quality_proxy:
        # leave-judge-out mean grade of the same (item, answer model)
        votes: Dict[Tuple[str, str], List[Tuple[str, bool]]] = defaultdict(list)
        for r in keep:
            votes[(r["id"], r["model"])].append((r["judge"], bool(r["passed"])))
        q = np.array([np.mean([pv for jv, pv in votes[(r["id"], r["model"])] if jv != r["judge"]] or [0.5])
                      for r in keep])
        qc = centre(q)
        for jn in judges:
            cols.append(jmask[jn] * qc)
            names.append(f"quality:{jn}")
    Z = np.column_stack(cols)
    y = np.array([1.0 if r["passed"] else 0.0 for r in keep])
    p_ = Z.shape[1]
    b = np.zeros(p_)
    a = np.zeros(ni)
    lam = SELFPREF_RIDGE
    converged = False
    S = np.eye(p_)
    for _ in range(100):
        eta = Z @ b + a[ix]
        mu = 1 / (1 + np.exp(-np.clip(eta, -35, 35)))
        w = mu * (1 - mu)
        r_ = y - mu
        gz = Z.T @ r_ - lam * b
        ga = np.zeros(ni)
        np.add.at(ga, ix, r_)
        ga -= lam * a
        d = np.zeros(ni)
        np.add.at(d, ix, w)
        d += lam
        C = np.zeros((ni, p_))
        np.add.at(C, ix, Z * w[:, None])
        Hzz = (Z * w[:, None]).T @ Z + lam * np.eye(p_)
        S = Hzz - (C / d[:, None]).T @ C
        rhs = gz - C.T @ (ga / d)
        dz = np.linalg.solve(S, rhs)
        da = (ga - C @ dz) / d
        b += dz
        a += da
        if max(np.abs(dz).max(), np.abs(da).max()) < 1e-9:
            converged = True
            break
    cov = np.linalg.inv(S)
    vpi = len(keep) / ni
    out = {"coef_own": float(b[0]), "se_own": float(math.sqrt(max(cov[0, 0], 0.0))), "converged": converged,
           "n_votes_used": len(keep), "n_items_used": ni, "n_items_dropped_constant": len(by_item) - ni,
           "votes_per_item": vpi, "quality_proxy": quality_proxy,
           "length_slope_by_judge": {jn: float(b[names.index(f"len:{jn}")]) for jn in judges},
           "ref_f1_slope_by_judge": {jn: float(b[names.index(f"f1:{jn}")]) for jn in judges}}
    if quality_proxy:
        out["quality_slope_by_judge"] = {jn: float(b[names.index(f"quality:{jn}")]) for jn in judges}
    if vpi < SELFPREF_MIN_VOTES_PER_ITEM:
        out["design_warning"] = (f"below validated design ({vpi:.1f} < {SELFPREF_MIN_VOTES_PER_ITEM} votes per item): "
                                 "incidental-parameter bias of the item fixed effects, up to +20% at 2 judges x 3 models")
    return out


def _did(rows: Sequence[Dict[str, Any]], judge: str, mode: str) -> Optional[float]:
    """(judge on own answers - other judges on those answers) - (judge on others' - other judges on those)."""
    def rate(rs):
        return sum(r["passed"] for r in rs) / len(rs) if rs else None
    own_ans = {(r["id"], r["model"]) for r in rows if _own(judge, r["model"], mode)}
    j_own = rate([r for r in rows if r["judge"] == judge and (r["id"], r["model"]) in own_ans])
    o_own = rate([r for r in rows if r["judge"] != judge and (r["id"], r["model"]) in own_ans])
    j_oth = rate([r for r in rows if r["judge"] == judge and (r["id"], r["model"]) not in own_ans])
    o_oth = rate([r for r in rows if r["judge"] != judge and (r["id"], r["model"]) not in own_ans])
    if None in (j_own, o_own, j_oth, o_oth):
        return None
    return (j_own - o_own) - (j_oth - o_oth)


SELFPREF_LIMITATIONS = (
    "selfpref cannot separate self-preference from any judge taste that is correlated with model family (style, "
    "format, verbosity conventions shared within a family): judge x answer-family slopes are collinear with own and "
    "are not estimable. The quality-proxy sensitivity absorbs judges that differ in discrimination, using the other "
    "judges' grades of the same answer, which are themselves model-graded. Validated for >= 3 judges x 4 answer "
    "models; smaller designs carry incidental-parameter bias.")


def self_preference(rows: Sequence[Dict[str, Any]], mode: str = "family", n_boot: int = 100,
                    seed: int = DEFAULT_SEED) -> Dict[str, Any]:
    """R3-3. Needs at least two judges grading the same answers. mode: 'family' (own = shared family, or self)
    or 'self' (own = the judge's own alias). The logistic coefficient on own (log-odds) is estimated with item
    fixed effects, judge and answer-model fixed effects, and per-judge length and reference-overlap slopes; the
    per-judge difference-in-differences is the model-free secondary estimate. CIs come from an item bootstrap.
    Unknown families raise ValueError."""
    rows = list(rows)
    judges = sorted({r["judge"] for r in rows})
    point = _selfpref_fit(rows, mode)
    sens = _selfpref_fit(rows, mode, quality_proxy=True)
    res: Dict[str, Any] = {"mode": mode, "n_votes": len(rows), "judges": judges, "bootstrap": n_boot, "seed": seed,
                           "primary": point, "sensitivity_quality_proxy": sens, "did_by_judge": {},
                           "limitations": SELFPREF_LIMITATIONS}
    if point and sens:
        # A-J10: validation (selfpref_validation_r4.json) showed the leave-judge-out proxy does not remove the
        # discrimination confound (null flag rate ~0.2 with or without it), so a flag is never read as pure
        # self-preference.
        res["interpretation"] = "own-family preference OR differential judge discrimination (not separable here)"
        res["sensitivity_agrees"] = ((abs(sens["coef_own"]) < 1.96 * sens["se_own"])
                                     == (abs(point["coef_own"]) < 1.96 * point["se_own"]))
    if point is None:
        res["note"] = "not estimable: needs >= 2 judges and both own and non-own votes"
        return res
    by_item: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_item[r["id"]].append(r)
    ids = sorted(by_item)
    rng = random.Random(f"{seed}:selfpref")
    coefs: List[float] = []
    dids: Dict[str, List[float]] = defaultdict(list)
    for _ in range(n_boot):
        rs = []
        for k, iid in enumerate(rng.choices(ids, k=len(ids))):
            rs += [{**r, "id": f"{iid}#{k}"} for r in by_item[iid]]
        f = _selfpref_fit(rs, mode)
        if f:
            coefs.append(f["coef_own"])
        for j in judges:
            v = _did(rs, j, mode)
            if v is not None:
                dids[j].append(v)
    point["coef_own_ci95"] = [_pct(coefs, 0.025), _pct(coefs, 0.975)] if coefs else None
    for j in judges:
        v = _did(rows, j, mode)
        res["did_by_judge"][j] = {"did": v, "ci95": [_pct(dids[j], 0.025), _pct(dids[j], 0.975)] if dids[j] else None}
    return res


# ------------------------------------------------------------------------------------------- CLI
def _runs(specs: Sequence[str]) -> List[Path]:
    out = []
    for s in specs:
        p = Path(s)
        if (p / "responses.jsonl").exists():
            out.append(p)
    return sorted(out)


def cmd_sheet(a) -> int:
    rows = load_rows(_runs(a.runs), a.suite)
    items = load_items([Path(p) for p in a.data])
    alloc = {"extract": a.per_model_extract, "freeform": a.per_model_freeform}
    sample = calibration_sample(rows, a.seed, alloc)
    paths = write_calibration(sample, items, Path(a.out_dir), Path(a.prompt), a.seed)
    strata = Counter(r["stratum"] for r in sample)
    print(json.dumps({"units": len(sample), "population": len(rows), "strata": dict(sorted(strata.items())),
                      "reference_withheld": sum(not r["ref_shown"] for r in sample),
                      **{k: str(v) for k, v in paths.items()}}, indent=2))
    return 0


def cmd_adjudicate(a) -> int:
    items = load_items([Path(p) for p in a.data])
    n = write_adjudication(Path(a.key), Path(a.sheet_a), Path(a.sheet_b), items, Path(a.out), a.seed)
    print(f"{n} disagreements -> {a.out}")
    return 0


VERDICT_SCHEMA = "judge-calibration-verdict-v1"
ATTEMPTS_FILE = "calibration_attempts.jsonl"


def _sha(path: Optional[Path]) -> Optional[str]:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if path and Path(path).exists() else None


def write_verdict(res: Dict[str, Any], out: Path, key: Path, sheets: Dict[str, Path], adjudication: Optional[Path],
                  attest: Dict[str, Optional[Path]], judge_info: Optional[Dict[str, Any]], d1_scope: str,
                  deviations: Sequence[str]) -> Dict[str, Any]:
    """R4-1: the bound verdict file, plus an attempt ledger next to the key. A second attempt on the same key is
    refused unless a calibration_retry deviation is declared (ValueError). The ledger is honour-system; the binding
    is the stage-2 manifest pinning this file's sha256."""
    key_sha = _sha(key)
    ledger = Path(key).parent / ATTEMPTS_FILE
    prior = [r for r in R.read_jsonl(ledger) if r.get("calibration_key_sha256") == key_sha]
    devs = {d.partition("=")[0]: d.partition("=")[2] for d in deviations}
    if prior and not devs.get("calibration_retry"):
        raise ValueError(f"calibration attempt {len(prior) + 1} on the same key: declare "
                         "--deviation calibration_retry=<reason> (recorded in the verdict)")
    runs = (judge_info or {}).get("runs", {})
    grades_sha = {k: v.get("scores_sha256") for k, v in sorted(runs.items())}
    judges = sorted({j for v in runs.values() for j in v.get("judges", [])})
    served: Dict[str, Any] = {}
    for v in runs.values():
        served.update(v.get("served_identity") or {})
    prompt_shas = sorted({v.get("prompt_sha256") for v in runs.values() if v.get("prompt_sha256")})
    verdict = res.get("verdict", "")
    doc = {
        "schema": VERDICT_SCHEMA, "verdict": "pass" if verdict == "pass" else verdict, "attempt": len(prior) + 1,
        "d1_scope": d1_scope, "calibration_key_sha256": key_sha,
        "sheets_sha256": {r: _sha(p_) for r, p_ in sheets.items()}, "adjudication_sha256": _sha(adjudication),
        "attestations_sha256": {r: _sha(p_) for r, p_ in attest.items()},
        "judge_models": judges, "judge_served_identity": served,
        "judge_prompt_sha256": prompt_shas[0] if len(prompt_shas) == 1 else prompt_shas,
        "grades_sha256": grades_sha,
        "grades_sha256_combined": hashlib.sha256(json.dumps(grades_sha, sort_keys=True).encode()).hexdigest(),
        "kappa_results": {"gates": res.get("gates"), "inter_rater": res.get("inter_rater"),
                          "judge_vs_consensus": res.get("judge_vs_consensus")},
        "design": res.get("design"), "judge_source_problems": (judge_info or {}).get("problems", []),
        "deviations": devs, "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")}
    text = json.dumps(doc, indent=2, sort_keys=True, default=str) + "\n"
    Path(out).write_text(text, encoding="utf-8")
    vsha = hashlib.sha256(text.encode()).hexdigest()
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"calibration_key_sha256": key_sha, "attempt": doc["attempt"], "verdict": doc["verdict"],
                             "verdict_sha256": vsha, "verdict_file": str(out), "deviations": devs,
                             "at": doc["created_at"]}) + "\n")
    return {**doc, "verdict_sha256": vsha}


def cmd_agree(a) -> int:
    grades = None
    info = None
    if a.judge_runs:
        contestants = {k["model"] for k in _key(Path(a.key))}
        try:
            grades, info = load_judge_grades(_runs(a.judge_runs), contestants, a.allow_nonv2)
        except ValueError as exc:
            print(f"refused: {exc} (pass --allow-nonv2 to explore; it is recorded)", file=sys.stderr)
            return 2
    attest = {"A": Path(a.attest_a) if a.attest_a else None, "B": Path(a.attest_b) if a.attest_b else None}
    res = agreement(Path(a.key), Path(a.sheet_a), Path(a.sheet_b), Path(a.adjudication) if a.adjudication else None,
                    grades, attest, a.bootstrap, a.seed, a.d1_scope)
    if info:
        res["judge_source"] = info
        if info["problems"]:
            res["verdict"] = "exploratory (non-v2 or non-independent judge): " + res["verdict"]
    if a.verdict_out:
        try:
            v = write_verdict(res, Path(a.verdict_out), Path(a.key), {"A": Path(a.sheet_a), "B": Path(a.sheet_b)},
                              Path(a.adjudication) if a.adjudication else None, attest, info, a.d1_scope,
                              a.deviation or [])
        except ValueError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        res["verdict_file"] = {"path": a.verdict_out, "sha256": v["verdict_sha256"], "attempt": v["attempt"]}
    print(json.dumps(res, indent=2, default=str))
    return 0


def cmd_selfpref(a) -> int:
    items = load_items([Path(p) for p in a.data])
    rows = load_judge_votes(_runs(a.runs), items, a.suite)
    print(json.dumps(self_preference(rows, a.mode, a.bootstrap, a.seed), indent=2))
    return 0


def cmd_length(a) -> int:
    rows = load_rows(_runs(a.runs), a.suite)
    res = length_bias(rows, a.bootstrap, a.seed)
    res["length_quintiles"] = length_quintiles(rows)
    res["runs"] = [p.name for p in _runs(a.runs)]
    txt = json.dumps(res, indent=2)
    if a.out:
        Path(a.out).write_text(txt + "\n", encoding="utf-8")
    print(txt)
    return 0


def cmd_perturb(a) -> int:
    rows = load_rows(_runs(a.runs), a.suite)
    items = load_items([Path(p) for p in a.data])
    pr = [{"key": r["key"], "model": r["model"], "item": items[r["id"]], "candidate": r["candidate"],
           "orig_passed": r["passed"] if r["judge_status"] == "ok" else None} for r in rows if r["id"] in items]
    plan = G.perturbation_plan(pr, a.transform or list(G.PERTURBATIONS), a.n, a.seed)
    out = Path(a.out)
    out.write_text("".join(json.dumps({k: v for k, v in p.items() if k != "item"} | {"id": p["item"]["id"]},
                                      ensure_ascii=False) + "\n" for p in plan), encoding="utf-8")
    print(f"plan: {len(plan)} perturbed answers -> {out}")
    if not a.grade:
        return 0
    overlap = G.family_overlap(a.grader_model.split(","), {r["model"] for r in rows})
    if overlap and not a.allow_self_grade:
        print(f"grader(s) {overlap} are also contestants; pass --allow-self-grade to proceed", file=sys.stderr)
        return 2
    if not a.yes_call_model:
        print("--grade calls the grader model; pass --yes-call-model to confirm", file=sys.stderr)
        return 3
    import run as RUN  # the gateway client and grader prompt wiring live in run.py
    site = RUN.load_site_env(Path(a.site_env))
    key = RUN.read_key(a.key_file or RUN.setting("SPARK_EVAL_KEY_FILE", site, RUN.DEFAULT_KEY_FILE))
    base = a.base_url or RUN.setting("EVAL_BASE_URL", site) or f"https://{RUN.setting('SPARK_API_HOST', site)}/v1"
    client = RUN.GatewayClient(base, key, a.timeout)
    del key
    template = Path(a.prompt).read_text(encoding="utf-8")
    ns = argparse.Namespace(grader_max_chars=a.grader_max_chars, seed=a.grader_seed, retries=2, backoff=5.0,
                            grader_parse_retries=a.grader_parse_retries)
    judges = [(m, RUN.make_grader(client, m, template, ns)) for m in a.grader_model.split(",")]
    scorer = G.make_judge_scorer(judges, "first", question_fn=RUN.user_content)
    res = perturbation_run(plan, scorer, not a.use_stored_original)
    Path(str(out) + ".results.json").write_text(json.dumps(res, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(res["summary"], indent=2))
    return 0


def perturbation_run(plan, scorer: Callable, regrade_original: bool) -> Dict[str, Any]:
    return G.perturbation_check(plan, scorer, regrade_original)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sheet")
    s.add_argument("--runs", nargs="+", required=True)
    s.add_argument("--data", nargs="+", required=True, help="suite JSONL(s) holding the questions and references")
    s.add_argument("--suite", default="sevenllm-qa")
    s.add_argument("--per-model-extract", type=int, default=CALIB_ALLOCATION["extract"])
    s.add_argument("--per-model-freeform", type=int, default=CALIB_ALLOCATION["freeform"])
    s.add_argument("--seed", type=int, default=DEFAULT_SEED)
    s.add_argument("--out-dir", required=True)
    s.add_argument("--prompt", default=str(EVALS_DIR / "prompts" / "llm_judge.md"))
    s.set_defaults(fn=cmd_sheet)
    ad = sub.add_parser("adjudicate")
    ad.add_argument("--key", required=True)
    ad.add_argument("--sheet-a", required=True)
    ad.add_argument("--sheet-b", required=True)
    ad.add_argument("--data", nargs="+", required=True)
    ad.add_argument("--out", required=True)
    ad.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ad.set_defaults(fn=cmd_adjudicate)
    g = sub.add_parser("agree")
    g.add_argument("--key", required=True)
    g.add_argument("--sheet-a", required=True)
    g.add_argument("--sheet-b", required=True)
    g.add_argument("--adjudication")
    g.add_argument("--attest-a")
    g.add_argument("--attest-b")
    g.add_argument("--judge-runs", nargs="+", help="run dirs re-graded by the v2 non-contestant judge")
    g.add_argument("--allow-nonv2", action="store_true",
                   help="accept judge runs with another prompt or a non-independent judge (verdict marked exploratory)")
    g.add_argument("--bootstrap", type=int, default=CALIB_BOOT)
    g.add_argument("--seed", type=int, default=DEFAULT_SEED)
    g.add_argument("--d1-scope", choices=D1_SCOPES, default="all",
                   help="required gates: all (extract, freeform, pooled, ref_withheld) or extract_only "
                        "(OWNER-DECISION; recorded in the verdict)")
    g.add_argument("--verdict-out", help="write the bound verdict file (schema judge-calibration-verdict-v1)")
    g.add_argument("--deviation", action="append", metavar="ID=REASON",
                   help="declared deviation, e.g. calibration_retry=<reason> for a second attempt on the same key")
    g.set_defaults(fn=cmd_agree)
    sp = sub.add_parser("selfpref")
    sp.add_argument("--runs", nargs="+", required=True, help="run dirs graded by several judges")
    sp.add_argument("--data", nargs="+", required=True)
    sp.add_argument("--suite", default="sevenllm-qa")
    sp.add_argument("--mode", choices=("family", "self"), default="family")
    sp.add_argument("--bootstrap", type=int, default=100)
    sp.add_argument("--seed", type=int, default=DEFAULT_SEED)
    sp.set_defaults(fn=cmd_selfpref)
    ln = sub.add_parser("length")
    ln.add_argument("--runs", nargs="+", required=True)
    ln.add_argument("--suite", default=None)
    ln.add_argument("--bootstrap", type=int, default=200)
    ln.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ln.add_argument("--out")
    ln.set_defaults(fn=cmd_length)
    p = sub.add_parser("perturb")
    p.add_argument("--runs", nargs="+", required=True)
    p.add_argument("--data", nargs="+", required=True)
    p.add_argument("--suite", default="sevenllm-qa")
    p.add_argument("--transform", action="append", choices=sorted(G.PERTURBATIONS))
    p.add_argument("--n", type=int, default=40, help="answers per transform")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--out", required=True, help="plan JSONL; results go to <out>.results.json")
    p.add_argument("--grade", action="store_true", help="grade the plan (calls the grader model)")
    p.add_argument("--yes-call-model", action="store_true")
    p.add_argument("--use-stored-original", action="store_true",
                   help="compare with the stored grade instead of re-grading the unperturbed answer with the same "
                        "judge config (only valid when the stored grades used the same judge and prompt)")
    p.add_argument("--grader-model", required=True, help="judge alias(es), comma-separated; the first decides")
    p.add_argument("--allow-self-grade", action="store_true")
    p.add_argument("--grader-seed", type=int, default=1234)
    p.add_argument("--grader-parse-retries", type=int, default=2)
    p.add_argument("--grader-max-chars", type=int, default=12000)
    p.add_argument("--prompt", default=str(EVALS_DIR / "prompts" / "llm_judge.md"))
    p.add_argument("--key-file")
    p.add_argument("--site-env", default=str(EVALS_DIR.parent / "site.env"))
    p.add_argument("--base-url")
    p.add_argument("--timeout", type=float, default=900.0)
    p.set_defaults(fn=cmd_perturb)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
