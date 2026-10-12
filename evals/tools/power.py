#!/usr/bin/env python3
"""A-priori power and sample size for the comparisons in a pre-registration file.

    python3 evals/tools/power.py --comparisons evals/plans/comparisons-C1.json --n ctibench-mcq=698 --n nvd-cwe=2009
    python3 evals/tools/power.py --comparisons plan.json --json          # machine-readable rows

Every comparison needs its design assumptions, written before the run, in an ``assumed`` block on the comparison
or in ``defaults.assumed`` (per metric), for example::

    "assumed": {"p_b": 0.65, "psi": 0.20, "delta": 0.10}     # accuracy: B's accuracy, discordant share, effect
    "assumed": {"sd_diff": 1.2, "delta": 0.3}                 # mae: SD of per-item differences, effect

A calibration-planning mode (``--kappa 0.75 --kappa-n 150``) gives the probability that the lower 95% Wald CI of
Cohen's kappa clears the threshold (``stats.power_kappa_lower_ci``).

``delta`` is the effect the decision needs to detect (directional comparisons). For an equivalence comparison it is
the assumed true difference (default 0), and the margin comes from the comparison. The planned n per suite comes
from ``--n suite=N``, the comparison's ``n``, or the plan's ``planned_n``.

The alpha used is the worst case the analysis can apply: alpha / m for a Holm family of m comparisons (the first
Holm step), and alpha for an intersection-union family (no adjustment). The tests are the ones report.py runs:
- paired accuracy, directional: exact power of the exact McNemar test (``stats.power_mcnemar_exact``);
- paired accuracy, equivalence: exact power of the size-correct TOST report.py runs (Tango score statistic,
  exact unconditional p; ``stats.power_tost_paired_exact``); its "n for 80%" is found by bisection without the
  saw-tooth guard (each evaluation is costly);
- MAE and other means: normal-approximation power of the paired mean difference or its TOST.
The exact McNemar power is saw-toothed in n (the test is discrete), so "n for 80%" is found by bisection and can
sit a few items off the true smallest n; check the power at the planned n, which is exact.
An IUT family's power is reported as the product of its members' powers (exact if the tests were independent;
the members share items, so treat it as an approximation).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

EVALS = Path(__file__).resolve().parent.parent
if str(EVALS) not in sys.path:
    sys.path.insert(0, str(EVALS))
import stats as ST  # noqa: E402

TARGET = 0.8
BINARY = ("accuracy", "opener_refusal", "refusal_full", "refusal_partial", "refusal_refuse", "accuracy_either")


def _assumed(plan: Dict[str, Any], c: Dict[str, Any], metric: str) -> Dict[str, Any]:
    d = dict(((plan.get("defaults") or {}).get("assumed") or {}).get(metric) or {})
    d.update(c.get("assumed") or {})
    return d


def comparison_power(metric: str, direction: str, n: Optional[int], a: Dict[str, Any], alpha: float,
                     margin: Optional[float] = None, n_sim: int = 2000, seed: int = ST.DEFAULT_SEED,
                     seed_group: bool = False, want_n80: bool = True, seed_pairs: int = 1) -> Dict[str, Any]:
    """Power at n, the n for 80% power, and (directional) the MDE at n, for one comparison, under the test the
    report actually runs: exact McNemar / exact Tango TOST for two single runs on a binary metric; for seed groups
    and continuous metrics the per-item mean difference (sign-flip test, t-TOST), modelled by the normal
    approximation with sd_diff (for a seed group without an assumed sd_diff, the single-seed sd sqrt(psi - delta^2)
    is used: averaging over seeds can only shrink it, so the power is conservative; noted in the row)."""
    out: Dict[str, Any] = {"alpha": alpha, "n": n, "power": None, "n_for_80": None, "mde_at_n": None, "missing": []}
    if seed_group and metric in BINARY and direction == "equivalent":
        # the report decides seed-group equivalence on a proportion by the exact Tango TOST of every seed pairing
        # (all must pass): power >= the product of the per-pairing powers (positively associated tests)
        if a.get("psi") is None:
            out["missing"] = ["psi"]
            return out
        delta = float(a.get("delta", 0.0))

        def pw(k: int) -> float:
            return ST.power_tost_paired_exact(k, float(a["psi"]), float(margin), delta, alpha) ** max(1, seed_pairs)
        out["note"] = f"seed group: exact Tango TOST per seed pairing ({seed_pairs}), product = lower bound"
        if n:
            out["power"] = pw(n)
        if want_n80:
            out["n_for_80"] = ST.n_for_power(pw, TARGET, n_max=4000, guard=False)
        return out
    binary = metric in BINARY and not seed_group
    if seed_group and metric in BINARY and a.get("sd_diff") is None and a.get("psi") is not None:
        d0 = float(a.get("delta", 0.0))
        if float(a["psi"]) > abs(d0):
            a = dict(a, sd_diff=(float(a["psi"]) - d0 * d0) ** 0.5)
            out["note"] = "seed group: sd_diff from psi (single-seed bound, conservative)"
    if binary:
        need = ["p_b", "psi"] + (["delta"] if direction != "equivalent" else [])
    else:
        need = ["sd_diff"] + (["delta"] if direction != "equivalent" else [])
    out["missing"] = [k for k in need if a.get(k) is None]
    if direction == "equivalent" and margin is None:
        out["missing"].append("margin")
    if out["missing"]:
        return out
    delta = float(a.get("delta", 0.0))
    if binary and float(a["psi"]) <= abs(delta):
        # every discordant pair would go one way: no 2x2 table has more effect than discordance
        out["degenerate"] = f"psi {a['psi']} <= |delta| {abs(delta)}: not a possible design (psi must exceed |delta|)"
        return out
    if binary and direction != "equivalent":
        def pw(k: int) -> float:
            return ST.power_mcnemar_exact(k, float(a["psi"]), delta, alpha)
        if n:
            out["mde_at_n"] = ST.mde_mcnemar_exact(n, float(a["psi"]), alpha)
    elif binary:
        def pw(k: int) -> float:
            return ST.power_tost_paired_exact(k, float(a["psi"]), float(margin), delta, alpha)
    elif direction != "equivalent":
        def pw(k: int) -> float:
            return ST.power_mean_diff(k, float(a["sd_diff"]), delta, alpha)
        if n:
            out["mde_at_n"] = ST.mde_paired_mean(float(a["sd_diff"]), n, alpha)
    else:
        def pw(k: int) -> float:
            return ST.power_tost_mean(k, float(a["sd_diff"]), float(margin), delta, alpha)
    if n:
        out["power"] = pw(n)
    if not want_n80:
        return out
    if binary and direction == "equivalent":
        out["n_for_80"] = ST.n_for_power(pw, TARGET, n_max=4000, guard=False)
    else:
        out["n_for_80"] = ST.n_for_power(pw, TARGET, n_max=200000)
    return out


def n_after_exclusions(items: List[Dict[str, Any]], suite: str, ops: List[str], heldout_only: bool = False) -> int:
    """How many items of a suite survive a primary's model-blind exclusions (item flags only; keep-only strata and
    per-model flags such as post_cutoff are not applied). With ``heldout_only``, only sealed items count."""
    import report as R
    n = 0
    for it in items:
        if it.get("suite") != suite:
            continue
        rg = (it.get("meta") or {}).get("rigor") or {}
        if heldout_only and not rg.get("heldout"):
            continue
        leak, name = R._leak_flags(None, it, stripped_seen=True)
        rec = {"followup": False, "truncated": False,
               "flags": {"has_signal": rg.get("has_signal"), "leak": leak, "name_in_text": name,
                         "dev_cluster_exposed": rg.get("dev_cluster_exposed"),
                         "dev_neardup_exposed": rg.get("dev_neardup_exposed"), "frr_exclude": rg.get("frr_exclude"),
                         "post_cutoff": True, "key_longest": R._key_longest(it)}}
        if not R.item_excluded([rec], tuple(o for o in ops if o not in R.KEEP_ONLY)):
            n += 1
    return n


def planned_n_from_items(plan: Dict[str, Any], items: List[Dict[str, Any]], heldout_only: bool = False) -> Dict[str, int]:
    """Per comparison id: the after-exclusion n of its suite under its declared primary."""
    import report as R
    out = {}
    for fam in plan.get("families") or []:
        for c in fam.get("comparisons") or []:
            out[c["id"]] = n_after_exclusions(items, c["suite"], list(R.primary_spec(plan, c)["ops"]), heldout_only)
    return out


def plan_power(plan: Dict[str, Any], n_by_suite: Optional[Dict[str, int]] = None, n_sim: int = 2000,
               n_by_id: Optional[Dict[str, int]] = None) -> List[Dict[str, Any]]:
    """One row per comparison (plus one per IUT family) with the a-priori power at the planned n (per comparison
    from ``n_by_id``, e.g. the after-exclusion n from ``planned_n_from_items``; else per suite)."""
    n_by_suite = dict(plan.get("planned_n") or {}, **(n_by_suite or {}))
    n_by_id = n_by_id or {}
    defaults = plan.get("defaults") or {}
    alpha_all = float(plan.get("alpha", 0.05))
    rows: List[Dict[str, Any]] = []
    for fam in plan.get("families") or []:
        comps = fam.get("comparisons") or []
        test = fam.get("test", "holm")
        alpha = float(fam.get("alpha", alpha_all))
        a_eff = alpha if test == "iut" else alpha / max(1, len(comps))
        fam_rows = []
        for c in comps:
            metric = c.get("metric", defaults.get("metric", "accuracy"))
            direction = c.get("direction", "two-sided")
            margin = c.get("margin", defaults.get("margin")) if direction == "equivalent" else None
            n = n_by_id.get(c["id"], c.get("n", n_by_suite.get(c["suite"])))
            r = comparison_power(metric, direction, int(n) if n else None, _assumed(plan, c, metric), a_eff, margin,
                                 n_sim, seed_group=isinstance(c.get("a"), list) or isinstance(c.get("b"), list),
                                 seed_pairs=(len(c["a"]) if isinstance(c.get("a"), list) else 1) *
                                 (len(c["b"]) if isinstance(c.get("b"), list) else 1))
            r.update({"id": c["id"], "family": fam.get("name"), "test": test, "suite": c["suite"], "metric": metric,
                      "direction": direction, "margin": margin})
            r["underpowered"] = r["power"] is not None and r["power"] < TARGET
            fam_rows.append(r)
        rows += fam_rows
        if test == "iut" and fam_rows:
            ps = [r["power"] for r in fam_rows]
            prod = None
            if all(p is not None for p in ps):
                prod = 1.0
                for p in ps:
                    prod *= p
            rows.append({"id": f"{fam.get('name')} (IUT family)", "family": fam.get("name"), "test": "iut",
                         "suite": "-", "metric": "-", "direction": "all members", "margin": None, "alpha": alpha,
                         "n": None, "power": prod, "n_for_80": None, "mde_at_n": None, "missing": [],
                         "underpowered": prod is not None and prod < TARGET})
    return rows


def render(rows: List[Dict[str, Any]]) -> str:
    f = lambda x, s: "-" if x is None else format(x, s)  # noqa: E731
    out = ["| ID | Suite | Metric | Claim | alpha used | planned n | power | n for 80% | MDE at n | note |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        claim = r["direction"] + (f" ±{r['margin']:g}" if r.get("margin") is not None else "")
        if r.get("degenerate"):
            note = r["degenerate"]
        elif r.get("missing"):
            note = "missing assumptions: " + ", ".join(r["missing"])
        else:
            note = "; ".join(x for x in (r.get("note"), "**underpowered**" if r.get("underpowered") else "") if x)
        out.append(f"| {r['id']} | {r['suite']} | {r['metric']} | {claim} | {f(r['alpha'], '.4f')} | "
                   f"{f(r['n'], 'd')} | {f(r['power'], '.2f')} | {f(r['n_for_80'], 'd')} | {f(r['mde_at_n'], '.3f')} | "
                   f"{note} |")
    return "\n".join(out) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--comparisons", help="pre-registration file (JSON)")
    ap.add_argument("--n", action="append", default=[], metavar="SUITE=N", help="planned n for a suite")
    ap.add_argument("--items", action="append", default=[], metavar="JSONL",
                    help="annotated item files: the planned n per comparison is the after-exclusion n of its suite")
    ap.add_argument("--heldout-only", action="store_true", help="with --items: count sealed (heldout) items only")
    ap.add_argument("--sims", type=int, default=2000, help="simulations (kappa power)")
    ap.add_argument("--kappa", type=float, help="calibration planning: the true judge-vs-human kappa")
    ap.add_argument("--kappa-n", type=int, action="append", default=[], help="calibration n to evaluate")
    ap.add_argument("--prevalence", type=float, default=0.5, help="calibration: human pass prevalence")
    ap.add_argument("--threshold", type=float, default=0.6, help="calibration: required lower 95%% CI of kappa")
    ap.add_argument("--units-per-item", type=int, default=1, help="calibration: rated units per item (clustering)")
    ap.add_argument("--tau", type=float, default=0.1, help="calibration: sd of item-level kappa")
    ap.add_argument("--json", action="store_true", help="print JSON rows instead of a table")
    ap.add_argument("--out", help="also write the table here (e.g. power_table.md, which PROTOCOL.md quotes)")
    a = ap.parse_args(argv)
    if a.kappa is not None:
        rows = [{"kappa": a.kappa, "n": n, "prevalence": a.prevalence, "threshold": a.threshold,
                 "power": ST.power_kappa_cluster(a.kappa, max(1, n // a.units_per_item), a.units_per_item,
                                                 a.prevalence, a.tau, a.threshold, n_sim=min(a.sims, 400))}
                for n in (a.kappa_n or [60, 90, 120, 150, 200, 300])]
        if a.json:
            sys.stdout.write(json.dumps(rows, indent=1) + "\n")
        else:
            sys.stdout.write("| kappa | prevalence | n | P(lower 95% CI >= threshold) |\n|---|---|---|---|\n" +
                             "".join(f"| {r['kappa']} | {r['prevalence']} | {r['n']} | {r['power']:.2f} |\n"
                                     for r in rows))
        if not a.comparisons:
            return 0
    if not a.comparisons:
        ap.error("give --comparisons (or --kappa for calibration planning)")
    plan = json.loads(Path(a.comparisons).read_text(encoding="utf-8"))
    n_by = {}
    for kv in a.n:
        k, _, v = kv.partition("=")
        if not v.isdigit():
            ap.error(f"--n wants SUITE=N, got {kv!r}")
        n_by[k] = int(v)
    n_id = None
    if a.items:
        import report as R
        its = list(R.load_items([Path(p) for p in a.items]).values())
        n_id = planned_n_from_items(plan, its, a.heldout_only)
    rows = plan_power(plan, n_by, a.sims, n_id)
    table = render(rows)
    sys.stdout.write(json.dumps(rows, indent=1) + "\n" if a.json else table)
    if a.out:
        head = (f"<!-- generated by evals/tools/power.py --comparisons {Path(a.comparisons).name}; do not edit -->\n"
                f"plan sha256: {hashlib.sha256(Path(a.comparisons).read_bytes()).hexdigest()}\n\n")
        Path(a.out).write_text(head + table, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
