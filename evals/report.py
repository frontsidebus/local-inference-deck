#!/usr/bin/env python3
"""Compare eval runs: one table per suite, one row per run (model + thinking mode).

    python3 evals/report.py evals/results/<run-dir> [<run-dir> ...] [--out report.md] [--csv report.csv]
    python3 evals/report.py --latest 4          # the 4 newest run dirs under evals/results/
    python3 evals/report.py <run-dirs> --split-label-source [--items evals/data/nvd-cwe.jsonl ...]

Columns: n, passed, accuracy with a Wilson 95% interval, mean score (partial credit, F1, or the MAE for
cvss_mae), mean latency, end-to-end tokens/s, and failure counts:
- api: the request failed after all retries;
- trunc: the reply stopped at max_tokens;
- unparsed: no answer could be extracted (counted as wrong);
- item/grader: the item or the grader is broken (excluded from accuracy).

Refusal suites (scorer ``refusal``) also get an opener-refusal rate: replies whose opening is a refusal, even when
CyberSecEval's code-block exemption counts them as compliance (see scorers.opens_with_refusal).

With two or more runs, a "Paired comparisons" section gives, per suite and pair of runs, the items only one of them
passed and an exact McNemar p-value over the items both scored. It is the right test for two models on the same
items; overlapping Wilson intervals are a much weaker signal.

``--split-label-source`` adds a row per ``meta.label_source`` value (for example the NVD suites' CNA vs NVD labels)
next to the whole suite, as ``<suite> [label_source=<value>]``. New runs record the label in scores.jsonl; for runs
made before that, pass the suite JSONL files with ``--items`` and the labels are looked up by item id.

The summary is recomputed from responses.jsonl and scores.jsonl, so it works on partial runs.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

EVALS_DIR = Path(__file__).resolve().parent
ERROR_METRICS = {"cvss_mae"}  # same as scorers.ERROR_METRICS (kept here so report.py stands alone)
RESULTS_DIR = EVALS_DIR / "results"
Z95 = 1.959963984540054


def wilson(k: int, n: int, z: float = Z95) -> Tuple[float, float]:
    """Wilson score interval for k successes in n trials; (0, 1) when n = 0."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


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


def summarize(run_dir: Path, split_label_source: bool = False,
              labels: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Per-suite statistics for one run dir (responses.jsonl + scores.jsonl + run.json)."""
    run_dir = Path(run_dir)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8")) if (run_dir / "run.json").exists() else {}
    resp = latest_by_id(read_jsonl(run_dir / "responses.jsonl"))
    scores = latest_by_id(read_jsonl(run_dir / "scores.jsonl"))
    suites: Dict[str, Dict[str, Any]] = {}
    for rid, r in resp.items():
        for key in suite_keys(r["suite"], rid, scores.get(rid), split_label_source, labels):
            _add(suites, key, rid, r, scores.get(rid))
    for name, s in suites.items():
        n, k = s["n_scored"], s["passed"]
        lo, hi = wilson(k, n)
        s["accuracy"] = (k / n) if n else None
        s["ci95"] = [round(lo, 4), round(hi, 4)]
        n_score = n - s["n_err_metric"]
        s["mean_value"] = (s["value_sum"] / n_score) if n_score else None
        s["mae"] = (s["err_sum"] / s["n_err_metric"]) if s["n_err_metric"] else None
        s["mean_latency_s"] = (s["latency_sum"] / s["n_latency"]) if s["n_latency"] else None
        s["tokens_per_s"] = (s["completion_tokens"] / s["gen_seconds"]) if s["gen_seconds"] else None
        s["cost_usd"] = round(s["cost_usd"], 4)
        if s["n_opener"]:
            s["opener_refusal_rate"] = s["opener_refusals"] / s["n_opener"]
            s["opener_refusal_ci95"] = [round(x, 4) for x in wilson(s["opener_refusals"], s["n_opener"])]
        else:
            for k2 in ("n_opener", "opener_refusals"):
                s.pop(k2)
        for k2 in ("value_sum", "latency_sum", "n_latency", "err_sum", "n_err_metric"):
            s.pop(k2)
        s["gen_seconds"] = round(s["gen_seconds"], 2)
    return {"run_dir": run_dir.name, "label": run_label(run), "run": run, "suites": suites}


def _add(suites: Dict[str, Dict[str, Any]], key: str, rid: str, r: Dict[str, Any],
         sc: Optional[Dict[str, Any]]) -> None:
    """Count one response (and its score) into the row ``key``."""
    s = suites.setdefault(key, {
        "n_items": 0, "n_scored": 0, "passed": 0, "value_sum": 0.0, "api_errors": 0, "truncated": 0,
        "unparsed": 0, "item_or_grader_errors": 0, "final_answer_prompts": 0, "latency_sum": 0.0, "n_latency": 0,
        "completion_tokens": 0, "prompt_tokens": 0, "gen_seconds": 0.0, "scorers": {}, "model_graded": False,
        "cost_usd": 0.0, "n_err_metric": 0, "err_sum": 0.0, "n_opener": 0, "opener_refusals": 0,
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
        return
    s["scorers"][sc.get("scorer")] = s["scorers"].get(sc.get("scorer"), 0) + 1
    if sc.get("model_graded"):
        s["model_graded"] = True
    st = sc.get("status", "ok")
    if st == "error":
        s["item_or_grader_errors"] += 1
        return
    if st == "unparsed":
        s["unparsed"] += 1
    if sc.get("final_answer_prompt"):
        s["final_answer_prompts"] += 1
    s["n_scored"] += 1
    s["passed"] += 1 if sc.get("passed") else 0
    if sc.get("scorer") in ERROR_METRICS:  # an error (lower is better), not a score: kept apart
        s["n_err_metric"] += 1
        s["err_sum"] += float(sc.get("value") or 0.0)
    else:
        s["value_sum"] += float(sc.get("value") or 0.0)
    opener = (sc.get("extra") or {}).get("opener_refusal")
    if opener is not None:
        s["n_opener"] += 1
        s["opener_refusals"] += 1 if opener else 0


# ------------------------------------------------------------------------------------------- paired comparisons
def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value: b and c are the discordant counts (only A passed, only B passed)."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def item_passes(run_dir: Path, split_label_source: bool = False,
                labels: Optional[Dict[str, str]] = None) -> Dict[str, Dict[str, bool]]:
    """row key -> {item id: passed} for items with a usable score (not api, item or grader errors)."""
    run_dir = Path(run_dir)
    resp = latest_by_id(read_jsonl(run_dir / "responses.jsonl"))
    out: Dict[str, Dict[str, bool]] = {}
    for rid, sc in latest_by_id(read_jsonl(run_dir / "scores.jsonl")).items():
        r = resp.get(rid)
        if r is None or r.get("error") or sc.get("status", "ok") == "error":
            continue
        for key in suite_keys(sc.get("suite") or r["suite"], rid, sc, split_label_source, labels):
            out.setdefault(key, {})[rid] = bool(sc.get("passed"))
    return out


def paired(summaries: List[Dict[str, Any]], passes: List[Dict[str, Dict[str, bool]]]) -> List[Dict[str, Any]]:
    """Per row key and pair of runs: shared items, wins each way, and the exact McNemar p."""
    out = []
    for suite in sorted({n for s in summaries for n in s["suites"]}):
        for i in range(len(summaries)):
            for j in range(i + 1, len(summaries)):
                a, b = passes[i].get(suite) or {}, passes[j].get(suite) or {}
                shared = sorted(set(a) & set(b))
                if not shared:
                    continue
                a_only = sum(1 for k in shared if a[k] and not b[k])
                b_only = sum(1 for k in shared if b[k] and not a[k])
                out.append({"suite": suite, "run_a": summaries[i]["label"], "run_b": summaries[j]["label"],
                            "n_shared": len(shared), "a_passed": sum(a[k] for k in shared),
                            "b_passed": sum(b[k] for k in shared), "a_only": a_only, "b_only": b_only,
                            "p_mcnemar": mcnemar_exact(a_only, b_only)})
    return out


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
    return "-" if x is None else format(x, spec)


COLUMNS = ["suite", "run", "n", "passed", "accuracy", "ci95_lo", "ci95_hi", "mean_score", "mae",
           "mean_latency_s", "tokens_per_s", "api_errors", "truncated", "unparsed", "item_or_grader_errors",
           "model_graded", "cost_usd", "opener_refusals", "opener_n", "opener_refusal_rate"]
PAIR_COLUMNS = ["suite", "run_a", "run_b", "n_shared", "a_passed", "b_passed", "a_only", "b_only", "p_mcnemar"]


def rows(summaries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    suite_names = sorted({n for s in summaries for n in s["suites"]})
    for suite in suite_names:
        for s in summaries:
            st = s["suites"].get(suite)
            if not st:
                continue
            out.append({
                "suite": suite, "run": s["label"], "run_dir": s["run_dir"], "n": st["n_scored"],
                "passed": st["passed"], "accuracy": st["accuracy"], "ci95_lo": st["ci95"][0], "ci95_hi": st["ci95"][1],
                "mean_score": st["mean_value"], "mae": st["mae"], "mean_latency_s": st["mean_latency_s"],
                "tokens_per_s": st["tokens_per_s"], "api_errors": st["api_errors"], "truncated": st["truncated"],
                "unparsed": st["unparsed"], "item_or_grader_errors": st["item_or_grader_errors"],
                "final_answer_prompts": st.get("final_answer_prompts", 0),
                "model_graded": st["model_graded"], "cost_usd": st["cost_usd"],
                "opener_refusals": st.get("opener_refusals"), "opener_n": st.get("n_opener"),
                "opener_refusal_rate": st.get("opener_refusal_rate"),
            })
    return out


def render_markdown(summaries: List[Dict[str, Any]], title: str = "Eval report",
                    pairs: Optional[List[Dict[str, Any]]] = None) -> str:
    lines = [f"# {title}", ""]
    lines.append("| Run | Dir | Model | Thinking | Temp | Seed | Git | Started (UTC) | Items |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for s in summaries:
        r = s["run"]
        samp = r.get("sampling") or {}
        n = sum(st["n_items"] for st in s["suites"].values())
        lines.append(f"| {s['label']} | `{s['run_dir']}` | `{r.get('model', '?')}` | {r.get('thinking', '-')} | "
                     f"{samp.get('temperature', '-')} | {samp.get('seed', '-')} | `{(r.get('git') or {}).get('sha', '?')[:10]}"
                     f"{'+dirty' if (r.get('git') or {}).get('dirty') else ''}` | {r.get('started', '-')} | {n} |")
    lines.append("")
    graded = False
    for suite in sorted({n for s in summaries for n in s["suites"]}):
        lines += [f"## {suite}", "",
                  "| Run | n | Passed | Accuracy | 95% CI | Mean score | Latency (s) | Tok/s | api / trunc / unparsed / item | final-answer prompts |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for row in [x for x in rows(summaries) if x["suite"] == suite]:
            acc = "-" if row["accuracy"] is None else f"{row['accuracy']:.1%}"
            ci = "-" if row["accuracy"] is None else f"{row['ci95_lo']:.1%} – {row['ci95_hi']:.1%}"
            parts = []
            if row["mean_score"] is not None:
                parts.append(_fmt(row["mean_score"]))
            if row["mae"] is not None:
                parts.append(f"MAE {_fmt(row['mae'], '.2f')}")
            ms = "; ".join(parts) or "-"
            if row["model_graded"]:
                ms += " *"
                graded = True
            lines.append(f"| {row['run']} | {row['n']} | {row['passed']} | {acc} | {ci} | {ms} | "
                         f"{_fmt(row['mean_latency_s'], '.2f')} | {_fmt(row['tokens_per_s'], '.1f')} | "
                         f"{row['api_errors']} / {row['truncated']} / {row['unparsed']} / {row['item_or_grader_errors']} | "
                         f"{row.get('final_answer_prompts', 0)} |")
        lines.append("")
        opener = [x for x in rows(summaries) if x["suite"] == suite and x.get("opener_n")]
        if opener:
            lines += ["Opener refusal (the reply opens with a refusal, even if a code block then counts it as "
                      "compliance above):", ""]
            for row in opener:
                lo, hi = wilson(row["opener_refusals"], row["opener_n"])
                lines.append(f"- {row['run']}: {row['opener_refusals']}/{row['opener_n']} = "
                             f"{row['opener_refusal_rate']:.1%} ({lo:.1%} – {hi:.1%})")
            lines.append("")
    if pairs:
        lines += ["## Paired comparisons", "",
                  "Items both runs scored. A only / B only: items just one of them passed. p: exact two-sided "
                  "McNemar test.", "",
                  "| Suite | A | B | Shared n | A passed | B passed | A only | B only | p |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for pr in pairs:
            lines.append(f"| {pr['suite']} | {pr['run_a']} | {pr['run_b']} | {pr['n_shared']} | {pr['a_passed']} | "
                         f"{pr['b_passed']} | {pr['a_only']} | {pr['b_only']} | {pr['p_mcnemar']:.3f} |")
        lines.append("")
    lines += ["## Notes", ""]
    if graded:
        graders = sorted({(s["run"].get("grader") or {}).get("model", "?") for s in summaries
                          if any(st["model_graded"] for st in s["suites"].values())})
        lines.append(f"- `*` llm_judge scores are **model-graded** (grader: {', '.join(graders)}), not ground truth. "
                     "Grader errors are counted in the last column and excluded from accuracy.")
    lines += [
        "- Opener refusal is our metric, not CyberSecEval's: the same keyword check on the first 220 characters "
        "without the code-block exemptions. The keyword figure above stays comparable with published CSE numbers.",
        "- Accuracy = passed / scored items. The 95% CI is a Wilson score interval; overlapping intervals mean the "
        "difference is not established at this n.",
        "- Mean score is the average partial credit (F1, set F1, field fraction, grader score / 10). cvss_mae items "
        "are reported separately as MAE: the mean absolute error in CVSS points (lower is better).",
        "- Tok/s is end-to-end: completion tokens / wall time per request, including prompt processing and retries.",
        "- Public benchmarks may be in the models' training data (contamination); compare models on the same items, "
        "and prefer private golden sets for decisions.",
        "",
    ]
    return "\n".join(lines)


def render_csv(summaries: List[Dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS + ["run_dir"], extrasaction="ignore")
    w.writeheader()
    for row in rows(summaries):
        w.writerow({k: ("" if v is None else v) for k, v in row.items()})
    return buf.getvalue()


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
                    help="suite files to look up label_source by id, for runs made before scores.jsonl recorded it")
    ap.add_argument("--pairs-csv", help="also write the paired comparisons as CSV here")
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
    labels = load_labels(a.items) if a.items else None
    summaries = unique_labels([summarize(d, a.split_label_source, labels) for d in dirs])
    pairs = paired(summaries, [item_passes(d, a.split_label_source, labels) for d in dirs]) if len(dirs) > 1 else []
    md = render_markdown(summaries, a.title, pairs)
    if a.out:
        Path(a.out).write_text(md, encoding="utf-8")
    else:
        sys.stdout.write(md)
    if a.csv:
        Path(a.csv).write_text(render_csv(summaries), encoding="utf-8")
    if a.pairs_csv:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=PAIR_COLUMNS)
        w.writeheader()
        w.writerows(pairs)
        Path(a.pairs_csv).write_text(buf.getvalue(), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
