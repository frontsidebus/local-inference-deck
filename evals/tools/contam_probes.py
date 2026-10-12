#!/usr/bin/env python3
"""Build (never run) contamination-probe suites in the harness item format, plus the pre-registered decision
rules. Never includes sealed items (heldout-v1) so building and running these probes cannot leak the split.

Probes (each is a run.py suite; run with --protocol exploratory, thinking off, greedy):
  P1 perm   MCQ option permutation (Zong et al. 2023; Yang et al. 2023 "Rethinking benchmark and contamination"):
            options cyclically shifted so the key moves. Signals: paired accuracy drop orig->perm (McNemar), and the
            "stale letter" rate = share of wrong permuted answers that pick the ORIGINAL key letter; chance is
            1/(k-1). A stale-letter rate well above chance = position memorisation.
  P2 tsg    TS-Guessing (Deng et al. 2024, NAACL, "Investigating Data Contamination in Modern Benchmarks"): one wrong
            option replaced by [MASK]; the model must reproduce it. Exact-match rate (normalised) per suite.
  P3 idonly CVE-id-only recall: "CVE-XXXX-YYYY: which CWE / which CVSS vector?" with no description. Compared with
            the majority-class baseline of the same items. Above-baseline = memorised id->label. NVD items published
            after every model's upper bound are the negative control (must sit at baseline).
  P4 cont   Verbatim continuation: first half of the content (word level); the model continues. Metric: longest
            common token substring / 13-gram hit vs the true second half. Threshold = 99th percentile of the same
            metric on the post-release NVD control items (published >= 2026-08-25, after all upper bounds), fixed
            before the public-suite probes are scored.
Decision rules (fixed here, before any probe output exists):
  - suite-level: a suite is "contamination-suspect for model m" if any of P1 (Holm p<0.05 on drop or stale-letter
    binomial vs 1/(k-1)), P2 (EM > 2x control), P3 (acc - baseline > 10pp, binomial p<0.05), P4 (>5% items above the
    control p99) holds.
  - item-level flags are model-specific outputs, so they are NEVER used to exclude items (that would be an
    output-dependent exclusion); they define a reported stratum only.
  - the probes give SUITE-LEVEL evidence of contamination (or its absence) only; a negative probe does not show that
    a suite is clean, and no probe says anything about a single item.

    python3 evals/tools/contam_probes.py --data-dir evals/data --out <dir>      # writes <dir>/<probe-suite>.jsonl

Pre-registered in evals/PROTOCOL.md (C1.13 arm f). Builds item files only; it makes no model calls. Never includes
sealed items (datasets/heldout/heldout-v1.json), so building and running the probes cannot spend the split.
"""
import argparse
import hashlib
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

EVALS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(EVALS / "datasets"))
import audit as A  # noqa: E402

DATA = EVALS / "data"
OUTD = EVALS / "results" / "contam-probes"
SEALED = A.load_heldout(EVALS / "datasets" / "heldout" / "heldout-v1.json")
N = 200
SEED = 20261011
CONTROL_FROM = "2026-08-25"   # NVD items published on or after this date are after every model's upper bound


def pick(items, n):
    items = [it for it in items if A.heldout_key(it) not in SEALED]
    return sorted(items, key=lambda it: hashlib.sha256(f"{SEED}|{it['id']}".encode()).hexdigest())[:n]


def opts(it):
    return [re.sub(r"^[A-Z]\)\s*", "", c) for c in it["choices"]]


def perm(it):
    o = opts(it); k = len(o); L = [chr(65 + i) for i in range(k)]
    key = L.index(it["answer"])
    new = o[1:] + o[:1]                       # cyclic shift: every option moves, the key always moves
    return dict(it, id=it["id"] + "-perm", suite=it["suite"] + "-perm", choices=[f"{L[i]}) {t}" for i, t in enumerate(new)],
                answer=L[(key - 1) % k], meta=dict(it.get("meta") or {}, probe="perm", orig_answer=it["answer"]))


def tsg(it, rng):
    o = opts(it); L = [chr(65 + i) for i in range(len(o))]
    wrong = [i for i in range(len(o)) if L[i] != it["answer"]]
    m = rng.choice(wrong)
    shown = "\n".join(f"{L[i]}) {'[MASK]' if i == m else t}" for i, t in enumerate(o))
    prompt = ("Fill in the [MASK] option in this multiple-choice question from a public benchmark. Reply with only "
              f"the exact option text.\n\n{it['prompt']}\n\n{shown}")
    return {"id": it["id"] + "-tsg", "suite": it["suite"] + "-tsg", "type": "extract", "prompt": prompt,
            "answer": o[m], "scorer": "exact", "meta": {"probe": "tsg", "src": it["id"]}}


def idonly(it, kind):
    cve = A.cve_of(it)
    if kind == "cwe":
        q = (f"Without any description: which CWE is {cve} classified under? The last line of your response must "
             "contain only the CWE ID.")
    else:
        q = (f"Without any description: what is the CVSS v3.1 base vector of {cve}? The last line of your response "
             "must contain only the CVSS v3.1 vector string.")
    return {"id": it["id"] + "-idonly", "suite": it["suite"] + "-idonly", "type": "classify", "prompt": q,
            "answer": it["answer"], "scorer": it["scorer"],
            "meta": {"probe": "idonly", "src": it["id"], "cve": cve, "published": (it.get("meta") or {}).get("published")}}


def cont(it):
    words = A.content_text(it).split()
    h = len(words) // 2
    return {"id": it["id"] + "-cont", "suite": it["suite"] + "-cont", "type": "freeform",
            "prompt": ("Continue this text exactly as it appears in its original source. Output only the continuation."
                       f"\n\n{' '.join(words[:h])}"),
            "answer": " ".join(words[h:]), "scorer": "f1_tokens",
            "meta": {"probe": "cont", "src": it["id"], "published": (it.get("meta") or {}).get("published")}}


def main(argv=None):
    global DATA, OUTD
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=str(DATA))
    ap.add_argument("--out", default=str(OUTD))
    a = ap.parse_args(argv)
    DATA, OUTD = Path(a.data_dir), Path(a.out)
    OUTD.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)
    out, stats = {}, {}
    for s in ("ctibench-mcq", "cybermetric-500", "secqa-v1", "secqa-v2", "sevenllm-mcq"):
        base = pick(A.read_jsonl(DATA / f"{s}.jsonl"), N)
        out[f"{s}-perm"] = [perm(it) for it in base]
        out[f"{s}-tsg"] = [tsg(it, rng) for it in base]
        out[f"{s}-cont"] = [cont(it) for it in base if len(A.content_text(it).split()) >= 16]
    for s, kind in (("ctibench-rcm", "cwe"), ("ctibench-vsp", "cvss")):
        base = pick(A.read_jsonl(DATA / f"{s}.jsonl"), N)
        out[f"{s}-idonly"] = [idonly(it, kind) for it in base]
        out[f"{s}-cont"] = [cont(it) for it in base]
    for s, kind in (("nvd-cwe", "cwe"), ("nvd-cvss", "cvss")):   # negative control: after every upper bound
        ctrl = [it for it in A.read_jsonl(DATA / f"{s}.jsonl") if (it["meta"].get("published") or "") >= CONTROL_FROM]
        base = pick(ctrl, N)
        out[f"{s}-idonly-control"] = [idonly(it, kind) for it in base]
        out[f"{s}-cont-control"] = [cont(it) for it in base]
    for name, items in out.items():
        for it in items:
            it["suite"] = name
        (OUTD / f"{name}.jsonl").write_text("".join(json.dumps(it, ensure_ascii=False) + "\n" for it in items))
        stats[name] = len(items)
    # majority-class baselines for P3 (label prior of the probed items: no model involved)
    for name in [n for n in out if "idonly" in n]:
        c = Counter(it["answer"] for it in out[name])
        stats[name + ":majority_baseline_acc"] = round(c.most_common(1)[0][1] / len(out[name]), 3)
    leaked = sum(1 for items in out.values() for it in items
                 if A.heldout_key({**it, "id": it["id"], "meta": it.get("meta") or {}}) in SEALED and A.cve_of(it))
    stats["sealed_cves_in_probes"] = leaked
    print(json.dumps(stats, indent=1))


if __name__ == "__main__":
    main()
