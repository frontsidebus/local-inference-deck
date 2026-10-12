#!/usr/bin/env python3
"""Compare eval runs: one table per suite, one row per run (model + thinking mode).

    python3 evals/report.py evals/results/<run-dir> [<run-dir> ...] [--out report.md] [--csv report.csv]
    python3 evals/report.py --latest 4          # the 4 newest run dirs under evals/results/
    python3 evals/report.py <run-dirs> --split-label-source [--items evals/data/nvd-cwe.jsonl ...]
    python3 evals/report.py <run-dirs> --comparisons evals/plans/comparisons.json   # pre-specified comparisons

Pre-specified comparisons (``--comparisons``) open the report. Each comparison has a declared primary analysis
(metric, exclusions, error policy, CVSS imputation) and gets an effect with a 95% interval, a raw p, the
Holm-adjusted p within its family (an intersection-union family has no adjustment: every member must win), BH q,
Holm across every family, the power check and a verdict. Pre-registered sensitivity analyses can downgrade a verdict
to "confirmed (not robust to X)". The section is titled **pre-registered** only when the confirmatory gate passes
(every compared run is ``protocol.mode == confirmatory``, each failed check has its own deviation, equal budget
policy hashes, ``sampler_check.ok``, equal scorer versions, and the comparisons file hash equals the one frozen in the
freeze manifest); otherwise it is "Post-hoc / exploratory (pre-specified comparisons, not confirmatory)".
Everything else in the report is **exploratory**: raw p-values with no multiplicity correction.

Per suite and run: n, passed, accuracy with a Wilson 95% interval, mean score (partial credit, F1, or the MAE for
cvss_mae), latency, tokens/s, and the denominators: ok / unparsed (of which truncated) / truncated / grader or item
error / api error, plus how many scores used the final-answer follow-up.
- Errors (grader or item ``status=error``, and api errors) count as **FAIL** by default (``--errors fail``); the
  "excl. err" column gives the accuracy with them left out. ``--errors exclude`` swaps the two.
- ``unparsed`` and ``unparsed_truncated`` replies are always wrong. For CVSS MAE an item with no parsed prediction
  is imputed by ``--cvss-impute``: ``max`` (error 10, the legacy rule; default for the tables), ``baseline`` (the
  error of the constant ``--cvss-constant``) or ``none`` (left out; the count is shown). A comparison declares its own.
- Refusal suites: CyberSecEval keyword score, the opener-refusal rate, and the three refusal tiers (full / partial /
  refuse; truncated replies excluded) from ``extra.refusal_tier``.
- Baselines under each table: MCQ chance, the most common gold letter and the longest option; CWE "always CWE-79"
  and the most frequent gold label; CVSS a constant 7.5 and the set's median. Rows marked "oracle" use the set's own
  labels and are optimistic. Where items carry both NVD and CNA labels (``meta.labels``), a label-agreement ceiling
  row and "matches either label" accuracy are shown.
- Strata (where item flags exist): no-signal, leak, name-in-text (CWE), key-is-longest (MCQ), post-cutoff,
  dev-cluster exposure, CSE prompt exclusion; per run, the accuracy in each stratum and the gap. MCQ also gets
  letter-balanced accuracy (the mean of per-gold-letter accuracy).

Item flags come from the score record (top level, ``meta``, ``extra``) or the item file's ``meta`` / ``meta.rigor``
(``has_signal``, ``leak_remaining`` minus name codes = a true key leak, ``cwe_name_in_text`` or the
``gold_cwe_name_in_text`` code, ``template_cluster`` = cluster id, ``post_cutoff`` per model, ``dev_cluster_exposed``,
``frr_exclude``). Give annotated item files with ``--items``; they take precedence over the run.json dataset paths.
Legacy scores: a score's ``legacy`` object, ``extra.legacy_passed``/``legacy_value``, or ``--legacy-scores``.

Paired comparisons (exploratory): per suite and pair of runs, the items both scored, wins each way, the exact
McNemar p, the paired accuracy difference with a Newcombe 95% interval, and the minimum detectable difference at 80%
power from the **exact** power of the exact McNemar test at the observed discordance.

Seeds: runs that differ only in the sampling seed form a seed group, compared on each item's mean pass over seeds.
A sampled run (temperature > 0) with a single seed is marked "single seed — not confirmatory".

``--aggregate`` adds a macro average over suites; it refuses when the runs do not cover the same suites, and within
a suite it uses only the items every run scored.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

EVALS_DIR = Path(__file__).resolve().parent
if str(EVALS_DIR) not in sys.path:
    sys.path.insert(0, str(EVALS_DIR))
import stats as ST  # noqa: E402

ERROR_METRICS = {"cvss_mae"}  # same as scorers.ERROR_METRICS (kept here so report.py stands alone)
RESULTS_DIR = EVALS_DIR / "results"
REPO_DIR = EVALS_DIR.parent
Z95 = 1.959963984540054
MAE_FAIL = 10.0               # the legacy penalty for an unparsed or failed cvss_mae item
SINGLE_SEED = "single seed — not confirmatory"
FLAG_NAMES = ("has_signal", "leak", "post_cutoff", "name_in_text", "dev_cluster_exposed", "dev_neardup_exposed",
              "frr_exclude", "key_longest")
NAME_LEAK_CODES = {"gold_cwe_name_in_text", "cwe_name_in_text"}   # a stratum, not a key leak
CWE_PRIOR = "CWE-79"          # most frequent CWE across NVD as a whole (CWE Top 25); fixed a priori
CVSS_PRIOR = 7.5              # median = mode = MAE-optimal constant of the full NVD pool; fixed a priori
IMPUTE = ("none", "baseline", "max")
BINARY_METRICS = ("accuracy", "opener_refusal", "refusal_full", "refusal_partial", "refusal_refuse",
                  "accuracy_either", "accuracy_onehop")
CONT_METRICS = ("mae", "mean_score", "mae_either")
POWER_TARGET = 0.8
MIN_STRATUM_GAP_N = 5        # strata smaller than this show no gap
MIN_N = 30                    # below this a comparison, sensitivity or stratum is "not evaluable" (plan: defaults.min_n)


def wilson(k: int, n: int, z: float = Z95) -> Tuple[float, float]:
    """Wilson score interval for k successes in n trials; (0, 1) when n = 0."""
    return ST.wilson(k, n, z)


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    out = []
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn last line from an interrupted run
    return out


def latest_by_id(rows: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    d: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        if "id" in r:
            d[r["id"]] = r
    return d


def label_source(rid: str, sc: Optional[Dict[str, Any]], labels: Optional[Dict[str, str]]) -> Optional[str]:
    """The item's meta.label_source: from scores.jsonl, else from --items."""
    return (sc or {}).get("label_source") or (labels or {}).get(rid)


def suite_keys(suite: str, rid: str, sc: Optional[Dict[str, Any]], split: bool,
               labels: Optional[Dict[str, str]]) -> List[str]:
    """The table rows an item counts in: its suite, plus its label_source row when splitting."""
    keys = [suite]
    lab = label_source(rid, sc, labels) if split else None
    if lab:
        keys.append(f"{suite} [label_source={lab}]")
    return keys


def load_labels(paths: Iterable[Path]) -> Dict[str, str]:
    """id -> meta.label_source from suite JSONL files (for runs made before scores.jsonl recorded it)."""
    out: Dict[str, str] = {}
    for p in paths:
        for it in read_jsonl(Path(p)):
            lab = (it.get("meta") or {}).get("label_source")
            if lab and "id" in it:
                out[it["id"]] = lab
    return out


def load_items(paths: Iterable[Path]) -> Dict[str, Dict[str, Any]]:
    """id -> item from suite JSONL files; a later file overrides an earlier one."""
    out: Dict[str, Dict[str, Any]] = {}
    for p in paths:
        try:
            sha = sha256_file(Path(p))
        except OSError:
            sha = None
        for it in read_jsonl(Path(p)):
            if "id" in it:
                it["_src_sha256"] = sha
                it["_src_path"] = str(p)
                out[it["id"]] = it
    return out


def dataset_paths(run_dirs: Iterable[Path], data_root: Path) -> List[Path]:
    """The suite files each run.json names, resolved under data_root (or data_root/evals/data/<name>)."""
    out: List[Path] = []
    seen = set()
    for d in run_dirs:
        run = _read_run(Path(d))
        for ds in run.get("datasets") or []:
            p = ds.get("path")
            if not p:
                continue
            for cand in (Path(p), Path(data_root) / p, Path(data_root) / "evals" / "data" / Path(p).name):
                if cand.is_file():
                    if cand.resolve() not in seen:
                        seen.add(cand.resolve())
                        out.append(cand)
                    break
    return out


def _read_run(run_dir: Path) -> Dict[str, Any]:
    p = Path(run_dir) / "run.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


# ------------------------------------------------------------------------------------------- item records
def _sources(sc: Optional[Dict[str, Any]], item: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    meta = (item or {}).get("meta") or {}
    out = [(sc or {}), (sc or {}).get("meta") or {}, (sc or {}).get("extra") or {}, meta.get("rigor") or {}, meta,
           (item or {})]
    return [s for s in out if isinstance(s, dict)]


def _lookup(name: str, sc: Optional[Dict[str, Any]], item: Optional[Dict[str, Any]]) -> Any:
    """A field from the score record (top level, meta, extra), else from the item's meta.rigor, meta, top level."""
    for src in _sources(sc, item):
        if src.get(name) is not None:
            return src.get(name)
    return None


def _leak_flags(sc: Optional[Dict[str, Any]], item: Optional[Dict[str, Any]],
                stripped_seen: bool = True) -> Tuple[Optional[bool], Optional[bool]]:
    """(true key leak in the prompt the model saw, gold CWE name in the text). A key leak stripped from the prompt
    (``leak_stripped``) counts as absent only when the run read the stripped (annotated) file itself
    (``stripped_seen``: the run's dataset sha256 equals the annotation file's); otherwise it counts as present.
    Name-in-text is a stratum, never a leak."""
    leak: Optional[bool] = None
    name: Optional[bool] = None
    for src in _sources(sc, item):
        if leak is None and "leak_remaining" in src and isinstance(src["leak_remaining"], list):
            codes = set(map(str, src["leak_remaining"]))
            if not stripped_seen:
                codes |= set(map(str, src.get("leak_stripped") or []))
            leak = bool(codes - NAME_LEAK_CODES)
            if name is None and codes & NAME_LEAK_CODES:
                name = True
        if leak is None and isinstance(src.get("leak"), bool):
            leak = src["leak"]
        elif leak is None and isinstance(src.get("leak"), list):
            codes = set(map(str, src["leak"]))
            if stripped_seen:
                codes -= set(map(str, src.get("leak_stripped") or []))
            leak = bool(codes - NAME_LEAK_CODES)
            if name is None and codes & NAME_LEAK_CODES:
                name = True
    for src in _sources(sc, item):
        if "cwe_name_in_text" in src:          # explicit field (True / False / None = unknown) wins
            v = src["cwe_name_in_text"]
            return leak, (bool(v) if v is not None else None)
    return leak, name


def _label_role(sc: Optional[Dict[str, Any]], item: Optional[Dict[str, Any]]) -> Optional[str]:
    """nvd / cna / adp: who supplied the gold label. The item's own provenance wins: meta.rigor.label_source
    ('adp-secondary:CISA-ADP' etc.), then meta.label_source, and only then the score record (which may carry a
    legacy 'cna' for an ADP-labelled item). None for benchmark keys and other sources."""
    meta = (item or {}).get("meta") or {}
    for src in (meta.get("rigor") or {}, meta, (sc or {}), (sc or {}).get("meta") or {}):
        v = src.get("label_source")
        if isinstance(v, str):
            role = re.split(r"[-:\s]", v.strip().lower(), maxsplit=1)[0]
            if role in ("nvd", "cna", "adp"):
                return role
    return None


def _key_longest(item: Optional[Dict[str, Any]]) -> Optional[bool]:
    """MCQ: whether the gold option is the unique longest option (None for non-MCQ items)."""
    if not item or not item.get("choices") or item.get("scorer") not in (None, "mcq_letter"):
        return None
    ans = str(item.get("answer") or "").strip().upper()[:1]
    lens = []
    for i, ch in enumerate(item["choices"]):
        txt = re.sub(r"^\s*[A-Za-z][).:]\s*", "", str(ch))
        lens.append((chr(65 + i), len(txt.strip())))
    if not ans or ans not in dict(lens):
        return None
    top = max(n for _, n in lens)
    longest = [k for k, n in lens if n == top]
    return len(longest) == 1 and longest[0] == ans


def _longest_letters(item: Dict[str, Any]) -> List[str]:
    """The letters of the longest option(s); several when they tie."""
    lens = [(chr(65 + i), len(re.sub(r"^\s*[A-Za-z][).:]\s*", "", str(ch)).strip()))
            for i, ch in enumerate(item.get("choices") or [])]
    if not lens:
        return []
    top = max(n for _, n in lens)
    return [k for k, n in lens if n == top]


def _longest_credit(item: Dict[str, Any]) -> float:
    """'Pick the longest option' scores 1/k when the key is among k tied longest options."""
    tied = _longest_letters(item)
    g = str(item.get("answer") or "").strip().upper()[:1]
    return (1.0 / len(tied)) if tied and g in tied else 0.0


def _legacy(sc: Dict[str, Any], legacy_sc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if legacy_sc is not None:
        return {"passed": bool(legacy_sc.get("passed")), "value": legacy_sc.get("value"),
                "status": legacy_sc.get("status", "ok")}
    lg = sc.get("legacy")
    if isinstance(lg, dict):
        return {"passed": bool(lg.get("passed")), "value": lg.get("value"), "status": lg.get("status", "ok")}
    ex = sc.get("extra") or {}
    if "legacy_passed" in ex or "legacy_value" in ex:
        return {"passed": bool(ex.get("legacy_passed")), "value": ex.get("legacy_value"),
                "status": ex.get("legacy_status", "ok")}
    return None


def item_records(run_dir: Path, items: Optional[Dict[str, Dict[str, Any]]] = None,
                 legacy_name: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """id -> a normalised record of one run's response and score.

    status: ok | unparsed | unparsed_truncated | error (grader or item) | api_error | unscored."""
    run_dir = Path(run_dir)
    run_json = _read_run(run_dir)
    model = run_json.get("model")
    run_shas = {ds.get("sha256") for ds in run_json.get("datasets") or [] if ds.get("sha256")}
    resp = latest_by_id(read_jsonl(run_dir / "responses.jsonl"))
    scores = latest_by_id(read_jsonl(run_dir / "scores.jsonl"))
    legacy = latest_by_id(read_jsonl(run_dir / legacy_name)) if legacy_name else {}
    out: Dict[str, Dict[str, Any]] = {}
    for rid in list(resp) + [k for k in scores if k not in resp]:
        r = resp.get(rid) or {}
        sc = scores.get(rid)
        it = (items or {}).get(rid)
        suite = (sc or {}).get("suite") or r.get("suite") or (it or {}).get("suite")
        if not suite:
            continue
        ex = (sc or {}).get("extra") or {}
        if r.get("error"):
            status = "api_error"
        elif sc is None:
            status = "unscored"
        else:
            status = sc.get("status", "ok") or "ok"
        trunc = (r.get("finish_reason") == "length" or status == "unparsed_truncated" or bool(ex.get("truncated"))
                 or bool((sc or {}).get("truncated")))
        follow = bool(ex.get("followup_used") or (sc or {}).get("followup_used") or (sc or {}).get("final_answer_prompt")
                      or r.get("final_answer_prompt") or r.get("followup_used"))
        value = (sc or {}).get("value")
        pc = _lookup("post_cutoff", sc, it)
        if isinstance(pc, dict):
            pc = pc.get(model)
        leak, name = _leak_flags(sc, it, stripped_seen=bool(it and it.get("_src_sha256") in run_shas))
        cl = _lookup("cluster_id", sc, it)
        if cl is None:
            cl = _lookup("template_cluster", sc, it)
        meta = (it or {}).get("meta") or {}
        rec = {
            "id": rid, "suite": suite, "model": model,
            "scorer": (sc or {}).get("scorer") or r.get("scorer") or (it or {}).get("scorer"), "status": status,
            "passed": bool((sc or {}).get("passed")) if sc is not None else None,
            "value": float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None,
            "parsed": (sc or {}).get("parsed"), "expected": ex.get("expected"), "predicted": ex.get("predicted"),
            "tier": ex.get("refusal_tier"), "tier_prefix": ex.get("refusal_tier_prefix"),
            "truncated": trunc, "followup": follow,
            "opener": ex.get("opener_refusal"),
            "label_source": (sc or {}).get("label_source") or meta.get("label_source"),
            "cluster": cl,
            "flags": {"has_signal": _lookup("has_signal", sc, it), "leak": leak, "post_cutoff": pc,
                      "name_in_text": name, "dev_cluster_exposed": _lookup("dev_cluster_exposed", sc, it),
                      "dev_neardup_exposed": _lookup("dev_neardup_exposed", sc, it),
                      "frr_exclude": _lookup("frr_exclude", sc, it), "key_longest": _key_longest(it),
                      "late_cve_id": _lookup("late_cve_id", sc, it),
                      "other_cwe_name_in_text": _lookup("other_cwe_name_in_text", sc, it)},
            "related": ex.get("cwe_related_match"),
            "type": r.get("type") or (it or {}).get("type"),
            "cna": _lookup("cna_cluster", sc, it) or ((meta.get("labels") or {}).get("cna")
                                                      if isinstance(meta.get("labels"), dict) else None),
            "at_ceiling": bool(_lookup("suite_at_ceiling", sc, it)) or
            str(_lookup("suite_status", sc, it) or "") in ("at_ceiling", "at_ceiling_among_sufficient"),
            "cats": {"frr_tier": _lookup("frr_tier", sc, it), "label_role": _label_role(sc, it),
                     "cwe_mapping_usage": _lookup("cwe_mapping_usage", sc, it)},
            "labels": meta.get("labels") if isinstance(meta.get("labels"), dict) else None,
            "gold": (it or {}).get("answer"),
            "legacy": _legacy(sc, legacy.get(rid)) if sc is not None else None,
        }
        out[rid] = rec
    return out


# ------------------------------------------------------------------------------------------- analysis variants
# Each op changes how items count. A variant is a tuple of ops; the primary analysis of a comparison is the tuple
# its plan declares, and each sensitivity analysis toggles one op.
OPS = {
    # key: (label, kind)
    "errors_excluded": ("errors excluded", "policy"),
    "followup_fail": ("follow-up → fail", "fail"),
    "drop_followup": ("excl. follow-up items", "drop"),
    "truncated_fail": ("truncated → fail", "fail"),
    "drop_truncated": ("excl. truncated items", "drop"),
    "has_signal": ("excl. no-signal items", "drop"),
    "no_leak": ("excl. key-leak items", "drop"),
    "no_name_in_text": ("excl. CWE-name-in-text", "drop"),
    "name_in_text": ("CWE-name-in-text only", "keep"),
    "post_cutoff": ("post-cutoff only (both models)", "keep"),
    "no_dev_exposed": ("excl. dev-cluster-exposed", "drop"),
    "no_dev_neardup": ("excl. dev-near-duplicate-exposed", "drop"),
    "no_frr_exclude": ("excl. CSE covert-harmful prompts", "drop"),
    "no_key_longest": ("excl. key-is-longest (MCQ)", "drop"),
    "letter_balanced": ("letter-balanced (MCQ)", "estimator"),
    "impute_none": ("MAE: unparsed left out", "impute"),
    "impute_max": ("MAE: unparsed = 10", "impute"),
    "trunc_best": ("truncated = full compliance", "tier"),
    "trunc_worst": ("truncated = refusal", "tier"),
    "extract_only": ("extract-type items only (judge calibration scope)", "drop"),
    "cluster_bootstrap": ("cluster bootstrap (template clusters)", "inference"),
    "cna_cluster": ("cluster bootstrap (CNA clusters)", "inference"),
    "legacy": ("legacy scorer", "legacy"),
}
KEEP_ONLY = {"post_cutoff", "name_in_text"}       # strata: reported with an interaction, never verdict flips
OP_FLAG = {"has_signal": "has_signal", "no_leak": "leak", "no_name_in_text": "name_in_text",
           "name_in_text": "name_in_text", "post_cutoff": "post_cutoff", "no_dev_exposed": "dev_cluster_exposed",
           "no_dev_neardup": "dev_neardup_exposed", "no_frr_exclude": "frr_exclude", "no_key_longest": "key_longest"}
VARIANTS = [("primary", "primary", None)] + [(k, v[0], v[1]) for k, v in OPS.items()]   # back-compat listing
VARIANT_LABEL = {"primary": "primary", **{k: v[0] for k, v in OPS.items()}}
RUN_TABLE_BASE = "all items (no exclusions)"


def as_ops(variant: Union[str, Sequence[str], None]) -> Tuple[str, ...]:
    if variant in (None, "", "primary"):
        return ()
    if isinstance(variant, str):
        return (variant,)
    return tuple(sorted(set(variant)))


def ops_label(ops: Sequence[str]) -> str:
    return " + ".join(VARIANT_LABEL.get(o, o) for o in ops) if ops else "all items"


def is_error_metric(rec: Dict[str, Any]) -> bool:
    return rec.get("scorer") in ERROR_METRICS


def _gold_cvss(ans: Any) -> Optional[float]:
    if isinstance(ans, dict):
        ans = ans.get("vector", ans.get("score"))
    if isinstance(ans, (int, float)) and not isinstance(ans, bool):
        return float(ans)
    s = str(ans)
    if "AV:" in s.upper():
        try:
            import scorers  # read-only use of the scoring formula
            return scorers.cvss31_base(s)
        except Exception:
            return None
    try:
        return float(s)
    except ValueError:
        return None


def _rec_gold_cvss(rec: Dict[str, Any]) -> Optional[float]:
    if isinstance(rec.get("expected"), (int, float)):
        return float(rec["expected"])
    return _gold_cvss(rec.get("gold")) if rec.get("gold") is not None else None


def _label_list(rec: Dict[str, Any], key: str) -> List[str]:
    v = (rec.get("labels") or {}).get(key)
    if v is None:
        return []
    return [str(x) for x in (v if isinstance(v, list) else [v]) if x]


def _norm_cwe(s: Any) -> Optional[str]:
    m = re.search(r"CWE[\s_-]*0*(\d{1,5})", str(s), re.I)
    return f"CWE-{int(m.group(1))}" if m else None


def _impute_mae(rec: Dict[str, Any], impute: str, constant: float, golds: Optional[List[float]] = None) -> Optional[float]:
    if impute == "none":
        return None
    if impute == "max":
        return MAE_FAIL
    gs = golds if golds else ([g] if (g := _rec_gold_cvss(rec)) is not None else [])
    return min(abs(g - constant) for g in gs) if gs else None


def outcome(rec: Dict[str, Any], metric: str = "accuracy", policy: str = "fail",
            variant: Union[str, Sequence[str], None] = "primary", impute: str = "max",
            constant: float = CVSS_PRIOR) -> Optional[float]:
    """The item's number for a metric under an error policy, a variant (tuple of ops) and, for MAE, an imputation
    rule for items with no parsed prediction; None = not counted. accuracy_onehop: passed or a one-hop (MITRE
    ChildOf) match (extra.cwe_related_match).

    accuracy: 1/0; mae: absolute error; mean_score: partial credit; opener_refusal: 1/0; refusal_full / partial /
    refuse: 1 if the reply is in that tier (truncated replies tiered from their visible prefix); accuracy_either /
    mae_either: against the closest of the item's gold and every NVD, CNA and ADP label (any item; on a single-label
    item this is the strict metric), so "matches either" is never below the strict figure."""
    ops = as_ops(variant)
    status = rec["status"]
    if "errors_excluded" in ops:
        policy = "exclude"
    if "impute_none" in ops:
        impute = "none"
    elif "impute_max" in ops:
        impute = "max"
    if metric in ("accuracy_either", "mae_either"):
        if "legacy" in ops and rec.get("legacy") is not None:   # the legacy scorer had no either-label rule
            return outcome(rec, "accuracy" if metric == "accuracy_either" else "mae", policy, ops, impute, constant)
        return _either(rec, metric, policy, ops, impute, constant)
    if metric.startswith("refusal_"):
        want = metric[len("refusal_"):]
        if status == "unscored":
            return None
        if status in ("api_error", "error"):
            got = "refuse" if policy == "fail" else None        # no answer = no compliance
        elif rec["truncated"] or rec.get("tier") in (None, "unparsed_truncated"):
            got = rec.get("tier_prefix") if rec.get("tier_prefix") not in (None, "unparsed_truncated") else None
            if "trunc_best" in ops:
                got = "full"
            elif "trunc_worst" in ops:
                got = "refuse"
        else:
            got = rec["tier"]
        return None if got is None else (1.0 if got == want else 0.0)
    if metric == "mae":
        fail_v = _impute_mae(rec, impute, constant)
    else:
        fail_v = {"accuracy": 0.0, "mean_score": 0.0}.get(metric)
    if "legacy" in ops:
        lg = rec.get("legacy")
        if lg is not None:
            if lg.get("status") == "error":
                return fail_v if policy == "fail" else None
            if metric == "accuracy":
                return 1.0 if lg.get("passed") else 0.0
            v = lg.get("value")
            return float(v) if isinstance(v, (int, float)) else None
        # never rescored: its only score is the legacy scorer's
    if status == "unscored":
        return None
    if metric == "opener_refusal":
        if status in ("api_error", "error") or rec.get("opener") is None:
            return None
        return 1.0 if rec["opener"] else 0.0
    if status in ("error", "api_error"):
        return fail_v if policy == "fail" else None
    if ("followup_fail" in ops and rec["followup"]) or ("truncated_fail" in ops and rec["truncated"]):
        return fail_v
    if metric == "accuracy":
        return 1.0 if rec["passed"] else 0.0
    if metric == "accuracy_onehop":
        return 1.0 if (rec["passed"] or rec.get("related") is True) else 0.0
    if metric == "mae":
        if status in ("unparsed", "unparsed_truncated"):
            return fail_v
        return rec["value"]
    if metric == "mean_score":
        return rec["value"]
    raise ValueError(f"unknown metric {metric!r}")


def item_labels(rec: Dict[str, Any]) -> Dict[str, Dict[str, List[str]]]:
    """{'cwe'|'cvss': {role: [labels]}} from meta.labels: the cwe_/cvss_ lists and the per-source
    weaknesses / metrics entries (role nvd, cna, adp); plus the item's gold under role 'gold'."""
    lab = rec.get("labels") or {}
    out: Dict[str, Dict[str, List[str]]] = {"cwe": {}, "cvss": {}}
    for role in ("nvd", "cna", "adp"):
        for kind in ("cwe", "cvss"):
            for x in lab.get(f"{kind}_{role}") or []:
                out[kind].setdefault(role, []).append(str(x))
    for w in lab.get("weaknesses") or []:
        for x in (w or {}).get("cwes") or []:
            out["cwe"].setdefault(str((w or {}).get("role") or "other"), []).append(str(x))
    for m in lab.get("metrics") or []:
        v = (m or {}).get("vector")
        if v and str((m or {}).get("version", "3")).startswith("3"):
            out["cvss"].setdefault(str((m or {}).get("role") or "other"), []).append(str(v))
    g = rec.get("gold")
    if isinstance(g, dict):
        g = g.get("cwe", g.get("vector", g.get("score")))
    if g is not None:
        kind = "cwe" if rec.get("scorer") == "cwe_match" else "cvss"
        for x in (g if isinstance(g, list) else [g]):
            out[kind].setdefault("gold", []).append(str(x))
    return out


def _either(rec: Dict[str, Any], metric: str, policy: str, ops: Tuple[str, ...], impute: str,
            constant: float) -> Optional[float]:
    """Against ANY available label (gold, NVD, CNA, ADP). On a single-label item this is the strict metric."""
    status = rec["status"]
    if status == "unscored":
        return None
    labs = item_labels(rec)
    if rec.get("scorer") == "cwe_match":
        if metric != "accuracy_either":
            return None
        allc = {_norm_cwe(x) for v in labs["cwe"].values() for x in v} - {None}
        if not allc:
            return None
        if status in ("error", "api_error"):
            return 0.0 if policy == "fail" else None
        if ("followup_fail" in ops and rec["followup"]) or ("truncated_fail" in ops and rec["truncated"]):
            return 0.0
        got = _norm_cwe(rec.get("parsed")) if status == "ok" else None
        return 1.0 if got and got in allc else 0.0
    if rec.get("scorer") == "cvss_mae":
        golds = sorted({g for g in (_gold_cvss(x) for v in labs["cvss"].values() for x in v) if g is not None})
        if not golds:
            return None
        if status in ("error", "api_error") and policy != "fail":
            return None
        failed = status in ("error", "api_error", "unparsed", "unparsed_truncated") or \
            ("followup_fail" in ops and rec["followup"]) or ("truncated_fail" in ops and rec["truncated"]) or \
            not isinstance(rec.get("predicted"), (int, float))
        if failed:
            if metric == "accuracy_either":
                return 0.0
            return _impute_mae(rec, impute, constant, golds)
        err = round(min(abs(float(rec["predicted"]) - g) for g in golds), 4)   # the scorer's rounding
        return (1.0 if err <= 1.0 else 0.0) if metric == "accuracy_either" else err
    return None


def item_excluded(recs: Sequence[Dict[str, Any]], variant: Union[str, Sequence[str], None]) -> bool:
    """Whether a variant drops this item (any run's record of it matches)."""
    ops = as_ops(variant)
    fl = lambda name: [r["flags"].get(name) for r in recs]  # noqa: E731
    if "drop_followup" in ops and any(r["followup"] for r in recs):
        return True
    if "drop_truncated" in ops and any(r["truncated"] for r in recs):
        return True
    if "has_signal" in ops and any(v is False for v in fl("has_signal")):
        return True
    if "no_leak" in ops and any(v is True for v in fl("leak")):
        return True
    if "no_name_in_text" in ops and any(v is True for v in fl("name_in_text")):
        return True
    if "name_in_text" in ops and not any(v is True for v in fl("name_in_text")):
        return True
    if "post_cutoff" in ops and not all(v is True for v in fl("post_cutoff")):
        return True        # unknown for any compared model = not known to be post-cutoff
    if "no_dev_exposed" in ops and any(v is True for v in fl("dev_cluster_exposed")):
        return True
    if "no_dev_neardup" in ops and any(v is True for v in fl("dev_neardup_exposed")):
        return True
    if "no_frr_exclude" in ops and any(v is True for v in fl("frr_exclude")):
        return True
    if "not_post_cutoff" in ops and all(v is True for v in fl("post_cutoff")):
        return True        # internal complement of post_cutoff (for the stratum interaction)
    if "no_key_longest" in ops and any(v is True for v in fl("key_longest")):
        return True
    if "extract_only" in ops and any(r.get("type") != "extract" for r in recs):
        return True        # judge calibration licensed the extract half only (verdict d1_scope extract_only)
    return False


def variant_applies(recs: Iterable[Dict[str, Any]], variant: Union[str, Sequence[str], None]) -> bool:
    """Whether a variant can touch any of these records (a cheap pre-filter; the report then checks whether it
    actually changed a result before showing it)."""
    rs = list(recs)
    ops = as_ops(variant)
    if not ops:
        return True
    for op in ops:
        if op == "errors_excluded" and any(r["status"] in ("error", "api_error") for r in rs):
            return True
        if op in ("followup_fail", "drop_followup") and any(r["followup"] for r in rs):
            return True
        if op in ("truncated_fail", "drop_truncated") and any(r["truncated"] for r in rs):
            return True
        if op == "has_signal" and any(r["flags"].get("has_signal") is False for r in rs):
            return True
        if op == "no_leak" and any(r["flags"].get("leak") for r in rs):
            return True
        if op in ("no_name_in_text", "name_in_text") and any(r["flags"].get("name_in_text") is not None for r in rs):
            return True
        if op == "post_cutoff" and any(r["flags"].get("post_cutoff") is not None for r in rs):
            return True
        if op == "no_dev_exposed" and any(r["flags"].get("dev_cluster_exposed") for r in rs):
            return True
        if op == "no_dev_neardup" and any(r["flags"].get("dev_neardup_exposed") for r in rs):
            return True
        if op == "no_frr_exclude" and any(r["flags"].get("frr_exclude") for r in rs):
            return True
        if op == "legacy" and any(r.get("legacy") is not None for r in rs):
            return True
        if op == "no_key_longest" and any(r["flags"].get("key_longest") for r in rs):
            return True
        if op == "letter_balanced" and any(r.get("scorer") == "mcq_letter" and r.get("gold") is not None for r in rs):
            return True
        if op in ("impute_none", "impute_max") and any(is_error_metric(r) and r["status"] in
                                                       ("unparsed", "unparsed_truncated", "error", "api_error")
                                                       for r in rs):
            return True
        if op in ("trunc_best", "trunc_worst") and any(r.get("tier") is not None and r["truncated"] for r in rs):
            return True
        if op == "cluster_bootstrap" and any(r.get("cluster") is not None for r in rs):
            return True
        if op == "cna_cluster" and any(r.get("cna") for r in rs):
            return True
    return False


# ------------------------------------------------------------------------------------------- per-run summary
def summarize(run_dir: Path, split_label_source: bool = False,
              labels: Optional[Dict[str, str]] = None, errors: str = "fail", impute: str = "max",
              constant: float = CVSS_PRIOR, items: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Per-suite statistics for one run dir (responses.jsonl + scores.jsonl + run.json)."""
    run_dir = Path(run_dir)
    run = _read_run(run_dir)
    resp = latest_by_id(read_jsonl(run_dir / "responses.jsonl"))
    scores = latest_by_id(read_jsonl(run_dir / "scores.jsonl"))
    suites: Dict[str, Dict[str, Any]] = {}
    for rid, r in resp.items():
        if "suite" not in r:
            continue
        for key in suite_keys(r["suite"], rid, scores.get(rid), split_label_source, labels):
            _add(suites, key, rid, r, scores.get(rid), impute, constant, items)
    for name, s in suites.items():
        k = s["passed"]
        n_excl = s["n_scored"]
        n_fail = n_excl + s["item_or_grader_errors"] + s["api_errors"]
        n = n_fail if errors == "fail" else n_excl
        n_alt = n_excl if errors == "fail" else n_fail
        lo, hi = wilson(k, n)
        s["error_policy"] = errors
        s["n_denominator"] = n
        s["accuracy"] = (k / n) if n else None
        s["ci95"] = [round(lo, 4), round(hi, 4)]
        s["n_alt"] = n_alt
        s["accuracy_alt"] = (k / n_alt) if n_alt else None
        s["accuracy_excl_errors"] = (k / n_excl) if n_excl else None
        n_score = n_excl - s["n_err_metric"]
        s["mean_value"] = (s["value_sum"] / n_score) if n_score else None
        s["mae"] = (s["err_sum"] / s["n_mae"]) if s["n_mae"] else None
        s["mae_impute"] = impute
        s["mean_latency_s"] = (s["latency_sum"] / s["n_latency"]) if s["n_latency"] else None
        s["tokens_per_s"] = (s["completion_tokens"] / s["gen_seconds"]) if s["gen_seconds"] else None
        s["cost_usd"] = round(s["cost_usd"], 4)
        if s["n_opener"]:
            s["opener_refusal_rate"] = s["opener_refusals"] / s["n_opener"]
            s["opener_refusal_ci95"] = [round(x, 4) for x in wilson(s["opener_refusals"], s["n_opener"])]
        else:
            for k2 in ("n_opener", "opener_refusals"):
                s.pop(k2)
        if not s["tiers"]:
            s.pop("tiers")
        if not s["declines"]:
            s.pop("declines")
        for k2 in ("value_sum", "latency_sum", "n_latency", "err_sum", "n_err_metric"):
            s.pop(k2)
        s["gen_seconds"] = round(s["gen_seconds"], 2)
    return {"run_dir": run_dir.name, "path": str(run_dir), "label": run_label(run), "run": run, "suites": suites}


def _add(suites: Dict[str, Dict[str, Any]], key: str, rid: str, r: Dict[str, Any],
         sc: Optional[Dict[str, Any]], impute: str = "max", constant: float = CVSS_PRIOR,
         items: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
    """Count one response (and its score) into the row ``key``."""
    s = suites.setdefault(key, {
        "n_items": 0, "n_scored": 0, "passed": 0, "value_sum": 0.0, "api_errors": 0, "truncated": 0,
        "unparsed": 0, "unparsed_truncated": 0, "item_or_grader_errors": 0, "final_answer_prompts": 0,
        "latency_sum": 0.0, "n_latency": 0, "n_ok": 0, "n_unscored": 0,
        "completion_tokens": 0, "prompt_tokens": 0, "gen_seconds": 0.0, "scorers": {}, "model_graded": False,
        "cost_usd": 0.0, "n_err_metric": 0, "n_mae": 0, "n_mae_unimputed": 0, "err_sum": 0.0, "n_opener": 0,
        "opener_refusals": 0, "tiers": {}, "declines": {},
    })
    s["n_items"] += 1
    if r.get("error"):
        s["api_errors"] += 1
        return
    if r.get("finish_reason") == "length":
        s["truncated"] += 1
    lat = r.get("latency_s")
    if isinstance(lat, (int, float)):
        s["latency_sum"] += lat
        s["n_latency"] += 1
        s["gen_seconds"] += lat
    u = r.get("usage") or {}
    s["completion_tokens"] += int(u.get("completion_tokens") or 0)
    s["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
    s["cost_usd"] += float(r.get("cost_usd") or 0.0)
    if sc is None:
        s["n_unscored"] += 1
        return
    s["scorers"][sc.get("scorer")] = s["scorers"].get(sc.get("scorer"), 0) + 1
    if sc.get("model_graded"):
        s["model_graded"] = True
    st = sc.get("status", "ok")
    if st == "error":
        s["item_or_grader_errors"] += 1
        return
    unparsed = st in ("unparsed", "unparsed_truncated")
    if unparsed:
        s["unparsed"] += 1
        if st == "unparsed_truncated":
            s["unparsed_truncated"] += 1
    else:
        s["n_ok"] += 1
    if sc.get("final_answer_prompt") or (sc.get("extra") or {}).get("followup_used") or r.get("final_answer_prompt"):
        s["final_answer_prompts"] += 1
    s["n_scored"] += 1
    s["passed"] += 1 if sc.get("passed") else 0
    ex = sc.get("extra") or {}
    if sc.get("scorer") in ERROR_METRICS:  # an error (lower is better), not a score: kept apart
        s["n_err_metric"] += 1
        if unparsed or not isinstance(sc.get("value"), (int, float)):
            rec = {"expected": ex.get("expected"), "gold": ((items or {}).get(rid) or {}).get("answer")}
            v = _impute_mae(rec, impute, constant)
        else:
            v = float(sc["value"])
        if v is None:
            s["n_mae_unimputed"] += 1
        else:
            s["n_mae"] += 1
            s["err_sum"] += v
    else:
        s["value_sum"] += float(sc.get("value") or 0.0)
    opener = ex.get("opener_refusal")
    if opener is not None:
        s["n_opener"] += 1
        s["opener_refusals"] += 1 if opener else 0
    dk = ex.get("decline_kind")
    if dk:
        s["declines"][dk] = s["declines"].get(dk, 0) + 1
    tier = ex.get("refusal_tier")
    if tier is not None and tier != "unparsed_truncated" and r.get("finish_reason") != "length" \
            and st != "unparsed_truncated":
        s["tiers"][tier] = s["tiers"].get(tier, 0) + 1


# ------------------------------------------------------------------------------------------- paired comparisons
def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value: b and c are the discordant counts (only A passed, only B passed)."""
    return ST.mcnemar_exact(b, c)


def item_passes(run_dir: Path, split_label_source: bool = False,
                labels: Optional[Dict[str, str]] = None, errors: str = "fail") -> Dict[str, Dict[str, bool]]:
    """row key -> {item id: passed} for items with a usable score. Errors count as a fail (``errors='fail'``) or
    are left out (``errors='exclude'``, the pre-2026-10 behaviour)."""
    out: Dict[str, Dict[str, bool]] = {}
    for rid, rec in item_records(Path(run_dir)).items():
        v = outcome(rec, "accuracy", errors)
        if v is None:
            continue
        sc = {"label_source": rec["label_source"]}
        for key in suite_keys(rec["suite"], rid, sc, split_label_source, labels):
            out.setdefault(key, {})[rid] = bool(v)
    return out


def _row_records(recs: Dict[str, Dict[str, Any]], key: str,
                 labels: Optional[Dict[str, str]]) -> Dict[str, Dict[str, Any]]:
    """The records that belong to a table row key (a suite, or a suite's label_source split)."""
    m = re.fullmatch(r"(.+) \[label_source=(.+)\]", key)
    if not m:
        return {k: r for k, r in recs.items() if r["suite"] == key}
    suite, lab = m.groups()
    return {k: r for k, r in recs.items()
            if r["suite"] == suite and (r["label_source"] or (labels or {}).get(k)) == lab}


def paired_binary(a: Sequence[float], b: Sequence[float], alpha: float = 0.05,
                  margin: Optional[float] = None, alpha_power: Optional[float] = None,
                  power: bool = True) -> Dict[str, Any]:
    """McNemar, Newcombe CI, exact-power MDE and (with a margin) the size-correct TOST (Tango score statistic,
    exact unconditional p; ``stats.tost_paired_exact``) and its exact power at a true difference of 0 (at
    ``alpha_power``, e.g. the Holm worst-case alpha). The Newcombe 90% interval is shown for description only."""
    n11 = sum(1 for x, y in zip(a, b) if x and y)
    n10 = sum(1 for x, y in zip(a, b) if x and not y)
    n01 = sum(1 for x, y in zip(a, b) if y and not x)
    n00 = len(a) - n11 - n10 - n01
    n = len(a)
    d, lo, hi = ST.newcombe_paired_ci(n11, n10, n01, n00, 1 - alpha)
    psi = (n10 + n01) / n if n else 0.0
    out = {"method": "McNemar exact; Newcombe CI", "n": n, "a_mean": (n11 + n10) / n if n else None,
           "b_mean": (n11 + n01) / n if n else None, "a_passed": n11 + n10, "b_passed": n11 + n01,
           "a_only": n10, "b_only": n01, "diff": d if n else None, "lo": lo, "hi": hi,
           "p": ST.mcnemar_exact(n10, n01),
           "mde": ST.mde_mcnemar_exact(n, psi, alpha_power or alpha) if n else None}
    if margin is not None and n:
        out["p_tost"] = ST.tost_paired_exact(n11, n10, n01, n00, margin)["p"]
        _, lo90, hi90 = ST.newcombe_paired_ci(n11, n10, n01, n00, 1 - 2 * alpha)
        out["lo90"], out["hi90"] = lo90, hi90
        out["tost_power"] = ST.power_tost_paired_exact(n, max(psi, 1.0 / n), margin, 0.0, alpha_power or alpha) \
            if power else None
    return out


def paired_continuous(a: Sequence[float], b: Sequence[float], alpha: float = 0.05, margin: Optional[float] = None,
                      clusters: Optional[Sequence[str]] = None, n_boot: int = ST.DEFAULT_BOOT,
                      n_perm: Optional[int] = ST.DEFAULT_PERM, seed: int = ST.DEFAULT_SEED,
                      alpha_power: Optional[float] = None, strata: Optional[Sequence[str]] = None,
                      power: bool = True, decide_clusters: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Paired mean difference (MAE, partial credit, per-item mean pass over seeds).

    Decision rules (stated in the report):
    - directional: the sign-flip permutation p (``p``; exact for few non-zero differences, else n_perm draws);
      the percentile bootstrap CI is for display only. Without n_perm (exploratory tables) the bootstrap test
      whose decision equals the CI's is used and labelled.
    - equivalence: the paired t-TOST (``stats.ttost``), with the matching 1-2 alpha t interval.
    - with ``strata`` (letter-balanced) or ``decide_clusters`` (cluster-bootstrap sensitivity), the stratified or
      cluster bootstrap decides both (test and TOST), with its Monte Carlo SE."""
    diffs = [x - y for x, y in zip(a, b)]
    n = len(diffs)
    if strata is not None:
        reps = ST.bootstrap_stratified_means(diffs, strata, n_boot, seed)
        est = ST.stratified_mean(diffs, strata)
        am, bm = ST.stratified_mean(list(a), strata), ST.stratified_mean(list(b), strata)
    else:
        reps = ST.bootstrap_means(diffs, list(decide_clusters) if decide_clusters is not None else None, n_boot, seed)
        est, am, bm = ST.mean(diffs), ST.mean(list(a)), ST.mean(list(b))
    sdd = ST.sd(diffs) or 0.0
    lo, hi, p_boot = ST.boot_ci_and_p(reps, alpha) if reps else (None, None, None)
    boot_decides = strata is not None or decide_clusters is not None or not n_perm
    p_perm = ST.sign_flip_p(diffs, n_perm, seed) if (n_perm and n) else None
    out: Dict[str, Any] = {"n": n, "a_mean": am, "b_mean": bm, "diff": est, "lo": lo, "hi": hi,
                           "p": (p_boot if boot_decides else p_perm) if n else None,
                           "p_boot": p_boot, "p_perm": p_perm, "n_boot": len(reps),
                           "p_is_boot": boot_decides,
                           "mde": ST.mde_paired_mean(sdd, n, alpha_power or alpha) if n > 1 else None}
    if boot_decides:
        out["method"] = ("stratified (letter-balanced) " if strata is not None else
                         "cluster " if decide_clusters is not None else "") + "bootstrap test and CI (same decision)"
        out["p_mcse"] = ST.boot_p_mcse(p_boot, len(reps))
        if decide_clusters is not None:
            out["n_clusters"] = len(set(decide_clusters))
    else:
        out["method"] = "sign-flip permutation test; percentile bootstrap CI (display)"
    if margin is not None and n:
        if boot_decides:
            out["p_tost"] = ST.tost_bootstrap(reps, margin)["p"]
            out["p_tost_mcse"] = ST.boot_p_mcse(out["p_tost"], len(reps))
            lo90, hi90, _ = ST.boot_ci_and_p(reps, 2 * alpha)
        else:
            out["p_tost"] = ST.ttost(diffs, margin)["p"]
            lo90, hi90 = ST.t_ci(diffs, 1 - 2 * alpha)
        out["lo90"], out["hi90"] = lo90, hi90
        out["tost_power"] = ST.power_tost_mean(n, sdd, margin, 0.0, alpha_power or alpha) if power else None
    if clusters is not None and n and len(set(clusters)) < n and decide_clusters is None:
        _, clo, chi = ST.bootstrap_mean_ci(diffs, 1 - alpha, clusters, n_boot, seed)
        out["cluster_lo"], out["cluster_hi"], out["n_clusters"] = clo, chi, len(set(clusters))
    return out


def compare(a_runs: Sequence[Dict[str, Dict[str, Any]]], b_runs: Sequence[Dict[str, Dict[str, Any]]],
            metric: str = "accuracy", variant: Union[str, Sequence[str], None] = "primary", errors: str = "fail",
            alpha: float = 0.05, margin: Optional[float] = None, n_boot: int = ST.DEFAULT_BOOT,
            n_perm: Optional[int] = ST.DEFAULT_PERM, seed: int = ST.DEFAULT_SEED, cluster: bool = True,
            impute: str = "max", constant: float = CVSS_PRIOR, alpha_power: Optional[float] = None,
            power: bool = True) -> Dict[str, Any]:
    """Paired comparison of A and B on the items every run of both scored. A and B are lists of record dicts (one
    per seed, already restricted to the suite/row); with several seeds the item's value is its mean over seeds."""
    if not a_runs or not b_runs:
        return {"n": 0}
    ops = as_ops(variant)
    shared = set(a_runs[0])
    for r in list(a_runs[1:]) + list(b_runs):
        shared &= set(r)
    av: List[float] = []
    bv: List[float] = []
    cl: List[str] = []
    cna: List[str] = []
    letters: List[str] = []
    n_unimputed = 0
    for rid in sorted(shared):
        recs = [r[rid] for r in list(a_runs) + list(b_runs)]
        if item_excluded(recs, ops):
            continue
        xa = [outcome(r[rid], metric, errors, ops, impute, constant) for r in a_runs]
        xb = [outcome(r[rid], metric, errors, ops, impute, constant) for r in b_runs]
        if any(x is None for x in xa + xb):
            if metric in ("mae", "mae_either") and impute == "none":
                n_unimputed += 1
            continue
        av.append(sum(xa) / len(xa))
        bv.append(sum(xb) / len(xb))
        c = next((r["cluster"] for r in recs if r.get("cluster") is not None), None)
        cl.append(str(c) if c is not None else f"item:{rid}")
        cn = next((r.get("cna") for r in recs if r.get("cna")), None)
        cna.append(str(cn) if cn else f"item:{rid}")
        letters.append(str(recs[0].get("gold") or "?").strip().upper()[:1])
    balanced = "letter_balanced" in ops
    decide = cl if "cluster_bootstrap" in ops else cna if "cna_cluster" in ops else None
    binary = metric in BINARY_METRICS and len(a_runs) == 1 and len(b_runs) == 1 and not balanced and decide is None
    if binary:
        out = paired_binary(av, bv, alpha, margin, alpha_power, power)
        if cluster and av and len(set(cl)) < len(cl):
            diffs = [x - y for x, y in zip(av, bv)]
            _, clo, chi = ST.bootstrap_mean_ci(diffs, 1 - alpha, cl, n_boot, seed)
            out["cluster_lo"], out["cluster_hi"], out["n_clusters"] = clo, chi, len(set(cl))
    else:
        out = paired_continuous(av, bv, alpha, margin, cl if cluster else None, n_boot, n_perm, seed, alpha_power,
                                letters if balanced else None, power, decide)
        if margin is not None and metric in BINARY_METRICS and not balanced and decide is None and \
                (len(a_runs) > 1 or len(b_runs) > 1):
            # Seed groups on a proportion: the t-TOST on item means is liberal near the boundary (seed-noise sd .2:
            # 0.037 at nominal .0167). Size-correct instead: the exact Tango TOST of every seed pairing, equivalence
            # only when all of them show it (p = max). Each seed has the estimand's difference (seeds are
            # exchangeable), so under H0 every test has size <= alpha and so does their intersection.
            ps = []
            for ra in a_runs:
                for rb in b_runs:
                    xa, xb = [], []
                    for rid in sorted(shared):
                        recs = [r[rid] for r in list(a_runs) + list(b_runs)]
                        if item_excluded(recs, ops):
                            continue
                        u = outcome(ra[rid], metric, errors, ops, impute, constant)
                        w = outcome(rb[rid], metric, errors, ops, impute, constant)
                        if u is not None and w is not None:
                            xa.append(u)
                            xb.append(w)
                    n11 = sum(1 for u, w in zip(xa, xb) if u and w)
                    n10 = sum(1 for u, w in zip(xa, xb) if u and not w)
                    n01 = sum(1 for u, w in zip(xa, xb) if w and not u)
                    ps.append(ST.tost_paired_exact(n11, n10, n01, len(xa) - n11 - n10 - n01, margin)["p"])
            out["p_tost"] = max(ps) if ps else 1.0
            out["p_tost_seed_pairs"] = ps
            out["method"] += "; equivalence: exact Tango TOST per seed pairing, all must pass (p = max)"
    out["n_seeds_a"], out["n_seeds_b"] = len(a_runs), len(b_runs)
    out["n_unimputed"] = n_unimputed
    return out


def paired(summaries: List[Dict[str, Any]], passes: List[Dict[str, Dict[str, bool]]]) -> List[Dict[str, Any]]:
    """Per row key and pair of runs: shared items, wins each way, the exact McNemar p and the Newcombe CI of the
    accuracy difference (from ``item_passes`` dicts)."""
    out = []
    for suite in sorted({n for s in summaries for n in s["suites"]}):
        for i in range(len(summaries)):
            for j in range(i + 1, len(summaries)):
                a, b = passes[i].get(suite) or {}, passes[j].get(suite) or {}
                shared = sorted(set(a) & set(b))
                if not shared:
                    continue
                r = paired_binary([float(a[k]) for k in shared], [float(b[k]) for k in shared])
                out.append(_pair_row(suite, summaries[i]["label"], summaries[j]["label"], r))
    return out


def _pair_row(suite: str, la: str, lb: str, r: Dict[str, Any]) -> Dict[str, Any]:
    return {"suite": suite, "run_a": la, "run_b": lb, "n_shared": r["n"], "a_passed": r["a_passed"],
            "b_passed": r["b_passed"], "a_only": r["a_only"], "b_only": r["b_only"], "p_mcnemar": r["p"],
            "diff": r["diff"], "diff_lo": r["lo"], "diff_hi": r["hi"], "mde80": r["mde"]}


def paired_records(summaries: List[Dict[str, Any]], recs: List[Dict[str, Dict[str, Any]]],
                   labels: Optional[Dict[str, str]] = None, errors: str = "fail", cluster: bool = False,
                   n_boot: int = 2000, seed: int = ST.DEFAULT_SEED, impute: str = "max",
                   constant: float = CVSS_PRIOR) -> List[Dict[str, Any]]:
    """The exploratory pairs table from item records: accuracy (McNemar + Newcombe) for every row, plus the mean
    difference (MAE or partial credit) with a bootstrap CI where the scores are not plain 0/1."""
    out = []
    for key in sorted({n for s in summaries for n in s["suites"]}):
        rows_ = [_row_records(r, key, labels) for r in recs]
        for i in range(len(summaries)):
            for j in range(i + 1, len(summaries)):
                a, b = rows_[i], rows_[j]
                if not (set(a) & set(b)):
                    continue
                r = compare([a], [b], "accuracy", "primary", errors, cluster=cluster, n_boot=n_boot, seed=seed)
                if not r["n"]:
                    continue
                row = _pair_row(key, summaries[i]["label"], summaries[j]["label"], r)
                if cluster and "cluster_lo" in r:
                    row["cluster_lo"], row["cluster_hi"] = r["cluster_lo"], r["cluster_hi"]
                cm = _continuous_metric(list(a.values()) + list(b.values()))
                if cm:
                    rc = compare([a], [b], cm, "primary", errors, n_boot=n_boot, n_perm=None, seed=seed,
                                 cluster=cluster, impute=impute, constant=constant)
                    row.update({"metric2": cm, "diff2": rc.get("diff"), "diff2_lo": rc.get("lo"),
                                "diff2_hi": rc.get("hi")})
                out.append(row)
    return out


def _continuous_metric(recs: Iterable[Dict[str, Any]]) -> Optional[str]:
    """'mae' for cvss_mae rows, 'mean_score' where values are partial credit, else None."""
    rs = list(recs)
    if any(is_error_metric(r) for r in rs):
        return "mae"
    if any(r["value"] is not None and r["value"] not in (0.0, 1.0) for r in rs):
        return "mean_score"
    return None


# ------------------------------------------------------------------------------------------- seeds
def seed_of(run: Dict[str, Any]) -> Optional[Any]:
    return (run.get("sampling") or {}).get("seed")


def sampled(run: Dict[str, Any]) -> bool:
    t = (run.get("sampling") or {}).get("temperature")
    return isinstance(t, (int, float)) and t > 0


def config_key(run: Dict[str, Any]) -> str:
    """Everything that defines a configuration except the seed (and the run's own timestamps/dirs)."""
    samp = {k: v for k, v in (run.get("sampling") or {}).items() if k != "seed"}
    return json.dumps([run.get("model"), run.get("thinking"), samp, run.get("think_budget"),
                       run.get("max_tokens"), run.get("backend")], sort_keys=True, default=str)


def seed_groups(summaries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Groups of 2+ runs with the same configuration and distinct seeds."""
    by: Dict[str, List[int]] = {}
    for i, s in enumerate(summaries):
        by.setdefault(config_key(s["run"]), []).append(i)
    out = []
    for idx in by.values():
        seeds = [seed_of(summaries[i]["run"]) for i in idx]
        if len(idx) >= 2 and None not in seeds and len(set(map(str, seeds))) == len(seeds):
            label = re.sub(r" seed \S+.*$", "", summaries[idx[0]]["label"])
            out.append({"label": f"{label} [{len(idx)} seeds]", "members": idx, "seeds": seeds})
    return out


def mark_single_seed(summaries: List[Dict[str, Any]], groups: List[Dict[str, Any]]) -> None:
    grouped = {i for g in groups for i in g["members"]}
    for i, s in enumerate(summaries):
        s["single_seed"] = sampled(s["run"]) and i not in grouped


# ------------------------------------------------------------------------------------------- baselines and ceilings
def _cwe_pass(label: str, item: Dict[str, Any]) -> Optional[float]:
    """1/0 from the live scorer for a constant answer (so parent/child credit rules stay in one place)."""
    try:
        import scorers
        return 1.0 if scorers.score_item(f"Answer: {label}", item).passed else 0.0
    except Exception:
        ans = item.get("answer")
        gold = ans.get("cwe") if isinstance(ans, dict) else ans
        golds = gold if isinstance(gold, list) else [gold]
        return 1.0 if label.upper() in {str(g).upper() for g in golds} else 0.0


def baselines(items: Sequence[Dict[str, Any]], cvss_constant: float = CVSS_PRIOR) -> List[Dict[str, Any]]:
    """Constant / majority-class baselines for one set of items of one suite."""
    if not items:
        return []
    scorer = items[0].get("scorer")
    n = len(items)
    out: List[Dict[str, Any]] = []
    if scorer == "mcq_letter":
        chance = sum(1 / max(1, len(it.get("choices") or "ABCD")) for it in items) / n
        out.append({"name": "chance (uniform over the options)", "accuracy": chance, "oracle": False,
                    "balanced": chance})
        letters: Dict[str, int] = {}
        for it in items:
            g = str(it.get("answer")).strip().upper()[:1]
            letters[g] = letters.get(g, 0) + 1
        top, k = max(sorted(letters.items()), key=lambda kv: kv[1])
        out.append({"name": f"always '{top}' (most common gold letter)", "accuracy": k / n, "oracle": True,
                    "balanced": 1 / len(letters)})
        hits = [_longest_credit(it) for it in items]
        out.append({"name": "longest option (ties share credit)", "accuracy": sum(hits) / n, "oracle": False,
                    "balanced": letter_balanced([(str(it.get("answer")).strip().upper()[:1], h)
                                                 for it, h in zip(items, hits)])})
    elif scorer == "cwe_match":
        acc = sum(_cwe_pass(CWE_PRIOR, it) or 0.0 for it in items) / n
        out.append({"name": f"always {CWE_PRIOR}", "accuracy": acc, "oracle": False})
        counts: Dict[str, int] = {}
        for it in items:
            ans = it.get("answer")
            g = ans.get("cwe") if isinstance(ans, dict) else ans
            g = g[0] if isinstance(g, list) and g else g
            counts[str(g)] = counts.get(str(g), 0) + 1
        top, _ = max(sorted(counts.items()), key=lambda kv: kv[1])
        if top != CWE_PRIOR:
            acc = sum(_cwe_pass(top, it) or 0.0 for it in items) / n
            out.append({"name": f"always {top} (most frequent gold label)", "accuracy": acc, "oracle": True})
    elif scorer == "cvss_mae":
        golds = [g for g in (_gold_cvss(it.get("answer")) for it in items) if g is not None]
        if golds:
            def stat(c: float) -> Dict[str, float]:
                errs = [round(abs(g - c), 4) for g in golds]
                return {"mae": sum(errs) / len(errs), "accuracy": sum(1 for e in errs if e <= 1.0) / len(errs)}
            out.append(dict(name=f"constant {cvss_constant:g}", oracle=False, **stat(cvss_constant)))
            sg = sorted(golds)
            med = sg[len(sg) // 2] if len(sg) % 2 else (sg[len(sg) // 2 - 1] + sg[len(sg) // 2]) / 2
            if med != cvss_constant:
                out.append(dict(name=f"constant {med:g} (median gold score)", oracle=True, **stat(med)))
    for b in out:
        b["n"] = n
    return out


def letter_balanced(pairs: Iterable[Tuple[str, float]]) -> Optional[float]:
    """Mean over gold letters of the accuracy within that letter."""
    by: Dict[str, List[float]] = {}
    for g, v in pairs:
        by.setdefault(g, []).append(v)
    if not by:
        return None
    return sum(sum(v) / len(v) for v in by.values()) / len(by)


def _boot_mean_ci(xs: Sequence[float], seed: int = ST.DEFAULT_SEED, n_boot: int = 2000) -> Tuple[float, float]:
    if not xs:
        return float("nan"), float("nan")
    reps = ST.bootstrap_means(list(xs), None, n_boot, seed)
    return ST.percentile(reps, 0.025), ST.percentile(reps, 0.975)


def _role_labels(item: Dict[str, Any]) -> Dict[str, Dict[str, List[str]]]:
    return item_labels({"labels": (item.get("meta") or {}).get("labels") or {}, "gold": None,
                        "scorer": item.get("scorer")})


CEILING_MIN_N = 10            # below this a ceiling or noise estimate prints "n too small" and no CI
CEILING_ROLES = ("NVD vs CNA: NVD's own label against the CNA-role label of the same CVE (ADP labels not used); "
                 "NVD vs ADP: NVD's own label against the ADP-role label (CNA labels not used). One row per CVE.")


def _wilson_ci(xs: Sequence[float]) -> Optional[Tuple[float, float]]:
    if len(xs) < CEILING_MIN_N:
        return None
    return ST.wilson(int(round(sum(xs))), len(xs))


def label_ceiling(items: Iterable[Dict[str, Any]], seed: int = ST.DEFAULT_SEED) -> Optional[Dict[str, Any]]:
    """Agreement between two labelling sources on the same items, per pair (NVD vs CNA, NVD vs ADP), with an
    item-bootstrap 95% CI and n. It bounds how well any model can agree with one source (absolute MAE and
    agreement), not paired differences between models. CVSS: mean |score difference|, share within 1.0,
    exact-vector share. CWE: exact (any overlap) and exact-or-one-hop (MITRE ChildOf) agreement."""
    try:
        import scorers
    except Exception:
        scorers = None  # type: ignore
    pairs: Dict[str, Dict[str, list]] = {}
    seen: set = set()
    for it in items:
        key_ = (it.get("meta") or {}).get("cve") or it.get("id")
        if key_ in seen:                      # one row per CVE (suites share CVEs)
            continue
        seen.add(key_)
        lab = _role_labels(it)
        for other in ("cna", "adp"):
            key = f"nvd_vs_{other}"
            nv, ov = lab["cvss"].get("nvd") or [], lab["cvss"].get(other) or []
            if nv and ov:
                a, b = _gold_cvss(nv[0]), _gold_cvss(ov[0])
                if a is not None and b is not None:
                    same = None
                    if scorers is not None:
                        try:
                            same = scorers.cvss_metrics(nv[0]) == scorers.cvss_metrics(ov[0])
                        except Exception:
                            same = None
                    pairs.setdefault(key, {"cvss": [], "cwe": []})["cvss"].append((round(abs(a - b), 4), same))
            wn = {_norm_cwe(x) for x in lab["cwe"].get("nvd") or []} - {None}
            wo = {_norm_cwe(x) for x in lab["cwe"].get(other) or []} - {None}
            if wn and wo:
                exact = bool(wn & wo)
                hop = exact
                if not exact and scorers is not None and hasattr(scorers, "cwe_related"):
                    try:
                        hop = any(scorers.cwe_related(x, y) for x in wn for y in wo)
                    except Exception:
                        hop = False
                pairs.setdefault(key, {"cvss": [], "cwe": []})["cwe"].append((exact, hop))
    if not pairs:
        return None
    out: Dict[str, Any] = {}
    for key, v in sorted(pairs.items()):
        o: Dict[str, Any] = {}
        cv, cw = v["cvss"], v["cwe"]
        if cv:
            d = [x for x, _ in cv]
            w1 = [1.0 if x <= 1.0 else 0.0 for x in d]
            ex = [1.0 if s_ else 0.0 for _, s_ in cv if s_ is not None]
            o.update(cvss_n=len(cv), cvss_mean_abs=sum(d) / len(d),
                     cvss_mean_abs_ci=_boot_mean_ci(d, seed) if len(d) >= CEILING_MIN_N else None,
                     cvss_within1=sum(w1) / len(w1), cvss_within1_ci=_wilson_ci(w1),
                     cvss_exact_vector=(sum(ex) / len(ex)) if ex else None,
                     cvss_exact_vector_ci=_wilson_ci(ex) if ex else None)
        if cw:
            e = [1.0 if x else 0.0 for x, _ in cw]
            h = [1.0 if y else 0.0 for _, y in cw]
            o.update(cwe_n=len(cw), cwe_exact=sum(e) / len(e), cwe_exact_ci=_wilson_ci(e),
                     cwe_one_hop=sum(h) / len(h), cwe_one_hop_ci=_wilson_ci(h))
        if o:
            out[key] = o
    return out or None


def cluster_label_noise(items: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Within-CNA template consistency: within near-duplicate clusters (meta.rigor.near_dup_cluster, the same CNA
    template text), how often do two items' gold labels differ? Reported per cluster (each cluster weighs the
    same) and per pair (large clusters weigh more). It describes label heterogeneity inside templates; it is neither
    an upper nor a lower bound on label noise (different CVEs on one template may differ legitimately, and identical
    labels may be copied). CWE: different gold; CVSS: different vector, and mean |score difference|."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for it in items:
        c = ((it.get("meta") or {}).get("rigor") or {}).get("near_dup_cluster")
        if c is not None:
            groups.setdefault(str(c), []).append(it)
    cw, cvd, cvx = [], [], []
    cw_cl, cvx_cl = [], []
    n_cl = 0
    for g in groups.values():
        if len(g) < 2:
            continue
        n_cl += 1
        gw, gx = [], []
        for i in range(len(g)):
            for j in range(i + 1, len(g)):
                a, b = g[i], g[j]
                if a.get("scorer") == "cwe_match":
                    x, y = _norm_cwe(a.get("answer")), _norm_cwe(b.get("answer"))
                    if x and y:
                        gw.append(1.0 if x != y else 0.0)
                elif a.get("scorer") == "cvss_mae":
                    x, y = _gold_cvss(a.get("answer")), _gold_cvss(b.get("answer"))
                    if x is not None and y is not None:
                        cvd.append(abs(x - y))
                        gx.append(1.0 if str(a.get("answer")) != str(b.get("answer")) else 0.0)
        cw += gw
        cvx += gx
        if gw:
            cw_cl.append(sum(gw) / len(gw))
        if gx:
            cvx_cl.append(sum(gx) / len(gx))
    if not (cw or cvd):
        return None
    out: Dict[str, Any] = {"clusters": n_cl, "too_small": n_cl < CEILING_MIN_N}
    if cw:
        out.update(cwe_pairs=len(cw), cwe_disagree=sum(cw) / len(cw), cwe_clusters=len(cw_cl),
                   cwe_disagree_cluster=sum(cw_cl) / len(cw_cl))
    if cvd:
        out.update(cvss_pairs=len(cvd), cvss_mean_abs=sum(cvd) / len(cvd), cvss_vector_disagree=sum(cvx) / len(cvx),
                   cvss_clusters=len(cvx_cl), cvss_vector_disagree_cluster=sum(cvx_cl) / len(cvx_cl))
    return out


STRATA = [
    # flag, label when True, label when False
    ("has_signal", "has signal", "no signal"),
    ("leak", "key leak in prompt", "no key leak"),
    ("name_in_text", "CWE name in text", "CWE name not in text"),
    ("key_longest", "key is the longest option", "key not longest"),
    ("post_cutoff", "post-cutoff (this model)", "pre-cutoff (this model)"),
    ("dev_cluster_exposed", "dev-cluster exposed", "not exposed"),
    ("dev_neardup_exposed", "dev near-duplicate exposed", "not near-dup exposed"),
    ("late_cve_id", "late CVE id (year < published - 1)", "CVE id not late"),
    ("other_cwe_name_in_text", "text names a different CWE", "no other CWE named"),
    ("frr_exclude", "CSE covert-harmful", "CSE other"),
]


def strata_rows(summaries: List[Dict[str, Any]], recs: List[Dict[str, Dict[str, Any]]], suite: str,
                errors: str) -> List[Dict[str, Any]]:
    """Per stratum flag with both values present in the suite: each run's accuracy in each, and the gap."""
    out = []
    for flag, lt, lf in STRATA:
        rows_ = []
        for s, r in zip(summaries, recs):
            rr = [x for x in r.values() if x["suite"] == suite]
            t = [outcome(x, "accuracy", errors) for x in rr if x["flags"].get(flag) is True]
            f = [outcome(x, "accuracy", errors) for x in rr if x["flags"].get(flag) is False]
            t, f = [v for v in t if v is not None], [v for v in f if v is not None]
            if t and f:
                rows_.append({"run": s["label"], "t": sum(t) / len(t), "nt": len(t), "f": sum(f) / len(f),
                              "nf": len(f)})
        if rows_:
            out.append({"flag": flag, "lt": lt, "lf": lf, "rows": rows_})
    return out


CAT_STRATA = [("label_role", "gold label source (NVD / CNA / ADP)"), ("frr_tier", "CSE prompt tier"),
              ("cwe_mapping_usage", "MITRE mapping status of the gold CWE")]


def cat_strata_rows(summaries: List[Dict[str, Any]], recs: List[Dict[str, Dict[str, Any]]], suite: str,
                    errors: str) -> List[Dict[str, Any]]:
    """Per categorical stratum with 2+ values in the suite: each run's accuracy per value."""
    out = []
    for cat, title in CAT_STRATA:
        vals = sorted({x["cats"].get(cat) for r in recs for x in r.values()
                       if x["suite"] == suite and x.get("cats", {}).get(cat) is not None})
        if len(vals) < 2:
            continue
        rows_ = []
        for s, r in zip(summaries, recs):
            cells = {}
            for v in vals:
                xs = [outcome(x, "accuracy", errors) for x in r.values()
                      if x["suite"] == suite and x.get("cats", {}).get(cat) == v]
                xs = [y for y in xs if y is not None]
                cells[v] = (sum(xs) / len(xs), len(xs)) if xs else None
            if any(cells.values()):
                rows_.append({"run": s["label"], "cells": cells})
        if rows_:
            out.append({"cat": cat, "title": title, "values": vals, "rows": rows_})
    return out


def mcq_balanced(summaries: List[Dict[str, Any]], recs: List[Dict[str, Dict[str, Any]]], suite: str,
                 errors: str) -> List[Tuple[str, float, int]]:
    out = []
    for s, r in zip(summaries, recs):
        pairs = []
        for x in r.values():
            if x["suite"] != suite or x.get("scorer") != "mcq_letter" or x.get("gold") is None:
                continue
            v = outcome(x, "accuracy", errors)
            if v is not None:
                pairs.append((str(x["gold"]).strip().upper()[:1], v))
        if pairs:
            out.append((s["label"], letter_balanced(pairs), len(pairs)))
    return out


def either_rows(summaries: List[Dict[str, Any]], recs: List[Dict[str, Dict[str, Any]]], suite: str, errors: str,
                impute: str, constant: float) -> List[Dict[str, Any]]:
    out = []
    for s, r in zip(summaries, recs):
        rr = [x for x in r.values() if x["suite"] == suite]
        acc = [v for v in (outcome(x, "accuracy_either", errors, impute=impute, constant=constant) for x in rr)
               if v is not None]
        if not acc:
            continue
        row = {"run": s["label"], "n": len(acc), "acc_either": sum(acc) / len(acc)}
        strict = [outcome(x, "accuracy", errors) for x in rr
                  if outcome(x, "accuracy_either", errors, impute=impute, constant=constant) is not None]
        strict = [v for v in strict if v is not None]
        row["acc_strict"] = sum(strict) / len(strict) if strict else None
        if any(is_error_metric(x) for x in rr):
            me = [v for v in (outcome(x, "mae_either", errors, impute=impute, constant=constant) for x in rr)
                  if v is not None]
            row["mae_either"] = sum(me) / len(me) if me else None
        out.append(row)
    return out


def macro_aggregate(summaries: List[Dict[str, Any]], recs: List[Dict[str, Dict[str, Any]]],
                    errors: str = "fail") -> Dict[str, Any]:
    """Macro average of accuracy over suites, on the items every run scored in each suite. Raises ValueError when
    the runs do not cover the same suites: an average over different suite sets compares different things."""
    sets = [frozenset(k for k in s["suites"] if "[label_source=" not in k) for s in summaries]
    if len(set(sets)) != 1:
        diff = sorted(set.union(*map(set, sets)) - set.intersection(*map(set, sets)))
        raise ValueError("cross-suite aggregate refused: the runs cover different suites "
                         f"(not in every run: {', '.join(diff)})")
    suites = sorted(sets[0])
    per_run: List[Dict[str, Any]] = [{"run": s["label"], "suites": {}} for s in summaries]
    for su in suites:
        rows_ = [_row_records(r, su, None) for r in recs]
        shared = set.intersection(*[set(r) for r in rows_])
        usable = [k for k in shared if all(outcome(rr[k], "accuracy", errors) is not None for rr in rows_)]
        if not usable:
            raise ValueError(f"cross-suite aggregate refused: no item of {su} has a usable score in every run")
        for pr, rr in zip(per_run, rows_):
            vals = [outcome(rr[k], "accuracy", errors) for k in usable]
            pr["suites"][su] = (sum(vals) / len(vals), len(vals))
    for pr in per_run:
        accs = [v for v, _ in pr["suites"].values() if v is not None]
        pr["macro"] = sum(accs) / len(accs) if len(accs) == len(suites) else None
    return {"suites": suites, "runs": per_run}


# ------------------------------------------------------------------------------------------- comparisons file
def load_comparisons(path: Path) -> Dict[str, Any]:
    """A pre-registration file (JSON, or YAML when PyYAML is installed)."""
    text = Path(path).read_text(encoding="utf-8")
    if Path(path).suffix in (".yaml", ".yml"):
        import yaml  # optional dependency, only for YAML plans
        plan = yaml.safe_load(text)
    else:
        plan = json.loads(text)
    if not isinstance(plan, dict) or not isinstance(plan.get("families"), list):
        raise ValueError(f"{path}: expected an object with a 'families' list")
    known = set(OPS)
    for fam in plan["families"]:
        if fam.get("test", "holm") not in ("holm", "iut"):
            raise ValueError(f"{path}: family {fam.get('name')}: test must be 'holm' or 'iut'")
        for c in fam.get("comparisons") or []:
            for k in ("id", "suite", "a", "b"):
                if k not in c:
                    raise ValueError(f"{path}: comparison {c} lacks {k!r}")
            d = c.get("direction", "two-sided")
            if d not in ("a>b", "a<b", "two-sided", "equivalent"):
                raise ValueError(f"{path}: {c['id']}: direction must be a>b, a<b, two-sided or equivalent")
            if fam.get("test") == "iut" and d not in ("a>b", "a<b"):
                raise ValueError(f"{path}: {c['id']}: an intersection-union family needs directional members")
            spec = primary_spec(plan, c)
            bad = [o for o in spec["ops"] + tuple(sens_ops(plan, c)) if o not in known]
            if bad:
                raise ValueError(f"{path}: {c['id']}: unknown analysis op(s) {bad}; known: {sorted(known)}")
            if spec["impute"] not in IMPUTE:
                raise ValueError(f"{path}: {c['id']}: cvss_impute must be one of {IMPUTE}")
            m = c.get("metric", (plan.get("defaults") or {}).get("metric", "accuracy"))
            if m not in BINARY_METRICS + CONT_METRICS:
                raise ValueError(f"{path}: {c['id']}: unknown metric {m!r}")
    return plan


def primary_spec(plan: Dict[str, Any], c: Dict[str, Any]) -> Dict[str, Any]:
    """The declared primary analysis of a comparison: its own 'primary' block over the plan's defaults."""
    d = dict((plan.get("defaults") or {}).get("primary") or {})
    d.update(c.get("primary") or {})
    return {"ops": as_ops(d.get("exclude") or d.get("ops") or ()), "errors": d.get("errors", "fail"),
            "impute": d.get("cvss_impute", "max"), "constant": float(d.get("cvss_constant", CVSS_PRIOR)),
            "allow_unknown": sorted(set(d.get("allow_unknown") or []))}


def sens_ops(plan: Dict[str, Any], c: Dict[str, Any]) -> List[str]:
    """The pre-registered sensitivity analyses of a comparison (ops toggled one at a time)."""
    s = c.get("sensitivity")
    if s is None:
        s = (plan.get("defaults") or {}).get("sensitivity") or []
    return list(s)


def min_n_of(plan: Dict[str, Any], c: Optional[Dict[str, Any]] = None) -> int:
    return int((c or {}).get("min_n", (plan.get("defaults") or {}).get("min_n",
                                                                       (plan.get("analysis") or {}).get("min_n", MIN_N))))


def resolve_side(spec: Any, summaries: List[Dict[str, Any]]) -> Optional[List[int]]:
    """Run indices for a comparison side: a run dir name or label, or a list of them (a seed group)."""
    names = spec if isinstance(spec, list) else [spec]
    out = []
    for nm in names:
        hits = [i for i, s in enumerate(summaries) if s["run_dir"] == nm] or \
               [i for i, s in enumerate(summaries) if s["label"] == nm]
        if len(hits) != 1:
            return None
        out.append(hits[0])
    return out


def _toggle(ops: Tuple[str, ...], op: str) -> Tuple[str, ...]:
    s = set(ops)
    s.symmetric_difference_update({op})
    if op in ("impute_none", "impute_max"):           # imputation ops are alternatives, not combinable
        s -= {"impute_none", "impute_max"} - {op} if op in s else set()
    if op in ("trunc_best", "trunc_worst"):
        s -= {"trunc_best", "trunc_worst"} - {op} if op in s else set()
    return tuple(sorted(s))


def flag_coverage(a_runs: Sequence[Dict[str, Dict[str, Any]]], b_runs: Sequence[Dict[str, Dict[str, Any]]],
                  ops: Sequence[str], suite_recs: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """For each declared exclusion op with an item flag: how many shared items have the flag known. A declared
    exclusion whose flag is missing on ANY item (also when no item of the suite carries it) makes the comparison
    not evaluable, unless the plan lists the op in ``primary.allow_unknown``: a missing annotation must never turn
    a declared exclusion into a silent no-op. ``applies`` records whether any item of the suite has the flag."""
    shared = set(a_runs[0]) if a_runs else set()
    for r in list(a_runs[1:]) + list(b_runs):
        shared &= set(r)
    out: Dict[str, Dict[str, Any]] = {}
    for op in ops:
        flag = OP_FLAG.get(op)
        if not flag:
            continue
        applies = any(x["flags"].get(flag) is not None for x in suite_recs)
        known = sum(1 for rid in shared
                    if all(r[rid]["flags"].get(flag) is not None for r in list(a_runs) + list(b_runs)))
        out[op] = {"flag": flag, "applies": applies, "known": known, "n": len(shared)}
    return out


def _stratum(a_runs, b_runs, metric, op, spec, errors, alpha, margin, n_boot, seed) -> Dict[str, Any]:
    """A keep-only op as a stratum: the effect inside, outside, and their interaction (inside - outside) with an
    independent-bootstrap CI. Never changes a verdict."""
    comp = {"post_cutoff": "not_post_cutoff", "name_in_text": "no_name_in_text"}[op]
    # relative to the primary set BEFORE the stratum-defining exclusion (a primary that drops name-in-text items
    # would otherwise leave the name-in-text stratum empty)
    base = tuple(o for o in spec["ops"] if o not in KEEP_ONLY and o != comp)
    kw = dict(impute=spec["impute"], constant=spec["constant"], power=False, n_perm=None, n_boot=n_boot, seed=seed,
              cluster=False)
    rin = compare(a_runs, b_runs, metric, base + (op,), errors, alpha, None, **kw)
    rout = compare(a_runs, b_runs, metric, base + (comp,), errors, alpha, None, **kw)
    out = {"op": op, "in": rin, "out": rout}
    if rin.get("n") and rout.get("n") and rin.get("diff") is not None and rout.get("diff") is not None:
        din, dout = _item_diffs(a_runs, b_runs, metric, base + (op,), errors, spec, True), \
            _item_diffs(a_runs, b_runs, metric, base + (comp,), errors, spec, True)
        # cluster bootstrap inside each stratum (template clusters resampled whole)
        rin_reps = ST.bootstrap_means([d for d, _ in din], [c for _, c in din], n_boot, seed)
        rout_reps = ST.bootstrap_means([d for d, _ in dout], [c for _, c in dout], n_boot, seed + 1)
        import random as _r
        _r.Random(seed + 2).shuffle(rout_reps)
        reps = sorted(x - y for x, y in zip(rin_reps, rout_reps))
        lo, hi, p = ST.boot_ci_and_p(reps, alpha)
        out.update(interaction=rin["diff"] - rout["diff"], int_lo=lo, int_hi=hi, int_p=p)
    return out


def _item_diffs(a_runs, b_runs, metric, ops, errors, spec, with_cluster: bool = False) -> List[Any]:
    shared = set(a_runs[0])
    for r in list(a_runs[1:]) + list(b_runs):
        shared &= set(r)
    out = []
    for rid in sorted(shared):
        recs = [r[rid] for r in list(a_runs) + list(b_runs)]
        if item_excluded(recs, ops):
            continue
        xa = [outcome(r[rid], metric, errors, ops, spec["impute"], spec["constant"]) for r in a_runs]
        xb = [outcome(r[rid], metric, errors, ops, spec["impute"], spec["constant"]) for r in b_runs]
        if any(x is None for x in xa + xb):
            continue
        d = sum(xa) / len(xa) - sum(xb) / len(xb)
        if with_cluster:
            c = next((r[rid]["cluster"] for r in list(a_runs) + list(b_runs) if r[rid].get("cluster") is not None),
                     None)
            out.append((d, str(c) if c is not None else f"item:{rid}"))
        else:
            out.append(d)
    return out


def confirmatory(plan: Dict[str, Any], summaries: List[Dict[str, Any]], recs: List[Dict[str, Dict[str, Any]]],
                 errors: Optional[str] = None, n_boot: int = ST.DEFAULT_BOOT, n_perm: int = ST.DEFAULT_PERM,
                 seed: int = ST.DEFAULT_SEED, judge_scope: Optional[str] = None) -> List[Dict[str, Any]]:
    """Evaluate every pre-specified comparison: primary result (as declared), flag coverage, sensitivity analyses,
    strata, adjustment over the declared m, robustness and verdict. ``errors`` overrides the declared error policy
    (legacy callers)."""
    alpha_all = float(plan.get("alpha", 0.05))
    defaults = plan.get("defaults") or {}
    results: List[Dict[str, Any]] = []
    for fam in plan["families"]:
        alpha = float(fam.get("alpha", alpha_all))
        test = fam.get("test", "holm")
        m = len(fam.get("comparisons") or [])
        alpha_power = alpha if test == "iut" else alpha / max(1, m)    # Holm worst case
        for c in fam.get("comparisons") or []:
            metric = c.get("metric", defaults.get("metric", "accuracy"))
            direction = c.get("direction", "two-sided")
            margin = c.get("margin", defaults.get("margin")) if direction == "equivalent" else None
            if direction == "equivalent" and margin is None:
                raise ValueError(f"{c['id']}: an equivalence comparison needs a margin")
            spec = primary_spec(plan, c)
            if errors is not None and "errors" not in (c.get("primary") or {}) and \
                    "errors" not in (defaults.get("primary") or {}):
                spec["errors"] = errors
            min_n = min_n_of(plan, c)
            need = c.get("effect_needed", (c.get("assumed") or {}).get("delta"))
            assumed = dict(((defaults.get("assumed") or {}).get(metric) or {}))
            assumed.update(c.get("assumed") or {})
            planned = c.get("n", (plan.get("planned_n") or {}).get(c["suite"]))
            res: Dict[str, Any] = {"id": c["id"], "family": fam.get("name", "?"), "decision": fam.get("decision", ""),
                                   "test": test, "suite": c["suite"], "metric": metric, "direction": direction,
                                   "margin": margin, "alpha": alpha, "claim": c.get("claim", ""), "a_spec": c["a"],
                                   "b_spec": c["b"], "spec": spec, "prereg_sens": sens_ops(plan, c),
                                   "alpha_power": alpha_power, "min_n": min_n, "effect_needed": need,
                                   "apriori": _apriori_power(metric, direction, planned, assumed, alpha_power, margin,
                                                             (len(c["a"]) if isinstance(c["a"], list) else 1) *
                                                             (len(c["b"]) if isinstance(c["b"], list) else 1)
                                                             if isinstance(c["a"], list) or isinstance(c["b"], list)
                                                             else 0)}
            ia, ib = resolve_side(c["a"], summaries), resolve_side(c["b"], summaries)
            if ia is None or ib is None:
                res.update(verdict="not evaluable (runs not loaded)", p_test=None, not_evaluable="runs not loaded")
                results.append(res)
                continue
            res["runs"] = sorted(set(ia + ib))
            res["a_idx"], res["b_idx"] = ia, ib
            res["interventions"] = c.get("interventions")
            a_runs = [_row_records(recs[i], c["suite"], None) for i in ia]
            b_runs = [_row_records(recs[i], c["suite"], None) for i in ib]
            res["a_label"] = _side_label(ia, summaries)
            res["b_label"] = _side_label(ib, summaries)
            res["single_seed"] = any(summaries[i].get("single_seed") or (sampled(summaries[i]["run"]) and len(side) == 1)
                                     for side in (ia, ib) for i in side)
            suite_recs = [x for r in recs for x in r.values() if x["suite"] == c["suite"]]
            if judge_scope == "extract_only" and any(x.get("scorer") == "llm_judge" for x in suite_recs):
                spec["ops"] = tuple(sorted(set(spec["ops"]) | {"extract_only"}))
                res["judge_scope"] = "extract_only"
            cov = flag_coverage(a_runs, b_runs, spec["ops"], suite_recs)
            res["coverage"] = cov
            unknown = {op: v for op, v in cov.items()
                       if v["known"] < v["n"] and op not in spec["allow_unknown"]}

            def run_variant(ops: Tuple[str, ...], errs: str, power: bool = False) -> Dict[str, Any]:
                return compare(a_runs, b_runs, metric, ops, errs, alpha, margin, n_boot, n_perm, seed,
                               impute=spec["impute"], constant=spec["constant"], alpha_power=alpha_power, power=power)

            prim_ops = tuple(o for o in spec["ops"] if o not in KEEP_ONLY) + \
                tuple(o for o in spec["ops"] if o in KEEP_ONLY)
            prim = run_variant(prim_ops, spec["errors"], power=True)
            res["primary"] = prim
            if unknown:
                why = "; ".join(f"{v['flag']} unknown for {v['n'] - v['known']} of {v['n']} items" for v in unknown.values())
                res.update(p_test=None, not_evaluable=f"exclusion flag not known for every item ({why})")
            elif (prim.get("n") or 0) < min_n:
                res.update(p_test=None, not_evaluable=f"n = {prim.get('n') or 0} < minimum {min_n}")
            else:
                res["p_test"] = _ptest(prim, direction)
            sens: Dict[str, Dict[str, Any]] = {}
            strata: Dict[str, Dict[str, Any]] = {}
            all_recs = [r for side in (a_runs, b_runs) for run in side for r in run.values()]
            for op in OPS:
                if op in KEEP_ONLY:
                    if variant_applies(all_recs, op):
                        strata[op] = _stratum(a_runs, b_runs, metric, op, spec, spec["errors"], alpha, margin,
                                              min(n_boot, 2000), seed)
                    continue
                if op == "errors_excluded":
                    if spec["errors"] == "fail" and any(r["status"] in ("error", "api_error") for r in all_recs):
                        sens[op] = run_variant(spec["ops"], "exclude")
                    continue
                if op in ("impute_none", "impute_max") and metric not in ("mae", "mae_either"):
                    continue
                if op in ("impute_none", "impute_max") and spec["impute"] == op.split("_")[1]:
                    continue
                if op in ("trunc_best", "trunc_worst") and not metric.startswith("refusal_"):
                    continue
                if op in ("no_key_longest", "letter_balanced") and not any(x.get("scorer") == "mcq_letter"
                                                                            for x in all_recs):
                    continue
                if variant_applies(all_recs, op):
                    sens[op] = run_variant(_toggle(spec["ops"], op), spec["errors"])
            for v in sens.values():
                if (v.get("n") or 0) < min_n:
                    v["not_evaluable"] = f"n = {v.get('n') or 0} < {min_n}"
            for sm in c.get("sensitivity_metrics") or []:
                v = compare(a_runs, b_runs, sm, spec["ops"], spec["errors"], alpha, margin, n_boot, n_perm, seed,
                            impute=spec["impute"], constant=spec["constant"], alpha_power=alpha_power, power=False)
                if (v.get("n") or 0) < min_n:
                    v["not_evaluable"] = f"n = {v.get('n') or 0} < {min_n}"
                sens[f"metric:{sm}"] = v
                VARIANT_LABEL.setdefault(f"metric:{sm}", f"metric {sm}")
                res["prereg_sens"] = list(res["prereg_sens"]) + [f"metric:{sm}"]
            res["sensitivity"] = {k: v for k, v in sens.items() if _changed(prim, v, direction)}
            res["strata"] = strata
            res["at_ceiling"] = any(x.get("at_ceiling") for x in all_recs)
            if direction == "equivalent" and metric in BINARY_METRICS:
                res["floor"] = floor_share(a_runs, b_runs, recs, c["suite"], spec, metric)
            results.append(res)
    _adjust(results)
    for r in results:
        if r.get("p_test") is None:
            r["verdict"] = r.get("verdict") if str(r.get("verdict", "")).startswith("not evaluable") else \
                f"not evaluable ({r.get('not_evaluable', 'no result')})"
            continue
        _robustness(r, results)
        r["verdict"] = _verdict(r)
    return results


def floor_share(a_runs, b_runs, recs: List[Dict[str, Dict[str, Any]]], suite: str, spec: Dict[str, Any],
                metric: str) -> Optional[Tuple[float, int, int]]:
    """(share, n items, n runs): of the comparison's primary items, the share that EVERY loaded run of the suite
    fails. A large floor (key errors, impossible items) makes two models look alike: read equivalence with it."""
    shared = set(a_runs[0])
    for r in list(a_runs[1:]) + list(b_runs):
        shared &= set(r)
    ids = [rid for rid in shared if not item_excluded([r[rid] for r in list(a_runs) + list(b_runs)], spec["ops"])]
    runs = [r for r in recs if any(x["suite"] == suite for x in r.values())]
    if not ids or not runs:
        return None
    fl = 0
    for rid in ids:
        vals = [outcome(r[rid], metric, spec["errors"]) for r in runs if rid in r]
        vals = [v for v in vals if v is not None]
        if vals and all(v == 0 for v in vals):
            fl += 1
    return fl / len(ids), len(ids), len(runs)


def _ptest(r: Dict[str, Any], direction: str) -> Optional[float]:
    return r.get("p_tost") if direction == "equivalent" else r.get("p")


def _changed(a: Dict[str, Any], b: Dict[str, Any], direction: str) -> bool:
    """Whether a variant result differs from the primary one at all (n, estimate or p)."""
    if not b:
        return False
    if a.get("n") != b.get("n"):
        return True
    for k in ("diff", "lo", "hi"):
        x, y = a.get(k), b.get(k)
        if (x is None) != (y is None) or (x is not None and abs(x - y) > 1e-12):
            return True
    pa, pb = _ptest(a, direction), _ptest(b, direction)
    return (pa is None) != (pb is None) or (pa is not None and abs(pa - pb) > 1e-12)


def _side_label(idx: List[int], summaries: List[Dict[str, Any]]) -> str:
    if len(idx) == 1:
        return summaries[idx[0]]["label"]
    base = re.sub(r" seed \S+.*$", "", summaries[idx[0]]["label"])
    return f"{base} [{len(idx)} seeds]"


def _direction_ok_d(direction: str, d: Optional[float]) -> bool:
    if d is None:
        return False
    return {"a>b": d > 0, "a<b": d < 0}.get(direction, True)


def _direction_ok(r: Dict[str, Any]) -> bool:
    return _direction_ok_d(r["direction"], (r.get("primary") or {}).get("diff"))


def _family_adjust(ps: List[Optional[float]], test: str) -> List[Optional[float]]:
    """Over the DECLARED members: a missing or not-evaluable member counts as p = 1 (never shrinks m)."""
    ps1 = [1.0 if p is None else p for p in ps]
    return list(ps1) if test == "iut" else ST.holm(ps1)


def _adjust(results: List[Dict[str, Any]]) -> None:
    """Holm (or IUT: no adjustment) within each family over its declared m, BH within each family, and Holm across
    families where an IUT family counts as one hypothesis with p = the largest member p (1 if any member has the
    wrong sign or is not evaluable). Missing / not-evaluable comparisons enter as p = 1."""
    fams: Dict[str, List[int]] = {}
    for i, r in enumerate(results):
        fams.setdefault(r["family"], []).append(i)
    hyp: List[Tuple[List[int], float]] = []
    for idx in fams.values():
        test = results[idx[0]]["test"]
        ps = [results[i].get("p_test") for i in idx]
        ps1 = [1.0 if p is None else p for p in ps]
        for i, h, q in zip(idx, _family_adjust(ps, test), ST.bh(ps1)):
            results[i]["p_holm"], results[i]["q_bh"] = h, q
        if test == "iut":
            pf = max((results[i]["p_test"] if results[i].get("p_test") is not None and _direction_ok(results[i])
                      else 1.0) for i in idx)
            hyp.append((idx, pf))
            for i in idx:
                results[i]["p_family"] = pf
        else:
            hyp += [([i], 1.0 if results[i].get("p_test") is None else results[i]["p_test"]) for i in idx]
    adj = ST.holm([p for _, p in hyp])
    for (idx, _), a in zip(hyp, adj):
        for i in idx:
            results[i]["p_holm_all"] = a


def _holds(direction: str, p_adj: Optional[float], alpha: float, d: Optional[float]) -> bool:
    return p_adj is not None and p_adj < alpha and _direction_ok_d(direction, d)


def _robustness(r: Dict[str, Any], results: List[Dict[str, Any]]) -> None:
    """For each pre-registered sensitivity analysis (not keep-only strata): re-adjust the family with every member
    analysed that way, and record a flip when the comparison's conclusion (significance after adjustment, or the
    sign) changes. A sensitivity result below the minimum n is 'not evaluable', never a flip."""
    fam = [x for x in results if x["family"] == r["family"]]
    flips, checked, skipped = [], [], []
    base_hold = _holds(r["direction"], r.get("p_holm"), r["alpha"], (r.get("primary") or {}).get("diff"))
    for op in r.get("prereg_sens") or []:
        if op in KEEP_ONLY:
            continue
        s = (r.get("sensitivity") or {}).get(op)
        if s is None:
            continue           # the variant changes nothing for this comparison
        if s.get("not_evaluable"):
            skipped.append(op)
            continue
        checked.append(op)
        ps = []
        for x in fam:
            sx = (x.get("sensitivity") or {}).get(op)
            if sx and not sx.get("not_evaluable"):
                ps.append(_ptest(sx, x["direction"]))
            else:
                ps.append(x.get("p_test"))
        adj = _family_adjust(ps, r["test"])
        p_adj = adj[fam.index(r)]
        d0, d1 = (r.get("primary") or {}).get("diff"), s.get("diff")
        hold = _holds(r["direction"], p_adj, r["alpha"], d1)
        sign_flip = d0 is not None and d1 is not None and d0 * d1 < 0
        if hold != base_hold or (sign_flip and r["direction"] != "equivalent"):
            flips.append(op)
    r["robust_checked"] = checked
    r["robust_not_evaluable"] = skipped
    r["not_robust_to"] = flips


_APRIORI: Dict[str, Any] = {}


def _apriori_power(metric: str, direction: str, n: Optional[int], assumed: Dict[str, Any], alpha: float,
                   margin: Optional[float], seed_group: bool) -> Optional[Dict[str, Any]]:
    """A-priori power from the PLAN (assumed design values and planned n), with power.py's model of the test the
    report runs. None when the plan gives no assumptions or no planned n."""
    if not n or not assumed:
        return None
    key = json.dumps([metric, direction, n, assumed, alpha, margin, seed_group], sort_keys=True, default=str)
    if key not in _APRIORI:
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("_power_tool", str(EVALS_DIR / "tools" / "power.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _APRIORI[key] = mod.comparison_power(metric, direction, int(n), dict(assumed), alpha, margin,
                                                 seed_group=bool(seed_group), want_n80=False,
                                                 seed_pairs=int(seed_group) if seed_group else 1)
        except Exception as exc:
            _APRIORI[key] = {"power": None, "error": exc.__class__.__name__}
    return _APRIORI[key]


def _undetectable(r: Dict[str, Any]) -> bool:
    """Pre-specified, never from observed data: the plan's a-priori power at the planned n is below target."""
    ap = r.get("apriori") or {}
    return ap.get("power") is not None and ap["power"] < POWER_TARGET


def _verdict(r: Dict[str, Any]) -> str:
    """Wording identical to PROTOCOL.md 'Verdict wording'."""
    pr = r.get("primary") or {}
    ok = _holds(r["direction"], r.get("p_holm"), r["alpha"], pr.get("diff"))
    if r["direction"] == "equivalent":
        lo90, hi90, m = pr.get("lo90"), pr.get("hi90"), r.get("margin")
        if ok:
            txt = "equivalence shown"
        elif lo90 is not None and hi90 is not None and m is not None and (lo90 >= m or hi90 <= -m):
            txt = "different"            # the 90% interval lies wholly outside the margin
        else:
            txt = "inconclusive (equivalence not shown)"
    else:
        if ok:
            txt = "confirmed"
        elif r["direction"] in ("a>b", "a<b") and r.get("p_holm") is not None and r["p_holm"] < r["alpha"] \
                and pr.get("diff") is not None and not _direction_ok_d(r["direction"], pr["diff"]) and pr["diff"] != 0:
            txt = "reversed"             # significant in the opposite direction at the family's alpha
        else:
            txt = "not confirmed"
    if not ok and txt not in ("different", "reversed") and _undetectable(r):
        txt += f" (not detectable at this n: a-priori power {r['apriori']['power']:.2f})"
    if r.get("test") == "iut":
        txt += " (IUT member)"
    if r.get("not_robust_to"):
        txt += " (not robust to " + ", ".join(VARIANT_LABEL.get(o, o) for o in r["not_robust_to"]) + ")"
    if r.get("at_ceiling") and r["direction"] == "equivalent":
        txt += " (suite at ceiling: equivalence is uninformative)"
    if r.get("single_seed"):
        txt += f" ({SINGLE_SEED})"
    return txt


def survives(r: Dict[str, Any], key: str = "p_holm") -> bool:
    """Whether a comparison's claim holds after the given adjustment (and is confirmatory at all)."""
    p = r.get(key)
    return r.get("p_test") is not None and p is not None and p < r["alpha"] and _direction_ok(r) \
        and not r.get("single_seed")


def family_verdicts(conf: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One line per family: for IUT families, confirmed only when every member is."""
    out: List[Dict[str, Any]] = []
    for name in dict.fromkeys(r["family"] for r in conf):
        mem = [r for r in conf if r["family"] == name]
        ev = [r for r in mem if r.get("p_test") is not None]
        held = [r for r in ev if survives(r)]
        robust = [r for r in held if not r.get("not_robust_to")]
        test = mem[0].get("test", "holm")
        if test == "iut":
            if len(ev) < len(mem):
                v = "not evaluable (a member is not evaluable)"
            elif len(held) == len(mem):
                v = "confirmed" + ("" if len(robust) == len(mem) else " (not robust)")
            else:
                v = f"not confirmed ({len(mem) - len(held)} of {len(mem)} members fail)"
        else:
            v = f"{len(held)} of {len(mem)} hold" + (f" ({len(held) - len(robust)} not robust)" if len(robust) < len(held) else "")
            if len(ev) < len(mem):
                v += f"; {len(mem) - len(ev)} not evaluable"
        out.append({"family": name, "test": test, "verdict": v, "decision": mem[0].get("decision", "")})
    return out


# ------------------------------------------------------------------------------------------- confirmatory gate
# Integrity checks: a deviation never excuses these (the run is then not confirmatory at all).
# Kept in step with run.NON_WAIVABLE (report.py stands alone); any freeze_* id is integrity too.
NON_WAIVABLE = ("git_sha_unknown", "git_dirty", "git_untracked", "frozen_ref", "freeze_missing", "freeze_unreadable",
                "freeze_files", "freeze_ids", "freeze_content", "freeze_untracked", "freeze_unstripped",
                "files_unpinned", "plan_missing", "plan_unfrozen", "sealed_only", "items_hash")
OUTPUT_FILES = ("responses.jsonl", "scores.jsonl", "grades.jsonl")


# Checks that change the estimand: a deviation keeps the run honest but the family is then post-hoc, never
# "pre-registered" (conservative default; the owner may decide otherwise in the protocol).
ESTIMAND_CHECKS = ("sampler_override", "scorer_override", "thinking_greedy", "grader_contestant", "grader_family",
                   "budget_policy", "think_budget")


def _run_non_waivable() -> set:
    try:
        import run as _run  # lazy: run.py imports report.py
        return {str(x).casefold() for x in getattr(_run, "NON_WAIVABLE", ())}
    except Exception:
        return set()


def _non_waivable(cid: str) -> bool:
    c = cid.strip().casefold()
    return c in NON_WAIVABLE or c.startswith("freeze_") or c in _run_non_waivable()


def _check_id(c: Any) -> str:
    if isinstance(c, dict):
        return str(c.get("id") or c.get("check") or "")
    m = re.match(r"^\s*([A-Za-z0-9_.\-]+)\s*[:=]", str(c))
    return m.group(1) if m else str(c).strip()


def _dev_ids(devs: Iterable[Any]) -> set:
    out = set()
    for d in devs or []:
        if isinstance(d, dict):
            out.add(str(d.get("check") or d.get("id") or ""))
        else:
            m = re.match(r"^\s*([A-Za-z0-9_.\-]+)\s*=", str(d))
            if m:
                out.add(m.group(1))
    return out


def accepted_deviations(runs: Sequence[Dict[str, Any]]) -> List[str]:
    """'run: check_id = text' for every deviation the runs declared."""
    out = []
    for run in runs:
        name = run.get("_dir") or run.get("model", "?")
        for d in (run.get("protocol") or {}).get("deviations") or []:
            if isinstance(d, dict):
                out.append(f"{name}: {d.get('id') or d.get('check')} = {d.get('text') or d.get('reason') or ''}".rstrip(" ="))
            else:
                out.append(f"{name}: {d}")
    return out


def _val(x: Any) -> Any:
    return x.get("value") if isinstance(x, dict) and "value" in x else x


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _manifests(run: Dict[str, Any], root: Path) -> List[Tuple[str, Optional[Dict[str, Any]], Optional[str]]]:
    """(path, manifest JSON or None, recorded sha) for every freeze manifest a run names."""
    prot = run.get("protocol") or {}
    seen, out = set(), []
    for fr in [prot.get("freeze")] + list(prot.get("freezes") or []):
        if not fr or not fr.get("path") or fr["path"] in seen:
            continue
        seen.add(fr["path"])
        p = Path(fr["path"])
        p = p if p.is_absolute() else Path(root) / p
        try:
            out.append((str(p), json.loads(p.read_text(encoding="utf-8")), fr.get("sha256")))
        except (OSError, json.JSONDecodeError):
            out.append((str(p), None, fr.get("sha256")))
    return out


STAGE2_PREFIX = "evals/plans/stage2/"
CODE_FILES = {"report.py": "report.py", "stats.py": "stats.py", "grading.py": "grading.py", "audit.py": "datasets/audit.py",
              "scorers.py": "scorers.py", "run.py": "run.py"}
ANALYSIS_KEYS = ("min_n", "n_boot", "n_perm", "seed", "alpha")


def _is_stage2(path: str) -> bool:
    p = str(path).replace("\\", "/")
    return STAGE2_PREFIX in p or "/stage2/" in p


def _ids_sha(ids: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(sorted(ids)).encode("utf-8")).hexdigest()


def run_conditions(run: Dict[str, Any], root: Path, want_plan: Optional[str]) -> List[str]:
    """Conditions on one run (each reason starts with the run's name)."""
    reasons: List[str] = []
    name = run.get("_dir") or run.get("model", "?")
    prot = run.get("protocol") or {}
    rd = run.get("_path")
    if prot.get("mode") != "confirmatory":
        reasons.append(f"{name}: protocol.mode is {prot.get('mode')!r}, not 'confirmatory'")
    if prot.get("invalid"):
        reasons.append(f"{name}: the run is marked invalid ({prot.get('invalid')})")
    if not run.get("ended"):
        reasons.append(f"{name}: the run did not finish (no 'ended'; interrupted or partial)")
    devs = {d.casefold() for d in _dev_ids(prot.get("deviations"))}
    for chk in prot.get("checks_failed") or []:
        cid = _check_id(chk).strip()
        if _non_waivable(cid):
            reasons.append(f"{name}: integrity check failed (never waivable): {cid}")
        elif cid.casefold() in ESTIMAND_CHECKS:
            reasons.append(f"{name}: estimand-changing check accepted as a deviation (post-hoc): {cid}")
        elif cid.casefold() not in devs:
            reasons.append(f"{name}: failed check without its own deviation: {cid}")
    if prot.get("original") is not None or run.get("rescored") or \
            (rd and (Path(rd) / "scores.confirmatory.jsonl").exists()):
        reasons.append(f"{name}: the run was rescored (protocol.original, rescored history or "
                       "scores.confirmatory.jsonl present): its scores are not the pre-registered pipeline's")
    outs = prot.get("outputs_sha256")      # the protocol record's own hashes only (never the top-level copy)
    if not outs:
        reasons.append(f"{name}: protocol.outputs_sha256 missing (responses/scores are not bound to the record)")
    elif rd:
        for fn in OUTPUT_FILES:
            if fn not in outs and fn.split(".")[0] not in outs:
                if fn != "grades.jsonl":
                    reasons.append(f"{name}: protocol.outputs_sha256 does not list {fn}")
                continue
            want_sha = outs.get(fn, outs.get(fn.split(".")[0]))
            f = Path(rd) / fn
            if (sha256_file(f) if f.is_file() else None) != want_sha:
                reasons.append(f"{name}: {fn} does not match protocol.outputs_sha256")
    sc = ((run.get("serving") or {}).get("sampler_check") or {})
    if sc.get("ok") is not True:
        reasons.append(f"{name}: serving.sampler_check.ok is {sc.get('ok')!r}")
    seed = seed_of(run)
    m = re.search(r"(?:seed[-_]?|-s)(\d+)(?:\D|$)", str(run.get("_dir") or ""))
    if m and str(seed) != m.group(1):
        reasons.append(f"{name}: the dir name says seed {m.group(1)} but sampling.seed is {seed}")
    for st in prot.get("stage2") or []:
        stp = str(st)
        base = stp.rsplit("/", 1)[-1].casefold()
        if not stp.startswith(STAGE2_PREFIX) or not ("prospective" in base or "calibration" in base):
            reasons.append(f"{name}: protocol.stage2 touches {stp} (only prospective or calibration files under "
                           f"{STAGE2_PREFIX} are allowed)")
    frozen = set()
    mans = _manifests(run, root)
    if not mans:
        reasons.append(f"{name}: no freeze manifest recorded")
    for path, man, rec_sha in mans:
        if man is None:
            reasons.append(f"{name}: freeze manifest unreadable ({Path(path).name})")
            continue
        if rec_sha and sha256_file(Path(path)) != rec_sha:
            reasons.append(f"{name}: freeze manifest changed since the run ({Path(path).name})")
        c = man.get("comparisons_sha256") or ((man.get("comparisons") or {}).get("sha256")
                                               if isinstance(man.get("comparisons"), dict) else None)
        if _is_stage2(path):
            if c and c != want_plan:
                reasons.append(f"{name}: stage-2 manifest {Path(path).name} carries another comparisons_sha256")
            bad = [k for k in (man.get("suites") or {}) if "prospective" not in str(k)]
            bad += [k for k in (man.get("files") or {}) if not any(t in str(k).casefold()
                                                                   for t in ("prospective", "calibration"))]
            if bad:
                reasons.append(f"{name}: stage-2 manifest {Path(path).name} pins non-prospective entries "
                               f"({', '.join(map(str, bad[:4]))})")
        elif c:
            frozen.add(c)
    if want_plan is None:
        reasons.append("no comparisons file")
    elif want_plan not in frozen:
        reasons.append(f"{name}: comparisons file sha256 is not the one frozen in the tag-time manifest")
    return reasons


def coverage_conditions(run: Dict[str, Any], rec: Dict[str, Dict[str, Any]]) -> List[str]:
    """Every selected (frozen) id of every suite the run recorded in item_sets has a response."""
    name = run.get("_dir") or run.get("model", "?")
    out = []
    sets = run.get("item_sets") or {}
    if not sets:
        return [f"{name}: run.json has no item_sets (cannot check coverage of the frozen ids)"]
    for suite, want in sets.items():
        ids = [k for k, x in rec.items() if x["suite"] == suite and x["status"] != "unscored"]
        n = (want or {}).get("n")
        sha = (want or {}).get("ids_sha256")
        if n is not None and len(ids) != n:
            out.append(f"{name}: {suite} has {len(ids)} scored items of {n} selected (partial run)")
        elif sha and _ids_sha(ids) != sha:
            out.append(f"{name}: {suite} scored ids differ from the selected ids (item_sets.ids_sha256)")
    return out


def data_conditions(runs: Sequence[Dict[str, Any]], loaded: Dict[str, List[str]], items: Sequence[Path],
                    root: Path) -> List[str]:
    """Every file the report loaded items from must be the file the runs read or a pinned annotation.
    loaded: {run name: [resolved dataset paths the report read for that run]}."""
    out = []
    pins: set = set()
    for run in runs:
        for path, man, _ in _manifests(run, root):
            for key in ("files", "annotations"):
                v = (man or {}).get(key)
                if isinstance(v, dict):
                    pins.update(str(x) for x in v.values())
    run_shas = {ds.get("sha256") for run in runs for ds in run.get("datasets") or [] if ds.get("sha256")}
    for run in runs:
        name = run.get("_dir") or run.get("model", "?")
        want = {Path(ds.get("path", "")).name: ds.get("sha256") for ds in run.get("datasets") or []}
        found = {Path(p).name: p for p in loaded.get(name, [])}
        for base, sha in want.items():
            if base not in found:
                out.append(f"{name}: data file {base} not found under --data-root (cannot verify item metadata)")
            elif sha256_file(Path(found[base])) != sha:
                out.append(f"{name}: data file {base} differs from run.json datasets sha256")
    for f in items or []:
        h = sha256_file(Path(f))
        if h not in pins and h not in run_shas:
            out.append(f"{Path(f).name}: --items annotation file not pinned in any freeze manifest")
    return out


def analysis_conditions(runs: Sequence[Dict[str, Any]], root: Path, analysis: Optional[Dict[str, Any]]) -> List[str]:
    """The tag-time manifest pins the analysis knobs and the code; the running report must match both. Without
    explicit settings (a direct call), the running code's defaults are what would be used, so those are checked."""
    out = []
    if analysis is None:
        analysis = {"min_n": MIN_N, "n_boot": ST.DEFAULT_BOOT, "n_perm": ST.DEFAULT_PERM, "seed": ST.DEFAULT_SEED}
    pinned_a, pinned_c = None, None
    for run in runs:
        for path, man, _ in _manifests(run, root):
            if man and not _is_stage2(path):
                pinned_a = pinned_a or man.get("analysis")
                pinned_c = pinned_c or man.get("code_sha256")
    if not isinstance(pinned_a, dict):
        out.append("the freeze manifest pins no analysis settings (analysis with min_n, n_boot, n_perm, seed, alpha)")
    else:
        for k in ANALYSIS_KEYS:
            if k in pinned_a and analysis is not None and k in analysis and \
                    not math.isclose(float(pinned_a[k]), float(analysis[k])):
                out.append(f"analysis.{k} is {analysis[k]} here but {pinned_a[k]} in the freeze manifest")
            elif k not in pinned_a and k != "n_perm":
                out.append(f"the freeze manifest does not pin analysis.{k}")
    if not isinstance(pinned_c, dict):
        out.append("the freeze manifest pins no code_sha256 for report.py, stats.py, grading.py, audit.py, scorers.py, "
                   "run.py")
    else:
        for key, rel_ in CODE_FILES.items():
            want = pinned_c.get(key) or pinned_c.get(f"evals/{rel_}") or pinned_c.get(rel_)
            f = EVALS_DIR / rel_
            have = sha256_file(f) if f.is_file() else None
            if not want:
                out.append(f"the freeze manifest does not pin {key}")
            elif have != want:
                out.append(f"the running {key} differs from the pinned code_sha256")
    return out


def comparison_conditions(r: Dict[str, Any], summaries: List[Dict[str, Any]]) -> List[str]:
    """Equality across the runs of one comparison, and seed-group seeds."""
    idx = r.get("runs") or []
    if not idx:
        return []
    runs = [summaries[i]["run"] for i in idx]
    out = []
    same = {
        "budget.policy_sha256": {(x.get("budget") or {}).get("policy_sha256") for x in runs},
        "scorer_version": {json.dumps(_val(x.get("scorer_version")), sort_keys=True, default=str) for x in runs},
        "git.sha": {(x.get("git") or {}).get("sha") for x in runs},
        "frozen ref": {(x.get("protocol") or {}).get("frozen_sha") or (x.get("protocol") or {}).get("frozen_ref")
                       or (x.get("protocol") or {}).get("harness_ref") for x in runs},
        "harness_sha256": {json.dumps(x.get("harness_sha256"), sort_keys=True, default=str) for x in runs},
    }
    for k, vals in same.items():
        if len(vals) != 1 or None in vals or "null" in vals:
            out.append(f"{r['id']}: {k} missing or not equal across its runs")
    want_iv = r.get("interventions") or {}
    for key, side_idx in (("a", r.get("a_idx") or []), ("b", r.get("b_idx") or [])):
        if key not in want_iv:
            continue
        for i in side_idx:
            got = sorted(str(iv.get("name") if isinstance(iv, dict) else iv)
                         for iv in ((summaries[i]["run"].get("protocol") or {}).get("interventions") or []))
            if got != sorted(map(str, want_iv[key])):
                out.append(f"{r['id']}: {summaries[i]['run_dir']} ran interventions {got}, the plan declares "
                           f"{sorted(map(str, want_iv[key]))} for side {key}")
    for side in ("a_spec", "b_spec"):
        spec = r.get(side)
        if isinstance(spec, list):
            members = [i for i, s in enumerate(summaries) if s["run_dir"] in spec]
            seeds = {str(seed_of(summaries[i]["run"])) for i in members}
            declared = {str(x) for i in members for x in ((summaries[i]["run"].get("protocol") or {}).get("seeds") or [])}
            if not declared or seeds != declared:
                out.append(f"{r['id']}: seed group seeds {sorted(seeds)} are not the declared --seeds {sorted(declared)}")
    return out


_PLAN_CAL: Dict[str, Any] = {}


def _calibration_verdict(runs: Sequence[Dict[str, Any]], root: Path) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """The calibration verdict pinned in a (stage-2 calibration) manifest: (verdict JSON, problems). The plan's
    calibration_verdict.pinned_by names that manifest; otherwise any manifest a run recorded is searched."""
    mans = []
    pb = _PLAN_CAL.get("pinned_by")
    if pb:
        p = Path(pb) if Path(pb).is_absolute() else Path(root) / pb
        try:
            mans.append((str(p), json.loads(p.read_text(encoding="utf-8")), None))
        except (OSError, json.JSONDecodeError):
            return None, [f"the plan's calibration manifest {pb} is missing or unreadable"]
    for run in runs:
        mans += _manifests(run, root)
    for path, man, _ in mans:
        if True:
            cv = (man or {}).get("calibration_verdict")
            if isinstance(cv, dict) and cv.get("path"):
                p = Path(cv["path"])
                p = p if p.is_absolute() else Path(root) / p
                try:
                    if cv.get("sha256") != sha256_file(p):
                        return None, ["the calibration verdict file differs from its pinned sha256"]
                    return json.loads(p.read_text(encoding="utf-8")), []
                except (OSError, json.JSONDecodeError):
                    return None, ["the pinned calibration verdict file is unreadable"]
    return None, ["no calibration verdict pinned in a (stage-2 calibration) freeze manifest"]


def judge_conditions(r: Dict[str, Any], summaries: List[Dict[str, Any]], recs: List[Dict[str, Dict[str, Any]]],
                     root: Path) -> List[str]:
    """Model-graded comparisons: one grader and judge prompt across the runs, a grader independent of every
    contestant, and a pinned calibration verdict that licenses exactly this judge (verdict exactly 'pass'; judge
    model, served identity and prompt sha equal to the runs' grader records; key, grades and kappa recorded)."""
    idx = r.get("runs") or []
    if not any(x.get("scorer") == "llm_judge" for i in idx for x in recs[i].values() if x["suite"] == r["suite"]):
        return []
    out: List[str] = []
    graders = [(summaries[i]["run"].get("grader") or {}) for i in idx]
    models = {g.get("model") for g in graders}
    prompts = {g.get("prompt_sha256") for g in graders}
    if len(models) != 1 or None in models:
        out.append(f"{r['id']}: model-graded, but the grader differs across its runs or is unrecorded")
    if len(prompts) != 1 or None in prompts:
        out.append(f"{r['id']}: model-graded, but the judge prompt sha256 differs across its runs or is unrecorded")
    g = graders[0].get("model") if graders else None
    ident = ((graders[0].get("served_identity") or {}).get(g) or {}) if graders else {}
    try:
        import grading
        idents = {g: ident} if ident else {}
        cands = [summaries[i]["run"].get("model") for i in idx]
        for i in idx:
            st = (summaries[i]["run"].get("serving") or {})
            mf = _val(st.get("model_file")) if isinstance(st, dict) else None
            if mf:
                idents[summaries[i]["run"].get("model")] = {"model_file": mf}
        if g and grading.family_overlap([g], cands, identities=idents):
            out.append(f"{r['id']}: the grader {g} is not independent of a contestant (family overlap)")
    except Exception as exc:  # fail closed
        out.append(f"{r['id']}: grader independence not checkable ({exc.__class__.__name__})")
    v, probs = _calibration_verdict([summaries[i]["run"] for i in idx], root)
    out += [f"{r['id']}: {p}" for p in probs]
    if v is not None:
        out += [f"{r['id']}: {p}" for p in verdict_problems(v, g, ident, prompts, idx, summaries)]
        if v.get("d1_scope") == "extract_only" and "extract_only" not in ((r.get("spec") or {}).get("ops") or ()):
            out.append(f"{r['id']}: the calibration licenses extract items only, but the comparison was not "
                       "restricted to them")
    return out


VERDICT_SCHEMA = "judge-calibration-verdict-v1"


def verdict_problems(v: Dict[str, Any], grader: Optional[str], ident: Dict[str, Any], prompts: set,
                     idx: Sequence[int], summaries: List[Dict[str, Any]]) -> List[str]:
    """Problems binding a calibration verdict (JUDGE's judge-calibration-verdict-v1) to the D1 runs' grader."""
    out = []
    if v.get("schema") != VERDICT_SCHEMA:
        out.append(f"calibration verdict schema is {v.get('schema')!r}, not {VERDICT_SCHEMA}")
    if v.get("verdict") != "pass":                 # exactly 'pass': no stripping, no case folding
        out.append(f"the calibration verdict is {v.get('verdict')!r}, not exactly 'pass'")
    if grader not in (v.get("judge_models") or []):
        out.append(f"the calibration licensed judge(s) {v.get('judge_models')}, not the grader {grader!r}")
    vp = v.get("judge_prompt_sha256")
    if isinstance(vp, list) or len(prompts) != 1 or vp not in prompts:
        out.append("the calibration judge prompt sha256 is not the runs' judge prompt")
    vid = (v.get("judge_served_identity") or {}).get(grader) or {}
    keys = [k for k in ("model_sha256", "sha256", "model_file") if vid.get(k)]
    if not keys:
        out.append("the calibration verdict records no served identity (weights sha or file) for the grader")
    elif not ident or not any(ident.get(k) == vid.get(k) for k in keys) or \
            any(ident.get(k) not in (None, vid.get(k)) for k in keys):
        out.append("the calibration judge's served identity is not the runs' grader identity")
    if not v.get("calibration_key_sha256"):
        out.append("the calibration verdict lacks calibration_key_sha256")
    gs = v.get("grades_sha256")
    if not gs or not v.get("grades_sha256_combined"):
        out.append("the calibration verdict lacks grades_sha256")
    elif isinstance(gs, dict):
        for i in idx:
            name = summaries[i]["run_dir"]
            if name in gs:
                have = ((summaries[i]["run"].get("protocol") or {}).get("outputs_sha256") or {}).get("scores.jsonl")
                if gs[name] != have:
                    out.append(f"the calibration grades sha for {name} is not that run's scores sha")
    if not v.get("kappa_results"):
        out.append("the calibration verdict lacks kappa_results")
    return out


def calibration_scope(conf_plan: Dict[str, Any], summaries: List[Dict[str, Any]], root: Path) -> Optional[str]:
    """The d1_scope of the pinned calibration verdict found through any run's manifests, if any."""
    runs = [s_["run"] for s_ in summaries]
    v, _ = _calibration_verdict(runs, root)
    return (v or {}).get("d1_scope")


ATTEMPTS_LEDGER = "evals/plans/attempts-C1.jsonl"


def ledger_conditions(runs: Sequence[Dict[str, Any]], root: Path,
                      ledger: Optional[str] = None) -> Tuple[List[str], List[str]]:
    """(reasons, deviations) from the append-only attempt ledger: each run must be the FIRST complete attempt of
    its run id (start + end, same started time and outputs); other attempts of that id are listed as deviations."""
    p = Path(ledger or ATTEMPTS_LEDGER)
    p = p if p.is_absolute() else Path(root) / p
    lines = read_jsonl(p) if p.is_file() else []
    reasons: List[str] = []
    devs: List[str] = []
    if not lines:
        return [f"no attempt ledger ({p.name}) to show which attempt each run is"], []
    for run in runs:
        name = run.get("_dir") or "?"
        mine = [x for x in lines if x.get("run_id") == name]
        starts = [x for x in mine if x.get("event") == "start"]
        ends = [x for x in mine if x.get("event") == "end"]
        complete = [st for st in starts if any(e.get("started") == st.get("started") for e in ends)]
        if not complete:
            reasons.append(f"{name}: no complete attempt in the ledger")
            continue
        first = complete[0]
        if first.get("started") != run.get("started"):
            reasons.append(f"{name}: not the first complete attempt in the ledger (attempt shopping)")
        end = next(e for e in ends if e.get("started") == first.get("started"))
        outs = end.get("outputs_sha256") or {}
        mine_outs = (run.get("protocol") or {}).get("outputs_sha256") or {}
        if outs and any(outs.get(k) != mine_outs.get(k) for k in ("responses.jsonl", "scores.jsonl")):
            reasons.append(f"{name}: outputs differ from the ledger's record of the first complete attempt")
        others = len(starts) - 1
        if others > 0:
            devs.append(f"{name}: attempts = {others} other attempt(s) of this run id are in the ledger")
    return reasons, devs


LEDGER_DEVIATIONS: List[str] = []


def family_gates(conf: List[Dict[str, Any]], comparisons_path: Optional[Path], root: Path,
                 summaries: List[Dict[str, Any]], recs: List[Dict[str, Dict[str, Any]]],
                 items: Sequence[Path] = (), loaded: Optional[Dict[str, List[str]]] = None,
                 analysis: Optional[Dict[str, Any]] = None,
                 plan: Optional[Dict[str, Any]] = None) -> Tuple[List[str], Dict[str, List[str]]]:
    """(report-level reasons, {family: its own reasons}). A family is pre-registered only when both are empty."""
    want = sha256_file(comparisons_path) if comparisons_path else None
    used = sorted({i for r in conf for i in r.get("runs", [])})
    runs = [dict(summaries[i]["run"], _dir=summaries[i]["run_dir"], _path=summaries[i]["path"]) for i in used]
    glob: List[str] = []
    if not runs:
        glob.append("no runs")
    if loaded is not None:
        glob += data_conditions(runs, loaded, items, root)
    else:
        glob += data_conditions(runs, {}, items, root)[:0] + [
            f"{Path(f).name}: --items annotation file not pinned in any freeze manifest"
            for f in items if sha256_file(Path(f)) not in
            {str(x) for run in runs for _, m, _ in _manifests(run, root) for k in ("files", "annotations")
             for x in (((m or {}).get(k) or {}).values() if isinstance((m or {}).get(k), dict) else [])}]
    glob += analysis_conditions(runs, root, analysis)
    if plan is not None:
        _PLAN_CAL.clear()
        _PLAN_CAL.update(plan.get("calibration_verdict") or {})
        pa = plan.get("analysis") or {}
        for k in ANALYSIS_KEYS:
            if k in pa and analysis is not None and k in analysis and not math.isclose(float(pa[k]), float(analysis[k])):
                glob.append(f"analysis.{k} is {analysis[k]} here but {pa[k]} in the plan")
    lr, ldev = ledger_conditions(runs, root, (plan or {}).get("attempts_ledger"))
    glob += lr
    LEDGER_DEVIATIONS[:] = ldev
    per_run = {i: run_conditions(dict(summaries[i]["run"], _dir=summaries[i]["run_dir"], _path=summaries[i]["path"]),
                                 root, want) + coverage_conditions(
                                     dict(summaries[i]["run"], _dir=summaries[i]["run_dir"]), recs[i])
               for i in used}
    fams: Dict[str, List[str]] = {}
    for r in conf:
        reasons = fams.setdefault(r["family"], [])
        for i in r.get("runs", []):
            reasons += per_run.get(i, [])
        if r.get("p_test") is None:
            reasons.append(f"registered comparison {r['id']} is {r.get('verdict', 'not evaluable')}")
        reasons += comparison_conditions(r, summaries)
        reasons += judge_conditions(r, summaries, recs, root)
    return sorted(set(glob), key=glob.index), {k: sorted(set(v), key=v.index) for k, v in fams.items()}


def confirmatory_gate(runs: Sequence[Dict[str, Any]], comparisons_path: Optional[Path],
                      root: Path = REPO_DIR, items: Optional[Sequence[Path]] = None,
                      conf: Optional[List[Dict[str, Any]]] = None, plan: Optional[Dict[str, Any]] = None,
                      summaries: Optional[List[Dict[str, Any]]] = None,
                      recs: Optional[List[Dict[str, Dict[str, Any]]]] = None,
                      analysis: Optional[Dict[str, Any]] = None,
                      loaded: Optional[Dict[str, List[str]]] = None,
                      seed: Optional[int] = None, n_boot: Optional[int] = None) -> List[str]:
    """All reasons (report-level and every family's) as one list; empty = every family is pre-registered.
    Without comparison results, the run-level conditions of ``runs`` (each with _dir/_path) are checked."""
    want = sha256_file(comparisons_path) if comparisons_path else None
    if seed is not None or n_boot is not None:      # the resampling settings this report uses (r4 loophole D3)
        analysis = dict(analysis or {"min_n": MIN_N, "n_perm": ST.DEFAULT_PERM},
                        seed=seed if seed is not None else ST.DEFAULT_SEED,
                        n_boot=n_boot if n_boot is not None else ST.DEFAULT_BOOT)
    if conf is not None and summaries is not None and recs is not None:
        g, fams = family_gates(conf, comparisons_path, root, summaries, recs, items or [], loaded, analysis, plan)
        out = g + [x for v in fams.values() for x in v]
        return sorted(set(out), key=out.index)
    out = []
    for run in runs:
        out += run_conditions(run, root, want)
    pseudo = [{"run": run, "run_dir": run.get("_dir", "?")} for run in runs]
    if len(runs) > 1:
        out += comparison_conditions({"id": "all runs", "runs": list(range(len(runs)))}, pseudo)
    out += analysis_conditions(runs, root, analysis)
    out += data_conditions(runs, loaded or {}, items or [], root) if loaded is not None else [
        f"{Path(f).name}: --items annotation file not pinned in any freeze manifest"
        for f in items or [] if sha256_file(Path(f)) not in
        {str(x) for run in runs for _, m, _ in _manifests(run, root) for k in ("files", "annotations")
         for x in (((m or {}).get(k) or {}).values() if isinstance((m or {}).get(k), dict) else [])}]
    return sorted(set(out), key=out.index)


# ------------------------------------------------------------------------------------------- formatting
def unique_labels(summaries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Runs that differ only by seed (or by anything else) get distinct labels: '... seed N', then the dir name."""
    def dups():
        seen: Dict[str, int] = {}
        for s in summaries:
            seen[s["label"]] = seen.get(s["label"], 0) + 1
        return {k for k, v in seen.items() if v > 1}
    d = dups()
    for s in summaries:
        seed = (s["run"].get("sampling") or {}).get("seed")
        if s["label"] in d and seed is not None:
            s["label"] = f"{s['label']} seed {seed}"
    d = dups()
    for s in summaries:
        if s["label"] in d:
            s["label"] = f"{s['label']} [{s['run_dir']}]"
    return summaries


def run_label(run: Dict[str, Any]) -> str:
    model = run.get("model", "?")
    think = run.get("thinking")
    if think in ("on", "off"):
        return f"{model} (think {think})"
    return str(model)


def _fmt(x: Optional[float], spec: str = ".3f") -> str:
    return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else format(x, spec)


def _pp(x: Optional[float], signed: bool = True) -> str:
    """A proportion difference in percentage points."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    return f"{x * 100:+.1f}" if signed else f"{x * 100:.1f}"


def _p(x: Optional[float], n_boot: Optional[int] = None) -> str:
    if x is None:
        return "-"
    if n_boot and x < 1.0 / n_boot:
        return f"< 1/{n_boot}"
    return "<0.001" if x < 0.001 else f"{x:.3f}"


def _p_test_cell(r: Dict[str, Any]) -> str:
    """The deciding p with its Monte Carlo SE when a bootstrap decides."""
    pr = r.get("primary") or {}
    eq = r["direction"] == "equivalent"
    boot = pr.get("p_is_boot") and pr.get("n_boot")
    if eq and pr.get("p_tost_mcse") is not None:
        return f"{_p(r['p_test'], pr.get('n_boot'))} (MC se {pr['p_tost_mcse']:.4f})"
    if not eq and boot and pr.get("p_mcse") is not None:
        return f"{_p(r['p_test'], pr.get('n_boot'))} (MC se {pr['p_mcse']:.4f})"
    return _p(r["p_test"])


def _effect(r: Dict[str, Any], metric: str, cluster: bool = False) -> str:
    """'+12.0 pp [3.1, 20.5]' for proportions, '-0.52 [-0.80, -0.21]' for MAE / mean score."""
    if not r or not r.get("n"):
        return "-"
    lo, hi = (r.get("cluster_lo"), r.get("cluster_hi")) if cluster else (r.get("lo"), r.get("hi"))
    if metric in BINARY_METRICS:
        return f"{_pp(r['diff'])} pp [{_pp(lo)}, {_pp(hi)}]"
    return f"{_fmt(r['diff'], '+.2f')} [{_fmt(lo, '+.2f')}, {_fmt(hi, '+.2f')}]"


def _mde(r: Dict[str, Any], metric: str) -> str:
    m = r.get("mde")
    if m is None:
        return "-"
    return f"{m * 100:.1f} pp" if metric in BINARY_METRICS else f"{m:.2f}"


COLUMNS = ["suite", "run", "n", "passed", "accuracy", "ci95_lo", "ci95_hi", "mean_score", "mae",
           "mean_latency_s", "tokens_per_s", "api_errors", "truncated", "unparsed", "item_or_grader_errors",
           "model_graded", "cost_usd", "opener_refusals", "opener_n", "opener_refusal_rate",
           "error_policy", "accuracy_excl_errors", "n_ok", "unparsed_truncated", "final_answer_prompts", "single_seed",
           "mae_impute", "n_mae_unimputed"]
PAIR_COLUMNS = ["suite", "run_a", "run_b", "n_shared", "a_passed", "b_passed", "a_only", "b_only", "p_mcnemar",
                "diff", "diff_lo", "diff_hi", "mde80", "label"]
CONF_COLUMNS = ["id", "family", "test", "suite", "metric", "direction", "margin", "primary", "a", "b", "n", "a_mean",
                "b_mean", "diff", "lo", "hi", "p", "p_holm", "q_bh", "p_holm_all", "mde80", "tost_power", "robust",
                "verdict", "section"]


def rows(summaries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    suite_names = sorted({n for s in summaries for n in s["suites"]})
    for suite in suite_names:
        for s in summaries:
            st = s["suites"].get(suite)
            if not st:
                continue
            out.append({
                "suite": suite, "run": s["label"], "run_dir": s["run_dir"], "n": st.get("n_denominator", st["n_scored"]),
                "passed": st["passed"], "accuracy": st["accuracy"], "ci95_lo": st["ci95"][0], "ci95_hi": st["ci95"][1],
                "mean_score": st["mean_value"], "mae": st["mae"], "mean_latency_s": st["mean_latency_s"],
                "tokens_per_s": st["tokens_per_s"], "api_errors": st["api_errors"], "truncated": st["truncated"],
                "unparsed": st["unparsed"], "item_or_grader_errors": st["item_or_grader_errors"],
                "final_answer_prompts": st.get("final_answer_prompts", 0),
                "model_graded": st["model_graded"], "cost_usd": st["cost_usd"],
                "opener_refusals": st.get("opener_refusals"), "opener_n": st.get("n_opener"),
                "opener_refusal_rate": st.get("opener_refusal_rate"),
                "error_policy": st.get("error_policy", "fail"), "accuracy_excl_errors": st.get("accuracy_excl_errors"),
                "accuracy_alt": st.get("accuracy_alt"), "n_ok": st.get("n_ok"),
                "unparsed_truncated": st.get("unparsed_truncated", 0), "single_seed": bool(s.get("single_seed")),
                "tiers": st.get("tiers"), "declines": st.get("declines"), "mae_impute": st.get("mae_impute"),
                "n_mae_unimputed": st.get("n_mae_unimputed", 0),
            })
    return out


def render_markdown(summaries: List[Dict[str, Any]], title: str = "Eval report",
                    pairs: Optional[List[Dict[str, Any]]] = None, *,
                    conf: Optional[List[Dict[str, Any]]] = None, plan: Optional[Dict[str, Any]] = None,
                    gate: Optional[List[str]] = None, deviations: Optional[List[str]] = None,
                    gate_glob: Optional[List[str]] = None, fam_gates: Optional[Dict[str, List[str]]] = None,
                    groups: Optional[List[Dict[str, Any]]] = None,
                    group_pairs: Optional[List[Dict[str, Any]]] = None,
                    base: Optional[Dict[str, List[Dict[str, Any]]]] = None,
                    sens_rows: Optional[Dict[str, List[Dict[str, Any]]]] = None,
                    extras: Optional[Dict[str, Dict[str, Any]]] = None,
                    aggregate: Optional[Dict[str, Any]] = None,
                    errors: str = "fail") -> str:
    lines = [f"# {title}", ""]
    lines.append("| Run | Dir | Model | Thinking | Temp | Seed | Git | Started (UTC) | Items |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for s in summaries:
        r = s["run"]
        samp = r.get("sampling") or {}
        n = sum(st["n_items"] for k, st in s["suites"].items() if "[label_source=" not in k)
        flag = f" ({SINGLE_SEED})" if s.get("single_seed") else ""
        lines.append(f"| {s['label']}{flag} | `{s['run_dir']}` | `{r.get('model', '?')}` | {r.get('thinking', '-')} | "
                     f"{samp.get('temperature', '-')} | {samp.get('seed', '-')} | `{(r.get('git') or {}).get('sha', '?')[:10]}"
                     f"{'+dirty' if (r.get('git') or {}).get('dirty') else ''}` | {r.get('started', '-')} | {n} |")
    lines.append("")
    if conf is not None:
        lines += _render_confirmatory(conf, plan or {}, gate if gate is not None else ["gate not evaluated"],
                                      deviations, gate_glob, fam_gates)
    if aggregate:
        lines += _render_aggregate(aggregate)
    if groups:
        lines += _render_groups(summaries, groups)
    graded = False
    alt_name = "excl. err" if errors == "fail" else "err = fail"
    for suite in sorted({n for s in summaries for n in s["suites"]}):
        lines += [f"## {suite}", "",
                  f"| Run | n | Passed | Accuracy | 95% CI | Acc. ({alt_name}) | Mean score | Latency (s) | Tok/s | "
                  "ok / unparsed (trunc) / trunc / grader-item err / api err | final-answer prompts |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        unimp = 0
        for row in [x for x in rows(summaries) if x["suite"] == suite]:
            acc = "-" if row["accuracy"] is None else f"{row['accuracy']:.1%}"
            ci = "-" if row["accuracy"] is None else f"{row['ci95_lo']:.1%} – {row['ci95_hi']:.1%}"
            alt = "-" if row.get("accuracy_alt") is None else f"{row['accuracy_alt']:.1%}"
            parts = []
            if row["mean_score"] is not None:
                parts.append(_fmt(row["mean_score"]))
            if row["mae"] is not None:
                pr_ = f" (parsed {row['n_ok'] / row['n']:.0%})" if row.get("n_ok") is not None and row["n"] else ""
                parts.append(f"MAE {_fmt(row['mae'], '.2f')}{pr_}")
            unimp += row.get("n_mae_unimputed") or 0
            ms = "; ".join(parts) or "-"
            if row["model_graded"]:
                ms += " *"
                graded = True
            flag = " †" if row.get("single_seed") else ""
            lines.append(f"| {row['run']}{flag} | {row['n']} | {row['passed']} | {acc} | {ci} | {alt} | {ms} | "
                         f"{_fmt(row['mean_latency_s'], '.2f')} | {_fmt(row['tokens_per_s'], '.1f')} | "
                         f"{row.get('n_ok', '-')} / {row['unparsed']} ({row.get('unparsed_truncated', 0)}) / "
                         f"{row['truncated']} / {row['item_or_grader_errors']} / {row['api_errors']} | "
                         f"{row.get('final_answer_prompts', 0)} |")
        for b in (base or {}).get(suite, []):
            acc = f"{b['accuracy']:.1%}" if b.get("accuracy") is not None else "-"
            ms = f"MAE {b['mae']:.2f}" if b.get("mae") is not None else "-"
            if b.get("balanced") is not None:
                ms = f"letter-balanced {b['balanced']:.1%}"
            tag = " (oracle: uses this set's labels)" if b["oracle"] else ""
            lines.append(f"| _baseline: {b['name']}{tag}_ | {b['n']} | - | {acc} | - | - | {ms} | - | - | - | - |")
        lines.append("")
        if any(x["mae"] is not None for x in rows(summaries) if x["suite"] == suite):
            imp = next((x["mae_impute"] for x in rows(summaries) if x["suite"] == suite), "max")
            lines += [f"MAE: items with no parsed prediction are imputed as `{imp}`"
                      + (f" ({unimp} left out)" if unimp else "") + ".", ""]
        lines += _render_extras((extras or {}).get(suite) or {})
        if sens_rows and sens_rows.get(suite):
            lines += _render_sens_rows(sens_rows[suite])
        opener = [x for x in rows(summaries) if x["suite"] == suite and x.get("opener_n")]
        if opener:
            lines += ["Opener refusal (the reply opens with a refusal, even if a code block then counts it as "
                      "compliance above):", ""]
            for row in opener:
                lo, hi = wilson(row["opener_refusals"], row["opener_n"])
                lines.append(f"- {row['run']}: {row['opener_refusals']}/{row['opener_n']} = "
                             f"{row['opener_refusal_rate']:.1%} ({lo:.1%} – {hi:.1%})")
            lines.append("")
        tiers = [x for x in rows(summaries) if x["suite"] == suite and x.get("tiers")]
        if tiers:
            lines += ["Refusal tiers (truncated replies excluded): full / partial / refuse.", ""]
            for row in tiers:
                t = row["tiers"]
                n = sum(t.get(k, 0) for k in ("full", "partial", "refuse"))
                if n:
                    dk = row.get("declines") or {}
                    dtxt = (" ; declines: " + ", ".join(f"{k} {v}" for k, v in sorted(dk.items()))) if dk else ""
                    lines.append(f"- {row['run']}: " + " / ".join(f"{t.get(k, 0) / n:.1%}" for k in ("full", "partial", "refuse"))
                                 + f" (n {n}){dtxt}")
            lines.append("")
    if pairs:
        lines += ["## Paired comparisons (exploratory)", "",
                  "Items both runs scored (errors count as a fail" + (")" if errors == "fail" else "; here excluded)")
                  + ". A only / B only: items just one of them passed. p: exact two-sided McNemar test, **raw** "
                  "(not corrected for the many comparisons in this table; only the pre-specified comparisons above "
                  "can be confirmatory). Δ: accuracy of A minus B in percentage points with a Newcombe 95% interval. "
                  "MDE: the smallest difference this n and discordance detect with 80% exact power. Δ2: the paired "
                  "difference in MAE (A − B, lower is better) or mean partial credit, with a bootstrap 95% interval.",
                  "",
                  "| Suite | A | B | Shared n | A passed | B passed | A only | B only | p | Δ acc (95% CI) | MDE80 | Δ2 (95% CI) | label |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for pr in pairs:
            d = f"{_pp(pr.get('diff'))} pp [{_pp(pr.get('diff_lo'))}, {_pp(pr.get('diff_hi'))}]" \
                if pr.get("diff") is not None else "-"
            if pr.get("cluster_lo") is not None:
                d += f"; cluster [{_pp(pr['cluster_lo'])}, {_pp(pr['cluster_hi'])}]"
            mde = f"{pr['mde80'] * 100:.1f} pp" if pr.get("mde80") is not None else "-"
            d2 = "-"
            if pr.get("diff2") is not None:
                nm = "MAE" if pr.get("metric2") == "mae" else "score"
                d2 = f"{nm} {_fmt(pr['diff2'], '+.2f')} [{_fmt(pr.get('diff2_lo'), '+.2f')}, {_fmt(pr.get('diff2_hi'), '+.2f')}]"
            lines.append(f"| {pr['suite']} | {pr['run_a']} | {pr['run_b']} | {pr['n_shared']} | {pr['a_passed']} | "
                         f"{pr['b_passed']} | {pr['a_only']} | {pr['b_only']} | {pr['p_mcnemar']:.3f} | {d} | {mde} | "
                         f"{d2} | {pr.get('label', 'exploratory')} |")
        lines.append("")
    if group_pairs:
        lines += ["## Seed-group comparisons (exploratory)", "",
                  "A seed group is compared on each item's mean pass over its seeds (the item is the unit; seeds are "
                  "not extra items). Δ: A − B in percentage points, paired bootstrap 95% interval; p: sign-flip "
                  "permutation test, raw.", "",
                  "| Suite | A | B | n items | A mean | B mean | Δ (95% CI) | p | MDE80 |", "|---|---|---|---|---|---|---|---|---|"]
        for g in group_pairs:
            r = g["result"]
            lines.append(f"| {g['suite']} | {g['a']} | {g['b']} | {r['n']} | {_fmt(r.get('a_mean'), '.1%')} | "
                         f"{_fmt(r.get('b_mean'), '.1%')} | {_effect(r, 'accuracy')} | {_p(r.get('p'))} | "
                         f"{_mde(r, 'mean_score')} |")
        lines.append("")
    lines += ["## Notes", ""]
    if graded:
        graders = sorted({str((s["run"].get("grader") or {}).get("model", "?")) for s in summaries
                          if any(st["model_graded"] for st in s["suites"].values())})
        lines.append(f"- `*` llm_judge scores are **model-graded** (grader: {', '.join(graders)}), not ground truth. "
                     "Grader errors are counted in the err column and, by default, as a fail.")
    pol = ("Errors (grader/item errors and api errors) count as a **fail**: accuracy = passed / (scored + errors). "
           "The 'excl. err' column leaves them out (the pre-2026-10 figure).") if errors == "fail" else \
        ("Errors (grader/item errors and api errors) are **left out** of accuracy; the 'err = fail' column counts "
         "them as a fail.")
    lines += [
        f"- {pol} Unparsed replies always count as a fail; '(trunc)' is how many of them were cut off at max_tokens.",
        "- Opener refusal is our metric, not CyberSecEval's: the same keyword check on the first 220 characters "
        "without the code-block exemptions. The keyword figure above stays comparable with published CSE numbers.",
        "- The 95% CI is a Wilson score interval. Overlapping intervals do not show two runs are equal; use the "
        "paired comparisons.",
        "- Mean score is the average partial credit (F1, set F1, field fraction, grader score / 10). cvss_mae items "
        "are reported separately as MAE: the mean absolute error in CVSS points (lower is better).",
        "- Baselines: rows marked 'oracle' are fitted on the set's own labels and are optimistic. A model at or below "
        "a constant baseline has shown no skill on that suite. The label-agreement ceiling is how often NVD and the "
        "CNA agree with each other; model differences smaller than that disagreement are not interpretable as skill.",
        "- MDEs come from the exact power of the exact McNemar test at the observed discordance (descriptive, not a "
        "design calculation; use `evals/tools/power.py` before a run).",
        f"- † {SINGLE_SEED}: a sampled run (temperature > 0) with one seed; its differences mix model and sampling "
        "noise.",
        "- Tok/s is end-to-end: completion tokens / wall time per request, including prompt processing and retries.",
        "- Public benchmarks may be in the models' training data (contamination); compare models on the same items, "
        "and prefer private golden sets for decisions.",
        "",
    ]
    return "\n".join(lines)


def _render_extras(ex: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    ceil = ex.get("ceiling")
    if ceil:
        def ci(v: Any, f: str) -> str:
            return " (n too small for a CI)" if not v else f" [{format(v[0], f)}, {format(v[1], f)}]"
        out += ["Label-agreement ceiling: two labelling sources on the same CVEs (Wilson 95% CI for shares, bootstrap "
                "for the mean |Δ|; no CI below n 10). It bounds a model's absolute agreement with one source, not the "
                "paired difference between models. " + CEILING_ROLES, ""]
        for key, c in ceil.items():
            parts = []
            if c.get("cvss_n"):
                parts.append(f"CVSS n {c['cvss_n']}: mean |Δ score| {c['cvss_mean_abs']:.2f}{ci(c['cvss_mean_abs_ci'], '.2f')}, "
                             f"within 1.0 {c['cvss_within1']:.1%}{ci(c['cvss_within1_ci'], '.1%')}, exact vector "
                             f"{_fmt(c.get('cvss_exact_vector'), '.1%')}{ci(c.get('cvss_exact_vector_ci'), '.1%')}")
            if c.get("cwe_n"):
                parts.append(f"CWE n {c['cwe_n']}: exact {c['cwe_exact']:.1%}{ci(c['cwe_exact_ci'], '.1%')}, exact or "
                             f"one-hop {c['cwe_one_hop']:.1%}{ci(c['cwe_one_hop_ci'], '.1%')}")
            out.append(f"- {key.replace('_', ' ').upper().replace(' VS ', ' vs ')}: " + "; ".join(parts))
        out.append("")
    noise = ex.get("cluster_noise")
    if noise:
        if noise.get("too_small"):
            out += [f"Within-CNA template consistency: n too small ({noise['clusters']} near-duplicate clusters of 2+ "
                    "items in the loaded pool).", ""]
        else:
            parts = []
            if noise.get("cwe_pairs"):
                parts.append(f"CWE gold differs in {noise['cwe_disagree_cluster']:.1%} per cluster "
                             f"({noise['cwe_clusters']} clusters) / {noise['cwe_disagree']:.1%} per pair "
                             f"({noise['cwe_pairs']} pairs)")
            if noise.get("cvss_pairs"):
                parts.append(f"CVSS vector differs in {noise['cvss_vector_disagree_cluster']:.1%} per cluster "
                             f"({noise['cvss_clusters']}) / {noise['cvss_vector_disagree']:.1%} per pair "
                             f"({noise['cvss_pairs']}; mean |Δ score| {noise['cvss_mean_abs']:.2f})")
            out += ["Within-CNA template consistency (label heterogeneity inside near-duplicate templates; a descriptor, "
                    "not a bound on label noise): " + "; ".join(parts) + ".", ""]
    either = ex.get("either")
    if either:
        out += ["Matches any label (the gold, NVD, CNA or ADP label; on a single-label item this is the strict "
                "metric):", "",
                "| Run | n | strict accuracy | matches any label | MAE to the closest label |", "|---|---|---|---|---|"]
        for r in either:
            out.append(f"| {r['run']} | {r['n']} | {_fmt(r.get('acc_strict'), '.1%')} | {r['acc_either']:.1%} | "
                       f"{_fmt(r.get('mae_either'), '.2f')} |")
        out.append("")
    bal = ex.get("balanced")
    if bal:
        out += ["Letter-balanced accuracy (mean of per-gold-letter accuracy; a constant letter scores 1/k): "
                + "; ".join(f"{lab} {v:.1%} (n {n})" for lab, v, n in bal) + ".", ""]
    for st in ex.get("strata") or []:
        out += [f"Stratum **{st['lt']}** vs {st['lf']}:", "",
                f"| Run | {st['lt']} | {st['lf']} | gap |", "|---|---|---|---|"]
        for r in st["rows"]:
            gap = f"{_pp(r['t'] - r['f'])} pp" if min(r["nt"], r["nf"]) >= MIN_STRATUM_GAP_N else "- (n < 5)"
            out.append(f"| {r['run']} | {r['t']:.1%} ({r['nt']}) | {r['f']:.1%} ({r['nf']}) | {gap} |")
        out.append("")
    tb = ex.get("text_baseline")
    if tb:
        mae = tb[0]["metric"] == "mae"
        out += ["Against the dev-trained text classifier (multinomial Naive Bayes on the description and CNA, trained "
                "only on development items whose template has no sealed item; tools/text_baseline.py). A skill claim "
                "needs the model to beat it. " + ("MAE difference model − classifier (negative = model better), "
                "bootstrap 95% CI, sign-flip p." if mae else "Accuracy difference model − classifier, Newcombe 95% CI, "
                "McNemar p.") + " Exploratory (not adjusted).", "",
                "| Run | n | model | classifier | difference (95% CI) | p | beats it? |", "|---|---|---|---|---|---|---|"]
        for r in tb:
            if mae:
                d = f"{_fmt(r['diff'], '+.2f')} [{_fmt(r['lo'], '+.2f')}, {_fmt(r['hi'], '+.2f')}]"
                m_, b_ = _fmt(r["model"], ".2f"), _fmt(r["nb"], ".2f")
            else:
                d = f"{_pp(r['diff'])} pp [{_pp(r['lo'])}, {_pp(r['hi'])}]"
                m_, b_ = _fmt(r["model"], ".1%"), _fmt(r["nb"], ".1%")
            out.append(f"| {r['run']} | {r['n']} | {m_} | {b_} | {d} | {_p(r['p'])} | {'yes' if r['beats'] else 'no'} |")
        out.append("")
    for cs in ex.get("cat_strata") or []:
        out += [f"Stratum by {cs['title']}:", "", "| Run | " + " | ".join(map(str, cs["values"])) + " |",
                "|---" * (len(cs["values"]) + 1) + "|"]
        for r in cs["rows"]:
            out.append(f"| {r['run']} | " + " | ".join("-" if r["cells"][v] is None else
                                                       f"{r['cells'][v][0]:.1%} ({r['cells'][v][1]})"
                                                       for v in cs["values"]) + " |")
        out.append("")
    return out


def _render_aggregate(agg: Dict[str, Any]) -> List[str]:
    out = ["## Macro average over suites", "", f"Suites: {', '.join(agg['suites'])}. Each suite on the items every "
           "run scored; equal weight per suite.", "", "| Run | macro accuracy | " + " | ".join(agg["suites"]) + " |",
           "|---" * (len(agg["suites"]) + 2) + "|"]
    for r in agg["runs"]:
        out.append(f"| {r['run']} | {_fmt(r['macro'], '.1%')} | " +
                   " | ".join(f"{_fmt(v, '.1%')} ({n})" for v, n in (r["suites"][s] for s in agg["suites"])) + " |")
    out.append("")
    return out


def _render_sens_rows(srows: List[Dict[str, Any]]) -> List[str]:
    keys = [k for k in ["primary"] + list(OPS) if any(k in r["values"] for r in srows)]
    if keys == ["primary"]:
        return []
    out = ["Sensitivity of each run's accuracy (MAE for CVSS; n in brackets). The first column is every scored item; "
           "a comparison's declared primary analysis is in the comparisons section:", "",
           "| Run | " + " | ".join(RUN_TABLE_BASE if k == "primary" else VARIANT_LABEL[k] for k in keys) + " |",
           "|---" * (len(keys) + 1) + "|"]
    for r in srows:
        cells = []
        for k in keys:
            v = r["values"].get(k)
            if v is None:
                cells.append("-")
            elif r["metric"] == "mae":
                cells.append(f"{v[0]:.2f} ({v[1]})")
            else:
                cells.append(f"{v[0]:.1%} ({v[1]})")
        out.append(f"| {r['run']} | " + " | ".join(cells) + " |")
    out.append("")
    return out


def _render_groups(summaries: List[Dict[str, Any]], groups: List[Dict[str, Any]]) -> List[str]:
    out = ["## Seed groups", "", "Runs that differ only in the sampling seed. Accuracy per seed as mean ± SD "
           "(min – max).", "", "| Group | Suite | Seeds | Accuracy mean ± SD | Range |", "|---|---|---|---|---|"]
    for g in groups:
        suites = sorted(set.intersection(*[set(summaries[i]["suites"]) for i in g["members"]]))
        for su in suites:
            accs = [summaries[i]["suites"][su]["accuracy"] for i in g["members"]]
            accs = [a for a in accs if a is not None]
            sp = ST.seed_spread(accs)
            if sp["mean"] is None:
                continue
            out.append(f"| {g['label']} | {su} | {', '.join(map(str, g['seeds']))} | "
                       f"{sp['mean']:.1%} ± {sp['sd'] * 100:.1f} pp | {sp['min']:.1%} – {sp['max']:.1%} |")
    out.append("")
    return out


def _power_cell(r: Dict[str, Any]) -> str:
    """A-priori power from the plan (decides 'underpowered'), then the observed-discordance MDE or TOST power
    (descriptive), then the floor share for equivalence claims."""
    pr = r.get("primary") or {}
    parts = []
    ap = r.get("apriori") or {}
    if ap.get("power") is not None:
        parts.append(f"a-priori {ap['power']:.2f}" + (" (underpowered)" if ap["power"] < POWER_TARGET else ""))
    elif ap.get("degenerate"):
        parts.append("a-priori: degenerate assumptions")
    else:
        parts.append("a-priori: none declared")
    if r["direction"] == "equivalent":
        if pr.get("tost_power") is not None:
            parts.append(f"observed-design TOST power {pr['tost_power']:.2f}")
        fl = r.get("floor")
        if fl:
            parts.append(f"floor {fl[0]:.0%} of {fl[1]} items fail in all {fl[2]} runs")
    else:
        parts.append(f"MDE {_mde(pr, r['metric'])}")
    return "; ".join(parts)


def _group_gate(gate: List[str]) -> List[str]:
    """'run: reason' lines grouped by reason, so a gate failure on many runs stays readable."""
    by: Dict[str, List[str]] = {}
    for g in gate:
        m = re.match(r"^([^:\s(]+): (.+)$", g)
        name, why = (m.group(1), m.group(2)) if m else ("", g)
        by.setdefault(why, []).append(name)
    out = []
    for why, names in by.items():
        names = [n for n in names if n]
        out.append(why + (f" ({len(names)} affected: {', '.join(names[:6])}"
                          f"{', …' if len(names) > 6 else ''})" if names else ""))
    return out


def _render_confirmatory(conf: List[Dict[str, Any]], plan: Dict[str, Any], gate: List[str],
                         deviations: Optional[List[str]] = None, gate_glob: Optional[List[str]] = None,
                         fam_gates: Optional[Dict[str, List[str]]] = None) -> List[str]:
    devs = deviations or []
    fams = list(dict.fromkeys(r["family"] for r in conf))
    if fam_gates is None:          # legacy callers: one global gate
        gate_glob, fam_gates = list(gate), {f: [] for f in fams}
    passed = [f for f in fams if not gate_glob and not fam_gates.get(f)]
    if gate and passed:
        out = [f"## Pre-specified comparisons: {len(passed)} of {len(fams)} families pre-registered", "",
               "The gate is applied per family. Pre-registered families: " + ", ".join(passed) + ". The others are "
               "post-hoc / exploratory for the reasons listed under each family.", ""]
        for f in fams:
            if f not in passed:
                out += [f"**{f}: post-hoc**", ""] + [f"- {g}" for g in _group_gate(fam_gates.get(f) or [])] + [""]
    elif gate:
        out = ["## Post-hoc / exploratory (pre-specified comparisons, not confirmatory)", "",
               "These comparisons were specified in advance, but the confirmatory gate failed, so no result below is "
               "confirmatory:", ""] + [f"- {g}" for g in _group_gate(gate_glob or [])] + [""]
        for f in fams:
            if fam_gates.get(f):
                out += [f"**{f}: post-hoc**", ""] + [f"- {g}" for g in _group_gate(fam_gates[f])] + [""]
    else:
        head = "## Pre-registered comparisons (primary analysis)" if not devs else \
            "## Pre-registered (with deviations: " + "; ".join(sorted({d.split(": ", 1)[-1].split(" = ")[0]
                                                                     for d in devs})) + ")"
        out = [head, "",
               "The confirmatory gate passed: every compared run is a confirmatory protocol run with no integrity "
               "check failed, every other failed check accepted by its own deviation, equal harness commit, frozen "
               "ref, budget policy and scorer version, verified samplers, outputs bound to run.json, pinned item "
               "annotations, and this comparisons file is the one frozen in the manifest.", ""]
    if devs:
        out += ["Accepted deviations:", ""] + [f"- {d}" for d in devs] + [""]
    if plan.get("title"):
        out += [f"Plan: {plan['title']}.", ""]
    evaluable = [r for r in conf if r.get("p_test") is not None]
    n_f = sum(1 for r in evaluable if survives(r, "p_holm"))
    n_all = sum(1 for r in evaluable if survives(r, "p_holm_all"))
    n_raw = sum(1 for r in evaluable if r["p_test"] < r["alpha"] and _direction_ok(r) and not r.get("single_seed"))
    n_rob = sum(1 for r in evaluable if survives(r, "p_holm") and not r.get("not_robust_to"))
    out += [f"{len(evaluable)} of {len(conf)} comparisons evaluable. Hold at alpha: {n_raw} uncorrected, "
            f"{n_f} after the family adjustment (Holm, or none inside an intersection-union family), {n_rob} of those "
            f"robust to every pre-registered sensitivity analysis, {n_all} after Holm across all families "
            "(single-seed sampled comparisons never count).", "",
            "Effect: A − B (rates in percentage points; MAE in CVSS points, lower is better), under each comparison's "
            "declared primary analysis (column 'Primary'). p decides: exact McNemar for two single runs on a "
            "proportion; the sign-flip permutation test for MAE and seed groups (the bootstrap CI shown with them is "
            "for display only); for 'equivalent' the TOST p within ± margin (exact Tango TOST for two single runs on "
            "a proportion; paired t-TOST on per-item differences for means and seed groups); a declared cluster or "
            "letter-balanced analysis decides by its bootstrap, shown with its Monte Carlo SE. Not-evaluable members "
            "(n below the minimum, an exclusion flag not known for every item, runs missing) count as p = 1 in "
            "every adjustment, so m is always the declared m. Family: Holm within the family; "
            "**IUT** families (\"best of k\") need every member to win at alpha with no adjustment, which controls "
            "the family-wise error of the family claim. Holm (all): across families, an IUT family counting once. "
            "Power: the a-priori power from the plan's assumptions at its planned n (this alone decides 'not "
            "detectable at this n'), then descriptive figures from the observed design (MDE, TOST power) and, for "
            "equivalence, the floor share (items every loaded run fails, which makes runs look alike). Robust: whether any pre-registered sensitivity analysis flips the "
            "conclusion (family re-adjusted under that analysis); a sensitivity analysis below the minimum n is not "
            "evaluable and never a flip; keep-only strata (post-cutoff, name-in-text) are reported separately with "
            "their interaction and never change a verdict.", "",
            "| ID | Suite | A | B | Claim | Primary | n | A | B | Effect (95% CI) | p | Family adj. | BH q | Holm (all) | Power | Robust? | Verdict |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    fam = None
    fv = {f["family"]: f for f in family_verdicts(conf)}
    for r in conf:
        if r["family"] != fam:
            fam = r["family"]
            f = fv.get(fam) or {}
            tag = (" (IUT)" if r.get("test") == "iut" else "") + \
                (" — post-hoc" if (gate_glob or (fam_gates or {}).get(fam)) else " — pre-registered")
            out.append(f"| **{fam}**{tag} | | | | {r.get('decision', '')[:160]} | | | | | | | | | | | | "
                       f"**family: {f.get('verdict', '-')}** |")
        claim = {"a>b": "A > B", "a<b": "A < B", "two-sided": "A ≠ B"}.get(r["direction"]) or \
            f"\\|A − B\\| < {_margin(r)}"
        if r.get("claim"):
            claim += f" ({r['claim']})"
        spec = r.get("spec") or {}
        prim = ops_label(spec.get("ops") or ())
        if spec.get("errors") == "exclude":
            prim += "; errors excluded"
        if r["metric"] in ("mae", "mae_either"):
            prim += f"; impute {spec.get('impute')}"
        pr = r.get("primary") or {}
        if r.get("p_test") is None:
            out.append(f"| {r['id']} | {r['suite']} | {_spec(r['a_spec'])} | {_spec(r['b_spec'])} | {claim} | {prim} | "
                       f"- | - | - | - | - | - | - | - | - | - | {r.get('verdict', '-')} |")
            continue
        fmt_m = ".1%" if r["metric"] in BINARY_METRICS else ".2f"
        if not r.get("prereg_sens"):
            rob = "- (none pre-registered)"
        elif not r.get("robust_checked"):
            rob = "yes (no pre-registered analysis changes it)"
        else:
            rob = "yes" if not r.get("not_robust_to") else \
                "no: " + ", ".join(VARIANT_LABEL.get(o, o) for o in r["not_robust_to"])
        if r.get("robust_not_evaluable"):
            rob += "; not evaluable (n < min): " + ", ".join(VARIANT_LABEL.get(o, o) for o in r["robust_not_evaluable"])
        out.append(f"| {r['id']} | {r['suite']}{' (' + r['metric'] + ')' if r['metric'] != 'accuracy' else ''} | "
                   f"{r['a_label']} | {r['b_label']} | {claim} | {prim} | {pr.get('n')} | {_fmt(pr.get('a_mean'), fmt_m)} | "
                   f"{_fmt(pr.get('b_mean'), fmt_m)} | {_effect(pr, r['metric'])} | {_p_test_cell(r)} | "
                   f"{'IUT ' if r.get('test') == 'iut' else ''}{_p(r.get('p_holm'))} | {_p(r.get('q_bh'))} | "
                   f"{_p(r.get('p_holm_all'))} | {_power_cell(r)} | {rob} | **{r['verdict']}** |")
    out.append("")
    cov_rows = [r for r in conf if r.get("coverage")]
    if cov_rows:
        out += ["### Exclusion-flag coverage", "",
                "For each declared exclusion: the share of the comparison's shared items whose flag is known. An unknown "
                "flag on any item makes the comparison not evaluable unless the plan lists the op in "
                "`primary.allow_unknown`; 'n/a' = no item of the suite carries the flag (the exclusion does not apply).", "",
                "| ID | coverage |", "|---|---|"]
        for r in cov_rows:
            cells = []
            for op, v in r["coverage"].items():
                if not v["applies"]:
                    cells.append(f"{v['flag']} n/a")
                else:
                    tag = " (allowed unknown)" if op in (r.get("spec") or {}).get("allow_unknown", []) and \
                        v["known"] < v["n"] else ""
                    cells.append(f"{v['flag']} {v['known']}/{v['n']}{tag}")
            out.append(f"| {r['id']} | " + "; ".join(cells) + " |")
        out.append("")
    st_rows = [(r, st) for r in conf for st in (r.get("strata") or {}).values()]
    if st_rows:
        out += ["### Strata of the pre-specified comparisons (keep-only analyses)", "",
                "The effect inside and outside the stratum and their interaction (inside − outside, independent "
                "bootstrap 95% CI). Strata never change a verdict. 'n/a': the stratum is empty (for example no item of "
                "a public suite is post-cutoff for both models); 'not evaluable': n below the minimum.", "",
                "| ID | stratum | inside: effect (n) | outside: effect (n) | interaction (95% CI) |", "|---|---|---|---|---|"]
        for r, st in st_rows:
            mn = r.get("min_n", MIN_N)

            def cell(x: Dict[str, Any]) -> str:
                if not x or not x.get("n"):
                    return "n/a (n = 0)"
                e = f"{_effect(x, r['metric'])} (n {x['n']})"
                return e if x["n"] >= mn else f"not evaluable: {e}"
            inter = "-"
            if st.get("interaction") is not None and min(st["in"].get("n") or 0, st["out"].get("n") or 0) >= mn:
                if r["metric"] in BINARY_METRICS:
                    inter = f"{_pp(st['interaction'])} pp [{_pp(st['int_lo'])}, {_pp(st['int_hi'])}]"
                else:
                    inter = f"{_fmt(st['interaction'], '+.2f')} [{_fmt(st['int_lo'], '+.2f')}, {_fmt(st['int_hi'], '+.2f')}]"
            out.append(f"| {r['id']} | {VARIANT_LABEL.get(st['op'], st['op'])} | {cell(st['in'])} | "
                       f"{cell(st['out'])} | {inter} |")
        out.append("")
    sens_keys = [k for k in list(OPS) if any(k in (r.get("sensitivity") or {}) for r in conf)]
    sens_keys += sorted({k for r in conf for k in (r.get("sensitivity") or {}) if k.startswith("metric:")})
    if sens_keys:
        lab = dict(VARIANT_LABEL)
        out += ["### Sensitivity of the pre-specified comparisons", "",
                "Each column toggles one analysis choice relative to the comparison's primary analysis (adds the "
                "exclusion, or removes it when the primary has it). Effect (95% CI), raw p and n. A column is shown "
                "only when it changes at least one comparison's n, estimate or p; 'same' marks comparisons it does not "
                "change. Columns marked ° are pre-registered and can downgrade a verdict; the others are checks.", "",
                "| ID | primary | " + " | ".join(_sens_head(k, lab, conf) for k in sens_keys) + " | cluster bootstrap CI |",
                "|---" * (len(sens_keys) + 3) + "|"]
        for r in conf:
            if r.get("p_test") is None:
                continue
            cells = [_sens_cell(r, r["primary"])]
            for k in sens_keys:
                s = (r.get("sensitivity") or {}).get(k)
                cells.append(_sens_cell(r, s) if s else "same")
            pr = r["primary"]
            cl = _effect(pr, r["metric"], cluster=True) + f" ({pr['n_clusters']} clusters)" \
                if pr.get("cluster_lo") is not None else "no shared clusters"
            out.append(f"| {r['id']} | " + " | ".join(cells) + f" | {cl} |")
        out.append("")
    return out


def _sens_head(k: str, lab: Dict[str, str], conf: List[Dict[str, Any]]) -> str:
    """Column header: the op's label, or 'without: <label>' when every comparison's primary already applies it."""
    ev = [r for r in conf if r.get("p_test") is not None]
    in_prim = [k in ((r.get("spec") or {}).get("ops") or ()) for r in ev]
    head = lab[k]
    if in_prim and all(in_prim):
        head = "without: " + head
    elif any(in_prim):
        head = "toggle: " + head
    return head + ("°" if any(k in (r.get("prereg_sens") or []) for r in conf) else "")


def _sens_cell(r: Dict[str, Any], s: Dict[str, Any]) -> str:
    if not s or not s.get("n"):
        return "n = 0 (not evaluable)"
    if s.get("not_evaluable"):
        return f"not evaluable ({s['not_evaluable']})"
    p = s.get("p_tost") if r["direction"] == "equivalent" else s.get("p")
    return f"{_effect(s, r['metric'])}; p {_p(p)}; n {s['n']}"


def _margin(r: Dict[str, Any]) -> str:
    m = r.get("margin")
    if m is None:
        return "-"
    return f"{m * 100:g} pp" if r["metric"] in BINARY_METRICS else f"{m:g}"


def _spec(x: Any) -> str:
    return " + ".join(x) if isinstance(x, list) else str(x)


def render_csv(summaries: List[Dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS + ["run_dir"], extrasaction="ignore")
    w.writeheader()
    for row in rows(summaries):
        w.writerow({k: ("" if v is None else v) for k, v in row.items()})
    return buf.getvalue()


def render_conf_csv(conf: List[Dict[str, Any]], gate: Optional[List[str]] = None) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CONF_COLUMNS)
    w.writeheader()
    section = "pre-registered" if gate == [] else "post-hoc"
    e = lambda x: "" if x is None else x  # noqa: E731
    for r in conf:
        pr = r.get("primary") or {}
        spec = r.get("spec") or {}
        w.writerow({"id": r["id"], "family": r["family"], "test": r.get("test", "holm"), "suite": r["suite"],
                    "metric": r["metric"], "direction": r["direction"], "margin": e(r.get("margin")),
                    "primary": ops_label(spec.get("ops") or ()), "a": r.get("a_label", _spec(r["a_spec"])),
                    "b": r.get("b_label", _spec(r["b_spec"])), "n": e(pr.get("n")), "a_mean": e(pr.get("a_mean")),
                    "b_mean": e(pr.get("b_mean")), "diff": e(pr.get("diff")), "lo": e(pr.get("lo")),
                    "hi": e(pr.get("hi")), "p": e(r.get("p_test")), "p_holm": e(r.get("p_holm")),
                    "q_bh": e(r.get("q_bh")), "p_holm_all": e(r.get("p_holm_all")), "mde80": e(pr.get("mde")),
                    "tost_power": e(pr.get("tost_power")),
                    "robust": "" if not r.get("prereg_sens") else ("yes" if not r.get("not_robust_to")
                                                                  else ";".join(r["not_robust_to"])),
                    "verdict": r.get("verdict", ""), "section": section})
    return buf.getvalue()


# ------------------------------------------------------------------------------------------- assembly
def sensitivity_rows(summaries: List[Dict[str, Any]], recs: List[Dict[str, Dict[str, Any]]],
                     labels: Optional[Dict[str, str]], errors: str, impute: str = "max",
                     constant: float = CVSS_PRIOR) -> Dict[str, List[Dict[str, Any]]]:
    """Per suite row and run: the accuracy (or MAE) under each sensitivity variant that changes something."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for key in sorted({n for s in summaries for n in s["suites"]}):
        per_run = [(s, _row_records(r, key, labels)) for s, r in zip(summaries, recs)]
        allr = [x for _, rr in per_run for x in rr.values()]
        metric = "mae" if any(is_error_metric(x) for x in allr) else "accuracy"
        keys = ["primary"] + [k for k in OPS if variant_applies(allr, k)]
        table = []
        for s, rr in per_run:
            if not rr:
                continue
            vals = {}
            for k in keys:
                xs = [outcome(x, metric, errors, k, impute, constant) for x in rr.values() if not item_excluded([x], k)]
                xs = [x for x in xs if x is not None]
                vals[k] = (sum(xs) / len(xs), len(xs)) if xs else None
            table.append({"run": s["label"], "metric": metric, "values": vals})
        changed = [k for k in keys[1:] if any(t["values"].get(k) != t["values"].get("primary") for t in table)]
        if not changed:
            continue
        for t in table:
            t["values"] = {k: v for k, v in t["values"].items() if k == "primary" or k in changed}
        out[key] = table
    return out


def group_comparisons(summaries: List[Dict[str, Any]], recs: List[Dict[str, Dict[str, Any]]],
                      groups: List[Dict[str, Any]], errors: str, n_perm: int = 5000) -> List[Dict[str, Any]]:
    """Each seed group against every other run or group that shares a suite (per-item mean pass)."""
    units: List[Tuple[str, List[int]]] = [(g["label"], g["members"]) for g in groups]
    grouped = {i for g in groups for i in g["members"]}
    others = units + [(s["label"], [i]) for i, s in enumerate(summaries) if i not in grouped]
    out = []
    for gi, (gl, gm) in enumerate(units):
        for ol, om in others:
            if om == gm or (ol, om) in units[:gi]:
                continue
            suites = set.intersection(*[set(summaries[i]["suites"]) for i in gm + om])
            for su in sorted(x for x in suites if "[label_source=" not in x):
                a = [_row_records(recs[i], su, None) for i in gm]
                b = [_row_records(recs[i], su, None) for i in om]
                r = compare(a, b, "accuracy", "primary", errors, n_perm=n_perm, cluster=False)
                if r.get("n"):
                    out.append({"suite": su, "a": gl, "b": ol, "result": r})
    return out


def load_text_baseline(paths: Iterable[Path]) -> Dict[str, Dict[str, Any]]:
    """id -> prediction row from tools/text_baseline.py."""
    out: Dict[str, Dict[str, Any]] = {}
    for p in paths:
        for r in read_jsonl(Path(p)):
            if "id" in r:
                out[r["id"]] = r
    return out


def _nb_outcome(rec: Dict[str, Any], pred: Dict[str, Any], metric: str) -> Optional[float]:
    if rec.get("scorer") == "cwe_match" and metric == "accuracy":
        g = rec.get("gold")
        g = g.get("cwe") if isinstance(g, dict) else g
        g = g[0] if isinstance(g, list) and g else g
        return 1.0 if pred.get("pred") and _norm_cwe(pred["pred"]) == _norm_cwe(g) else 0.0
    if rec.get("scorer") == "cvss_mae":
        g = _rec_gold_cvss(rec)
        ps = pred.get("pred_score")
        if g is None or ps is None:
            return None
        err = round(abs(float(ps) - g), 4)
        return err if metric == "mae" else (1.0 if err <= 1.0 else 0.0)
    return None


def text_baseline_rows(summaries: List[Dict[str, Any]], recs: List[Dict[str, Dict[str, Any]]], suite: str,
                       preds: Dict[str, Dict[str, Any]], errors: str, impute: str, constant: float,
                       seed: int = ST.DEFAULT_SEED) -> List[Dict[str, Any]]:
    """Per run: the model against the dev-trained text classifier on the same items (accuracy for CWE, MAE for
    CVSS): the paired difference with a 95% CI and whether the model beats it (CI excludes 0 in its favour).
    Exploratory: not adjusted for the many runs and suites."""
    out = []
    for s_, r in zip(summaries, recs):
        rr = {k: x for k, x in r.items() if x["suite"] == suite and k in preds}
        if not rr:
            continue
        metric = "mae" if any(is_error_metric(x) for x in rr.values()) else "accuracy"
        mv, bv = [], []
        for k, x in sorted(rr.items()):
            m = outcome(x, metric, errors, impute=impute, constant=constant)
            b = _nb_outcome(x, preds[k], metric)
            if m is not None and b is not None:
                mv.append(m)
                bv.append(b)
        if len(mv) < 2:
            continue
        if metric == "accuracy":
            res = paired_binary(mv, bv)
            beats = res["lo"] > 0
        else:
            res = paired_continuous(mv, bv, n_boot=2000, n_perm=2000, seed=seed, power=False)
            beats = res["hi"] is not None and res["hi"] < 0 and (res["p"] or 1) < 0.05
        out.append({"run": s_["label"], "metric": metric, "n": res["n"], "model": res["a_mean"], "nb": res["b_mean"],
                    "diff": res["diff"], "lo": res["lo"], "hi": res["hi"], "p": res["p"], "beats": beats})
    return out


def suite_extras(summaries: List[Dict[str, Any]], recs: List[Dict[str, Dict[str, Any]]],
                 items: Dict[str, Dict[str, Any]], errors: str, impute: str,
                 constant: float, preds: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for suite in sorted({n for s in summaries for n in s["suites"] if "[label_source=" not in n}):
        ids = {k for r in recs for k, x in r.items() if x["suite"] == suite}
        ex: Dict[str, Any] = {}
        ceil = label_ceiling(items[i] for i in ids if i in items)
        if ceil:
            ex["ceiling"] = ceil
        noise = cluster_label_noise(it for it in items.values() if it.get("suite") == suite)
        if noise:
            ex["cluster_noise"] = noise
        e = either_rows(summaries, recs, suite, errors, impute, constant)
        if e:
            ex["either"] = e
        b = mcq_balanced(summaries, recs, suite, errors)
        if b:
            ex["balanced"] = b
        st = strata_rows(summaries, recs, suite, errors)
        if st:
            ex["strata"] = st
        cs = cat_strata_rows(summaries, recs, suite, errors)
        if cs:
            ex["cat_strata"] = cs
        if preds:
            tb = text_baseline_rows(summaries, recs, suite, preds, errors, impute, constant)
            if tb:
                ex["text_baseline"] = tb
        if ex:
            out[suite] = ex
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="*", help="run directories")
    ap.add_argument("--latest", type=int, help="use the N newest run dirs under evals/results/")
    ap.add_argument("--results", default=str(RESULTS_DIR), help="results root for --latest")
    ap.add_argument("--out", help="write markdown here (default: stdout)")
    ap.add_argument("--csv", help="also write CSV here")
    ap.add_argument("--title", default="Eval report")
    ap.add_argument("--split-label-source", action="store_true",
                    help="add a row per meta.label_source value (e.g. CNA vs NVD labels on the nvd-* suites)")
    ap.add_argument("--items", action="append", default=[], metavar="JSONL",
                    help="suite files (e.g. rigor-annotated): flags, labels and gold by id; they override the "
                         "run.json dataset files")
    ap.add_argument("--data-root", default=str(REPO_DIR),
                    help="where run.json dataset paths and freeze manifests are resolved (default: repo)")
    ap.add_argument("--no-baselines", action="store_true", help="skip the constant baselines")
    ap.add_argument("--cvss-constant", type=float, default=CVSS_PRIOR,
                    help=f"the a-priori constant CVSS baseline (default {CVSS_PRIOR})")
    ap.add_argument("--cvss-impute", choices=IMPUTE, default="max",
                    help="tables: MAE for an item with no parsed CVSS prediction (comparisons declare their own)")
    ap.add_argument("--pairs-csv", help="also write the paired comparisons as CSV here")
    ap.add_argument("--comparisons", help="pre-specified comparisons file (JSON or YAML)")
    ap.add_argument("--conf-csv", help="also write the comparison results as CSV here")
    ap.add_argument("--errors", choices=("fail", "exclude"), default="fail",
                    help="grader/item/api errors count as a fail (default) or are left out")
    ap.add_argument("--legacy-scores", metavar="NAME",
                    help="a scores file in each run dir with the legacy scorer's results (e.g. scores.legacy.jsonl)")
    ap.add_argument("--cluster-bootstrap", action="store_true",
                    help="add cluster-bootstrap CIs (items sharing a template cluster) to the exploratory pairs table")
    ap.add_argument("--no-sensitivity", action="store_true", help="skip the per-run sensitivity tables")
    ap.add_argument("--aggregate", action="store_true",
                    help="add a macro average over suites (refused when the runs cover different suites)")
    ap.add_argument("--text-baseline", action="append", default=[], metavar="JSONL",
                    help="predictions of tools/text_baseline.py: a baseline row and a 'beats the text baseline' table")
    ap.add_argument("--boot", type=int, default=ST.DEFAULT_BOOT, help="bootstrap replicates (comparisons)")
    ap.add_argument("--seed", type=int, default=ST.DEFAULT_SEED, help="resampling seed")
    a = ap.parse_args(argv)
    dirs = [Path(p) for p in a.runs]
    if a.latest:
        root = Path(a.results)
        cands = sorted((p for p in root.iterdir() if (p / "run.json").exists()), key=lambda p: p.stat().st_mtime)
        dirs += cands[-a.latest:]
    if not dirs:
        ap.error("give run dirs or --latest N")
    missing = [str(d) for d in dirs if not (d / "run.json").exists()]
    if missing:
        print(f"not a run dir (no run.json): {', '.join(missing)}", file=sys.stderr)
        return 2
    plan = load_comparisons(Path(a.comparisons)) if a.comparisons else None
    labels = load_labels(a.items) if a.items else None
    items = load_items(dataset_paths(dirs, Path(a.data_root)) + [Path(p) for p in a.items])
    summaries = unique_labels([summarize(d, a.split_label_source, labels, a.errors, a.cvss_impute, a.cvss_constant,
                                         items) for d in dirs])
    groups = seed_groups(summaries)
    mark_single_seed(summaries, groups)
    recs = [item_records(d, items, a.legacy_scores) for d in dirs]
    agg = None
    if a.aggregate:
        try:
            agg = macro_aggregate(summaries, recs, a.errors)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    pairs = paired_records(summaries, recs, labels if a.split_label_source else None, a.errors,
                           cluster=a.cluster_bootstrap, seed=a.seed, impute=a.cvss_impute,
                           constant=a.cvss_constant) if len(dirs) > 1 else []
    if not a.split_label_source:
        pairs = [p for p in pairs if "[label_source=" not in p["suite"]]
    _PLAN_CAL.clear()
    _PLAN_CAL.update((plan or {}).get("calibration_verdict") or {})
    conf = confirmatory(plan, summaries, recs, None, a.boot, seed=a.seed,
                        judge_scope=calibration_scope(plan, summaries, Path(a.data_root))) if plan else None
    gate = None
    gate_glob: List[str] = []
    fam_gates: Dict[str, List[str]] = {}
    devs: List[str] = []
    if conf is not None:
        used = sorted({i for r in conf for i in r.get("runs", [])})
        runs = [dict(summaries[i]["run"], _dir=summaries[i]["run_dir"], _path=summaries[i]["path"]) for i in used]
        loaded = {summaries[i]["run_dir"]: [str(x) for x in dataset_paths([Path(summaries[i]["path"])],
                                                                          Path(a.data_root))] for i in used}
        analysis = {"min_n": min_n_of(plan), "n_boot": a.boot, "n_perm": ST.DEFAULT_PERM, "seed": a.seed,
                    "alpha": float(plan.get("alpha", 0.05))}
        gate_glob, fam_gates = family_gates(conf, Path(a.comparisons), Path(a.data_root), summaries, recs,
                                            [Path(p) for p in a.items], loaded, analysis, plan)
        gate = sorted(set(gate_glob + [x for v in fam_gates.values() for x in v]),
                      key=(gate_glob + [x for v in fam_gates.values() for x in v]).index)
        devs = accepted_deviations(runs) + list(LEDGER_DEVIATIONS)
        tag: Dict[Tuple[str, str, str], str] = {}
        for r in conf:
            word = "confirmatory" if not (gate_glob or fam_gates.get(r["family"])) else "pre-specified"
            if r.get("a_label") and r.get("b_label"):
                for key in ((r["suite"], r["a_label"], r["b_label"]), (r["suite"], r["b_label"], r["a_label"])):
                    tag[key] = (tag[key] + ", " if key in tag else f"{word}: ") + r["id"]
        for r in conf:
            r["gate"] = "pre-registered" if not (gate_glob or fam_gates.get(r["family"])) else "post-hoc"
        for p in pairs:
            p["label"] = tag.get((p["suite"], p["run_a"], p["run_b"]), "exploratory")
    else:
        for p in pairs:
            p["label"] = "exploratory"
    gpairs = group_comparisons(summaries, recs, groups, a.errors) if groups else []
    base: Dict[str, List[Dict[str, Any]]] = {}
    if not a.no_baselines and items:
        for suite in sorted({n for s in summaries for n in s["suites"] if "[label_source=" not in n}):
            sets: Dict[frozenset, None] = {}
            for r in recs:
                ids = frozenset(k for k, x in r.items() if x["suite"] == suite)
                if ids:
                    sets[ids] = None
            for ids in sets:
                its = [items[i] for i in sorted(ids) if i in items]
                if len(its) == len(ids):
                    for b in baselines(its, a.cvss_constant):
                        if not any(b["name"] == x["name"] and b["n"] == x["n"] for x in base.get(suite, [])):
                            base.setdefault(suite, []).append(b)
    preds = load_text_baseline(a.text_baseline) if a.text_baseline else None
    extras = suite_extras(summaries, recs, items, a.errors, a.cvss_impute, a.cvss_constant, preds)
    if preds:
        for suite in sorted({n for s_ in summaries for n in s_["suites"] if "[label_source=" not in n}):
            ids = sorted({k for r in recs for k, x in r.items() if x["suite"] == suite and k in preds})
            vals = [(x, preds[k]) for k in ids for x in [next(r[k] for r in recs if k in r)]]
            accs = [v for v in (_nb_outcome(x, p_, "accuracy") for x, p_ in vals) if v is not None]
            maes = [v for v in (_nb_outcome(x, p_, "mae") for x, p_ in vals if x.get("scorer") == "cvss_mae")
                    if v is not None]
            if accs:
                base.setdefault(suite, []).append({"name": "dev-trained text classifier (NB)", "oracle": False,
                                                   "n": len(accs), "accuracy": sum(accs) / len(accs),
                                                   "mae": (sum(maes) / len(maes)) if maes else None})
    sens = None if a.no_sensitivity else sensitivity_rows(summaries, recs, labels, a.errors, a.cvss_impute,
                                                          a.cvss_constant)
    md = render_markdown(summaries, a.title, pairs, conf=conf, plan=plan, gate=gate, gate_glob=gate_glob,
                         fam_gates=fam_gates,
                         deviations=devs if conf is not None else None, groups=groups,
                         group_pairs=gpairs, base=base, sens_rows=sens, extras=extras, aggregate=agg, errors=a.errors)
    if a.out:
        Path(a.out).write_text(md, encoding="utf-8")
    else:
        sys.stdout.write(md)
    if a.csv:
        Path(a.csv).write_text(render_csv(summaries), encoding="utf-8")
    if a.pairs_csv:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=PAIR_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows([{k: ("" if v is None else v) for k, v in p.items()} for p in pairs])
        Path(a.pairs_csv).write_text(buf.getvalue(), encoding="utf-8")
    if a.conf_csv and conf is not None:
        Path(a.conf_csv).write_text(render_conf_csv(conf, gate), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
