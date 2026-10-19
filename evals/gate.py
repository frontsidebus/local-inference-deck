#!/usr/bin/env python3
"""Upgrade gate: did a model, quant, llama.cpp build or serving-flag change make anything worse?

    python3 evals/gate.py calibrate --model coder --yes   # once per model: A/A (the baseline config twice)
    python3 evals/gate.py baseline  --model coder --yes   # the reference, before a change
    python3 evals/gate.py check     --model coder --yes   # the candidate, after it
    python3 evals/gate.py compare   BASELINE_DIR CANDIDATE_DIR [--manifest ...] [--out report.md]

A paired regression check on fixed development items (never the sealed split, never a judge model, never a
frontier call). Each model has its own manifest, ``evals/plans/gate-v1-<model>.json`` (the default for --manifest;
a model without one is refused). ``baseline`` runs the manifest's items through run.py (greedy, thinking off, the
manifest's budget policy with its per-suite caps, ``--no-prompt-cache``, concurrency 1, ``--probe-server``, the
C1.12 stopping rules) and stores the result under ``evals/results/gate/<model>/baseline-<UTC stamp>/``. ``check``
runs the candidate the same way and compares it with the latest compatible baseline; ``compare`` does the comparison
offline. ``calibrate`` runs the baseline config twice (a new baseline plus its A/A partner) and records the A/A
verdict; ``check`` refuses until the model has a passing A/A under the manifest, unless ``--accept-aa``.

Per suite (statistics and scoring are report.py's and stats.py's; nothing is reimplemented here):
- binary metrics (accuracy, accuracy_either, refusal tiers): exact paired Tango TOST at the suite margin for
  equivalence; exact McNemar for a difference, Holm-adjusted across every row of the manifest;
- MAE: the paired bootstrap test and bootstrap TOST at the suite's MAE margin;
- format (``format`` block): the parse-ok rate pooled over the listed suites, as a binary row (TOST at its margin,
  McNemar in the Holm family), plus a per-suite one-sided exact McNemar tripwire on newly unparsed items;
- verdict: "equivalent" if TOST p < alpha; else "different-worse" / "different-better" if the Holm-adjusted
  difference p < alpha (the sign, read with the metric's better direction, gives which: report.py's "reversed"
  rule); else "inconclusive". A tripped format check (tripwire, optional ``min_parse_rate`` / ``parse_margin``)
  makes the suite "different-worse (format)" whatever its accuracy.

Overall: FAIL (exit 1) if any row is different-worse; PASS (exit 0) if every row is equivalent or
different-better; INCONCLUSIVE ("hold", exit 2) otherwise; exit 3 for an invalid run (a stopping rule, serving
identity changed mid-run, too many api errors, incomplete, other samplers on the server); exit 4 when the gate
refuses (bad or missing manifest, no compatible baseline, no passing A/A, no --yes, STOP file present).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

EVALS_DIR = Path(__file__).resolve().parent
REPO_DIR = EVALS_DIR.parent
sys.path.insert(0, str(EVALS_DIR))
sys.dont_write_bytecode = True

import report as R  # noqa: E402
import run as RUN  # noqa: E402
import scorers as S  # noqa: E402
import stats as ST  # noqa: E402

MANIFEST_SCHEMA = "gate-manifest-v1"
RECORD_SCHEMA = "gate-record-v1"
PLANS_DIR = EVALS_DIR / "plans"
MANIFEST_PREFIX = "gate-v1"
RESULTS_ROOT = RUN.RESULTS_DIR / "gate"
SEALED_MANIFESTS = (EVALS_DIR / "datasets" / "heldout" / "heldout-v1.1.json",
                    EVALS_DIR / "datasets" / "heldout" / "heldout-v1.json")
GUARD = EVALS_DIR / "tools" / "gpu-guard.sh"
TUNNEL = EVALS_DIR / "tools" / "gateway-tunnel.sh"   # SSH to the backend's LiteLLM, skipping the public edge
REQUEST_TIMEOUT_S = 300      # run.py --timeout for gate runs; the slowest gate item takes well under a minute
# Read-only on the backend (walter/models/spark-model-agent, one sudoers line): the LIVE llama-swap config's cmd
# sha256 and server flags per model, never the cmd itself. run.py records the repo template's cmd, which is not
# what serves when the backend has not been redeployed.
LIVE_SERVING_CMD = ("sudo", "-n", "/usr/local/sbin/spark-model-agent", "serving", "--json")
RUN_NAME = "gate"            # run.py --run-name; the GPU guard and stop.sh match it (EVAL_RUN=gate)
RECORD = "gate.json"
REPORT = "gate-report.md"
SELECTION = "gate-selection.json"
BUDGET = "gate-budget.json"

EXIT_PASS, EXIT_FAIL, EXIT_INCONCLUSIVE, EXIT_INVALID, EXIT_REFUSED = 0, 1, 2, 3, 4
OVERALL_EXIT = {"PASS": EXIT_PASS, "FAIL": EXIT_FAIL, "INCONCLUSIVE": EXIT_INCONCLUSIVE, "INVALID": EXIT_INVALID}

# metric -> (kind, natural better direction; None = the manifest must say)
METRICS = {"accuracy": ("binary", "higher"), "accuracy_either": ("binary", "higher"),
           "refusal_full": ("binary", None), "refusal_partial": ("binary", None), "refusal_refuse": ("binary", None),
           "mae": ("continuous", "lower"), "mae_either": ("continuous", "lower")}
METRIC_SCORERS = {"mae": {"cvss_mae"}, "mae_either": {"cvss_mae"}, "accuracy_either": {"cwe_match", "cvss_mae"},
                  "refusal_full": {"refusal"}, "refusal_partial": {"refusal"}, "refusal_refuse": {"refusal"}}
EXCLUDE_OPS = sorted(k for k, (_, kind) in R.OPS.items() if kind == "drop")
TOP_KEYS = {"schema", "name", "model", "status", "note", "alpha", "default_margin", "scorer_version", "sampling",
            "validity", "selection", "format", "suites"}
SAMPLING_KEYS = {"thinking", "profile", "seed", "final_answer", "prompt_cache", "concurrency", "budget_policy_sha256"}
VALIDITY_KEYS = {"max_api_error_rate"}
SUITE_KEYS = {"name", "metric", "margin", "better", "min_parse_rate", "parse_margin", "max_tokens", "impute",
              "cvss_constant", "exclude", "note", "sources"}
SOURCE_KEYS = {"suite", "data", "sha256", "ids", "seconds_per_item", "note"}
FORMAT_KEYS = {"name", "pool", "margin", "tripwire", "note"}
SELECTION_KEYS = {"items_file", "items_sha256", "tool", "note"}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
TYPE_SECONDS = {"mcq": 8, "classify": 8, "extract": 15, "freeform": 20, "code": 25}   # rough, thinking off


class GateError(Exception):
    """The gate refuses (exit 4). ``problems`` lists every reason."""

    def __init__(self, msg: str, problems: Sequence[str] = ()):
        super().__init__(msg)
        self.problems = list(problems)

    def __str__(self) -> str:
        return super().__str__() + "".join(f"\n  - {p}" for p in self.problems)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _repo_path(p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else REPO_DIR / q


def manifest_for(model: str) -> Path:
    return PLANS_DIR / f"{MANIFEST_PREFIX}-{RUN_slug(model)}.json"


def RUN_slug(model: str) -> str:  # noqa: N802 - run.py's dir slug
    return re.sub(r"[^A-Za-z0-9._-]+", "_", model)


# ------------------------------------------------------------------------------------------- manifest
def sealed_ids(paths: Sequence[Path] = SEALED_MANIFESTS) -> set:
    """Every id listed by the sealed held-out manifests (any suite: an id is refused wherever it appears)."""
    out: set = set()
    for p in paths:
        if not Path(p).is_file():
            continue
        for want in (RUN._load_json(p).get("suites") or {}).values():
            if isinstance(want, dict):
                out |= set((RUN._listed_ids(want) or {}).keys())
    return out


def suite_caps(suites: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """item suite -> answer-token cap, from the manifest's per-suite max_tokens."""
    out: Dict[str, int] = {}
    for e in suites:
        if isinstance(e, dict) and _int(e.get("max_tokens")):
            for s in e.get("sources") or []:
                if isinstance(s, dict) and isinstance(s.get("suite"), str):
                    out[s["suite"]] = e["max_tokens"]
    return out


def budget_doc(m: Dict[str, Any]) -> Dict[str, Any]:
    """The --budget-policy file the gate gives run.py: run.py's BUDGET_POLICY plus the manifest's per-suite caps."""
    return {"name": str(m.get("name")), "suite_answer_tokens": suite_caps(m.get("suites") or [])}


def budget_policy(m: Dict[str, Any]) -> Dict[str, Any]:
    """The policy exactly as run.py will load it (through its own loader)."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / BUDGET
        p.write_text(json.dumps(budget_doc(m)), encoding="utf-8")
        return RUN.load_budget_policy(str(p))


def budget_sha(pol: Dict[str, Any]) -> str:
    """run.py's recorded ``budget.policy_sha256`` for a policy."""
    return RUN.sha256_text(json.dumps(pol, sort_keys=True))


def load_manifest(path: Path, sealed: Optional[Sequence[Path]] = None, check_data: bool = True) -> Dict[str, Any]:
    """Read and strictly validate a gate manifest; raise GateError listing every problem. With check_data=False the
    data files are not opened (structure, ids and sealed ids only). The result carries ``_path``, ``_sha256`` and,
    per suite, ``_keys`` [(item suite, id)] and ``_items`` (with check_data)."""
    path = Path(path)
    try:
        raw = path.read_bytes()
        m = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise GateError(f"manifest {path.name}: unreadable ({exc.__class__.__name__})") from None
    probs: List[str] = []
    if not isinstance(m, dict):
        raise GateError(f"manifest {path.name}: not a JSON object")
    for k in sorted(set(m) - TOP_KEYS):
        probs.append(f"unknown top-level key {k!r}")
    if m.get("schema") != MANIFEST_SCHEMA:
        probs.append(f"schema must be {MANIFEST_SCHEMA!r}")
    for k in ("name", "model"):
        if not isinstance(m.get(k), str) or not m.get(k):
            probs.append(f"{k}: a non-empty string")
    alpha = m.get("alpha")
    if not _num(alpha) or not 0 < alpha < 0.5:
        probs.append("alpha: a number in (0, 0.5)")
    dm = m.get("default_margin")
    if not _num(dm) or not 0 < dm < 1:
        probs.append("default_margin: a number in (0, 1) (a proportion: 0.05 = 5 pp)")
    if not isinstance(m.get("scorer_version"), str) or not m.get("scorer_version"):
        probs.append("scorer_version: a non-empty string")
    sp = m.get("sampling")
    if not isinstance(sp, dict):
        probs.append("sampling: an object")
        sp = {}
    for k in sorted(set(sp) - SAMPLING_KEYS):
        probs.append(f"sampling: unknown key {k!r}")
    if sp.get("thinking") != "off":
        probs.append("sampling.thinking must be 'off' (the gate decodes greedily)")
    if sp.get("profile") != "off":
        probs.append("sampling.profile must be 'off' (run.py's greedy profile)")
    if not _int(sp.get("seed")):
        probs.append("sampling.seed: an integer")
    if sp.get("final_answer") is not False:
        probs.append("sampling.final_answer must be false (the follow-up changes the estimand)")
    if sp.get("prompt_cache") is not False:
        probs.append("sampling.prompt_cache must be false (the gate always sends cache_prompt=false)")
    if sp.get("concurrency") != 1:
        probs.append("sampling.concurrency must be 1")
    va = m.get("validity", {})
    if not isinstance(va, dict):
        probs.append("validity: an object")
        va = {}
    for k in sorted(set(va) - VALIDITY_KEYS):
        probs.append(f"validity: unknown key {k!r}")
    mar = va.get("max_api_error_rate", 0.05)
    if not _num(mar) or not 0 <= mar < 1:
        probs.append("validity.max_api_error_rate: a number in [0, 1)")
    sel = m.get("selection")
    if sel is not None:
        if not isinstance(sel, dict) or set(sel) - SELECTION_KEYS:
            probs.append(f"selection: an object with keys from {sorted(SELECTION_KEYS)}")
        elif sel.get("items_file"):
            ip = _repo_path(sel["items_file"])
            if not ip.is_file():
                probs.append(f"selection.items_file {sel['items_file']} not found")
            elif sha256_file(ip) != sel.get("items_sha256"):
                probs.append("selection.items_sha256 does not match the items file")
    suites = m.get("suites")
    if not isinstance(suites, list) or not suites:
        probs.append("suites: a non-empty list")
        suites = []
    sealed_set = sealed_ids(SEALED_MANIFESTS if sealed is None else sealed)
    sealed_paths = [Path(p) for p in (SEALED_MANIFESTS if sealed is None else sealed) if Path(p).is_file()]
    names: set = set()
    taken: Dict[Tuple[str, str], str] = {}
    capped: Dict[str, Tuple[str, Optional[int]]] = {}
    for i, e in enumerate(suites):
        where = f"suites[{i}]"
        if not isinstance(e, dict):
            probs.append(f"{where}: an object")
            continue
        where = f"suites[{i}] ({e.get('name')})"
        for k in sorted(set(e) - SUITE_KEYS):
            probs.append(f"{where}: unknown key {k!r}")
        name = e.get("name")
        if not isinstance(name, str) or not name:
            probs.append(f"{where}: name: a non-empty string")
        elif name in names:
            probs.append(f"{where}: duplicate name")
        names.add(name)
        metric = e.get("metric")
        if metric not in METRICS:
            probs.append(f"{where}: metric must be one of {sorted(METRICS)}")
            continue
        kind, natural = METRICS[metric]
        better = e.get("better", natural)
        if better not in ("higher", "lower"):
            probs.append(f"{where}: better: 'higher' or 'lower' (required for {metric})")
        elif natural and better != natural:
            probs.append(f"{where}: better must be {natural!r} for {metric}")
        margin = e.get("margin", dm if kind == "binary" else None)
        if kind == "binary" and (not _num(margin) or not 0 < margin < 1):
            probs.append(f"{where}: margin: a proportion in (0, 1)")
        if kind == "continuous" and (not _num(margin) or not 0 < margin <= 10):
            probs.append(f"{where}: margin: required for {metric}, in CVSS points (0, 10]")
        for k in ("parse_margin", "min_parse_rate"):
            if k in e and (not _num(e[k]) or not 0 <= e[k] <= 1):
                probs.append(f"{where}: {k}: a proportion in [0, 1]")
        mt = e.get("max_tokens")
        if mt is not None and (not _int(mt) or not 0 < mt <= RUN.HARD_MAX_TOKENS):
            probs.append(f"{where}: max_tokens: a positive integer (the answer cap for this suite's items)")
        if kind == "continuous":
            if e.get("impute", "baseline") not in R.IMPUTE:
                probs.append(f"{where}: impute: one of {list(R.IMPUTE)}")
            if not _num(e.get("cvss_constant", R.CVSS_PRIOR)):
                probs.append(f"{where}: cvss_constant: a number")
        elif "impute" in e or "cvss_constant" in e:
            probs.append(f"{where}: impute / cvss_constant apply to MAE metrics only")
        ex = e.get("exclude", [])
        if not isinstance(ex, list) or any(o not in EXCLUDE_OPS for o in ex):
            probs.append(f"{where}: exclude: a list of {EXCLUDE_OPS}")
        srcs = e.get("sources")
        if not isinstance(srcs, list) or not srcs:
            probs.append(f"{where}: sources: a non-empty list of {{suite, data, sha256, ids}}")
            continue
        keys: List[Tuple[str, str]] = []
        items: List[Dict[str, Any]] = []
        for j, s in enumerate(srcs):
            sw = f"{where}.sources[{j}]"
            if not isinstance(s, dict):
                probs.append(f"{sw}: an object")
                continue
            for k in sorted(set(s) - SOURCE_KEYS):
                probs.append(f"{sw}: unknown key {k!r}")
            suite, data, sha, ids = s.get("suite"), s.get("data"), s.get("sha256"), s.get("ids")
            if not isinstance(suite, str) or not suite:
                probs.append(f"{sw}: suite: the items' suite name")
                continue
            prev = capped.get(suite)
            if prev is not None and prev[1] != mt:
                probs.append(f"{sw}: item suite {suite} is also in {prev[0]} with another max_tokens")
            capped[suite] = (str(name), mt)
            if "seconds_per_item" in s and (not _num(s["seconds_per_item"]) or s["seconds_per_item"] <= 0):
                probs.append(f"{sw}: seconds_per_item: a positive number")
            if not isinstance(data, str) or not data:
                probs.append(f"{sw}: data: the items file")
                continue
            if not isinstance(sha, str) or not HEX64.match(sha):
                probs.append(f"{sw}: sha256: the data file's sha256 (64 hex)")
                continue
            if ids is not None:
                if not isinstance(ids, list) or not ids or not all(isinstance(x, str) and x for x in ids):
                    probs.append(f"{sw}: ids: a non-empty list of item ids")
                    continue
                if len(set(ids)) != len(ids):
                    probs.append(f"{sw}: duplicate ids")
                    continue
                bad_sealed = sorted(x for x in ids if x in sealed_set)
                if bad_sealed:
                    probs.append(f"{sw}: {len(bad_sealed)} SEALED held-out id(s) (the gate never touches the "
                                 "sealed split): " + ", ".join(bad_sealed[:5]))
            elif not check_data:
                probs.append(f"{sw}: ids are required when the data file is not read")
                continue
            if check_data:
                dp = _repo_path(data)
                if not dp.is_file():
                    probs.append(f"{sw}: data file {data} not found (evals/tools/gate_select.py --write-data "
                                 "builds the gate's data files)")
                    continue
                got = sha256_file(dp)
                if got != sha:
                    probs.append(f"{sw}: data file {data} sha256 is {got}, the manifest pins {sha}")
                    continue
                try:
                    file_items, _ = RUN.load_datasets([dp], None, [suite])
                except RUN.DatasetError as exc:
                    probs.append(f"{sw}: {exc}")
                    continue
                by_id = {it["id"]: it for it in file_items}
                if ids is None:
                    ids = [it["id"] for it in file_items]
                    bad_sealed = sorted(x for x in ids if x in sealed_set)
                    if bad_sealed:
                        probs.append(f"{sw}: {len(bad_sealed)} SEALED held-out id(s): " + ", ".join(bad_sealed[:5]))
                missing = [x for x in ids if x not in by_id]
                if missing:
                    probs.append(f"{sw}: {len(missing)} id(s) not in suite {suite!r} of {data}: "
                                 + ", ".join(missing[:5]))
                    continue
                chosen = [by_id[x] for x in ids]
                n_sealed = sum(RUN.sealed_overlap(chosen, p) for p in sealed_paths)
                if n_sealed and not any(x in sealed_set for x in ids):
                    probs.append(f"{sw}: {n_sealed} item(s) match a sealed held-out item by content")
                judge = sorted(it["id"] for it in chosen if it["scorer"] == "llm_judge")
                if judge:
                    probs.append(f"{sw}: llm_judge items need a judge model, which the gate never uses: "
                                 + ", ".join(judge[:5]))
                want = METRIC_SCORERS.get(metric)
                wrong = sorted(it["id"] for it in chosen if want and it["scorer"] not in want)
                if wrong:
                    probs.append(f"{sw}: metric {metric} needs {sorted(want)} items; not: " + ", ".join(wrong[:5]))
                own = sorted(it["id"] for it in chosen if (it.get("meta") or {}).get("max_tokens"))
                if mt is not None and own:
                    probs.append(f"{sw}: items with their own meta.max_tokens would override the suite cap: "
                                 + ", ".join(own[:5]))
                unstripped: List[str] = []
                RUN._unstripped_check(chosen, lambda _cid, msg: unstripped.append(msg))
                probs += [f"{sw}: {x} (use the gate's data files)" for x in unstripped]
                items += chosen
                s["_path"] = dp
            for x in ids:
                key = (suite, x)
                if key in taken:
                    probs.append(f"{sw}: item {x} is also in {taken[key]}")
                taken[key] = str(name)
                keys.append(key)
        e["_keys"], e["_items"] = keys, items
    fm = m.get("format")
    if fm is not None:
        if not isinstance(fm, dict):
            probs.append("format: an object")
        else:
            for k in sorted(set(fm) - FORMAT_KEYS):
                probs.append(f"format: unknown key {k!r}")
            pool = fm.get("pool")
            if not isinstance(pool, list) or not pool or any(p not in names for p in pool):
                probs.append(f"format.pool: a non-empty list of suite names ({sorted(n for n in names if n)})")
            if not _num(fm.get("margin")) or not 0 < fm["margin"] < 1:
                probs.append("format.margin: a proportion in (0, 1)")
            if fm.get("name", "format") in names:
                probs.append("format.name clashes with a suite name")
            if not isinstance(fm.get("tripwire", True), bool):
                probs.append("format.tripwire: true or false")
    if isinstance(sp.get("budget_policy_sha256"), str) and HEX64.match(sp["budget_policy_sha256"]):
        try:
            got = budget_sha(budget_policy(m))
            if got != sp["budget_policy_sha256"]:
                probs.append(f"sampling.budget_policy_sha256 does not match the policy with the per-suite caps "
                             f"(it is {got})")
        except SystemExit as exc:
            probs.append(f"budget policy: {exc}")
    else:
        probs.append("sampling.budget_policy_sha256: the policy's sha256 (64 hex)")
    if probs:
        raise GateError(f"manifest {path.name} is invalid", probs)
    for e in suites:
        kind, natural = METRICS[e["metric"]]
        e["_kind"] = kind
        e["_better"] = e.get("better", natural)
        e["_margin"] = float(e.get("margin", dm))
    m["_path"], m["_sha256"] = path, hashlib.sha256(raw).hexdigest()
    m["_max_api_error_rate"] = float(va.get("max_api_error_rate", 0.05))
    m["_checked_data"] = check_data
    return m


def manifest_items(m: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [it for e in m["suites"] for it in e["_items"]]


def all_keys(m: Dict[str, Any]) -> List[Tuple[str, str]]:
    return [k for e in m["suites"] for k in e["_keys"]]


def selection(m: Dict[str, Any]) -> Dict[str, Any]:
    """A run.py --freeze/--select-frozen manifest holding exactly the gate's ids."""
    out: Dict[str, List[str]] = {}
    for s, x in all_keys(m):
        out.setdefault(s, []).append(x)
    return {"note": f"written by evals/gate.py from {m['name']} (sha256 {m['_sha256']}); item selection only",
            "suites": {s: {"ids": sorted(set(v))} for s, v in sorted(out.items())}}


# ------------------------------------------------------------------------------------------- run dirs
def resolve_run_dir(d: Path) -> Tuple[Path, Optional[Dict[str, Any]]]:
    """A gate dir (gate.json + its run.py dir) or a bare run.py dir -> (run dir, gate record or None)."""
    d = Path(d)
    rec = None
    if (d / RECORD).is_file():
        rec = json.loads((d / RECORD).read_text(encoding="utf-8"))
        rd = d / rec.get("run_dir", "")
        if (rd / "run.json").is_file():
            return rd, rec
        raise GateError(f"{d}: its run dir {rec.get('run_dir')!r} has no run.json")
    if (d / "run.json").is_file():
        return d, None
    raise GateError(f"{d}: neither a gate dir ({RECORD}) nor a run dir (run.json)")


def _val(x: Any) -> Any:
    return x.get("value") if isinstance(x, dict) and "value" in x else x


def _cache_sent(run: Dict[str, Any]) -> Any:
    return (run.get("prompt_cache") or {}).get("sent")


def sampling_signature(run: Dict[str, Any]) -> Dict[str, Any]:
    """What must be equal for two runs to be compared: decoding, the answer budget and prompt caching."""
    return {"thinking": run.get("thinking"), "sampling": run.get("sampling"), "max_tokens": run.get("max_tokens"),
            "budget_policy_sha256": (run.get("budget") or {}).get("policy_sha256"),
            "followup": (run.get("followup") or {}).get("enabled"), "prompt_cache": _cache_sent(run),
            "concurrency": run.get("concurrency")}


def harness_signature(run: Dict[str, Any]) -> Dict[str, Any]:
    """The code that produced the outputs (generation and scoring)."""
    h = run.get("harness_sha256") or {}
    return {k: h.get(k) for k in ("run.py", "scorers.py")}


def manifest_problems(run: Dict[str, Any], m: Dict[str, Any], who: str) -> List[str]:
    """A run against the manifest's sampling profile, budget, caching, scorer version and item sets."""
    out = []
    sp = m["sampling"]
    if run.get("thinking") != sp["thinking"]:
        out.append(f"{who}: thinking {run.get('thinking')!r}, the manifest wants {sp['thinking']!r}")
    smp = run.get("sampling") or {}
    if smp.get("profile") != sp["profile"] or smp.get("overrides"):
        out.append(f"{who}: sampling profile {smp.get('profile')!r} with overrides {smp.get('overrides') or {}}; "
                   f"the manifest wants {sp['profile']!r} and none")
    if smp.get("seed") != sp["seed"]:
        out.append(f"{who}: seed {smp.get('seed')}, the manifest wants {sp['seed']}")
    if (run.get("budget") or {}).get("policy_sha256") != sp["budget_policy_sha256"] or \
            (run.get("budget") or {}).get("flat_override"):
        out.append(f"{who}: budget policy differs from the manifest's (per-suite caps included)")
    if (run.get("followup") or {}).get("enabled"):
        out.append(f"{who}: the final-answer follow-up was on")
    if _cache_sent(run) != {"cache_prompt": False}:
        out.append(f"{who}: prompt caching was not turned off (--no-prompt-cache)")
    if run.get("concurrency") != sp["concurrency"]:
        out.append(f"{who}: concurrency {run.get('concurrency')}, the manifest wants {sp['concurrency']}")
    if _val(run.get("scorer_version")) != m["scorer_version"]:
        out.append(f"{who}: scorer_version {_val(run.get('scorer_version'))!r}, the manifest requires "
                   f"{m['scorer_version']!r}")
    have = run.get("item_sets") or {}
    for s, v in selection(m)["suites"].items():
        got = (have.get(s) or {}).get("ids_sha256")
        if got != RUN.sha256_text("\n".join(sorted(v["ids"]))):
            out.append(f"{who}: suite {s}: the run's item set is not the manifest's")
    return out


def compatibility(base_dir: Path, cand_dir: Path, m: Dict[str, Any], allow_stale_harness: bool = False
                  ) -> Dict[str, Any]:
    """Refuse (GateError) unless baseline and candidate share the manifest sha, scorer_version, sampling profile,
    budget, caching and item sets. A harness difference is refused unless allow_stale_harness (then ``stale``)."""
    brd, brec = resolve_run_dir(base_dir)
    crd, crec = resolve_run_dir(cand_dir)
    b, c = R._read_run(brd), R._read_run(crd)
    probs: List[str] = []
    for rec, who in ((brec, "baseline"), (crec, "candidate")):
        if rec is not None and rec.get("manifest", {}).get("sha256") != m["_sha256"]:
            probs.append(f"{who}: made with manifest sha256 {str(rec.get('manifest', {}).get('sha256'))[:12]}…, "
                         f"this manifest is {m['_sha256'][:12]}…")
    if _val(b.get("scorer_version")) != _val(c.get("scorer_version")):
        probs.append(f"scorer_version differs: baseline {_val(b.get('scorer_version'))!r}, candidate "
                     f"{_val(c.get('scorer_version'))!r}")
    sb, sc = sampling_signature(b), sampling_signature(c)
    for k in sb:
        if sb[k] != sc[k]:
            probs.append(f"sampling profile differs ({k}): baseline {sb[k]!r}, candidate {sc[k]!r}")
    probs += manifest_problems(b, m, "baseline") + manifest_problems(c, m, "candidate")
    pb = (b.get("prompts") or {}).get("system_prompt_sha256")
    pc = (c.get("prompts") or {}).get("system_prompt_sha256")
    if pb != pc:
        probs.append("the system prompts sent differ (prompts.system_prompt_sha256)")
    hb, hc = harness_signature(b), harness_signature(c)
    stale = None
    if hb != hc:
        stale = {"baseline": hb, "candidate": hc,
                 "git": [(b.get("git") or {}).get("sha"), (c.get("git") or {}).get("sha")]}
        if not allow_stale_harness:
            probs.append("harness differs (run.py / scorers.py sha256); re-baseline, or pass --allow-stale-harness "
                         "(recorded and printed)")
    if probs:
        raise GateError(f"baseline {Path(base_dir).name} is not compatible with candidate {Path(cand_dir).name}",
                        probs)
    return {"stale_harness": stale, "baseline_run": brd, "candidate_run": crd, "baseline": b, "candidate": c,
            "baseline_rec": brec, "candidate_rec": crec}


def public_sampling(m: Dict[str, Any]) -> Dict[str, Any]:
    return {k: m["sampling"].get(k) for k in sorted(SAMPLING_KEYS)}


def record_compatible(rec: Dict[str, Any], m: Dict[str, Any]) -> List[str]:
    """Pre-run check of a baseline's gate record against the manifest (before spending GPU time)."""
    out = []
    if rec.get("schema") != RECORD_SCHEMA or rec.get("kind") != "baseline":
        out.append("not a gate baseline record")
    if rec.get("status") != "complete":
        out.append(f"status {rec.get('status')!r} (not complete)")
    if (rec.get("manifest") or {}).get("sha256") != m["_sha256"]:
        out.append("made with another manifest (sha256 differs)")
    if rec.get("scorer_version") != m["scorer_version"]:
        out.append(f"scorer_version {rec.get('scorer_version')!r} != {m['scorer_version']!r}")
    if rec.get("sampling") != public_sampling(m):
        out.append("sampling profile differs")
    return out


def _records(root: Path, model: str, kind: str) -> List[Tuple[Path, Optional[Dict[str, Any]]]]:
    d = Path(root) / RUN_slug(model)
    out = []
    for p in sorted(d.glob(f"{kind}-*")) if d.is_dir() else []:
        try:
            out.append((p, json.loads((p / RECORD).read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            out.append((p, None))
    out.sort(key=lambda t: ((t[1] or {}).get("created") or "", t[0].name), reverse=True)
    return out


def find_baseline(root: Path, model: str, m: Dict[str, Any]) -> Path:
    """The latest compatible baseline for a model, or GateError with the reason each candidate was skipped."""
    cands = [(p, rec, ["no readable gate.json"] if rec is None else record_compatible(rec, m))
             for p, rec in _records(root, model, "baseline")]
    if not cands:
        raise GateError(f"no baseline for {model} under {Path(root) / RUN_slug(model)}; run "
                        f"`gate.py calibrate --model {model}` (or `baseline`) first")
    for p, _, why in cands:
        if not why:
            return p
    raise GateError(f"no compatible baseline for {model}", [f"{p.name}: {'; '.join(w)}" for p, _, w in cands])


def aa_status(root: Path, model: str, m: Dict[str, Any]) -> Tuple[Optional[Path], Optional[Dict[str, Any]]]:
    """The latest A/A calibration of this model under this manifest (a baseline record with an ``aa`` block)."""
    for p, rec in _records(root, model, "baseline"):
        if rec and rec.get("aa") and (rec.get("manifest") or {}).get("sha256") == m["_sha256"]:
            return p, rec["aa"]
    return None, None


# ------------------------------------------------------------------------------------------- validity
def run_invalid(run_dir: Path, m: Dict[str, Any], items: Optional[Dict[str, Dict[str, Any]]] = None
                ) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    """(reasons the run is invalid, its item records). Invalid = a stopping rule fired, the serving identity changed
    mid-run, api errors above the manifest's limit, items missing, or the server used other samplers."""
    run = R._read_run(run_dir)
    recs = R.item_records(run_dir, items)
    out: List[str] = []
    if not run:
        return [f"{Path(run_dir).name}: no run.json"], recs
    prot = run.get("protocol") or {}
    if prot.get("invalid"):
        out.append(f"stopped as invalid by run.py: {prot['invalid']}")
    served = run.get("served") or {}
    if len(served.get("models") or []) > 1 or len(served.get("system_fingerprints") or []) > 1:
        out.append(f"serving identity changed mid-run: models {served.get('models')}, builds "
                   f"{served.get('system_fingerprints')}")
    sv = run.get("serving") if isinstance(run.get("serving"), dict) else {}
    if (sv.get("sampler_check") or {}).get("ok") is False:
        out.append(f"the server used other samplers than sent: {sv['sampler_check'].get('mismatch')}")
    want = all_keys(m)
    missing = [x for s, x in want if x not in recs or recs[x]["suite"] != s]
    if missing:
        out.append(f"incomplete: {len(missing)} of {len(want)} manifest items have no response "
                   f"(e.g. {', '.join(missing[:3])}); resume the run")
    n_api = sum(1 for s, x in want if x in recs and recs[x]["status"] == "api_error")
    if want and n_api / len(want) > m["_max_api_error_rate"]:
        out.append(f"{n_api} of {len(want)} items are api errors (limit {m['_max_api_error_rate']:.0%})")
    return out, recs


def _subset(recs: Dict[str, Dict[str, Any]], keys: Sequence[Tuple[str, str]]) -> Dict[str, Dict[str, Any]]:
    return {x: recs[x] for s, x in keys if x in recs and recs[x]["suite"] == s}


def parse_ok(rec: Dict[str, Any]) -> Optional[float]:
    """1 = a parsed answer (status ok); 0 = unparsed, item error or api error; None = never scored."""
    if rec["status"] == "unscored":
        return None
    return 1.0 if rec["status"] == "ok" else 0.0


def suite_counts(recs: Dict[str, Dict[str, Any]], keys: Sequence[Tuple[str, str]]) -> Dict[str, Any]:
    """Run-validity counts for one suite's items: api errors, truncation and parse rate (ok among answered)."""
    rs = list(_subset(recs, keys).values())
    n = len(keys)
    api = sum(1 for r in rs if r["status"] == "api_error")
    answered = [r for r in rs if r["status"] not in ("api_error", "unscored")]
    ok = sum(1 for r in answered if r["status"] == "ok")
    trunc = sum(1 for r in answered if r["truncated"])
    return {"n": n, "api_errors": api, "api_error_rate": api / n if n else None,
            "truncated": trunc, "truncation_rate": trunc / len(answered) if answered else None,
            "unparsed": len(answered) - ok, "parse_rate": ok / len(answered) if answered else None}


def tripwire(base: Dict[str, Dict[str, Any]], cand: Dict[str, Dict[str, Any]], keys, alpha: float
             ) -> Dict[str, Any]:
    """One-sided exact McNemar on parse-ok: items parsed by the baseline and not by the candidate (lost) against
    the reverse (gained); fires when p < alpha."""
    b, c = _subset(base, keys), _subset(cand, keys)
    lost = gained = 0
    for x in set(b) & set(c):
        u, w = parse_ok(b[x]), parse_ok(c[x])
        if u is None or w is None:
            continue
        lost += int(u == 1 and w == 0)
        gained += int(u == 0 and w == 1)
    n = lost + gained
    p = 1.0 if lost == 0 else 1.0 - ST.binom_cdf(lost - 1, n, 0.5)
    return {"lost": lost, "gained": gained, "p": p, "fired": p < alpha}


# ------------------------------------------------------------------------------------------- comparison
def compare_suite(e: Dict[str, Any], base: Dict[str, Dict[str, Any]], cand: Dict[str, Dict[str, Any]],
                  alpha: float) -> Dict[str, Any]:
    """report.compare on one suite's items; A = candidate, B = baseline, so diff = candidate - baseline."""
    cont = e["_kind"] == "continuous"
    r = R.compare([_subset(cand, e["_keys"])], [_subset(base, e["_keys"])], metric=e["metric"],
                  variant=tuple(e.get("exclude") or ()), errors="fail", alpha=alpha, margin=e["_margin"],
                  n_perm=None if cont else ST.DEFAULT_PERM, impute=e.get("impute", "baseline"),
                  constant=float(e.get("cvss_constant", R.CVSS_PRIOR)), power=False)
    r["test"] = "bootstrap test; bootstrap TOST" if cont else "exact McNemar; exact Tango TOST"
    return r


def compare_format(keys, base, cand, margin: float, alpha: float) -> Dict[str, Any]:
    """The pooled parse-ok rate as a paired binary comparison (report.paired_binary)."""
    b, c = _subset(base, keys), _subset(cand, keys)
    av, bv = [], []
    for x in sorted(set(b) & set(c)):
        u, w = parse_ok(c[x]), parse_ok(b[x])
        if u is None or w is None:
            continue
        av.append(u)
        bv.append(w)
    r = R.paired_binary(av, bv, alpha, margin, power=False) if av else {"n": 0}
    r["test"] = "exact McNemar; exact Tango TOST (parse-ok)"
    return r


def _diag(e: Dict[str, Any], base, cand) -> Dict[str, Any]:
    """A/A diagnostics: the discordant share (binary) or the SD of per-item differences (MAE)."""
    b, c = _subset(base, e["_keys"]), _subset(cand, e["_keys"])
    ops = tuple(e.get("exclude") or ())
    diffs = []
    for x in sorted(set(b) & set(c)):
        u = R.outcome(c[x], e["metric"], "fail", ops, e.get("impute", "baseline"),
                      float(e.get("cvss_constant", R.CVSS_PRIOR)))
        w = R.outcome(b[x], e["metric"], "fail", ops, e.get("impute", "baseline"),
                      float(e.get("cvss_constant", R.CVSS_PRIOR)))
        if u is not None and w is not None:
            diffs.append(u - w)
    if e["_kind"] == "binary":
        return {"psi": sum(1 for d in diffs if d) / len(diffs) if diffs else None}
    return {"sd_diff": ST.sd(diffs)}


def verdicts(m: Dict[str, Any], base: Dict[str, Dict[str, Any]], cand: Dict[str, Dict[str, Any]]
             ) -> List[Dict[str, Any]]:
    alpha = float(m["alpha"])
    fm = m.get("format")
    rows = []
    for e in m["suites"]:
        rows.append({"name": e["name"], "metric": e["metric"], "better": e["_better"], "kind": e["_kind"],
                     "margin": e["_margin"], "n_manifest": len(e["_keys"]), "r": compare_suite(e, base, cand, alpha),
                     "base": suite_counts(base, e["_keys"]), "cand": suite_counts(cand, e["_keys"]),
                     "min_parse_rate": e.get("min_parse_rate"), "parse_margin": e.get("parse_margin"),
                     "diag": _diag(e, base, cand),
                     "tripwire": tripwire(base, cand, e["_keys"], alpha)
                     if fm and fm.get("tripwire", True) and e["name"] in fm["pool"] else None})
    if fm:
        keys = [k for e in m["suites"] if e["name"] in fm["pool"] for k in e["_keys"]]
        r = compare_format(keys, base, cand, float(fm["margin"]), alpha)
        rows.append({"name": fm.get("name", "format"), "metric": "parse-ok (pooled: " + ", ".join(fm["pool"]) + ")",
                     "better": "higher", "kind": "binary", "margin": float(fm["margin"]), "n_manifest": len(keys),
                     "r": r, "base": suite_counts(base, keys), "cand": suite_counts(cand, keys),
                     "min_parse_rate": None, "parse_margin": None, "tripwire": None, "format_row": True,
                     "diag": {"psi": ((r.get("a_only") or 0) + (r.get("b_only") or 0)) / r["n"] if r.get("n") else None}})
    # Holm over the difference test of every row (a row with no test counts as p = 1)
    adj = ST.holm([1.0 if row["r"].get("p") is None else row["r"]["p"] for row in rows])
    for row, p_adj in zip(rows, adj):
        r = row["r"]
        row["p_holm"] = p_adj
        direction = "a>b" if row["better"] == "higher" else "a<b"     # A = candidate
        d = r.get("diff")
        pc, pb = row["cand"]["parse_rate"], row["base"]["parse_rate"]
        fmt = []
        if row["tripwire"] and row["tripwire"]["fired"]:
            t = row["tripwire"]
            fmt.append(f"format tripwire: {t['lost']} newly unparsed vs {t['gained']} newly parsed "
                       f"(one-sided exact McNemar p = {t['p']:.3g})")
        if row["min_parse_rate"] is not None and pc is not None and pc < row["min_parse_rate"]:
            fmt.append(f"parse rate {pc:.0%} below the minimum {row['min_parse_rate']:.0%}")
        if row["parse_margin"] is not None and pc is not None and pb is not None \
                and pc < pb - row["parse_margin"] - 1e-12:
            fmt.append(f"parse rate {pc:.0%} more than {row['parse_margin']:.0%} below the baseline's {pb:.0%}")
        row["format"] = fmt
        if fmt:
            v = "different-worse (format)"
        elif not r.get("n"):
            v = "inconclusive"
        elif r.get("p_tost") is not None and r["p_tost"] < alpha:
            v = "equivalent"
        elif p_adj is not None and p_adj < alpha and d:
            # report.py's rule: significant with the declared sign = better; with the other sign = "reversed"
            v = "different-better" if R._direction_ok_d(direction, d) else "different-worse"
        else:
            v = "inconclusive"
        row["verdict"] = v
        row["shift_inside_margin"] = v == "equivalent" and p_adj is not None and p_adj < alpha
    return rows


def overall(rows: Sequence[Dict[str, Any]]) -> str:
    vs = [r["verdict"] for r in rows]
    if any(v.startswith("different-worse") for v in vs):
        return "FAIL"
    if vs and all(v in ("equivalent", "different-better") for v in vs):
        return "PASS"
    return "INCONCLUSIVE"


# ------------------------------------------------------------------------------------------- identity
IDENTITY_FIELDS = ("model", "model_file", "model_sha256", "quant", "llama_cpp_build", "image_digest", "ctx_size",
                   "chat_template_sha256", "flags", "llama_swap_cmd_sha256", "llama_swap_source", "served_models",
                   "system_fingerprints")
SOURCE_LIVE, SOURCE_TEMPLATE = "live config", "repo template"


def _gpu_ssh(a) -> Optional[str]:
    """The backend's ssh target, as tools/gpu-guard.sh picks it: EVAL_GPU_SSH, else BACKEND_SSH_USER@BACKEND_LAN_IP."""
    if os.environ.get("EVAL_GPU_SSH"):
        return os.environ["EVAL_GPU_SSH"]
    site = RUN.load_site_env(Path(a.site_env))
    host, user = RUN.setting("BACKEND_LAN_IP", site), RUN.setting("BACKEND_SSH_USER", site)
    return (f"{user}@{host}" if user else host) if host else None


def _ssh_live_serving(target: str) -> Dict[str, Any]:
    """spark-model-agent serving --json on the backend -> its JSON object; raises GateError when unavailable."""
    try:
        out = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, *LIVE_SERVING_CMD],
                             capture_output=True, text=True, timeout=60)
        data = json.loads(out.stdout) if out.stdout.strip() else {}
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise GateError(f"spark-model-agent serving: {exc}") from None
    if out.returncode != 0 or not data.get("ok"):
        raise GateError(f"spark-model-agent serving exited {out.returncode}: "
                        f"{(data.get('error') or out.stderr or '').strip()[:200]}")
    return data


LIVE_FETCH = _ssh_live_serving     # tests replace this


def live_snapshot(a, when: str) -> Dict[str, Any]:
    """The live llama-swap entry serving a.model now: {when, at, model_id, aliases, cmd_sha256, server_flags,
    config_sha256}, or {when, at, error} when it cannot be read (the identity then falls back to the template)."""
    snap: Dict[str, Any] = {"when": when, "at": RUN.utcnow()}
    target = _gpu_ssh(a)
    if not target:
        snap["error"] = "no backend in site.env (EVAL_GPU_SSH or BACKEND_LAN_IP)"
        return snap
    try:
        data = LIVE_FETCH(target)
    except GateError as exc:
        snap["error"] = str(exc)
        return snap
    for mid, e in (data.get("models") or {}).items():
        if a.model == mid or a.model in (e.get("aliases") or []):
            snap.update(model_id=mid, aliases=e.get("aliases"), cmd_sha256=e.get("cmd_sha256"),
                        server_flags=e.get("server_flags"), config_sha256=data.get("config_sha256"))
            return snap
    snap["error"] = f"{a.model!r} is not in the live llama-swap config"
    return snap


def _live(rec: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [x for x in ((rec or {}).get("llama_swap_live") or []) if isinstance(x, dict)]


def live_problems(rec: Optional[Dict[str, Any]]) -> List[str]:
    """A run whose model's live cmd changed between the snapshots (before and after every run.py invocation) was
    served by two configs: invalid."""
    seen = [x for x in _live(rec) if x.get("cmd_sha256")]
    hashes = list(dict.fromkeys(x["cmd_sha256"] for x in seen))
    if len(hashes) > 1:
        return [f"the live llama-swap cmd for {seen[0].get('model_id')} changed during the run ("
                + " -> ".join(h[:12] + "…" for h in hashes) + ")"]
    return []


def serving_identity(run: Dict[str, Any], rec: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """What served the tokens, from run.json: the serving fields (static config > probe > template), the static
    record's model sha256 / image digest / flags when given, the llama-swap flags and cmd hash (not the cmd: it can
    hold paths), and what the responses reported. The cmd hash and flags come from the live config snapshot in the
    gate record when there is one, else from the repo template run.py read (``llama_swap_source`` says which; the two
    never hash alike, as deploy fills site values into the template)."""
    sv = run.get("serving") if isinstance(run.get("serving"), dict) else {}
    f = sv.get("fields") or {}
    st = sv.get("static") or {}
    sw = run.get("llama_swap") or {}
    served = run.get("served") or {}
    live = next((x for x in _live(rec) if x.get("cmd_sha256")), None)
    if live:
        cmd_sha, swap_flags, source = live["cmd_sha256"], live.get("server_flags"), SOURCE_LIVE
    else:
        cmd_sha = RUN.sha256_text(sw["cmd"]) if sw.get("cmd") else None
        swap_flags, source = sw.get("server_flags"), SOURCE_TEMPLATE if cmd_sha else None
    return {
        "model": run.get("model"),
        "model_file": _val(f.get("model_file")), "model_sha256": st.get("model_sha256"),
        "quant": _val(f.get("quant")), "llama_cpp_build": _val(f.get("llama_cpp_build")),
        "image_digest": st.get("image_digest"), "ctx_size": _val(f.get("ctx_size")),
        "chat_template_sha256": _val(f.get("chat_template_sha256")),
        "flags": st.get("flags") or (swap_flags or None),
        "llama_swap_cmd_sha256": cmd_sha, "llama_swap_source": source,
        "served_models": served.get("models"), "system_fingerprints": served.get("system_fingerprints"),
    }


# ------------------------------------------------------------------------------------------- report
def _pct(x: Optional[float]) -> str:
    return "–" if x is None else f"{100 * x:.1f}%"


def _fmt_mean(x: Optional[float], kind: str) -> str:
    if x is None:
        return "–"
    return f"{x:.3f}" if kind == "continuous" else f"{100 * x:.1f}%"


def _fmt_diff(r: Dict[str, Any], kind: str) -> str:
    d, lo, hi = r.get("diff"), r.get("lo"), r.get("hi")
    if d is None:
        return "–"
    if kind == "continuous":
        return f"{d:+.3f} [{lo:+.3f}, {hi:+.3f}]"
    return f"{100 * d:+.1f} pp [{100 * lo:+.1f}, {100 * hi:+.1f}]"


def _p(x: Optional[float]) -> str:
    if x is None:
        return "–"
    return "<0.001" if x < 0.001 else f"{x:.3f}"


def _short(v: Any) -> str:
    if v is None:
        return "–"
    s = json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else str(v)
    s = s.replace("|", "\\|")
    return s if len(s) <= 60 else s[:57] + "…"


def render(m: Dict[str, Any], rows: Sequence[Dict[str, Any]], verdict: str, base_label: str, cand_label: str,
           base_ident: Dict[str, Any], cand_ident: Dict[str, Any], notes: Sequence[str] = (),
           title: str = "Upgrade gate") -> str:
    code = OVERALL_EXIT[verdict]
    word = {"INCONCLUSIVE": "INCONCLUSIVE (hold: the owner decides)"}.get(verdict, verdict)
    out = [f"# {title}: {cand_ident.get('model') or '?'}", "",
           f"**Overall: {word}** (exit {code}). Manifest `{m['name']}` (sha256 {m['_sha256'][:12]}…"
           f"{', ' + m['status'] if m.get('status') else ''}), alpha {m['alpha']}, Holm over {len(rows)} row(s); "
           f"scorer {m['scorer_version']}.", "",
           f"Baseline `{base_label}` · candidate `{cand_label}` · difference = candidate − baseline.", "",
           "| Suite | Metric | n | Baseline | Candidate | Diff (95% CI) | Margin | TOST p | Diff p (Holm) "
           "| Parse b→c | Trunc b→c | Verdict |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for row in rows:
        r, k = row["r"], row["kind"]
        margin = f"±{row['margin']:.2f}" if k == "continuous" else f"±{100 * row['margin']:g} pp"
        nat = METRICS.get(row["metric"], (None, row["better"]))[1]
        metric = row["metric"] + ("" if row["better"] == nat else f" ({row['better']} better)")
        out.append(
            f"| {row['name']} | {metric} | {r.get('n', 0)} | {_fmt_mean(r.get('b_mean'), k)} | "
            f"{_fmt_mean(r.get('a_mean'), k)} | {_fmt_diff(r, k)} | {margin} | {_p(r.get('p_tost'))} | "
            f"{_p(r.get('p'))} ({_p(row['p_holm'])}) | {_pct(row['base']['parse_rate'])} → "
            f"{_pct(row['cand']['parse_rate'])} | {_pct(row['base']['truncation_rate'])} → "
            f"{_pct(row['cand']['truncation_rate'])} | **{row['verdict']}** |")
    out += ["", "Tests: binary metrics use the exact McNemar test (difference) and the exact Tango TOST "
                "(equivalence); MAE uses the paired bootstrap test and bootstrap TOST. Equivalent = TOST p < alpha; "
                "different = Holm-adjusted difference p < alpha. Errors count as fails. The format row pools the "
                "parse-ok rate; each pooled suite also has a one-sided exact McNemar tripwire on newly unparsed "
                "items."]
    fmt = [f"- {row['name']}: " + "; ".join(row["format"]) for row in rows if row["format"]]
    api = [f"- {row['name']}: api errors {row['base']['api_errors']} → {row['cand']['api_errors']}"
           for row in rows if row["base"]["api_errors"] or row["cand"]["api_errors"]]
    if fmt or api:
        out += ["", "## Run validity", ""] + fmt + api
    shift = [f"{row['name']} ({_fmt_diff(row['r'], row['kind'])})" for row in rows if row.get("shift_inside_margin")]
    if shift:
        notes = list(notes) + ["significant shift inside the margin (equivalent by the margin; worth a look): "
                               + ", ".join(shift)]
    out += ["", "## Serving identity", "", "| Field | Baseline | Candidate | Changed |", "|---|---|---|---|"]
    for k in IDENTITY_FIELDS:
        b, c = base_ident.get(k), cand_ident.get(k)
        if b is None and c is None:
            continue
        out.append(f"| {k} | {_short(b)} | {_short(c)} | {'**yes**' if b != c else ''} |")
    if notes:
        out += ["", "## Notes", ""] + [f"- {n}" for n in notes]
    out += ["", f"**Overall: {word}** (exit {code})."]
    return "\n".join(out) + "\n"


def load_items_for(m: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """id -> item (gold, labels, rigor flags) for report.item_records, from the manifest's data files."""
    return {it["id"]: it for it in manifest_items(m)}


def analyse(m: Dict[str, Any], base_dir: Path, cand_dir: Path, allow_stale_harness: bool = False,
            title: str = "Upgrade gate", extra_notes: Sequence[str] = ()) -> Dict[str, Any]:
    """Compatibility, validity, per-suite verdicts and the report for two existing dirs. No model calls."""
    comp = compatibility(base_dir, cand_dir, m, allow_stale_harness)
    notes = list(extra_notes)
    if comp["stale_harness"]:
        notes.append("STALE HARNESS allowed by --allow-stale-harness: run.py/scorers.py differ between baseline "
                     f"and candidate ({json.dumps(comp['stale_harness']['baseline'])} vs "
                     f"{json.dumps(comp['stale_harness']['candidate'])})")
    items = load_items_for(m) if m.get("_checked_data") else None
    inv_b, base = run_invalid(comp["baseline_run"], m, items)
    inv_c, cand = run_invalid(comp["candidate_run"], m, items)
    inv_b += live_problems(comp["baseline_rec"])
    inv_c += live_problems(comp["candidate_rec"])
    bi = serving_identity(comp["baseline"], comp["baseline_rec"])
    ci = serving_identity(comp["candidate"], comp["candidate_rec"])
    if bi["llama_swap_source"] and ci["llama_swap_source"] and bi["llama_swap_source"] != ci["llama_swap_source"]:
        notes.append(f"llama_swap_cmd_sha256 comes from the {bi['llama_swap_source']} for the baseline and the "
                     f"{ci['llama_swap_source']} for the candidate: the two hashes are not comparable (deploy fills "
                     "site values into the template)")
    invalid = [f"baseline: {x}" for x in inv_b] + [f"candidate: {x}" for x in inv_c]
    rows = [] if invalid else verdicts(m, base, cand)
    verdict = "INVALID" if invalid else overall(rows)
    notes = [f"INVALID {x}" for x in invalid] + notes
    md = render(m, rows, verdict, Path(base_dir).name, Path(cand_dir).name, bi, ci, notes, title)
    return {"verdict": verdict, "exit": OVERALL_EXIT[verdict], "rows": rows, "invalid": invalid,
            "stale_harness": comp["stale_harness"], "report": md, "identity": {"baseline": bi, "candidate": ci}}


def rows_json(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    keep = ("n", "a_mean", "b_mean", "diff", "lo", "hi", "p", "p_tost", "method")
    return [{"name": r["name"], "metric": r["metric"], "verdict": r["verdict"], "p_holm": r["p_holm"],
             "format": r["format"], "tripwire": r["tripwire"], "diag": r.get("diag"), "base": r["base"],
             "cand": r["cand"], **{k: r["r"].get(k) for k in keep}} for r in rows]


# ------------------------------------------------------------------------------------------- running
def _state_dir() -> Path:
    return Path(os.environ.get("EVAL_STATE_DIR") or (REPO_DIR / "evals" / "results" / "_runs" / RUN_NAME))


def _subprocess_runner(argv: List[str]) -> int:
    """run.py as its own process, on the command line the GPU guard and stop.sh match
    (``python3 -B evals/run.py ... --run-name gate``), so a guard stop interrupts it cleanly."""
    try:
        return subprocess.call(["python3", "-B", "evals/run.py", *argv], cwd=str(REPO_DIR))
    except KeyboardInterrupt:
        return 130


RUNNER = _subprocess_runner      # tests replace this with an in-process call


def start_guard(a) -> Optional[subprocess.Popen]:
    """tools/gpu-guard.sh for the run (as the plans do), unless --no-guard or no GPU host is configured."""
    if a.no_guard:
        return None
    site = RUN.load_site_env(Path(a.site_env))
    if not (os.environ.get("EVAL_GPU_SSH") or RUN.setting("BACKEND_LAN_IP", site)):
        log("warning: GPU guard not started (set EVAL_GPU_SSH or BACKEND_LAN_IP in site.env); --no-guard silences this")
        return None
    env = dict(os.environ, EVAL_RUN=RUN_NAME)
    return subprocess.Popen(["bash", str(GUARD)], cwd=str(REPO_DIR), env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def start_route(a) -> Tuple[str, Optional[str], Optional[Path]]:
    """Pick the gateway route for one run. -> (route, base URL for run.py or None, tunnel script to stop or None).

    ``--base-url`` or EVAL_BASE_URL (environment or site.env) is used as given. Otherwise the gate starts
    tools/gateway-tunnel.sh (as the plans do): requests go over SSH to the backend's LiteLLM instead of through the
    public edge, where a dead TLS handshake held a request until --timeout three times on 2026-10-13. When the tunnel
    dies, ssh closes the local socket and run.py retries at once. ``--via-edge``, or no backend in site.env, keeps
    the edge. A configured tunnel that does not come up is a refusal, not a silent fallback to the edge."""
    if a.base_url:
        return "base-url", a.base_url, None
    site = RUN.load_site_env(Path(a.site_env))
    if RUN.setting("EVAL_BASE_URL", site):
        return "EVAL_BASE_URL", None, None          # run.py reads it itself
    if getattr(a, "via_edge", False):
        return "edge", None, None
    if not ((os.environ.get("EVAL_GPU_SSH") or RUN.setting("BACKEND_LAN_IP", site)) and
            RUN.setting("BACKEND_WG_IP", site)):
        log("warning: no backend in site.env (BACKEND_LAN_IP, BACKEND_WG_IP): requests go through the public edge; "
            "--via-edge silences this")
        return "edge", None, None
    env = dict(os.environ, EVAL_RUN=RUN_NAME)
    try:
        out = subprocess.run(["bash", str(TUNNEL), "start"], cwd=str(REPO_DIR), env=env, capture_output=True,
                             text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GateError(f"gateway tunnel did not start ({exc}); --via-edge uses the public edge") from None
    url = next((ln.split("=", 1)[1].strip() for ln in out.stdout.splitlines() if ln.startswith("EVAL_BASE_URL=")), "")
    if out.returncode != 0 or not url:
        raise GateError(f"gateway tunnel did not start ({(out.stderr or out.stdout).strip()[:200] or out.returncode}); "
                        "--via-edge uses the public edge")
    log(f"gate: requests go over the SSH tunnel to the backend's LiteLLM ({url})")
    return "tunnel", url, TUNNEL


def stop_route(tunnel: Optional[Path]) -> None:
    if tunnel is not None:
        subprocess.run(["bash", str(tunnel), "stop"], cwd=str(REPO_DIR), env=dict(os.environ, EVAL_RUN=RUN_NAME),
                       capture_output=True, timeout=30)


def run_argv(m: Dict[str, Any], a, gate_dir: Path, base_url: Optional[str] = None) -> List[str]:
    files = sorted({str(s["_path"]) for e in m["suites"] for s in e["sources"]})
    argv = [x for f in files for x in ("--suite", f)]
    argv += ["--model", a.model, "--thinking", m["sampling"]["thinking"], "--seed", str(m["sampling"]["seed"]),
             "--freeze", str(gate_dir / SELECTION), "--select-frozen", "--probe-server", "--stop-rules",
             "--no-final-answer", "--no-prompt-cache", "--concurrency", str(m["sampling"]["concurrency"]),
             "--budget-policy", str(gate_dir / BUDGET), "--results", str(gate_dir), "--run-name", RUN_NAME,
             "--timeout", str(getattr(a, "request_timeout", REQUEST_TIMEOUT_S))]
    if base_url:
        argv += ["--base-url", base_url]
    for flag in ("key_file", "site_env", "serving_config", "llama_swap_config"):
        v = getattr(a, flag, None)
        if v:
            argv += ["--" + flag.replace("_", "-"), str(v)]
    return argv


def estimate_seconds(m: Dict[str, Any], baseline_run: Optional[Path] = None) -> Tuple[float, str]:
    items = manifest_items(m)
    if baseline_run is not None:
        lats = [r.get("latency_s") for r in R.read_jsonl(Path(baseline_run) / "responses.jsonl")
                if _num(r.get("latency_s"))]
        if lats:
            return len(items) * sum(lats) / len(lats), "the baseline's mean latency"
    tot, measured = 0.0, True
    for e in m["suites"]:
        for s in e["sources"]:
            its = [it for it in e["_items"] if it["suite"] == s["suite"]]
            if _num(s.get("seconds_per_item")):
                tot += s["seconds_per_item"] * len(its)
            else:
                measured = False
                tot += sum(TYPE_SECONDS.get(it["type"], 15) for it in its)
    return tot, "the manifest's measured s/item" if measured else "rough per-type guesses"


def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def new_gate_dir(root: Path, model: str, kind: str) -> Path:
    base = Path(root) / RUN_slug(model) / f"{kind}-{stamp()}"
    d, k = base, 1
    while d.exists():
        d = base.with_name(f"{base.name}-{k}")
        k += 1
    return d


def write_record(d: Path, rec: Dict[str, Any]) -> None:
    tmp = d / (RECORD + ".tmp")
    tmp.write_text(json.dumps(rec, indent=2, default=str) + "\n", encoding="utf-8")
    tmp.replace(d / RECORD)


def preflight(m: Dict[str, Any], a, what: str, n_runs: int = 1, baseline_run: Optional[Path] = None) -> None:
    """Refusals that need no GPU, then the plan (model, items, time); nothing runs without --yes."""
    if a.model == RUN.FRONTIER_PREFIX or a.model.startswith(RUN.FRONTIER_PREFIX + ":"):
        raise GateError("the gate never calls a frontier model")
    if m["model"] != a.model:
        raise GateError(f"manifest {m['_path'].name} is for {m['model']!r}, not {a.model!r}")
    if S.SCORER_VERSION != m["scorer_version"]:
        raise GateError(f"the manifest requires scorer_version {m['scorer_version']!r}; scorers.py is "
                        f"{S.SCORER_VERSION!r}")
    secs, how = estimate_seconds(m, baseline_run)
    secs *= n_runs
    caps = suite_caps(m["suites"])
    log(f"gate {what}: model {a.model}, {len(manifest_items(m))} items in {len(m['suites'])} suite(s)"
        f"{f' x {n_runs} runs' if n_runs > 1 else ''} (manifest {m['name']}, thinking off, greedy, prompt cache off, "
        f"concurrency 1{', caps ' + json.dumps(caps) if caps else ''}), estimated ~{secs / 3600:.1f} h ({how})")
    if a.model in RUN.SWAPPING_ALIASES:
        log(f"note: '{a.model}' loads on both GPUs and evicts coder/coder-fast while it runs")
    if not a.yes:
        raise GateError("not running without --yes (the owner approves GPU runs)")
    stop = _state_dir() / "STOP"
    if stop.exists():
        raise GateError(f"STOP file present ({stop}); delete it to run again (and use --resume DIR to continue)")


def generate(m: Dict[str, Any], a, kind: str, resume: Optional[str] = None) -> Tuple[Path, int, Dict[str, Any]]:
    """Run run.py into a new (or resumed) gate dir. -> (gate dir, run.py exit, record)."""
    if resume:
        gd = Path(resume)
        rec = json.loads((gd / RECORD).read_text(encoding="utf-8"))
        if rec.get("kind") != kind or rec.get("model") != a.model or \
                (rec.get("manifest") or {}).get("sha256") != m["_sha256"]:
            raise GateError(f"--resume {gd.name}: not a {kind} of {a.model} under this manifest")
    route, url, tunnel = start_route(a)     # a refusal here leaves no gate dir behind
    guard = None
    try:
        if not resume:
            gd = new_gate_dir(Path(a.results_root), a.model, kind)
            gd.mkdir(parents=True)
            rec = {"schema": RECORD_SCHEMA, "kind": kind, "model": a.model, "created": RUN.utcnow(),
                   "manifest": {"path": RUN.rel(m["_path"]), "sha256": m["_sha256"], "name": m["name"],
                                "status": m.get("status")},
                   "scorer_version": m["scorer_version"], "sampling": public_sampling(m),
                   "budget": {"policy": budget_doc(m), "policy_sha256": m["sampling"]["budget_policy_sha256"],
                              "suite_answer_tokens": suite_caps(m["suites"])},
                   "run_dir": f"{RUN_NAME}-{RUN_slug(a.model)}", "items": len(manifest_items(m)),
                   "status": "running"}
        (gd / SELECTION).write_text(json.dumps(selection(m), indent=2) + "\n", encoding="utf-8")
        (gd / BUDGET).write_text(json.dumps(budget_doc(m), indent=2) + "\n", encoding="utf-8")
        rec.setdefault("routes", []).append(route)      # one per run.py invocation (resumes add one)
        before = live_snapshot(a, "before")
        rec.setdefault("llama_swap_live", []).append(before)
        if before.get("error"):
            log(f"warning: live llama-swap config not read ({before['error']}); the serving identity falls back to "
                "the repo template")
        write_record(gd, rec)
        guard = start_guard(a)
        rc = RUNNER(run_argv(m, a, gd, url))
    finally:
        if guard is not None:
            guard.terminate()
        stop_route(tunnel)
    rec["llama_swap_live"].append(live_snapshot(a, "after"))
    rec.setdefault("run_exits", []).append(rc)
    return gd, rc, rec


def run_failure(gd: Path, rc: int, rec: Dict[str, Any]) -> Optional[int]:
    """Map a non-zero run.py exit to the gate's exit (None when run.py succeeded)."""
    if rc == 0:
        return None
    if rc == 4:
        rec["status"] = "invalid"
        why = "stopped as invalid by a stopping rule (see protocol.invalid in run.json)"
    elif rc == 130:
        rec["status"] = "interrupted"
        why = f"interrupted (GPU guard or Ctrl-C); delete any STOP file and resume with --resume {gd}"
    else:
        rec["status"] = "failed"
        write_record(gd, rec)
        log(f"gate: run.py exited {rc}; nothing to compare")
        return EXIT_REFUSED
    rec["invalid"] = [why]
    write_record(gd, rec)
    log(f"gate: INVALID run: {why}")
    return EXIT_INVALID


def finish_baseline(m: Dict[str, Any], gd: Path, rec: Dict[str, Any]) -> Optional[int]:
    """Validate a finished baseline run and mark it complete (None) or invalid (exit 3)."""
    rd = gd / rec["run_dir"]
    inv, _ = run_invalid(rd, m, load_items_for(m))
    run = R._read_run(rd)
    inv += manifest_problems(run, m, "baseline") + live_problems(rec)
    rec["serving_identity"] = serving_identity(run, rec)
    rec["harness"] = harness_signature(run)
    rec["budget"]["per_suite"] = (run.get("budget") or {}).get("per_suite")
    if inv:
        rec.update(status="invalid", invalid=inv)
        write_record(gd, rec)
        log("gate: INVALID baseline (not usable as a reference):\n  - " + "\n  - ".join(inv))
        return EXIT_INVALID
    rec.update(status="complete", ended=RUN.utcnow())
    write_record(gd, rec)
    return None


def cmd_baseline(m: Dict[str, Any], a) -> int:
    preflight(m, a, "baseline")
    gd, rc, rec = generate(m, a, "baseline", a.resume)
    bad = run_failure(gd, rc, rec)
    if bad is None:
        bad = finish_baseline(m, gd, rec)
    if bad is not None:
        return bad
    log(f"gate: baseline stored: {gd}")
    log("serving identity: " + json.dumps({k: v for k, v in rec["serving_identity"].items() if v is not None}))
    print(str(gd))
    return EXIT_PASS


def aa_passes(res: Dict[str, Any]) -> bool:
    """An A/A run must be equivalent on every row: overall PASS with no different-worse, and no
    "different-better" either (two runs of one config that differ "for the better" are just as non-deterministic)."""
    return res["verdict"] == "PASS" and bool(res["rows"]) and all(r["verdict"] == "equivalent" for r in res["rows"])


def cmd_calibrate(m: Dict[str, Any], a) -> int:
    """A/A: the baseline config twice back to back. The first run becomes a baseline; the A/A verdict is stored on
    it, and ``check`` needs the model's latest A/A under this manifest to pass (or --accept-aa)."""
    preflight(m, a, "calibrate (A/A)", n_runs=2)
    gd, rc, rec = generate(m, a, "baseline", a.resume)
    bad = run_failure(gd, rc, rec)
    if bad is None:
        bad = finish_baseline(m, gd, rec)
    if bad is not None:
        return bad
    a2, rc2, rec2 = generate(m, a, "aa", None)
    rec2["baseline"] = str(gd)
    bad = run_failure(a2, rc2, rec2)
    if bad is not None:
        rec["aa"] = {"partner": str(a2), "verdict": "INVALID", "pass": False, "at": RUN.utcnow()}
        write_record(gd, rec)
        return bad
    res = analyse(m, gd, a2, title="A/A calibration")
    ok = aa_passes(res)
    worse = [r["name"] for r in res["rows"] if r["verdict"].startswith("different-worse")]
    rec["aa"] = {"partner": str(a2), "verdict": res["verdict"], "pass": ok, "different_worse": worse,
                 "at": RUN.utcnow(), "diagnostics": {r["name"]: r.get("diag") for r in res["rows"]},
                 "tripwires": {r["name"]: r["tripwire"] for r in res["rows"] if r.get("tripwire")}}
    write_record(gd, rec)
    rec2.update(status="complete", ended=RUN.utcnow(), verdict=res["verdict"], suites=rows_json(res["rows"]))
    write_record(a2, rec2)
    (a2 / REPORT).write_text(res["report"], encoding="utf-8")
    print(res["report"])
    log(f"gate: A/A {'PASS' if ok else 'NOT PASSED'} ({res['verdict']}); diagnostics "
        + json.dumps(rec["aa"]["diagnostics"]))
    if not ok:
        log("gate: `check` refuses this model until a passing A/A, or --accept-aa (recorded). Fix determinism "
            "(prompt cache, -np 1, no other traffic) or raise the planning nuisances first.")
    return EXIT_PASS if ok else res["exit"] if res["exit"] != EXIT_PASS else EXIT_INCONCLUSIVE


def cmd_check(m: Dict[str, Any], a) -> int:
    if a.baseline:
        bdir = Path(a.baseline)
        resolve_run_dir(bdir)
        try:
            brec = json.loads((bdir / RECORD).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise GateError(f"--baseline {bdir}: no gate.json (use `gate.py compare` for bare run dirs)") from None
        why = record_compatible(brec, m)
        if brec.get("model") != a.model:
            why.append(f"baseline of {brec.get('model')!r}, not {a.model!r}")
        if why:
            raise GateError(f"--baseline {bdir.name} is not compatible", why)
    else:
        bdir = find_baseline(Path(a.results_root), a.model, m)
    aa_dir, aa = aa_status(Path(a.results_root), a.model, m)
    aa_note = None
    if aa is None or not aa.get("pass"):
        why = (f"no A/A calibration of {a.model} under this manifest (run `gate.py calibrate --model {a.model}`)"
               if aa is None else f"the latest A/A ({aa_dir.name}) did not pass: {aa.get('verdict')}"
               f"{', different-worse: ' + ', '.join(aa.get('different_worse') or []) if aa.get('different_worse') else ''}")
        if not a.accept_aa:
            raise GateError(f"check refused: {why}; pass --accept-aa to proceed anyway (recorded)")
        aa_note = f"A/A ACCEPTED by --accept-aa: {why}"
        log("gate: " + aa_note)
    log(f"gate check: baseline {bdir}")
    brd, _ = resolve_run_dir(bdir)
    preflight(m, a, "check", baseline_run=brd)
    gd, rc, rec = generate(m, a, "check", a.resume)
    rec["baseline"] = str(bdir)
    rec["aa"] = {"from": str(aa_dir) if aa_dir else None, "pass": bool(aa and aa.get("pass")),
                 "accepted": bool(aa_note)}
    bad = run_failure(gd, rc, rec)
    if bad is not None:
        return bad
    rd = gd / rec["run_dir"]
    rec["serving_identity"] = serving_identity(R._read_run(rd), rec)
    rec["harness"] = harness_signature(R._read_run(rd))
    try:
        res = analyse(m, bdir, gd, a.allow_stale_harness, extra_notes=[aa_note] if aa_note else ())
    except GateError:
        rec["status"] = "refused"
        write_record(gd, rec)
        raise
    (gd / REPORT).write_text(res["report"], encoding="utf-8")
    rec.update(status="invalid" if res["invalid"] else "complete", ended=RUN.utcnow(), verdict=res["verdict"],
               exit=res["exit"], invalid=res["invalid"] or None, stale_harness=res["stale_harness"],
               suites=rows_json(res["rows"]))
    write_record(gd, rec)
    print(res["report"])
    if res["stale_harness"]:
        log("gate: STALE HARNESS allowed (--allow-stale-harness); recorded in gate.json")
    log(f"gate: {res['verdict']} (exit {res['exit']}); report: {gd / REPORT}")
    return res["exit"]


def cmd_compare(m: Dict[str, Any], a) -> int:
    if S.SCORER_VERSION != m["scorer_version"]:
        raise GateError(f"the manifest requires scorer_version {m['scorer_version']!r}; scorers.py is "
                        f"{S.SCORER_VERSION!r}")
    res = analyse(m, Path(a.a), Path(a.b), a.allow_stale_harness)
    if a.out:
        Path(a.out).write_text(res["report"], encoding="utf-8")
    print(res["report"])
    return res["exit"]


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # usage errors are refusals (4), not a verdict (2)
        self.print_usage(sys.stderr)
        self.exit(EXIT_REFUSED, f"{self.prog}: error: {message}\n")


def parse_args(argv: Optional[List[str]] = None):
    ap = _Parser(description="Upgrade gate: paired regression check of a model/quant/build/flag change.")
    sub = ap.add_subparsers(dest="cmd", required=True, parser_class=_Parser)

    def common(p):
        p.add_argument("--manifest", help=f"default: evals/plans/{MANIFEST_PREFIX}-<model>.json")
        p.add_argument("--allow-stale-harness", action="store_true",
                       help="compare even if run.py/scorers.py differ from the baseline's (recorded and printed)")

    for name in ("calibrate", "baseline", "check"):
        p = sub.add_parser(name)
        common(p)
        p.add_argument("--model", required=True)
        p.add_argument("--yes", action="store_true", help="run on the GPUs (otherwise print the plan and stop)")
        p.add_argument("--resume", metavar="GATE_DIR", help="continue an interrupted gate dir of this kind")
        p.add_argument("--results-root", default=str(RESULTS_ROOT))
        p.add_argument("--no-guard", action="store_true", help="do not start tools/gpu-guard.sh")
        p.add_argument("--base-url", help="gateway URL (default: EVAL_BASE_URL, else an SSH tunnel to the backend)")
        p.add_argument("--via-edge", action="store_true",
                       help="send requests through the public edge instead of the SSH tunnel")
        p.add_argument("--request-timeout", type=float, default=REQUEST_TIMEOUT_S,
                       help=f"seconds per request for run.py (default {REQUEST_TIMEOUT_S:g})")
        p.add_argument("--key-file")
        p.add_argument("--site-env", default=str(REPO_DIR / "site.env"))
        p.add_argument("--serving-config")
        p.add_argument("--llama-swap-config")
        if name == "check":
            p.add_argument("--baseline", metavar="DIR", help="this baseline instead of the latest compatible one")
            p.add_argument("--accept-aa", action="store_true",
                           help="run although the model has no passing A/A calibration (recorded in the report)")
    p = sub.add_parser("compare", help="offline: compare two existing gate or run dirs (no model calls)")
    common(p)
    p.add_argument("a", metavar="BASELINE")
    p.add_argument("b", metavar="CANDIDATE")
    p.add_argument("--out", help="also write the markdown report here")
    return ap.parse_args(argv)


def _compare_model(a) -> Optional[str]:
    for d in (a.b, a.a):
        try:
            rd, rec = resolve_run_dir(Path(d))
        except GateError:
            continue
        return (rec or {}).get("model") or R._read_run(rd).get("model")
    return None


def main(argv: Optional[List[str]] = None) -> int:
    a = parse_args(argv)
    try:
        if a.manifest:
            path = Path(a.manifest)
        else:
            model = a.model if a.cmd != "compare" else _compare_model(a)
            if not model:
                raise GateError("compare: cannot tell the model; pass --manifest")
            path = manifest_for(model)
            if not path.is_file():
                raise GateError(f"no gate manifest for model {model!r} ({RUN.rel(path)}); the gate covers only "
                                "models with a manifest")
        m = load_manifest(path)
        return {"calibrate": cmd_calibrate, "baseline": cmd_baseline, "check": cmd_check,
                "compare": cmd_compare}[a.cmd](m, a)
    except GateError as exc:
        log(f"gate refused: {exc}")
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
