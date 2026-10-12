#!/usr/bin/env python3
"""Offline re-score of stored eval responses with the current scorers (no model, gateway or network calls).

    python3 evals/tools/rescore.py evals/results --out /tmp/rescored            # every run dir under results/
    python3 evals/tools/rescore.py evals/results/<run> [<run> ...] --out DIR [--data evals/data]

For each run directory (one with responses.jsonl and run.json) it writes DIR/<run>/:
- ``scores.jsonl``: the new scores (an unparsed cvss_mae row has value None and extra.legacy_value 10; the diff and
  summary impute 10 for "new MAE" and exclude it for "new MAE ok"), one row per latest response, each with a ``legacy`` block (the stored
  value/passed/parsed/status) so published numbers can be reconciled. When the run used the final-answer follow-up,
  the row also has ``no_followup`` (the score of ``content_before_final``, the reply as the model first gave it).
  llm_judge rows are carried over unchanged (re-grading would need a model) and marked ``carried``.
- ``responses.jsonl`` and ``run.json``: copies, so ``evals/report.py DIR/<run>`` works on the rescored run.
- ``rescore_diff.json`` and ``rescore_diff.md``: per suite, old vs new pass count / accuracy / mean value, the
  status transitions (old -> new), pass flips, and for cvss_mae the exact-vector rate, metric matches and
  constant-guess baselines.
and DIR/summary.{md,json,csv} over all runs. The originals are never written: the tool refuses an output directory
inside a source run directory or the results directory.

Items are looked up by id in the dataset files listed in run.json (paths relative to the repo root, falling back to
the same file name under --data), then in every --data/*.jsonl. run.json ``scorer_overrides`` are applied.

CVSS constant-guess baselines (all computed on exactly the items the run scored):
- ``const_5.0``: the midpoint of the 0-10 scale; uses no data at all.
- ``const_7.5``: a fixed a-priori guess near the centre of the published NVD v3 score distribution (stated before
  looking at these runs); reported because it was proposed, not because it is optimal.
- ``xsuite_median``: the median gold score of a different CVSS source (``XSUITE``): NVD suites use the CTIBench-vsp
  gold set and CTIBench-vsp uses nvd-cvss. Out-of-sample for the suite it is applied to.
- ``oracle_median``: the median of this suite's own gold scores on these items. In-sample, so it is a lower bound on
  any constant guess, not a fair baseline; shown only for scale.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import shutil
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

EVALS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = EVALS_DIR.parent
sys.path.insert(0, str(EVALS_DIR))
import scorers as S  # noqa: E402

# The other CVSS gold source used for the cross-suite median baseline (see the module docstring).
XSUITE = {"nvd-cvss": "ctibench-vsp", "nvd-cvss-nvdlab": "ctibench-vsp", "ctibench-vsp": "nvd-cvss"}
CONST_BASELINES = {"const_5.0": 5.0, "const_7.5": 7.5}


# ------------------------------------------------------------------------------------------- io
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
                continue
    return out


def latest_by_id(rows: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        if "id" in r:
            out[r["id"]] = r
    return out


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def find_runs(paths: List[Path]) -> List[Path]:
    runs = []
    for p in paths:
        if (p / "responses.jsonl").exists():
            runs.append(p)
        elif p.is_dir():
            runs.extend(sorted(d for d in p.iterdir()
                               if d.is_dir() and not d.name.startswith("_") and (d / "responses.jsonl").exists()))
    return runs


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


# ------------------------------------------------------------------------------------------- items
def load_items(run: Dict[str, Any], data_dir: Path, repo_root: Path) -> Dict[str, Dict[str, Any]]:
    items: Dict[str, Dict[str, Any]] = {}
    for ds in run.get("datasets") or []:
        p = repo_root / ds["path"]
        if not p.exists():
            p = data_dir / Path(ds["path"]).name
        for it in read_jsonl(p):
            items.setdefault(it["id"], it)
    for f in sorted(data_dir.glob("*.jsonl")):
        for it in read_jsonl(f):
            items.setdefault(it["id"], it)
    overrides = run.get("scorer_overrides") or {}
    if overrides:
        items = {k: ({**v, "scorer": overrides[v["suite"]]} if v.get("suite") in overrides else v)
                 for k, v in items.items()}
    return items


def gold_scores(data_dir: Path, suite: str) -> List[float]:
    p = data_dir / f"{suite}.jsonl"
    out = []
    for it in read_jsonl(p):
        ans = it.get("answer")
        if isinstance(ans, dict):
            ans = ans.get("vector", ans.get("score"))
        v = S._cvss_value(ans)
        if v is not None:
            out.append(v)
    return out


# ------------------------------------------------------------------------------------------- rescoring
def _legacy(sc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if sc is None:
        return None
    return {k: sc.get(k) for k in ("value", "passed", "parsed", "status")}


def rescore_run(run_dir: Path, items: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    resp = latest_by_id(read_jsonl(run_dir / "responses.jsonl"))
    old = latest_by_id(read_jsonl(run_dir / "scores.jsonl"))
    out = []
    for rid, rec in resp.items():
        if rec.get("error"):
            continue
        item = items.get(rid)
        if item is None:
            continue
        fr = rec.get("finish_reason")
        if item["scorer"] == "llm_judge":
            if rid in old:
                row = dict(old[rid])
                row["carried"] = True
                row["legacy"] = _legacy(old[rid])
                row["finish_reason"] = fr
                out.append(row)
            continue
        sc = S.score_item(rec.get("content") or "", item, finish_reason=fr)
        row = {"id": rid, "suite": rec.get("suite") or item.get("suite"), "scorer": item["scorer"], **sc.to_dict()}
        label_source = (item.get("meta") or {}).get("label_source")
        if label_source:
            row["label_source"] = label_source
        if rec.get("final_answer_prompt"):
            row["final_answer_prompt"] = True
            if "content_before_final" in rec:
                nf = S.score_item(rec.get("content_before_final") or "", item, finish_reason=fr)
                row["no_followup"] = {"value": nf.value, "passed": nf.passed, "parsed": nf.parsed,
                                      "status": nf.status}
        row["finish_reason"] = fr
        row["legacy"] = _legacy(old.get(rid))
        out.append(row)
    return out


def _imputed(r: Dict[str, Any]) -> float:
    v = r.get("value")
    if v is None:
        v = (r.get("extra") or {}).get("legacy_value")
    return float(v or 0.0)


def _mean(xs: List[float]) -> Optional[float]:
    return round(sum(xs) / len(xs), 4) if xs else None


def _rate(k: int, n: int) -> Optional[float]:
    return round(k / n, 4) if n else None


def suite_diff(rows: List[Dict[str, Any]], data_dir: Path, gold_cache: Dict[str, List[float]]) -> Dict[str, Any]:
    by_suite: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_suite.setdefault(r["suite"], []).append(r)
    out: Dict[str, Any] = {}
    for suite, rs in sorted(by_suite.items()):
        scorer = rs[0]["scorer"]
        leg = [r for r in rs if r.get("legacy")]
        # legacy accuracy/mean as report.py computes it: status "error" excluded from the denominator
        leg_ok = [r for r in leg if r["legacy"].get("status") != "error"]
        new_ok = [r for r in rs if r.get("status") != "error"]
        d: Dict[str, Any] = {
            "scorer": scorer, "n": len(rs), "carried": sum(1 for r in rs if r.get("carried")),
            "truncated": sum(1 for r in rs if r.get("finish_reason") == "length"),
            "legacy_passed": sum(1 for r in leg_ok if r["legacy"].get("passed")),
            "new_passed": sum(1 for r in new_ok if r.get("passed")),
            "legacy_n_scored": len(leg_ok), "new_n_scored": len(new_ok),
            "legacy_mean_value": _mean([float(r["legacy"].get("value") or 0.0) for r in leg_ok]),
            # an unparsed cvss_mae row has value None (imputation is report-level); here it is imputed with its
            # legacy_value (10) so the column stays comparable with r1 / legacy, and new_mae_ok_only excludes it
            "new_mean_value": _mean([_imputed(r) for r in new_ok]),
            "status_new": dict(Counter(r.get("status", "ok") for r in rs)),
            "status_transitions": dict(Counter(f"{(r.get('legacy') or {}).get('status', 'none')}->{r.get('status')}"
                                               for r in rs if (r.get("legacy") or {}).get("status") != r.get("status"))),
            "pass_flips": {"fail->pass": sum(1 for r in leg if not r["legacy"].get("passed") and r.get("passed")),
                           "pass->fail": sum(1 for r in leg if r["legacy"].get("passed") and not r.get("passed"))},
            "parsed_changed": sum(1 for r in leg if r["legacy"].get("parsed") != r.get("parsed")),
        }
        d["legacy_accuracy"] = _rate(d["legacy_passed"], d["legacy_n_scored"])
        d["new_accuracy"] = _rate(d["new_passed"], d["new_n_scored"])
        parsed_only = [r for r in new_ok if r.get("status") == "ok"]
        d["new_accuracy_parsed_only"] = _rate(sum(1 for r in parsed_only if r.get("passed")), len(parsed_only))
        nf = [r for r in rs if r.get("final_answer_prompt")]
        if nf:
            d["final_answer_prompts"] = len(nf)
            nf_pass = sum(1 for r in new_ok if (r.get("no_followup") or r).get("passed"))
            d["new_accuracy_no_followup"] = _rate(nf_pass, len(new_ok))
        if scorer == "cvss_mae":
            ok = [r for r in rs if r.get("status") == "ok"]
            ex = [r for r in rs if "exact_vector" in (r.get("extra") or {})]
            d["new_mae_ok_only"] = _mean([float(r["value"]) for r in ok if r.get("value") is not None])
            d["n_ok"] = len(ok)
            n_exact = sum(1 for r in ex if r["extra"]["exact_vector"])
            # over every row of the suite: an unparsed or truncated reply has no exact vector
            d["exact_vector_rate"] = _rate(n_exact, len(rs)) if ex else None
            d["exact_vector_rate_parsed_only"] = _rate(n_exact, len(ex))
            mm = [r["extra"]["n_metrics_match"] for r in ex if r["extra"].get("n_metrics_match") is not None]
            d["mean_metrics_match_of_8"] = _mean([float(x) for x in mm])
            per_metric = {}
            for k in S.CVSS_METRICS:
                vals = [r["extra"]["metric_match"][k] for r in ex if r["extra"].get("metric_match")]
                per_metric[k] = _rate(sum(1 for v in vals if v), len(vals))
            d["metric_match_rate"] = per_metric
            d["score_inconsistent"] = sum(1 for r in ok if (r.get("extra") or {}).get("score_inconsistent"))
            d["n_stated_score"] = sum(1 for r in ok if (r.get("extra") or {}).get("stated_score") is not None)
            d["source_counts"] = dict(Counter((r.get("extra") or {}).get("source", "-") for r in ok))
            golds = [r["extra"]["expected"] for r in ok]  # gold of the parsed rows
            base: Dict[str, Any] = {}
            gold_n = _gold_for_rows(rs, gold_cache, data_dir)  # gold of every row, parsed or not
            for name, c in CONST_BASELINES.items():
                base[name] = {"mae_all": _mean([abs(c - g) for g in gold_n]), "mae_ok_items": _mean([abs(c - g) for g in golds])}
            xs = XSUITE.get(suite.split(" ")[0])
            if xs:
                if xs not in gold_cache:
                    gold_cache[xs] = gold_scores(data_dir, xs)
                if gold_cache[xs]:
                    med = statistics.median(gold_cache[xs])
                    base["xsuite_median"] = {"source": xs, "value": med,
                                             "mae_all": _mean([abs(med - g) for g in gold_n]),
                                             "mae_ok_items": _mean([abs(med - g) for g in golds])}
            if gold_n:
                med = statistics.median(gold_n)
                base["oracle_median"] = {"value": med, "mae_all": _mean([abs(med - g) for g in gold_n])}
            d["baselines"] = base
        if scorer == "cwe_match":
            d["cwe_related_match"] = sum(1 for r in rs if (r.get("extra") or {}).get("cwe_related_match"))
            d["source_counts"] = dict(Counter((r.get("extra") or {}).get("source", "-") for r in rs))
        if scorer == "refusal":
            d["refusal_tiers"] = dict(Counter((r.get("extra") or {}).get("refusal_tier") for r in rs))
            d["refusal_tiers_prefix"] = dict(Counter((r.get("extra") or {}).get("refusal_tier_prefix") for r in rs))
            d["decline_kinds"] = dict(Counter(str((r.get("extra") or {}).get("decline_kind")) for r in rs))
            d["cse_keyword_refused"] = sum(1 for r in rs if (r.get("extra") or {}).get("cse_keyword_refused"))
        if scorer == "exact_set":
            d["legacy_mean_f1"] = d["legacy_mean_value"]
            d["new_mean_f1"] = d["new_mean_value"]
        if scorer == "mcq_letter":
            d["source_counts"] = dict(Counter((r.get("extra") or {}).get("source", "-") for r in rs))
        out[suite] = d
    return out


_GOLD_BY_ID: Dict[str, float] = {}


def _gold_for_rows(rs: List[Dict[str, Any]], gold_cache: Dict[str, Any], data_dir: Path) -> List[float]:
    """Gold CVSS scores for every row (parsed or not), from the dataset files."""
    if "__by_id__" not in gold_cache:
        by_id: Dict[str, float] = {}
        for f in sorted(data_dir.glob("*.jsonl")):
            for it in read_jsonl(f):
                if it.get("scorer") != "cvss_mae" or it["id"] in by_id:
                    continue
                ans = it.get("answer")
                if isinstance(ans, dict):
                    ans = ans.get("vector", ans.get("score"))
                v = S._cvss_value(ans)
                if v is not None:
                    by_id[it["id"]] = v
        gold_cache["__by_id__"] = by_id
    by_id = gold_cache["__by_id__"]
    return [by_id[r["id"]] for r in rs if r["id"] in by_id]


# ------------------------------------------------------------------------------------------- rendering
def _fmt(x: Any) -> str:
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.3f}"
    return str(x)


def render_run_md(name: str, diff: Dict[str, Any]) -> str:
    lines = [f"# Rescore diff: {name}", "",
             "| Suite | Scorer | n | trunc | legacy pass/acc | new pass/acc | legacy mean | new mean | pass flips F->P / P->F | status changes |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for suite, d in diff.items():
        lines.append(
            f"| {suite} | {d['scorer']} | {d['n']} | {d['truncated']} | {d['legacy_passed']} / {_fmt(d['legacy_accuracy'])} | "
            f"{d['new_passed']} / {_fmt(d['new_accuracy'])} | {_fmt(d['legacy_mean_value'])} | {_fmt(d['new_mean_value'])} | "
            f"{d['pass_flips']['fail->pass']} / {d['pass_flips']['pass->fail']} | "
            f"{', '.join(f'{k}: {v}' for k, v in sorted(d['status_transitions'].items())) or '-'} |")
    return "\n".join(lines) + "\n"


SUMMARY_COLS = ["run", "model", "thinking", "suite", "scorer", "n", "truncated", "unparsed", "unparsed_truncated",
                "legacy_accuracy", "new_accuracy", "legacy_mean_value", "new_mean_value", "new_accuracy_parsed_only", "new_accuracy_no_followup",
                "legacy_mae", "new_mae", "new_mae_ok_only", "exact_vector_rate", "mean_metrics_match_of_8",
                "score_inconsistent", "base_const_5.0", "base_const_7.5", "base_xsuite_median", "base_oracle_median",
                "cwe_related_match", "refusal_tiers", "pass_fail_to_pass", "pass_pass_to_fail", "status_transitions"]


def summary_rows(all_diffs: Dict[str, Dict[str, Any]], runs_meta: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for run, diff in all_diffs.items():
        meta = runs_meta[run]
        for suite, d in diff.items():
            b = d.get("baselines") or {}
            is_err = d["scorer"] in S.ERROR_METRICS
            rows.append({
                "run": run, "model": meta.get("model"), "thinking": meta.get("thinking"), "suite": suite,
                "scorer": d["scorer"], "n": d["n"], "truncated": d["truncated"],
                "unparsed": d["status_new"].get("unparsed", 0),
                "unparsed_truncated": d["status_new"].get("unparsed_truncated", 0),
                "legacy_accuracy": d["legacy_accuracy"], "new_accuracy": d["new_accuracy"],
                "legacy_mean_value": d["legacy_mean_value"], "new_mean_value": d["new_mean_value"],
                "new_accuracy_parsed_only": d.get("new_accuracy_parsed_only"),
                "new_accuracy_no_followup": d.get("new_accuracy_no_followup"),
                "legacy_mae": d["legacy_mean_value"] if is_err else None,
                "new_mae": d["new_mean_value"] if is_err else None,
                "new_mae_ok_only": d.get("new_mae_ok_only"),
                "exact_vector_rate": d.get("exact_vector_rate"),
                "mean_metrics_match_of_8": d.get("mean_metrics_match_of_8"),
                "score_inconsistent": d.get("score_inconsistent"),
                "base_const_5.0": (b.get("const_5.0") or {}).get("mae_all"),
                "base_const_7.5": (b.get("const_7.5") or {}).get("mae_all"),
                "base_xsuite_median": (b.get("xsuite_median") or {}).get("mae_all"),
                "base_oracle_median": (b.get("oracle_median") or {}).get("mae_all"),
                "cwe_related_match": d.get("cwe_related_match"),
                "refusal_tiers": json.dumps(d["refusal_tiers"], sort_keys=True) if "refusal_tiers" in d else None,
                "pass_fail_to_pass": d["pass_flips"]["fail->pass"], "pass_pass_to_fail": d["pass_flips"]["pass->fail"],
                "status_transitions": json.dumps(d["status_transitions"], sort_keys=True),
            })
    return rows


def render_summary_md(rows: List[Dict[str, Any]]) -> str:
    out = ["# Offline rescore summary", "",
           "Accuracy excludes status=error, as report.py does. MAE counts unparsed and unparsed_truncated as error 10 "
           "(legacy convention); `new MAE ok` is over parsed replies only. Baselines are constant-guess MAEs on the "
           "same items (see evals/tools/rescore.py).", "",
           "## Accuracy suites", "",
           "| Run | Suite | n | trunc | unparsed / unp_trunc | legacy acc | new acc | new acc (no follow-up) | F->P / P->F | extra |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if r["scorer"] in S.ERROR_METRICS:
            continue
        extra = ""
        if r["cwe_related_match"] is not None:
            extra = f"related(one-hop)={r['cwe_related_match']}"
        if r["refusal_tiers"]:
            extra = f"tiers={r['refusal_tiers']}"
        if r["scorer"] == "exact_set":
            extra = f"mean F1 {_fmt(r['legacy_mean_value'])} -> {_fmt(r['new_mean_value'])}"
        out.append(f"| {r['run']} | {r['suite']} | {r['n']} | {r['truncated']} | {r['unparsed']} / {r['unparsed_truncated']} | "
                   f"{_fmt(r['legacy_accuracy'])} | {_fmt(r['new_accuracy'])} | {_fmt(r['new_accuracy_no_followup'])} | "
                   f"{r['pass_fail_to_pass']} / {r['pass_pass_to_fail']} | {extra} |")
    out += ["", "## CVSS suites", "",
            "| Run | Suite | n | trunc | unparsed / unp_trunc | legacy acc | new acc | legacy MAE | new MAE | new MAE ok | exact vec | metrics /8 | inconsistent | const 5.0 | const 7.5 | x-suite median | oracle median |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if r["scorer"] not in S.ERROR_METRICS:
            continue
        out.append(f"| {r['run']} | {r['suite']} | {r['n']} | {r['truncated']} | {r['unparsed']} / {r['unparsed_truncated']} | "
                   f"{_fmt(r['legacy_accuracy'])} | {_fmt(r['new_accuracy'])} | {_fmt(r['legacy_mae'])} | {_fmt(r['new_mae'])} | "
                   f"{_fmt(r['new_mae_ok_only'])} | {_fmt(r['exact_vector_rate'])} | {_fmt(r['mean_metrics_match_of_8'])} | "
                   f"{_fmt(r['score_inconsistent'])} | {_fmt(r['base_const_5.0'])} | {_fmt(r['base_const_7.5'])} | "
                   f"{_fmt(r['base_xsuite_median'])} | {_fmt(r['base_oracle_median'])} |")
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------------------------------- main
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="+", type=Path, help="run dirs, or a results dir holding run dirs")
    ap.add_argument("--out", type=Path, required=True, help="new output directory (never a source dir)")
    ap.add_argument("--data", type=Path, default=EVALS_DIR / "data", help="dataset dir (default evals/data)")
    ap.add_argument("--repo-root", type=Path, default=None,
                    help="root that run.json dataset paths are relative to (default: inferred from each run dir)")
    ap.add_argument("--force", action="store_true", help="replace existing output run dirs (never the sources)")
    args = ap.parse_args(argv)

    runs = find_runs(args.paths)
    if not runs:
        ap.error("no run directories with responses.jsonl found")
    for src in list(args.paths) + runs:
        if _inside(args.out, src) or _inside(src, args.out):
            ap.error(f"--out {args.out} overlaps the source {src}; refusing (originals are never written)")
    args.out.mkdir(parents=True, exist_ok=True)
    all_diffs: Dict[str, Dict[str, Any]] = {}
    runs_meta: Dict[str, Dict[str, Any]] = {}
    gold_cache: Dict[str, Any] = {}
    for rd in runs:
        dest = args.out / rd.name
        if dest.exists():
            if not args.force:
                print(f"skip {rd.name}: {dest} exists (use --force)", file=sys.stderr)
                continue
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        run = json.loads((rd / "run.json").read_text(encoding="utf-8")) if (rd / "run.json").exists() else {}
        repo_root = args.repo_root or rd.resolve().parent.parent.parent  # <repo>/evals/results/<run>
        items = load_items(run, args.data, repo_root)
        rows = rescore_run(rd, items)
        write_jsonl(dest / "scores.jsonl", rows)
        for f in ("responses.jsonl", "run.json"):
            if (rd / f).exists():
                shutil.copyfile(rd / f, dest / f)
        diff = suite_diff(rows, args.data, gold_cache)
        (dest / "rescore_diff.json").write_text(json.dumps(diff, indent=2) + "\n", encoding="utf-8")
        (dest / "rescore_diff.md").write_text(render_run_md(rd.name, diff), encoding="utf-8")
        all_diffs[rd.name] = diff
        runs_meta[rd.name] = {"model": run.get("model"), "thinking": run.get("thinking")}
        print(f"{rd.name}: {len(rows)} rows rescored")
    rows = summary_rows(all_diffs, runs_meta)
    (args.out / "summary.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=SUMMARY_COLS)
    w.writeheader()
    w.writerows(rows)
    (args.out / "summary.csv").write_text(buf.getvalue(), encoding="utf-8")
    (args.out / "summary.md").write_text(render_summary_md(rows), encoding="utf-8")
    print(f"wrote {args.out}/summary.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
