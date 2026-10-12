#!/usr/bin/env python3
"""Generate the protocol C1 freeze manifests and run script (PROTOCOL.md C1.3, C1.14). No model calls.

Order (PROTOCOL C1.14):
  1. commit the harness; note its full SHA (git rev-parse HEAD)
  2. python3 evals/datasets/audit.py verify --complete --sealed-only ...   # the data-side twin of run.py's check
  3. python3 evals/plans/make_c1.py freeze --harness-sha <SHA> --serving-config evals/plans/serving-C1.json
         # writes evals/plans/freeze-C1.json (with the arm subsets); --judge-alias/--judge-model-file only for a plan
         # with a judge-graded suite (C1 has none: D1 is exploratory, owner decision 2026-10-11)
     commit freeze-C1.json (alone, once); the external timestamp (owner decision 2026-10-11) is a SIGNED tag
     eval-protocol-C1 of that freeze commit, pushed to the public repository BEFORE any confirmatory run
     (an OSF registration may be added later; it is optional)
  4. after 2026-11-30: fetch the prospective set, then
     python3 evals/plans/make_c1.py stage2 --data-dir evals/data      # writes evals/plans/stage2/prospective-C1.json
     commit it (alone, once). (`stage2-calibration` pins a judge calibration verdict; C1 does not use it.)
  5. python3 evals/plans/make_c1.py runsh   # writes evals/plans/run-c1.sh. Every C1 run happens after stage 2, at
     the stage-2 commit, so sealed and prospective runs share one harness SHA.
  6. python3 evals/plans/make_c1.py arms --data-dir evals/data   # the arm manifests, the 2x budget policy and the
     shifted-option MCQ file (PROTOCOL C1.13), checked against the arm pins in freeze-C1.json; arms are exploratory

freeze pins: the harness commit (full SHA) and the sha256 of the analysis-defining files; the annotated data files
and the C1 pool (sealed ids minus dev_neardup_exposed); the comparisons file; the budget policy; the serving record;
the judge (alias, prompt sha256, candidate cap, weights file) when the plan grades with one; the analysis config
report.py runs with; the arm subsets and the arm-b budget policy (PROTOCOL C1.13).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

EVALS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(EVALS))
import run as RUN  # noqa: E402

PLAN = EVALS / "plans" / "comparisons-C1.json"
SEALED = [EVALS / "datasets" / "heldout" / "heldout-v1.1.json", EVALS / "datasets" / "heldout" / "heldout-v1.json"]
STAGE2 = EVALS / "plans" / "stage2"
SEEDS = "1234,1235,1236"
WINDOW = ["2026-10-01", "2026-10-31"]
ANALYSIS = {"min_n": 30, "n_boot": 4000, "n_perm": 10000, "seed": 20261011, "alpha": 0.05}  # = report.py defaults
JUDGED_SUITES = {"sevenllm-qa"}   # suites scored by llm_judge: a plan without them needs no judge pin or --grader-model
ARM_SALT = "c1-arm|"
CODE_FILES = {"report.py": "report.py", "stats.py": "stats.py", "grading.py": "grading.py",
              "audit.py": "datasets/audit.py", "scorers.py": "scorers.py", "run.py": "run.py"}


def plan_suites(plan: Dict[str, Any]) -> List[str]:
    return sorted({c["suite"] for f in plan["families"] for c in f["comparisons"]})


def _pool(items: Dict[str, Dict[str, Any]], allowed: Optional[set], prefix: Optional[str] = None) -> List[str]:
    return sorted(i for i, it in items.items()
                  if (allowed is None or i in allowed) and (prefix is None or i.startswith(prefix))
                  and not ((it.get("meta") or {}).get("rigor") or {}).get("dev_neardup_exposed"))


def arm_subset(pool: List[str], target: int = 200) -> List[str]:
    """PROTOCOL C1.13: the pool ids with int(sha256('c1-arm|' + id)[:8], 16) % k == 0, k = max(1, n // target)."""
    k = max(1, len(pool) // target)
    return [i for i in pool if int(hashlib.sha256((ARM_SALT + i).encode()).hexdigest()[:8], 16) % k == 0]


def arm_budget_policy(multiplier: int) -> Dict[str, Any]:
    """Arm (b): every answer_tokens value and the think budget of the frozen BUDGET_POLICY times the multiplier."""
    pol = json.loads(json.dumps(RUN.BUDGET_POLICY))
    return {"name": f"{pol.get('name', 'budget')}-x{multiplier}",
            "answer_tokens": {t: int(n) * multiplier for t, n in pol["answer_tokens"].items()},
            "suite_answer_tokens": {s: int(n) * multiplier for s, n in (pol.get("suite_answer_tokens") or {}).items()},
            "think_budget": int(pol["think_budget"]) * multiplier}


def _arm_suites(plan: Dict[str, Any], arm: Dict[str, Any]) -> List[str]:
    want = arm.get("applies_to")
    if isinstance(want, list):
        return sorted({c["suite"] for f in plan["families"] for c in f["comparisons"] if c["id"] in want})
    return [s for s in plan_suites(plan) if not s.endswith("-prospective")]


def build_arms(plan: Dict[str, Any], suites: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """The arm pins of freeze-C1.json: per arm, the subset ids of each sealed suite (and the arm-b budget policy)."""
    out: Dict[str, Any] = {}
    for name, arm in sorted(((plan.get("arms") or {}).get("arms") or {}).items()):
        rec: Dict[str, Any] = {"suites": {}}
        for suite in _arm_suites(plan, arm):
            if suite in suites:
                ids = arm_subset(suites[suite]["ids"])
                rec["suites"][suite] = {"n": len(ids), "ids_sha256": RUN.sha256_text("\n".join(ids)), "ids": ids}
        if arm.get("budget_multiplier"):
            pol = arm_budget_policy(int(arm["budget_multiplier"]))
            rec["budget_policy"] = pol
            rec["budget_policy_sha256"] = RUN.sha256_text(json.dumps(pol, sort_keys=True))
        out[name] = rec
    return out


def _items(path: Path) -> Dict[str, Dict[str, Any]]:
    return {json.loads(l)["id"]: json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()}


def build_freeze(data_dir: Path, plan_path: Path = PLAN, sealed_path: Optional[Path] = None,
                 harness_sha: str = "", serving_config: Optional[Path] = None, judge: Optional[Dict[str, Any]] = None,
                 analysis: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", harness_sha or ""):
        raise ValueError("harness_sha must be a full 40-hex commit SHA (a tag or branch can move)")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    sealed_path = sealed_path or next(p for p in SEALED if p.exists())
    sealed = RUN.frozen_ids([str(sealed_path)])
    files, suites, data_prov = {}, {}, {}
    for suite in plan_suites(plan):
        if suite.endswith("-prospective"):
            continue  # stage 2 (PROTOCOL C1.3)
        path = data_dir / f"{suite}.jsonl"
        pool = _pool(_items(path), sealed.get(suite, set()))
        files[RUN.rel(path)] = RUN.sha256_file(path)
        suites[suite] = {"n": len(pool), "ids_sha256": RUN.sha256_text("\n".join(pool)), "ids": pool}
        side = data_dir / f"{suite}.provenance.json"
        if side.is_file():  # the data build's own record (fetch + audit versions): which build these files are
            data_prov[RUN.rel(side)] = RUN.sha256_file(side)
    man: Dict[str, Any] = {
        "protocol": "C1", "harness_ref": harness_sha, "harness_sha256": RUN.harness_sha256(),
        "code_sha256": {k: RUN.sha256_file(EVALS / v) for k, v in CODE_FILES.items()},
        "scorer_version": getattr(RUN.S, "SCORER_VERSION", None),
        "files": files, "suites": suites, "data_dir": RUN.rel(data_dir), "data_provenance_sha256": data_prov,
        "comparisons_sha256": RUN.sha256_file(plan_path),
        "budget_policy_sha256": RUN.sha256_text(json.dumps(RUN.BUDGET_POLICY, sort_keys=True)),
        "analysis": dict(analysis or ANALYSIS),
        "sealed_manifest": {"path": RUN.rel(sealed_path), "sha256": RUN.sha256_file(sealed_path)}}
    if serving_config is not None:
        man["serving_config_sha256"] = RUN.sha256_file(serving_config)
    if judge:
        man["judge"] = judge
    if (plan.get("arms") or {}).get("arms"):
        man["arms"] = build_arms(plan, suites)
    return man


def build_stage2(data_dir: Path, plan_path: Path = PLAN, window: List[str] = WINDOW) -> Dict[str, Any]:
    """The prospective manifest: *-prospective suites only, nvdp- ids inside the window, content-bound entries."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    audit = RUN._audit()
    files, suites = {}, {}
    for suite in plan_suites(plan):
        if not suite.endswith("-prospective"):
            continue
        path = data_dir / f"{suite}.jsonl"
        items = _items(path)

        def day(i):
            m = items[i].get("meta") or {}
            return str(m.get("first_public_date") or m.get("published") or "")[:10]
        pool = [i for i in _pool(items, None, "nvdp-") if window[0] <= day(i) <= window[1]]
        files[RUN.rel(path)] = RUN.sha256_file(path)
        suites[suite] = {"n_sealed": len(pool), "entries": [
            f"{i}\t{audit.heldout_key(items[i])}\t{RUN.content_key(audit, items[i])}" for i in pool]}
    return {"stage": 2, "protocol": "C1", "window": list(window), "files": files, "suites": suites}


def build_stage2_calibration(verdict: Path) -> Dict[str, Any]:
    return {"stage": 2, "protocol": "C1",
            "calibration_verdict": {"path": RUN.rel(verdict), "sha256": RUN.sha256_file(verdict)}}


def write_arms(freeze_path: Path, data_dir: Path, out_dir: Path, plan_path: Path = PLAN) -> List[str]:
    """Arm run inputs from the freeze's arm pins: a manifest per arm (bare ids, for --freeze/--select-frozen), the
    arm-b budget policy file, and for arm c the shifted-option copy of each MCQ suite (contam_probes.perm: ids and
    suite get a -perm suffix). Refuses when the pins no longer match the frozen pool or plan."""
    sys.path.insert(0, str(EVALS / "tools"))
    import contam_probes as CP  # noqa: E402
    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    if freeze.get("comparisons_sha256") != RUN.sha256_file(plan_path):
        raise ValueError("the comparisons file is not the one frozen in the freeze manifest")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if build_arms(plan, freeze["suites"]) != freeze.get("arms"):
        raise ValueError("the freeze manifest's arm pins do not match the frozen pool and plan")
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, rec in freeze["arms"].items():
        arm = plan["arms"]["arms"][name]
        suites = {s: {"ids": v["ids"]} for s, v in rec["suites"].items()}
        if name == "c":
            suites = {}
            for s, v in rec["suites"].items():
                items = _items(data_dir / f"{s}.jsonl")
                if RUN.sha256_file(data_dir / f"{s}.jsonl") != freeze["files"].get(RUN.rel(data_dir / f"{s}.jsonl")):
                    raise ValueError(f"{s}.jsonl differs from its freeze pin")
                shifted = [CP.perm(items[i]) for i in v["ids"]]
                f = out_dir / f"{s}-perm.jsonl"
                f.write_text("".join(json.dumps(it) + "\n" for it in shifted), encoding="utf-8")
                written.append(str(f))
                suites[f"{s}-perm"] = {"ids": [it["id"] for it in shifted]}
        if rec.get("budget_policy"):
            f = out_dir / f"arm-{name}-budget.json"
            f.write_text(json.dumps(rec["budget_policy"], indent=1) + "\n", encoding="utf-8")
            written.append(str(f))
        f = out_dir / f"arm-{name}-C1.json"
        f.write_text(json.dumps({"protocol": "C1", "arm": name, "arm_name": arm.get("name"), "suites": suites},
                                indent=1) + "\n", encoding="utf-8")
        written.append(str(f))
    return written


def build_runsh(plan_path: Path = PLAN) -> str:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    suites = plan_suites(plan)
    ivs = plan.get("run_interventions") or {}
    common = ["python3", "-B", "evals/run.py", "--protocol", "confirmatory",
              "--freeze", "evals/datasets/heldout/heldout-v1.1.json", "--freeze", "evals/plans/freeze-C1.json",
              "--freeze", "evals/plans/stage2/prospective-C1.json", "--select-frozen",
              "--serving-config", "evals/plans/serving-C1.json", "--probe-server"]
    judged = bool(JUDGED_SUITES & set(suites))
    if judged:
        common += ["--grader-model", '"$C1_JUDGE"']
    steps = []

    def step(name: str, run_name: str, extra: List[str], suite_list: List[str], swap: str) -> None:
        argv = common + ["--llama-swap-config", swap] + [x for s in suite_list for x in ("--suite", s)]
        argv += ["--run-name", run_name] + extra + [x for iv in ivs.get(run_name, []) for x in ("--intervention", iv)]
        steps.append(f"eval_step {name} " + " ".join(a if a.startswith('"') else shlex.quote(a) for a in argv))

    live, untuned = '"$LIVE_LLAMA_SWAP"', '"$UNTUNED_LLAMA_SWAP"'
    step("c1-off", "c1", ["--model", ",".join(plan["contestants"]), "--thinking", "off", "--seed", "1234"],
         suites, live)
    think = ["--thinking", "on", "--seeds", SEEDS]
    step("c1-think", "c1", ["--model", "coder"] + think,          # D2 (confirmatory)
         [s for s in ("nvd-cwe", "nvd-cwe-prospective") if s in suites], live)
    # D7 is exploratory (owner decision 2026-10-11): its thinking runs are made under the same frozen conditions
    step("c1-think-d7", "c1", ["--model", "coder-fast"] + think, [s for s in ("ctibench-mcq", "nvd-cwe") if s in suites],
         live)
    step("c1-think-d7-coder", "c1", ["--model", "coder"] + think, [s for s in ("ctibench-mcq",) if s in suites], live)
    step("c1sys", "c1sys", ["--model", "coder-fast", "--thinking", "off", "--seed", "1234"], ["cse-frr"], live)
    step("c1pre-flash", "c1pre", ["--model", "local/qwen3.8-flash-next", "--thinking", "off", "--seed", "1234"],
         [s for s in ("ctibench-mcq", "nvd-cwe") if s in suites], untuned)
    head = [
        "#!/usr/bin/env bash",
        "# Protocol C1 confirmatory runs, generated by evals/plans/make_c1.py runsh (do not edit by hand).",
        "# Run after stage 2 (PROTOCOL C1.3), at the stage-2 commit, with a clean tree and no __pycache__ under evals/.",
        "# Commit evals/plans/attempts-C1.jsonl after each step (the attempt ledger; a dirty tree stops the next step).",
        "# The c1pre step needs the untuned flash deployed (UNTUNED_LLAMA_SWAP: a copy of that live config).",
        "# The report: python3 evals/report.py <run dirs> --comparisons evals/plans/comparisons-C1.json "
        "--data-root evals/data",
        "#   (no --items: the gate re-hashes every data file it loads against the freeze pins).",
        "# Steps c1-think-d7*: exploratory D7 data under the same frozen conditions (no confirmatory family uses them).",
        "set -euo pipefail"] + ([': "${C1_JUDGE:?set C1_JUDGE to the pinned judge alias (freeze-C1.json judge.alias)}"']
                                if judged else []) + [
        ': "${LIVE_LLAMA_SWAP:?set LIVE_LLAMA_SWAP to a copy of the live llama-swap config (tuned flash)}"',
        ': "${UNTUNED_LLAMA_SWAP:?set UNTUNED_LLAMA_SWAP to a copy of the live config with the untuned flash}"',
        "export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1",
        "eval_step() { local name=$1; shift; echo \"== $name\"; \"$@\"; }", ""]
    return "\n".join(head + steps) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("freeze")
    f.add_argument("--data-dir", default=str(EVALS / "data"))
    f.add_argument("--plan", default=str(PLAN))
    f.add_argument("--sealed", default=None)
    f.add_argument("--harness-sha", required=True, help="the full commit SHA of the frozen harness")
    f.add_argument("--serving-config", required=True)
    f.add_argument("--judge-alias", default=None, help="only for a plan with a judge-graded suite (not C1)")
    f.add_argument("--judge-model-file", default=None)
    f.add_argument("--judge-max-chars", type=int, default=12000)
    f.add_argument("--out", default=str(EVALS / "plans" / "freeze-C1.json"))
    s2 = sub.add_parser("stage2")
    s2.add_argument("--data-dir", default=str(EVALS / "data"))
    s2.add_argument("--plan", default=str(PLAN))
    s2.add_argument("--out", default=str(STAGE2 / "prospective-C1.json"))
    s2c = sub.add_parser("stage2-calibration")
    s2c.add_argument("--verdict", required=True)
    s2c.add_argument("--out", default=str(STAGE2 / "calibration-C1.json"))
    r = sub.add_parser("runsh")
    r.add_argument("--plan", default=str(PLAN))
    r.add_argument("--out", default=str(EVALS / "plans" / "run-c1.sh"))
    ar = sub.add_parser("arms")
    ar.add_argument("--freeze", default=str(EVALS / "plans" / "freeze-C1.json"))
    ar.add_argument("--data-dir", default=str(EVALS / "data"))
    ar.add_argument("--plan", default=str(PLAN))
    ar.add_argument("--out-dir", default=str(EVALS / "results" / "arms-C1"))
    a = ap.parse_args(argv)
    if a.cmd == "runsh":
        Path(a.out).write_text(build_runsh(Path(a.plan)), encoding="utf-8")
        print(a.out)
        return 0
    if a.cmd == "arms":
        for f in write_arms(Path(a.freeze), Path(a.data_dir), Path(a.out_dir), Path(a.plan)):
            print(f)
        return 0
    if a.cmd == "freeze":
        if not re.fullmatch(r"[0-9a-f]{40}", a.harness_sha):
            ap.error("--harness-sha must be a full 40-hex commit SHA")
        if bool(a.judge_alias) != bool(a.judge_model_file):
            ap.error("--judge-alias and --judge-model-file go together")
        if JUDGED_SUITES & set(plan_suites(json.loads(Path(a.plan).read_text(encoding="utf-8")))) and not a.judge_alias:
            ap.error("the plan has a judge-graded suite: pass --judge-alias and --judge-model-file")
        judge = {"alias": a.judge_alias, "prompt_sha256": RUN.sha256_file(RUN.GRADER_PROMPT),
                 "max_candidate_chars": a.judge_max_chars, "model_file": a.judge_model_file} if a.judge_alias else None
        out = build_freeze(Path(a.data_dir), Path(a.plan), Path(a.sealed) if a.sealed else None, a.harness_sha,
                           Path(a.serving_config), judge)
    elif a.cmd == "stage2":
        out = build_stage2(Path(a.data_dir), Path(a.plan))
    else:
        out = build_stage2_calibration(Path(a.verdict))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    print(a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
