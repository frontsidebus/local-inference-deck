"""LLM-judge grading logic for the eval harness (stdlib only). run.py wires it in; scorers.py keeps the legacy
single-judge scorer (``make_llm_judge``) unchanged.

What lives here, and the principle behind each piece:

- ``precheck``: a deterministic screen for text addressed to the grader inside a candidate answer. Whether an
  answer tried to manipulate the grader is decided in code, not by the judge. The judge once failed a clean
  answer for "instruction markers" that were the harness's own wrapper. Only two things trigger the rule:
  copies of the harness's wrapper markers, and instructions or claimed grades addressed to a grader. A phrase
  that also appears in the task text is a quotation, not an instruction, so it does not trigger the rule.
- ``make_judge_scorer``: grades with one or more judges and stores every judge's verdict. A judge reply that is
  still invalid after the parse retries is status ``error`` with ``count_as_fail: true``. The report can then
  count it as a FAIL instead of dropping it from the denominator.
- ``self_grade_overlap``: the grader must not be one of the contestants.
- ``REFERENCE_PROVENANCE``: where each judge-graded suite's references came from (criterion contamination).
- Perturbations (``strip_markdown``, ``json_to_prose``) and ``perturbation_check``: re-grade the same content
  in another format to measure how sensitive the judge is to format.
- ``judge_accuracy``: accuracy with grader errors counted as FAIL (strict) and with them dropped (legacy).
"""
from __future__ import annotations

import functools
import json
import random
import re
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import scorers as S

PRECHECK_VERSION = "precheck-v2"
RUBRIC_VERSION = "judge-v2"

# ------------------------------------------------------------------------------------------- injection
# Fixed a priori, before any stored answer was screened. Precision matters more than recall: a security answer
# may discuss prompt injection, CVSS scores or "grades" of a vulnerability, so every pattern needs a grader-
# directed word (grader/judge/evaluator/...) or the exact shape of this harness's grade JSON or markers.
_GRADER = r"(?:grader|judge|evaluator|grading\s+(?:model|system|ai|llm)|evaluation\s+(?:model|system|ai|llm)|scorer|assessor)"
_AI = r"(?:ai|a\.i\.|model|llm|language\s+model|assistant|automated\s+(?:system|evaluator|grader|checker)|bot)"
_GRADE_VERB = r"(?:grad(?:e|es|ing)|evaluat(?:e|es|ing)|scor(?:e|es|ing)|review(?:s|ing)?|assess(?:es|ing)?|judg(?:e|es|ing)|check(?:s|ing)?)"
_ANSWER = r"(?:answer|response|reply|output|submission|candidate)"
_GOOD = (r"(?:PASS(?:ED)?|correct|full\s+(?:marks|credit|points|score)|(?:a\s+)?perfect\s+score|10\s*/\s*10|"
         r"(?:a\s+)?(?:score|grade)\s+of\s+(?:10|PASS)|(?:a\s+)?10\b(?!\s*%|\.\d))")
INJECTION_RULES: List[Tuple[str, re.Pattern]] = [
    ("wrapper_marker", re.compile(r"<<<\s*(?:END\s+)?CANDIDATE\b", re.I)),
    ("template_placeholder", re.compile(r"@@(?:NONCE|CANDIDATE|QUESTION|RUBRIC|REFERENCE)@@")),
    ("note_to_grader", re.compile(
        rf"\b(?:note|message|instruction|instructions|reminder|request)s?\s+(?:to|for)\s+(?:the\s+|any\s+|all\s+)?{_GRADER}s?\b",
        re.I)),
    ("grader_addressed", re.compile(
        rf"(?:^|[\n.!?]\s*|\b(?:dear|attention|hey|hello)\s+)(?:the\s+)?{_GRADER}s?\s*[:,]\s*(?:please\s+)?"
        r"(?:give|grade|score|mark|rate|assign|award|ignore|disregard|output|return|respond|reply|pass|treat)\b", re.I)),
    ("instruct_grader", re.compile(
        rf"\b(?:the\s+)?{_GRADER}s?\s+(?:must|should|shall|is\s+instructed\s+to|needs\s+to)\s+"
        r"(?:give|grade|score|mark|rate|assign|award|ignore|disregard|output|return|pass)\b", re.I)),
    ("claimed_grade_json", re.compile(r"""["']grade["']\s*:\s*["'](?:PASS|FAIL)["']""", re.I)),
    ("claimed_grade_line", re.compile(r"(?im)^\s*[*_#>\s]*(?:grade|verdict)\s*[*_]*\s*[:=]\s*[*_\s]*PASS\b")),
    # precheck-v2 (rules R2-4, fixed before the stored corpus was re-screened)
    ("ai_evaluator_addressed", re.compile(
        rf"\b(?:to|for|attention|dear|hey|note\s+to|message\s+to)\s+(?:the\s+|any\s+|whatever\s+)?{_AI}\s+"
        rf"(?:that\s+is\s+|who\s+is\s+|which\s+is\s+)?{_GRADE_VERB}\s+(?:this|my|the)\s+{_ANSWER}", re.I)),
    ("whoever_grades", re.compile(
        rf"\b(?:whoever|anyone|anybody|whatever)\s+(?:is\s+)?{_GRADE_VERB}\s+(?:this|my|the)\s+{_ANSWER}", re.I)),
    ("role_assertion", re.compile(
        rf"\b(?:as\s+the\s+{_GRADER}\s*,?\s+you|you\s+are\s+(?:now\s+)?(?:the|a|an)\s+{_GRADER}\b|"
        rf"you\s+(?:are|act)\s+as\s+(?:the|a|an)\s+{_GRADER}\b)", re.I)),
    ("grading_override", re.compile(
        r"\b(?:grading|scoring|evaluation|grader|judge)\s+(?:override|directive|instruction|instructions|command)s?\b",
        re.I)),
    ("self_awarded_grade", re.compile(
        rf"\b(?:this|my|the\s+above)\s+{_ANSWER}\s+(?:deserves|merits|has\s+earned|should\s+(?:get|receive|be\s+"
        rf"given|be\s+graded|be\s+scored|be\s+marked)(?:\s+as)?)\s+{_GOOD}", re.I)),
    ("imperative_grade", re.compile(
        rf"\b(?:mark|rate|grade|score|label|classify)\s+(?:this|my|the\s+above)\s+{_ANSWER}\s+(?:as\s+)?{_GOOD}",
        re.I)),
    ("grade_shaped_json", re.compile(r"""["']score["']\s*:\s*\d+\s*,\s*["']reason["']""", re.I)),
    ("spoofed_role_line", re.compile(
        r"(?im)^\s*(?:\[\s*(?:system|admin|developer|assistant)\s*\]|(?:system|admin|developer)\s*:|"
        r"assistant\s+to\s+(?:grader|judge|evaluator)\s*:).*\b(?:grade[sd]?|grader|judge|PASS)\b")),
    ("award_request", re.compile(
        r"\b(?:give|award|assign)\s+(?:this|the|my)\s+(?:answer|response|candidate)\s+(?:a\s+)?"
        r"(?:PASS|full\s+(?:marks|credit|score)|(?:a\s+)?(?:score|grade)\s+of\s+(?:10|PASS))\b", re.I)),
]


def precheck(candidate: str, question: str = "") -> Optional[Dict[str, str]]:
    """The first injection rule the candidate trips, as {"rule", "match"}, or None. Matches whose text also occurs
    in the question (the task or its quoted source) are quotations and are ignored."""
    q = (question or "").casefold()
    for name, rx in INJECTION_RULES:
        for m in rx.finditer(candidate or ""):
            frag = m.group(0).strip()
            if frag and frag.casefold() in q:
                continue
            return {"rule": name, "match": frag[:200], "version": PRECHECK_VERSION}
    return None


# ------------------------------------------------------------------------------------------- provenance
# Criterion contamination: the references a judge compares against. Suites not listed are "unknown".
REFERENCE_PROVENANCE: Dict[str, Dict[str, str]] = {
    "sevenllm-qa": {
        "source": "SEvenLLM-Bench test.jsonl (Multilingual-Multimodal-NLP/SEVENLLM-Dataset)",
        "reference_origin": "llm_generated",
        "note": "The reference answers were produced by an LLM, not written by analysts. Use the judge as a "
                "fact-checker against the source text, and treat judge-graded results as secondary evidence.",
    },
}
EVIDENCE_TIER = "secondary"  # every judge-graded suite: model-graded against model-made or unaudited references


def reference_provenance(suite: str) -> Dict[str, str]:
    p = REFERENCE_PROVENANCE.get(suite)
    if p:
        return {**p, "evidence": EVIDENCE_TIER}
    return {"reference_origin": "unknown", "evidence": EVIDENCE_TIER}


# ------------------------------------------------------------------------------------------- self-grading
def _norm_model(m: str) -> str:
    return (m or "").strip().casefold()


FAMILIES_PATH = Path(__file__).resolve().parent / "prompts" / "model_families.json"


@functools.lru_cache(maxsize=4)
def load_families(path: str = str(FAMILIES_PATH)) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _entry_families(e: Any) -> frozenset:
    """Effective families of a table entry: families + base lineage (R4-3). Accepts the old list form."""
    if isinstance(e, dict):
        return frozenset(e.get("families", [])) | frozenset(e.get("base", []))
    return frozenset(e)


def model_families(model: str, table: Optional[Dict[str, Any]] = None) -> Optional[frozenset]:
    """Effective family tags (brand + base lineage) of a model alias, weight file name or weight sha256: sha256
    table, then file table (basename, case-insensitive), then the alias table, then patterns on the basename.
    A fine-tune brand pattern (brand=true) with no explicit entry resolves to None: unknown lineage fails closed.
    None when unknown."""
    t = table or load_families()
    m = _norm_model(model)
    shas = {k.casefold(): v for k, v in t.get("sha256", {}).items()}
    if m in shas:
        return _entry_families(shas[m])
    files = {k.casefold(): v for k, v in t.get("files", {}).items()}
    base = re.split(r"[\\/]", m)[-1]
    if base in files:
        return _entry_families(files[base])
    models = {_norm_model(k): v for k, v in t.get("models", {}).items()}
    if m in models:
        return _entry_families(models[m])
    fams = set()
    for pat in t.get("patterns", []):
        if re.search(pat["regex"], base, re.I):
            if pat.get("lineage"):
                return _entry_families(pat)  # an explicit lineage pattern decides
            if pat.get("brand"):
                return None  # a fine-tune brand: its base lineage must be declared explicitly
            fams.update(pat.get("families", []))
    return frozenset(fams) if fams else None


_SHA_KEYS = ("model_sha256", "sha256")


def identity_families(name: str, identity: Optional[Dict[str, Any]] = None,
                      table: Optional[Dict[str, Any]] = None) -> Tuple[Optional[frozenset], Optional[str]]:
    """(families, conflict note). With a served identity ({"model_sha256"|"sha256", "model_file"}), the served
    weights decide; when the alias resolves to different families, that is a conflict (callers fail closed).
    R4-3: when the sha256 table is pinned (non-empty), a served sha that is not in it is a conflict."""
    t = table or load_families()
    alias_f = model_families(name, t)
    ident = identity or {}
    sha = next((str(ident[k]) for k in _SHA_KEYS if ident.get(k)), None)
    served_f = None
    if sha:
        if t.get("sha256"):
            served_f = model_families(sha, t)
            if served_f is None or sha.casefold() not in {k.casefold() for k in t["sha256"]}:
                return None, f"served sha of {name!r} is not in the pinned sha256 table"
    if served_f is None and ident.get("model_file"):
        served_f = model_families(str(ident["model_file"]), t)
        if served_f is None:
            return None, f"served file of {name!r} has unknown lineage (not in the family table)"
    if served_f is None:
        return alias_f, None
    if alias_f is not None and alias_f != served_f:
        return served_f, f"{name!r}: alias families {sorted(alias_f)} != served families {sorted(served_f)}"
    return served_f, None


def identity_notes(identities: Optional[Dict[str, Dict[str, Any]]], table: Optional[Dict[str, Any]] = None) -> List[str]:
    """Non-failing notes: a served sha given while the sha256 table is still empty (pre-freeze) is 'sha_unpinned'."""
    t = table or load_families()
    out = []
    for name, ident in (identities or {}).items():
        if any((ident or {}).get(k) for k in _SHA_KEYS) and not t.get("sha256"):
            out.append(f"sha_unpinned: {name} (family decided by the served file name until shas are pinned)")
    return out


def family_overlap(grader_models: Iterable[str], candidate_models: Iterable[str],
                   table: Optional[Dict[str, Any]] = None,
                   identities: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Every (grader, candidate) pair that is not independent: the same alias, an identity conflict, either side of
    unknown family (fail closed), or a shared family. identities: {model name: served identity from run.json}
    (model_file and/or model_sha256); when given, the served weights decide the family.
    Entries: grader, candidate, reason (self|identity_conflict|unknown|family), plus shared/unknown/conflict."""
    out = []
    ids = identities or {}
    cands = list(candidate_models)
    for g in grader_models:
        gf, gc = identity_families(g, ids.get(g), table)
        for c in cands:
            cf, cc = identity_families(c, ids.get(c), table)
            if _norm_model(g) == _norm_model(c):
                out.append({"grader": g, "candidate": c, "reason": "self", "shared": sorted(gf or [])})
            elif gc or cc:
                out.append({"grader": g, "candidate": c, "reason": "identity_conflict",
                            "conflict": [x for x in (gc, cc) if x]})
            elif gf is None or cf is None:
                out.append({"grader": g, "candidate": c, "reason": "unknown",
                            "unknown": [x for x, f in ((g, gf), (c, cf)) if f is None]})
            elif gf & cf:
                out.append({"grader": g, "candidate": c, "reason": "family", "shared": sorted(gf & cf)})
    return out


def self_grade_overlap(grader_models: Iterable[str], candidate_models: Iterable[str]) -> List[str]:
    """Grader models that are also contestants (exact alias match, case-insensitive). Different aliases that
    serve the same weights cannot be detected here."""
    cands = {_norm_model(m) for m in candidate_models}
    return sorted({g for g in grader_models if _norm_model(g) in cands})


# ------------------------------------------------------------------------------------------- the scorer
Judge = Tuple[str, Optional[S.Grader]]  # (model alias, grader callable or None)
AGGREGATES = ("first", "majority")


def _one_vote(name: str, grader: S.Grader, item, cand, spec) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Returns (vote, meta). vote has model, status (ok|error), grade, score, detail."""
    try:
        raw, meta = grader(item, cand, spec)
    except Exception as exc:  # network/grader failure: not the candidate's fault
        return {"model": name, "status": "error", "grade": None, "score": None,
                "detail": f"grader call failed: {exc}"[:300]}, {}
    try:
        g = S.parse_grade(raw)
    except ValueError as exc:
        return {"model": name, "status": "error", "grade": None, "score": None,
                "detail": f"grader reply rejected: {exc}"[:300], "raw": (raw or "")[:2000]}, meta
    return {"model": name, "status": "ok", "grade": g["grade"], "score": g["score"],
            "detail": g["reason"][:500]}, meta


def make_judge_scorer(judges: Optional[Sequence[Judge]], aggregate: str = "first",
                      question_fn: Optional[Callable[[Dict[str, Any]], str]] = None) -> Callable[[str, Dict[str, Any]], S.Score]:
    """A drop-in for scorers.make_llm_judge with N judges.

    aggregate:
      - first: the first judge decides (single-judge behaviour); the others are stored only.
      - majority: PASS when more than half of all N judges say PASS. A judge error counts as not-PASS; the item
        is an error only when every judge errored. value = mean valid score / 10.
    question_fn(item) -> the task text, for the injection pre-check's quotation exemption."""
    if aggregate not in AGGREGATES:
        raise ValueError(f"aggregate must be one of {AGGREGATES}")
    judges = [(n, g) for n, g in (judges or []) if g is not None]

    def llm_judge(text: str, item: Dict[str, Any]) -> S.Score:
        spec = S.llm_judge_spec(item)
        prov = reference_provenance(str(item.get("suite") or ""))
        cand = S.strip_thinking(text)
        if not cand:
            return S.Score(0.0, False, None, "unparsed", "empty response", extra={"reference_provenance": prov})
        question = question_fn(item) if question_fn else str(item.get("prompt") or "")
        hit = precheck(cand, question)
        if hit:
            return S.Score(0.0, False, "FAIL", "ok", f"injection pre-check ({hit['rule']}): {hit['match']}"[:300],
                           extra={"injection": hit, "model_graded": False, "reference_provenance": prov})
        if not judges:
            return S.Score(0.0, False, None, "error", "ungraded: no grader configured",
                           extra={"reference_provenance": prov})
        votes, metas = [], []
        for name, grader in judges:
            v, meta = _one_vote(name, grader, item, cand, spec)
            votes.append(v)
            metas.append(meta)
        extra: Dict[str, Any] = {"reference_provenance": prov}
        if len(judges) > 1:
            extra["judges"] = votes
            extra["aggregate"] = aggregate
        if aggregate == "first" or len(judges) == 1:
            v = votes[0]
            if v["status"] != "ok":
                extra.update({"grader": metas[0], "grader_error": True, "count_as_fail": True})
                if "raw" in v:
                    extra["grader_raw"] = v["raw"]
                return S.Score(0.0, False, None, "error", v["detail"], extra=extra)
            extra.update({"grader": metas[0], "model_graded": True})
            return S.Score(v["score"] / 10.0, v["grade"] == "PASS", v["grade"], detail=v["detail"], extra=extra)
        ok = [v for v in votes if v["status"] == "ok"]
        if not ok:
            extra.update({"grader_error": True, "count_as_fail": True})
            return S.Score(0.0, False, None, "error", "every judge errored", extra=extra)
        n_pass = sum(v["grade"] == "PASS" for v in ok)
        passed = 2 * n_pass > len(judges)
        extra.update({"model_graded": True, "n_pass": n_pass, "n_valid": len(ok), "n_judges": len(judges)})
        value = sum(v["score"] for v in ok) / len(ok) / 10.0
        return S.Score(round(value, 4), passed, "PASS" if passed else "FAIL",
                       detail=f"{n_pass}/{len(judges)} judges PASS ({len(judges) - len(ok)} errored)", extra=extra)

    return llm_judge


def valid_grade(raw: str) -> bool:
    try:
        S.parse_grade(raw)
        return True
    except ValueError:
        return False


# ------------------------------------------------------------------------------------------- accounting
def judge_accuracy(scores: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Accuracy over llm_judge score records two ways. strict: grader errors count as FAIL and stay in the
    denominator. legacy: they are dropped (what report.py did when this was written)."""
    n = passed = errors = unparsed = injected = 0
    for r in scores:
        if r.get("scorer") != "llm_judge":
            continue
        n += 1
        st = r.get("status", "ok")
        if st == "error":
            errors += 1
        elif st == "unparsed":
            unparsed += 1
        if r.get("passed") and st == "ok":
            passed += 1
        if (r.get("extra") or {}).get("injection"):
            injected += 1
    legacy_n = n - errors
    return {"n": n, "passed": passed, "grader_errors": errors, "unparsed": unparsed, "injection_fails": injected,
            "acc_strict": passed / n if n else None, "acc_legacy": passed / legacy_n if legacy_n else None}


# ------------------------------------------------------------------------------------------- perturbations
_FENCE_RE = re.compile(r"```[A-Za-z0-9_+-]*\n?(.*?)```", re.S)


def strip_markdown(text: str) -> str:
    """The same content without Markdown: fences, headings, emphasis, bullets, numbering, quotes, table pipes."""
    t = _FENCE_RE.sub(lambda m: m.group(1), text or "")
    out = []
    for line in t.splitlines():
        s = line.strip()
        if re.fullmatch(r"\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?", s) or re.fullmatch(r"[-*_]{3,}", s):
            continue  # table separator or horizontal rule
        s = re.sub(r"^#{1,6}\s*", "", s)
        s = re.sub(r"^>\s?", "", s)
        s = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", s)
        if s.startswith("|") and s.endswith("|"):
            s = ", ".join(c.strip() for c in s.strip("|").split("|") if c.strip())
        s = re.sub(r"(\*\*|__)(.+?)\1", r"\2", s)
        s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", s)
        s = s.replace("`", "")
        out.append(s)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _find_json(text: str) -> Any:
    t = (text or "").strip()
    cands = [m.group(1) for m in _FENCE_RE.finditer(t)] + [t]
    i, j = t.find("{"), t.rfind("}")
    if 0 <= i < j:
        cands.append(t[i: j + 1])
    for c in cands:
        try:
            v = json.loads(c.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(v, (dict, list)) and v:
            return v
    return None


def _humanize(k: str) -> str:
    k = re.sub(r"[_\-]+", " ", str(k))
    k = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", k).strip()
    return k[:1].upper() + k[1:].lower() if k else k


def _prose(v: Any, prefix: str = "") -> List[str]:
    if isinstance(v, dict):
        out: List[str] = []
        for k, x in v.items():
            out += _prose(x, f"{prefix} - {_humanize(k)}" if prefix else _humanize(k))
        return out
    if isinstance(v, list):
        if all(not isinstance(x, (dict, list)) for x in v):
            body = ", ".join(str(x) for x in v) if v else "none"
            return [f"{prefix}: {body}." if prefix else f"{body}."]
        out = []
        for n, x in enumerate(v, 1):
            out += _prose(x, f"{prefix} {n}" if prefix else f"Item {n}")
        return out
    val = "none" if v is None else str(v)
    return [f"{prefix}: {val}." if prefix else f"{val}."]


def json_to_prose(text: str) -> Optional[str]:
    """The JSON object in a candidate, rewritten as plain sentences ("Malware name: X."), keeping every value.
    None when the candidate holds no JSON object or array."""
    v = _find_json(text)
    if v is None:
        return None
    return "\n".join(_prose(v))


PERTURBATIONS: Dict[str, Callable[[str], Optional[str]]] = {
    "strip_markdown": lambda t: (lambda s: s if s != (t or "").strip() else None)(strip_markdown(t)),
    "json_to_prose": json_to_prose,
}


def perturbation_plan(rows: Sequence[Dict[str, Any]], transforms: Sequence[str], n_per_transform: int,
                      seed: int) -> List[Dict[str, Any]]:
    """rows: {"key", "model", "item", "candidate", "orig_passed", ...}. For each transform, a seeded random sample
    of rows where the transform changes the text (a choice blind to the grades)."""
    plan = []
    for name in transforms:
        fn = PERTURBATIONS[name]
        elig = []
        for r in rows:
            p = fn(S.strip_thinking(r["candidate"]))
            if p:
                elig.append((r, p))
        elig.sort(key=lambda x: x[0]["key"])
        rng = random.Random(f"{seed}:{name}")
        for r, p in rng.sample(elig, min(n_per_transform, len(elig))):
            plan.append({**r, "transform": name, "perturbed": p})
    return plan


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value for b and c discordant pairs."""
    from math import comb
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    p = sum(comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * p)


def perturbation_check(plan: Sequence[Dict[str, Any]], scorer: Callable[[str, Dict[str, Any]], S.Score],
                       regrade_original: bool = False) -> Dict[str, Any]:
    """Grade every perturbed text with ``scorer`` (an llm_judge scorer built from the same judge config) and compare
    with the original grade (stored, or re-graded when regrade_original). Returns rows + per-transform flip counts."""
    out_rows, summ = [], {}
    for p in plan:
        orig = p.get("orig_passed")
        if regrade_original or orig is None:
            so = scorer(p["candidate"], p["item"])
            orig = so.passed if so.status == "ok" else None
        sp = scorer(p["perturbed"], p["item"])
        new = sp.passed if sp.status == "ok" else None
        out_rows.append({"key": p["key"], "model": p.get("model"), "transform": p["transform"], "orig_passed": orig,
                         "perturbed_passed": new, "perturbed_status": sp.status, "detail": sp.detail[:300]})
        s = summ.setdefault(p["transform"], {"n": 0, "pass_to_fail": 0, "fail_to_pass": 0, "same": 0, "errors": 0})
        s["n"] += 1
        if orig is None or new is None:
            s["errors"] += 1
        elif orig and not new:
            s["pass_to_fail"] += 1
        elif new and not orig:
            s["fail_to_pass"] += 1
        else:
            s["same"] += 1
    for s in summ.values():
        valid = s["n"] - s["errors"]
        s["flip_rate"] = (s["pass_to_fail"] + s["fail_to_pass"]) / valid if valid else None
        s["mcnemar_p"] = mcnemar_exact(s["pass_to_fail"], s["fail_to_pass"])
    return {"rows": out_rows, "summary": summ}
