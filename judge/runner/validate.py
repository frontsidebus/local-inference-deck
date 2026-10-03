#!/usr/bin/env python3
"""Validate and normalize a judge finding (CONTRACT.md "Finding").

    validate.py <finding.json> [--bundle <evidence-dir>]   prints the normalized finding; exit 1 if invalid

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
4. Schema check of the result against schema/finding.schema.json (a small stdlib JSON-Schema subset:
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
    "ssh_alias_test|port_listening|http_status|unit_state|file_hash|render_and_diff|check_sanitized|slots|"
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
                     bundle_text: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], List[str], List[str]]:
    """Return (finding | None, fatal_errors, dropped_item_notes).

    *raw* is model output text or an already-parsed object. Envelope fields given as arguments
    override what the model wrote. Fatal errors mean "ask the judge again"; dropped items do not.
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
    parts = []
    for p in sorted(evidence_dir.rglob("*")):
        if p.is_file():
            try:
                parts.append(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    return "\n".join(parts)


def main(argv: List[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 64
    raw = Path(argv[0]).read_text(encoding="utf-8")
    bundle = _bundle_text(Path(argv[argv.index("--bundle") + 1])) if "--bundle" in argv else None
    finding, errs, dropped = validate_finding(raw, bundle_text=bundle)
    for d in dropped:
        print(f"dropped {d}", file=sys.stderr)
    if errs:
        for e in errs:
            print(f"invalid: {e}", file=sys.stderr)
        return 1
    print(json.dumps(finding, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
