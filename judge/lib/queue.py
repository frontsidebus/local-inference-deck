"""judge/lib/queue.py: atomic read/write of review requests, findings and acks + schema validation.

Stdlib only. Import with judge/ on sys.path:  ``from lib import queue``  (never ``import queue``: that
is the stdlib module).

Every function takes an optional ``root`` (the review dir); default is ``config.review_dir()``.
Files are written atomically (tmp file in the same dir + rename) with mode 600; dirs are created 700.

Public API:
    SUBDIRS                                   ("queue", "evidence", "findings", "acks", "done", "snapshots")
    ValidationError(ValueError)               .errors -> list[str]
    validate(obj, schema) -> list[str]        minimal JSON-schema subset: type, required, properties, items,
                                              enum, pattern, minLength, maxLength (anything else is ignored)
    load_schema("request"|"finding") -> dict
    validate_request(obj) / validate_finding(obj) -> list[str]
    utc_now_iso(now=None) -> "YYYY-MM-DDTHH:MM:SSZ"
    session_short(session) -> 6 chars
    new_request_id(kind, session, now=None, root=None) -> str   unique in queue/ and done/ (bumps seconds)
    make_request(kind, session, since, *, changed_paths=(), claims="", plan=None, data_class="sensitive",
                 source_event, detail=None, created=None, request_id=None, root=None) -> dict (not written)
    write_request(req, root=None) -> Path     validates; raises ValidationError
    read_request(request_id, root=None) -> dict   from queue/ or done/
    list_pending(root=None) -> list[dict]     queue/*.json, oldest first (invalid files skipped)
    move_done(request_id, root=None) -> Path
    write_finding(finding, root=None) -> (json_path, md_path)   drops items with empty evidence, validates
    render_finding_md(finding) -> str
    read_findings(root=None, request_id=None) -> list[dict]
    ack(request_id, item_id, reason="", root=None) -> Path   (alias: write_ack)
    is_acked(request_id, item_id, root=None) -> bool
    unacked_items(root=None) -> list[(finding, item)]
    is_duplicate(req, window_s=None, root=None) -> str | None   completion dedupe (pre_verify vs on_session_end)
    evidence_dir(request_id, root=None, create=False) -> Path
    snapshot_dir(session, root=None, create=False) -> Path
    atomic_write(path, data, mode=0o600) / atomic_write_json(path, obj) / ensure_dir(path) / ensure_dirs(root)
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from . import config

SCHEMA_DIR = config.JUDGE_DIR / "schema"
SUBDIRS = ("queue", "evidence", "findings", "acks", "done", "snapshots")
KINDS = ("plan", "gate", "completion", "runaway")

REQUEST_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9]{1,6}-(plan|gate|completion|runaway)$")
ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_SESSION_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")


class ValidationError(ValueError):
    def __init__(self, errors: List[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors) or "invalid")


# ---------------------------------------------------------------- fs helpers
def _root(root=None) -> Path:
    return Path(root) if root is not None else config.review_dir()


def ensure_dir(path: Union[str, Path]) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(p, 0o700)
    except OSError:
        pass
    return p


def ensure_dirs(root=None) -> Path:
    r = ensure_dir(_root(root))
    for s in SUBDIRS:
        ensure_dir(r / s)
    return r


def atomic_write(path: Union[str, Path], data: Union[str, bytes], mode: int = 0o600) -> Path:
    p = Path(path)
    ensure_dir(p.parent)
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data.encode("utf-8") if isinstance(data, str) else data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


def atomic_write_json(path: Union[str, Path], obj: Any) -> Path:
    return atomic_write(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def _read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- schema validation
_TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "null": lambda v: v is None,
}


def validate(obj: Any, schema: Dict[str, Any], path: str = "$") -> List[str]:
    """Return a list of error strings (empty = valid)."""
    errs: List[str] = []
    if not isinstance(schema, dict):
        return errs
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_TYPES.get(x, lambda v: True)(obj) for x in types):
            return [f"{path}: expected {'|'.join(types)}, got {type(obj).__name__}"]
    if "enum" in schema and obj not in schema["enum"]:
        errs.append(f"{path}: {obj!r} not in {schema['enum']}")
    if isinstance(obj, str):
        if "pattern" in schema and not re.search(schema["pattern"], obj):
            errs.append(f"{path}: does not match {schema['pattern']}")
        if len(obj) < schema.get("minLength", 0):
            errs.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(obj) > schema["maxLength"]:
            errs.append(f"{path}: longer than {schema['maxLength']}")
    if isinstance(obj, dict):
        for k in schema.get("required", []):
            if k not in obj:
                errs.append(f"{path}: missing required '{k}'")
        for k, sub in (schema.get("properties") or {}).items():
            if k in obj:
                errs += validate(obj[k], sub, f"{path}.{k}")
    if isinstance(obj, list) and isinstance(schema.get("items"), dict):
        for i, v in enumerate(obj):
            errs += validate(v, schema["items"], f"{path}[{i}]")
    return errs


_schema_cache: Dict[str, Dict[str, Any]] = {}


def load_schema(name: str) -> Dict[str, Any]:
    if name not in _schema_cache:
        _schema_cache[name] = _read_json(SCHEMA_DIR / f"{name}.schema.json")
    return _schema_cache[name]


def validate_request(obj: Any) -> List[str]:
    return validate(obj, load_schema("request"))


def validate_finding(obj: Any) -> List[str]:
    return validate(obj, load_schema("finding"))


# ---------------------------------------------------------------- ids / time
def utc_now_iso(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(text: str) -> datetime:
    """Parse an ISO timestamp (Z or offset; naive = UTC) into an aware UTC datetime."""
    dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def session_short(session: str) -> str:
    alnum = re.sub(r"[^A-Za-z0-9]", "", session or "")
    return alnum[-6:] if alnum else "nosess"


def safe_session(session: str) -> str:
    s = _SESSION_SAFE_RE.sub("_", session or "nosession").strip(".") or "nosession"
    return s[:120]


def _check_request_id(request_id: str) -> str:
    if not isinstance(request_id, str) or not REQUEST_ID_RE.match(request_id):
        raise ValueError(f"bad request id: {request_id!r}")
    return request_id


def new_request_id(kind: str, session: str, now: Optional[datetime] = None, root=None) -> str:
    """`<UTC yyyymmddThhmmssZ>-<session short 6>-<kind>`; if that id is already in queue/ or done/, the
    timestamp is bumped one second at a time until it is free."""
    if kind not in KINDS:
        raise ValueError(f"bad kind: {kind!r}")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    r = _root(root)
    short = session_short(session)
    for i in range(3600):
        rid = f"{(now + timedelta(seconds=i)).strftime('%Y%m%dT%H%M%SZ')}-{short}-{kind}"
        if not (r / "queue" / f"{rid}.json").exists() and not (r / "done" / f"{rid}.json").exists():
            return rid
    raise RuntimeError("could not allocate a request id")


def make_request(kind: str, session: str, since: str, *, source_event: str, changed_paths: Iterable[str] = (),
                 claims: str = "", plan: Optional[str] = None, data_class: str = "sensitive",
                 detail: Optional[Dict[str, Any]] = None, created: Optional[str] = None,
                 request_id: Optional[str] = None, root=None) -> Dict[str, Any]:
    """Build a request dict (claims truncated to 4000 chars). Does not write it."""
    now = datetime.now(timezone.utc)
    created = created or utc_now_iso(now)
    return {
        "id": request_id or new_request_id(kind, session, parse_utc(created), root),
        "kind": kind,
        "session": session or "",
        "created": created,
        "since": utc_now_iso(parse_utc(since)),
        "changed_paths": sorted(dict.fromkeys(p for p in changed_paths if p)),
        "claims": (claims or "")[:4000],
        "plan": plan,
        "data_class": data_class,
        "source_event": source_event,
        "detail": detail or {},
    }


# ---------------------------------------------------------------- requests
def write_request(req: Dict[str, Any], root=None) -> Path:
    errs = validate_request(req)
    if errs:
        raise ValidationError(errs)
    r = ensure_dirs(root)
    return atomic_write_json(r / "queue" / f"{_check_request_id(req['id'])}.json", req)


def read_request(request_id: str, root=None) -> Dict[str, Any]:
    r = _root(root)
    _check_request_id(request_id)
    for sub in ("queue", "done"):
        p = r / sub / f"{request_id}.json"
        if p.is_file():
            return _read_json(p)
    raise FileNotFoundError(f"request {request_id} not in queue/ or done/")


def list_pending(root=None) -> List[Dict[str, Any]]:
    q = _root(root) / "queue"
    out = []
    if not q.is_dir():
        return out
    for p in sorted(q.glob("*.json")):
        if not REQUEST_ID_RE.match(p.stem):
            continue
        try:
            req = _read_json(p)
        except (OSError, ValueError):
            continue
        if isinstance(req, dict) and not validate_request(req):
            out.append(req)
    return out


def move_done(request_id: str, root=None) -> Path:
    r = ensure_dirs(root)
    _check_request_id(request_id)
    src, dst = r / "queue" / f"{request_id}.json", r / "done" / f"{request_id}.json"
    os.replace(src, dst)
    return dst


def evidence_dir(request_id: str, root=None, create: bool = False) -> Path:
    d = _root(root) / "evidence" / _check_request_id(request_id)
    return ensure_dir(d) if create else d


def snapshot_dir(session: str, root=None, create: bool = False) -> Path:
    d = _root(root) / "snapshots" / safe_session(session)
    return ensure_dir(d) if create else d


def _iter_all_requests(root=None):
    r = _root(root)
    for sub in ("queue", "done"):
        d = r / sub
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.json")):
            if not REQUEST_ID_RE.match(p.stem):
                continue
            try:
                req = _read_json(p)
            except (OSError, ValueError):
                continue
            if isinstance(req, dict):
                yield req


def _turn_id(req: Dict[str, Any]) -> str:
    t = (req.get("detail") or {}).get("turn_id")
    return str(t).strip() if isinstance(t, (str, int)) and str(t).strip() else ""


def is_duplicate(req: Dict[str, Any], window_s: Optional[float] = None, root=None) -> Optional[str]:
    """Completion dedupe rule shared by hooks/verify.py (pre_verify) and hooks/enqueue.py (on_session_end,
    which fires after pre_verify in the same turn).

    R duplicates an existing request E (pending in queue/ or in done/) when E.kind == R.kind == "completion",
    E.session == R.session, |E.created - R.created| <= window_s (default JUDGE_COMPLETION_DEDUPE_SECONDS, 900),
    and set(R.changed_paths) <= set(E.changed_paths) (empty counts as a subset). Returns E's id, or None.

    When both requests carry a Hermes turn id (detail.turn_id), the turn decides alone: same turn ->
    duplicate, different turn -> not. The time-window rule only applies when one side has no turn id
    (pre_verify payloads don't carry one)."""
    if req.get("kind") != "completion":
        return None
    if window_s is None:
        try:
            window_s = float(config.get("JUDGE_COMPLETION_DEDUPE_SECONDS", "900"))
        except ValueError:
            window_s = 900.0
    try:
        created = parse_utc(req.get("created") or "")
    except ValueError:
        created = datetime.now(timezone.utc)
    mine = set(req.get("changed_paths") or [])
    my_turn = _turn_id(req)
    for e in _iter_all_requests(root):
        if e.get("kind") != "completion" or e.get("session") != req.get("session") or e.get("id") == req.get("id"):
            continue
        their_turn = _turn_id(e)
        if my_turn and their_turn:
            # Both carry Hermes' turn id: same turn is a duplicate, a different turn never is
            # (two clean read-only turns in a row must both be reviewed).
            if my_turn == their_turn:
                return str(e.get("id"))
            continue
        try:
            ec = parse_utc(e.get("created") or "")
        except ValueError:
            continue
        if abs((ec - created).total_seconds()) <= window_s and mine <= set(e.get("changed_paths") or []):
            return str(e.get("id"))
    return None


# ---------------------------------------------------------------- findings
def render_finding_md(finding: Dict[str, Any]) -> str:
    lines = [f"# Judge findings: {finding.get('request', '?')}", "",
             f"- judge: {finding.get('judge', '?')} ({finding.get('mode', '?')})",
             f"- created: {finding.get('created', '?')}",
             f"- items: {len(finding.get('items') or [])}", ""]
    for n in finding.get("notes") or []:
        lines.append(f"> note: {n}")
    if finding.get("notes"):
        lines.append("")
    items = finding.get("items") or []
    if not items:
        lines.append("No findings.")
    for it in items:
        lines += [
            f"## {it.get('id')} [{it.get('severity', '?')}] {it.get('rubric', '?')}: verdict {it.get('verdict', '?')}",
            "",
            f"**Claim:** {it.get('claim', '')}",
            "",
            "**Evidence:**",
            "",
            "```",
            str(it.get("evidence", "")).replace("```", "'''"),
            "```",
            "",
            f"**Recommendation:** {it.get('recommendation', '')}",
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def write_finding(finding: Dict[str, Any], root=None) -> Tuple[Path, Path]:
    """Drop items whose evidence is empty, validate, write findings/<request>.json and .md."""
    f = dict(finding)
    f["items"] = [it for it in (f.get("items") or [])
                  if isinstance(it, dict) and str(it.get("evidence") or "").strip()]
    errs = validate_finding(f)
    if errs:
        raise ValidationError(errs)
    r = ensure_dirs(root)
    rid = _check_request_id(f["request"])
    jp = atomic_write_json(r / "findings" / f"{rid}.json", f)
    mp = atomic_write(r / "findings" / f"{rid}.md", render_finding_md(f))
    return jp, mp


def read_findings(root=None, request_id: Optional[str] = None) -> List[Dict[str, Any]]:
    d = _root(root) / "findings"
    if request_id is not None:
        paths = [d / f"{_check_request_id(request_id)}.json"]
    else:
        paths = sorted(d.glob("*.json")) if d.is_dir() else []
    out = []
    for p in paths:
        try:
            f = _read_json(p)
        except (OSError, ValueError):
            continue
        if isinstance(f, dict) and not validate_finding(f):
            out.append(f)
    return out


# ---------------------------------------------------------------- acks
def _ack_path(request_id: str, item_id: str, root=None) -> Path:
    _check_request_id(request_id)
    if not isinstance(item_id, str) or not ITEM_ID_RE.match(item_id):
        raise ValueError(f"bad item id: {item_id!r}")
    return _root(root) / "acks" / f"{request_id}.{item_id}"


def ack(request_id: str, item_id: str, reason: str = "", root=None) -> Path:
    p = _ack_path(request_id, item_id, root)
    ensure_dirs(root)
    line = (reason or "").strip().splitlines()[0][:500] if (reason or "").strip() else ""
    return atomic_write(p, line + "\n" if line else "")


write_ack = ack


def is_acked(request_id: str, item_id: str, root=None) -> bool:
    try:
        return _ack_path(request_id, item_id, root).exists()
    except ValueError:
        return False


def unacked_items(root=None) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    out = []
    for f in read_findings(root):
        for it in f.get("items") or []:
            if not is_acked(f["request"], str(it.get("id")), root):
                out.append((f, it))
    return out
