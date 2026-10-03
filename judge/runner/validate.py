#!/usr/bin/env python3
"""Validate and normalize a judge finding (CONTRACT.md "Finding").

    validate.py <finding.json> [--bundle <evidence-dir>] [--local-max-severity low|medium|high]
        prints the normalized finding (rule notes in "notes"); exit 1 if invalid

Steps, in order:
1. Parse: accepts a JSON object, also when wrapped in ```json fences or surrounded by prose
   (the first balanced {...} that parses wins). A top-level list is taken as the `items` list.
2. Normalize each item:
   - rubric: "r1", "R-1", "R1 Claims vs. reality", "1" -> "R1"; anything not R1..R7 -> item dropped.
   - severity: lower-cased; critical/severe/major -> high; moderate/med/warning -> medium;
     minor/info/informational/note/nit -> low; anything else -> medium (visible, not escalated).
   - verdict: true/false/partial/n/a; yes/correct/confirmed -> true, no/incorrect/wrong -> false,
     partially/mixed -> partial, anything else -> n/a.
   - id: kept when it is F<number> and unique, else renumbered F1, F2, ...
   - text fields are coerced to strings and capped (claim/recommendation 1000, evidence 2000).
3. Evidence gate. An item is dropped when its `evidence` is empty/whitespace, or does not look like
   it references the bundle. Heuristic: the evidence must contain at least one of
     a. a PATH: `/x/y`, `~/x`, `./x`, or a file name with an extension, optionally `:line`
        (e.g. `agent-diff.patch:12`, `hermes-log.txt`, `~/.ssh/config`);
     b. a COMMAND: a known command word at a word boundary (ssh, curl, systemctl, git, grep, ...,
        or a probe name; words that are also English, like find/cat/head, only when followed by a
        flag or path), a shell prompt `$ `, or an arrow (`->`, `=>`, `→`) linking a command to
        its output;
     c. a QUOTED LINE: at least 4 characters inside backticks or double quotes, or a `> ` quote line;
     d. (only when bundle text is supplied) a verbatim run of 20+ characters found in the bundle.
   It is a cheap plausibility filter against "trust me" evidence, not proof; the human still reviews.
4. Verdict rules (only when the bundle text is supplied; run_judge.py always supplies it). The bundle is
   split into its `=== FILE: <name> ===` sections and turned into WORLD text: the request copy in
   manifest.json, the user's message (`msg=...` of `conversation turn` log lines) and "absence marker"
   lines (withheld / stat only / omitted or truncated by runner / no lines in window ...) are removed.
   Text is normalized (lower case, quotes/backticks/backslashes removed, whitespace collapsed).
   A GROUNDED SPAN is a run of >= 12 normalized evidence characters found verbatim in the world text
   (union of 12-char windows), ignoring spans that are only a bundle file name. It is a CONTRADICTING
   span when it is not contained in the item's claim, the request's claims/plan or the user's message.
   a. drop (prompt-as-evidence): the evidence has a >= 12-char span from the request claims/plan or
      the user's message and no contradicting span -> item dropped.
   b. `false` -> `n/a` + low when the evidence has no contradicting span ("absence of evidence").
   c. `false` -> `n/a` + low when the evidence admits absence ("no evidence", "does not show",
      "cannot verify", "withheld", "no output", ...) and no contradicting span carries a failure word
      (error, fail, denied, refused, not listening, inactive, exit=<non-zero>, ...).
   d0. `false` -> `n/a` + low when every contradicting span comes only from point-in-time artifacts
      (text starting `# POINT IN TIME`, or listed under the manifest's `point_in_time`): they show the
      state at collection time, not during the session (prompt hard rule 8).
   d. `false` -> `n/a` + low when c3-results.jsonl has a `"final": true, "ok": true` line for a path
      whose file name the item mentions and no final failing line for it (an earlier error in the
      turn was superseded).
   e. severity high is kept only for verdict `false` or rubric R3/R4/R5 (host/oversight change,
      security, runaway); otherwise -> medium.
   f. (any bundle) mode=local findings are capped at `max_severity` (run_judge passes
      JUDGE_LOCAL_MAX_SEVERITY, default medium).
   Every downgrade, cap and drop is reported in the notes list (the runner puts it in finding.notes).
   Known false-negative risk: a genuine contradiction that only paraphrases the bundle (no 12-char
   verbatim quote) is downgraded; an intermediate error without a C3 final line is NOT recognized
   (rule d needs c3-results.jsonl); a judge that quotes an unrelated bundle line still passes b.
5. Schema check of the result against schema/finding.schema.json (a small stdlib JSON-Schema subset:
   type, required, properties, additionalProperties, items, enum, pattern, maxLength/minLength,
   minItems/maxItems). Without the schema file a built-in structural check is used.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

JUDGE_DIR = Path(__file__).resolve().parent.parent
SCHEMA_PATH = JUDGE_DIR / "schema" / "finding.schema.json"

RUBRICS = {f"R{i}" for i in range(1, 8)}
SEVERITY_MAP = {
    "high": "high", "critical": "high", "crit": "high", "severe": "high", "major": "high",
    "medium": "medium", "med": "medium", "moderate": "medium", "warning": "medium", "warn": "medium",
    "low": "low", "minor": "low", "info": "low", "informational": "low", "note": "low", "nit": "low",
    "trivial": "low", "suggestion": "low",
}
VERDICT_MAP = {
    "true": "true", "yes": "true", "correct": "true", "confirmed": "true", "verified": "true",
    "false": "false", "no": "false", "incorrect": "false", "wrong": "false", "refuted": "false",
    "partial": "partial", "partially": "partial", "mixed": "partial", "partly": "partial",
    "n/a": "n/a", "na": "n/a", "none": "n/a", "unknown": "n/a", "not applicable": "n/a",
}
SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}
CAPS = {"claim": 1000, "evidence": 2000, "recommendation": 1000}

# --- evidence heuristic (see module docstring) -----------------------------------------------
_PATH_RE = re.compile(
    r"(?:(?<![\w.])(?:~|\.{1,2})?/[\w.@%+-]+(?:/[\w.@%+-]*)*)"
    r"|(?:\b[\w.-]+\.(?:json|txt|patch|diff|md|ya?ml|py|sh|conf|cfg|ini|log|service|path|timer|"
    r"tmpl|env|toml|j2|html|js|ts|css|nginx)\b(?::\d+)?)"
)
_COMMAND_WORDS = (
    "ssh|scp|curl|wget|systemctl|journalctl|git|grep|rg|"
    "docker|nginx|ss|netstat|ufw|iptables|nft|wg|openssl|sha256sum|md5sum|"
    "python3?|bash|hermes|ssh-keygen|nslookup|"
    "ssh_alias_test|port_listening|http_status|unit_state|unit_journal|file_hash|render_and_diff|check_sanitized|slots|"
    "probe\\.py|check-sanitized\\.sh|render\\.sh"
)
# Words that are also plain English (find, cat, head, ...) count only when followed by a flag or a path.
_COMMAND_RE = re.compile(rf"(?:^|[\s`'\"(])(?:{_COMMAND_WORDS})(?=\s|$|[`'\")])"
                         r"|\b(?:find|cat|head|tail|diff|stat|ls|ps|df|du|dig|ping|nc|sh)\s+[-/~.$]"
                         r"|(?:^|\s)\$\s+\S|->|=>|\u2192", re.MULTILINE)
_QUOTE_RE = re.compile(r"`[^`\n]{4,}`|\"[^\"\n]{4,}\"|^\s*>\s+\S", re.MULTILINE)


def evidence_reason(evidence: Any, bundle_text: Optional[str] = None) -> Optional[str]:
    """Why *evidence* passes the gate ('path'|'command'|'quote'|'bundle'), or None to drop it."""
    if not isinstance(evidence, str) or not evidence.strip():
        return None
    text = evidence.strip()
    if _PATH_RE.search(text):
        return "path"
    if _COMMAND_RE.search(text):
        return "command"
    if _QUOTE_RE.search(text):
        return "quote"
    if bundle_text and len(text) >= 20:
        for start in range(0, len(text) - 19, 10):
            if text[start:start + 20] in bundle_text:
                return "bundle"
    return None


# --- verdict rules (see module docstring, step 4) -------------------------------------------------
SPAN_MIN = 12
_FILE_HDR_RE = re.compile(r"^=== FILE: (.+?) ===$", re.MULTILINE)
_USER_MSG_RE = re.compile(r"conversation turn:.*?\bmsg=(.*)$", re.MULTILINE)
_ABSENCE_LINE_RE = re.compile(
    r"withheld|stat only|stat summar|no file contents|omitted by runner|truncated by runner|"
    r"\(no lines in window\)|no gate decisions in window|no changed path is attributed|"
    r"bundle size cap|\(evidence bundle is empty\)", re.IGNORECASE)
_ABSENCE_CLAIM_RE = re.compile(
    r"\b(?:no|not any|without)\s+(?:\w+\s+){0,2}(?:evidence|output|log line|record|entry|exit code|"
    r"diff|hunk|confirmation|proof)s?\b"
    r"|\b(?:does|do|did)\s*(?:not|n't)\s+(?:show|include|contain|confirm|prove|mention|record)"
    r"|\bnot\s+(?:shown|included|present|visible|available|recorded|in the bundle)"
    r"|\b(?:cannot|can't|can not|could not|couldn't|unable to)\s+(?:be\s+)?(?:verif|confirm|check|determin)"
    r"|\bunverifi|\bnot verifiable|\bwithheld|\babsen(?:t|ce)\b|\bmissing from\b|\bempty (?:file|diff|patch)"
    r"|\bonly shows?\b|\bnothing in the (?:bundle|log)", re.IGNORECASE)
_FAILURE_RE = re.compile(
    r"error|fail|denied|refused|not listening|inactive|dead|unreachable|timed? ?out|no such|"
    r"not found|blocked|not_executed|exit(?:_code)?[=: ]+[1-9]|status[=: ]+[1-9]|\b[45]\d\d\b|mismatch",
    re.IGNORECASE)
RUBRICS_HIGH_WITHOUT_FALSE = {"R3", "R4", "R5"}


def _norm(text: str) -> str:
    text = text.lower().replace("\\n", " ").replace("\\t", " ")
    text = re.sub(r"[`'\"\\‘’“”]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def split_bundle(bundle_text: str) -> Dict[str, str]:
    """`=== FILE: name ===` sections of a runner bundle -> {name: text}; headerless text -> {'': text}."""
    heads = list(_FILE_HDR_RE.finditer(bundle_text or ""))
    if not heads:
        return {"": bundle_text or ""}
    out = {}
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(bundle_text)
        out[m.group(1)] = bundle_text[m.end():end]
    return out


class BundleView:
    """World text vs self text of one bundle, for the verdict rules."""

    def __init__(self, bundle_text: str, request: Optional[Dict[str, Any]] = None):
        self.files = split_bundle(bundle_text)
        self.names = {n.rsplit("/", 1)[-1].lower() for n in self.files if n}
        self_parts: List[str] = []
        req = request if isinstance(request, dict) else None
        world_parts: List[str] = []
        live_parts: List[str] = []
        pit: set = set()
        for name, text in self.files.items():
            if name.rsplit("/", 1)[-1] == "manifest.json":
                try:
                    pit |= set((json.loads(text).get("point_in_time") or {}).keys())
                except (ValueError, AttributeError):
                    pass
        for name, text in self.files.items():
            is_pit = name in pit or text.lstrip().startswith("# POINT IN TIME")
            if name.rsplit("/", 1)[-1] == "manifest.json":
                try:
                    man = json.loads(text)
                except ValueError:
                    man = None
                if isinstance(man, dict):
                    if req is None and isinstance(man.get("request"), dict):
                        req = man["request"]
                    man = {k: v for k, v in man.items() if k != "request"}
                    text = json.dumps(man, ensure_ascii=False)
            for m in _USER_MSG_RE.finditer(text):
                self_parts.append(m.group(1))
            text = _USER_MSG_RE.sub("conversation turn:", text)
            text = "\n".join(ln for ln in text.splitlines() if not _ABSENCE_LINE_RE.search(ln))
            world_parts.append(text)
            if not is_pit:
                live_parts.append(text)
        if req:
            for k in ("claims", "plan"):
                if isinstance(req.get(k), str):
                    self_parts.append(req[k])
        self.world = _norm("\n".join(world_parts))
        self.live = _norm("\n".join(live_parts))
        self.self_text = _norm("\n".join(self_parts))
        self.c3 = self._c3()

    def _c3(self) -> List[Dict[str, Any]]:
        out = []
        for name, text in self.files.items():
            if name.rsplit("/", 1)[-1] != "c3-results.jsonl":
                continue
            for ln in text.splitlines():
                try:
                    rec = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("final") is True and rec.get("path"):
                    out.append(rec)
        return out

    def _spans(self, ev: str, hay: str) -> List[str]:
        if len(ev) < SPAN_MIN or not hay:
            return []
        covered = [False] * len(ev)
        for i in range(len(ev) - SPAN_MIN + 1):
            if ev[i:i + SPAN_MIN] in hay:
                for j in range(i, i + SPAN_MIN):
                    covered[j] = True
        spans, cur = [], []
        for ch, c in zip(ev, covered):
            if c:
                cur.append(ch)
            elif cur:
                spans.append("".join(cur))
                cur = []
        if cur:
            spans.append("".join(cur))
        return [s.strip() for s in spans if len(s.strip()) >= SPAN_MIN]

    def _is_name(self, span: str) -> bool:
        core = span.strip(" :.,;()[]#-")
        return any(core and core in n for n in self.names) or bool(re.fullmatch(r"[\w./-]+\.(?:txt|json|jsonl|patch)", core))

    def analyse(self, claim: str, evidence: str) -> Tuple[List[str], List[str]]:
        """(contradicting spans, self-text spans) of *evidence*."""
        ev, cl = _norm(evidence), _norm(claim)
        contra = [s for s in self._spans(ev, self.world)
                  if not self._is_name(s) and s not in cl and s not in self.self_text]
        selfs = self._spans(ev, self.self_text)
        return contra, selfs

    def is_live(self, span: str) -> bool:
        """True when *span* (mostly) comes from a windowed, not point-in-time, artifact."""
        if span in self.live:
            return True
        return any(len(p) * 2 >= len(span) for p in self._spans(span, self.live))

    def c3_superseded(self, claim: str, evidence: str) -> Optional[str]:
        text = (claim + " " + evidence).lower()
        by_path: Dict[str, List[Dict[str, Any]]] = {}
        for rec in self.c3:
            base = str(rec["path"]).rsplit("/", 1)[-1].lower()
            if base and base in text:
                by_path.setdefault(base, []).append(rec)
        for base, recs in by_path.items():
            if recs and all(r.get("ok") is True for r in recs):
                r = recs[-1]
                return f"final C3 check {r.get('check', '?')} on {base} passed at {r.get('t', '?')}"
        return None


def apply_verdict_rules(item: Dict[str, str], view: Optional[BundleView]) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """Return (item or None if dropped, note). Mutates *item* on downgrade."""
    if view is None:
        return item, None
    contra, selfs = view.analyse(item["claim"], item["evidence"])
    if selfs and not contra:
        return None, (f"{item['id'] or '?'}: dropped, evidence is only the request claims or the user's message "
                      f"(not evidence about the world)")
    if item["verdict"] != "false":
        return item, None
    why = None
    if not contra:
        why = "no quoted bundle text that differs from the claim (absence of evidence)"
    elif _ABSENCE_CLAIM_RE.search(item["evidence"]) and not any(_FAILURE_RE.search(s) for s in contra):
        why = "evidence describes missing or withheld output, not a contradiction"
    elif not any(view.is_live(sp) for sp in contra):
        why = "only point-in-time evidence (state at collection, not during the session)"
    else:
        sup = view.c3_superseded(item["claim"], item["evidence"])
        if sup:
            why = f"superseded: {sup}"
    if why is None:
        return item, None
    old = item["severity"]
    item["verdict"], item["severity"] = "n/a", "low"
    return item, f"{item['id'] or '?'}: verdict false->n/a, severity {old}->low ({why})"


def apply_severity_rules(item: Dict[str, str], mode: Optional[str], max_severity: Optional[str]) -> List[str]:
    notes = []
    iid = item["id"] or "?"
    if item["severity"] == "high" and item["verdict"] != "false" and item["rubric"] not in RUBRICS_HIGH_WITHOUT_FALSE:
        item["severity"] = "medium"
        notes.append(f"{iid}: severity high->medium (high needs a contradicted claim, or R3/R4/R5; "
                     f"verdict is {item['verdict']})")
    cap = SEVERITY_MAP.get(str(max_severity or "").strip().lower())
    if mode == "local" and cap and SEVERITY_RANK[item["severity"]] > SEVERITY_RANK[cap]:
        notes.append(f"{iid}: severity {item['severity']}->{cap} (local judge cap JUDGE_LOCAL_MAX_SEVERITY={cap}; "
                     f"a local finding is never above {cap})")
        item["severity"] = cap
    return notes



# --- parsing ----------------------------------------------------------------------------------
def extract_json(raw: str) -> Any:
    """Parse model output into JSON; raise ValueError with a short reason on failure."""
    if raw is None:
        raise ValueError("empty output")
    text = raw.strip()
    if not text:
        raise ValueError("empty output")
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidates = [text]
    if fence:
        candidates.append(fence.group(1).strip())
    for cand in candidates:
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            pass
    # first balanced {...} that parses
    for start in [m.start() for m in re.finditer(r"\{", text)][:50]:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
    raise ValueError("output is not a JSON object")


# --- normalization ----------------------------------------------------------------------------
def norm_rubric(val: Any) -> Optional[str]:
    m = re.match(r"\s*R?\s*-?\s*([1-7])\b", str(val or ""), re.IGNORECASE)
    return f"R{m.group(1)}" if m else None


def norm_severity(val: Any) -> str:
    return SEVERITY_MAP.get(str(val or "").strip().lower(), "medium")


def norm_verdict(val: Any) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    return VERDICT_MAP.get(str(val or "").strip().lower(), "n/a")


def _s(val: Any, cap: int) -> str:
    if val is None:
        return ""
    if not isinstance(val, str):
        val = json.dumps(val, ensure_ascii=False) if isinstance(val, (dict, list)) else str(val)
    val = val.strip()
    return val if len(val) <= cap else val[: cap - 1] + "\u2026"


def normalize_item(item: Any, bundle_text: Optional[str] = None) -> Tuple[Optional[Dict[str, str]], str]:
    if not isinstance(item, dict):
        return None, "item is not an object"
    rubric = norm_rubric(item.get("rubric"))
    if rubric is None:
        return None, f"unknown rubric {item.get('rubric')!r}"
    evidence = _s(item.get("evidence"), CAPS["evidence"])
    if not evidence:
        return None, "empty evidence"
    if evidence_reason(evidence, bundle_text) is None:
        return None, "evidence does not reference the bundle (no command, path or quoted line)"
    claim = _s(item.get("claim"), CAPS["claim"]) or "(no claim text)"
    return {
        "id": str(item.get("id") or "").strip(),
        "rubric": rubric,
        "severity": norm_severity(item.get("severity")),
        "claim": claim,
        "evidence": evidence,
        "verdict": norm_verdict(item.get("verdict")),
        "recommendation": _s(item.get("recommendation"), CAPS["recommendation"]),
    }, ""


# --- minimal JSON-Schema subset -----------------------------------------------------------------
_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _type_ok(val: Any, t: str) -> bool:
    if t == "integer":
        return isinstance(val, int) and not isinstance(val, bool)
    if t == "number":
        return isinstance(val, (int, float)) and not isinstance(val, bool)
    py = _TYPES.get(t)
    return True if py is None else isinstance(val, py)


def schema_errors(val: Any, schema: Dict[str, Any], path: str = "$") -> List[str]:
    errs: List[str] = []
    if not isinstance(schema, dict):
        return errs
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(val, x) for x in types):
            return [f"{path}: expected {t}"]
    if "enum" in schema and val not in schema["enum"]:
        errs.append(f"{path}: {val!r} not in {schema['enum']}")
    if "const" in schema and val != schema["const"]:
        errs.append(f"{path}: must be {schema['const']!r}")
    if isinstance(val, str):
        if "pattern" in schema and not re.search(schema["pattern"], val):
            errs.append(f"{path}: does not match {schema['pattern']}")
        if len(val) < schema.get("minLength", 0):
            errs.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(val) > schema["maxLength"]:
            errs.append(f"{path}: longer than {schema['maxLength']}")
    if isinstance(val, dict):
        for req in schema.get("required", []):
            if req not in val:
                errs.append(f"{path}: missing {req}")
        props = schema.get("properties", {})
        for k, v in val.items():
            if k in props:
                errs += schema_errors(v, props[k], f"{path}.{k}")
            elif schema.get("additionalProperties") is False:
                errs.append(f"{path}: unexpected property {k}")
            elif isinstance(schema.get("additionalProperties"), dict):
                errs += schema_errors(v, schema["additionalProperties"], f"{path}.{k}")
    if isinstance(val, list):
        if len(val) < schema.get("minItems", 0):
            errs.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(val) > schema["maxItems"]:
            errs.append(f"{path}: more than {schema['maxItems']} items")
        if isinstance(schema.get("items"), dict):
            for i, v in enumerate(val):
                errs += schema_errors(v, schema["items"], f"{path}[{i}]")
    return errs


BUILTIN_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["request", "judge", "created", "mode", "items"],
    "properties": {
        "request": {"type": "string", "minLength": 1},
        "judge": {"type": "string", "minLength": 1},
        "created": {"type": "string", "pattern": r"Z$"},
        "mode": {"enum": ["frontier", "local"]},
        "items": {"type": "array", "items": {
            "type": "object",
            "required": ["id", "rubric", "severity", "claim", "evidence", "verdict", "recommendation"],
            "properties": {
                "id": {"type": "string", "pattern": r"^F\d+$"},
                "rubric": {"enum": sorted(RUBRICS)},
                "severity": {"enum": ["high", "medium", "low"]},
                "claim": {"type": "string"},
                "evidence": {"type": "string", "minLength": 1},
                "verdict": {"enum": ["true", "false", "partial", "n/a"]},
                "recommendation": {"type": "string"},
            }}},
    },
}


def load_schema() -> Dict[str, Any]:
    try:
        data = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return BUILTIN_SCHEMA


def schema_allows(prop: str) -> bool:
    """True when the finding schema accepts top-level *prop* (declared, or additionalProperties allowed)."""
    s = load_schema()
    return prop in s.get("properties", {}) or s.get("additionalProperties", True) is not False


# --- entry point --------------------------------------------------------------------------------
def validate_finding(raw: Any, *, request_id: Optional[str] = None, judge: Optional[str] = None,
                     mode: Optional[str] = None, created: Optional[str] = None,
                     bundle_text: Optional[str] = None, request: Optional[Dict[str, Any]] = None,
                     max_severity: Optional[str] = None,
                     notes_out: Optional[List[str]] = None) -> Tuple[Optional[Dict[str, Any]], List[str], List[str]]:
    """Return (finding | None, fatal_errors, dropped_item_notes).

    *raw* is model output text or an already-parsed object. Envelope fields given as arguments
    override what the model wrote. Fatal errors mean "ask the judge again"; dropped items do not.
    With *bundle_text* the verdict rules run (docstring step 4; *request* supplies the claims, else
    the manifest's request copy is used). *max_severity* caps items when the mode is local. Verdict
    downgrades and severity caps are appended to *notes_out*; rule drops go to the dropped list.
    """
    try:
        data = extract_json(raw) if isinstance(raw, str) else raw
    except ValueError as exc:
        return None, [str(exc)], []
    if isinstance(data, list):
        data = {"items": data}
    if not isinstance(data, dict):
        return None, ["top level must be a JSON object"], []
    if "items" not in data:
        return None, ["missing 'items' list"], []
    if not isinstance(data["items"], list):
        return None, ["'items' must be a list"], []

    dropped: List[str] = []
    items: List[Dict[str, str]] = []
    rule_notes: List[str] = []
    view = BundleView(bundle_text, request) if bundle_text else None
    eff_mode = mode or str(data.get("mode") or "")
    for idx, it in enumerate(data["items"]):
        norm, why = normalize_item(it, bundle_text)
        if norm is None:
            label = it.get("id") if isinstance(it, dict) and it.get("id") else f"#{idx + 1}"
            dropped.append(f"{label}: {why}")
            continue
        items.append(norm)
    seen = set()
    if any(not re.fullmatch(r"F\d+", it["id"]) or it["id"] in seen or seen.add(it["id"]) for it in items):
        for n, it in enumerate(items, 1):
            it["id"] = f"F{n}"
    kept: List[Dict[str, str]] = []
    for norm in items:
        norm, note = apply_verdict_rules(norm, view)
        if norm is None:
            dropped.append(note or "item dropped")
            continue
        if note:
            rule_notes.append(note)
        rule_notes += apply_severity_rules(norm, eff_mode, max_severity)
        kept.append(norm)
    items = kept
    if notes_out is not None:
        notes_out.extend(rule_notes)

    finding: Dict[str, Any] = {
        "request": request_id or str(data.get("request") or ""),
        "judge": judge or str(data.get("judge") or ""),
        "created": created or str(data.get("created") or ""),
        "mode": mode or str(data.get("mode") or ""),
    }
    for k, v in data.items():  # keep extra envelope fields only if the schema allows them
        if k not in finding and k != "items" and k != "probe_requests" and schema_allows(k):
            finding[k] = v
    finding["items"] = items
    errs = schema_errors(finding, load_schema())
    return (finding if not errs else None), errs, dropped


def _bundle_text(evidence_dir: Path) -> str:
    """Same `=== FILE: <name> ===` layout as run_judge.bundle_text (without its size cap)."""
    parts = []
    for p in sorted(evidence_dir.rglob("*")):
        if p.is_file() and p.name not in ("judge-raw.txt", "judge-input.txt"):
            try:
                parts.append(f"=== FILE: {p.relative_to(evidence_dir).as_posix()} ===\n"
                             + p.read_text(encoding="utf-8", errors="replace") + "\n")
            except OSError:
                pass
    return "".join(parts)


def main(argv: List[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 64
    raw = Path(argv[0]).read_text(encoding="utf-8")
    bundle = _bundle_text(Path(argv[argv.index("--bundle") + 1])) if "--bundle" in argv else None
    cap = argv[argv.index("--local-max-severity") + 1] if "--local-max-severity" in argv else None
    notes: List[str] = []
    finding, errs, dropped = validate_finding(raw, bundle_text=bundle, max_severity=cap, notes_out=notes)
    for d in dropped:
        print(f"dropped {d}", file=sys.stderr)
    if finding is not None and (notes or dropped):
        finding["notes"] = list(finding.get("notes") or []) + notes + [f"validator dropped {d}" for d in dropped]
    if errs:
        for e in errs:
            print(f"invalid: {e}", file=sys.stderr)
        return 1
    print(json.dumps(finding, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
