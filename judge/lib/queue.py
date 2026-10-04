"""judge/lib/queue.py: atomic read/write of review requests, findings and acks + schema validation.

Stdlib only. Import with judge/ on sys.path:  ``from lib import queue``  (never ``import queue``: that
is the stdlib module).

Every function takes an optional ``root`` (the review dir); default is ``config.review_dir()``.
Files are written atomically (tmp file in the same dir + rename) with mode 600; dirs are created 700.

Public API:
    SUBDIRS                                   ("queue", "evidence", "findings", "acks", "done", "snapshots")
    DEFERRED                                  "deferred": queue/deferred/ holds requests whose not_before lies
                                              ahead (#40). judge-review.path watches queue/ only (PathChanged=
                                              does not see writes in a subdirectory), so deferred plan writes
                                              never start the runner; release moves them into queue/.
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
    read_request(request_id, root=None) -> dict   from queue/, queue/deferred/ or done/
    list_pending(root=None) -> list[dict]     queue/*.json + queue/deferred/*.json (every request not judged
                                              yet), oldest first (invalid files skipped)
    list_ready(root=None, now=None) -> list[dict]   queue/*.json that are ready: what run_judge.py --pending judges
    move_done(request_id, root=None) -> Path
    write_finding(finding, root=None) -> (json_path, md_path)   drops items with empty evidence, validates
    render_finding_md(finding) -> str
    read_findings(root=None, request_id=None) -> list[dict]
    ack(request_id, item_id, reason="", root=None, *, actor="human", via=None) -> Path   (alias: write_ack)
                                              JSON {"actor", "reason", "ts"[, "via"]}
    is_acked(request_id, item_id, root=None) -> bool        any ack (stops C5 injection)
    read_ack(request_id, item_id, root=None) -> dict | None  legacy plain text reads as actor "human"
    agent_wrote_ack(...) / ack_actor(...) -> effective actor ("agent" if an agent tool call wrote the file)
    closure(actor, severity) / item_status(request_id, item) -> "open" | "agent-acked" | "closed"
    is_closed(request_id, item) / needs_human(status, item) / items_by_status(root=None)
    unacked_items(root=None) -> list[(finding, item)]
    is_duplicate(req, window_s=None, root=None) -> str | None   completion dedupe (pre_verify vs on_session_end)
    merge_into_pending_completion(req, since=None, root=None) -> str | None
                                              fold on_session_end into the turn's pending pre_verify request
    is_ready(req_or_path, now=None) -> bool   False while the request's optional `not_before` lies ahead
                                              (run_judge.py --pending skips it; #34 plan debounce)
    merge_into_pending_plan(req, root=None) -> str | None
                                              coalesce a plan write into the same turn's pending plan request
    release_plan_requests(session, root=None, now=None, refresh=None) -> list[str]
                                              turn ended: the session's deferred plan requests become ready
    release_due(root=None, now=None) -> list[str]
                                              move deferred requests whose not_before passed into queue/ (wakes
                                              the judge-review.path unit, which only fires on queue/ changes)
    stall_status(root=None, now=None, minutes=None) -> dict | None
                                              #41: the oldest ready request in queue/ has waited longer than
                                              JUDGE_STALL_MINUTES (default 15) and no runner holds the lock
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
DEFERRED = "deferred"  # queue/deferred/: not ready yet (not_before ahead); not watched by judge-review.path
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
    ensure_dir(r / "queue" / DEFERRED)
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
        if not any((r / sub / f"{rid}.json").exists() for sub in ("queue", f"queue/{DEFERRED}", "done")):
            return rid
    raise RuntimeError("could not allocate a request id")


def make_request(kind: str, session: str, since: str, *, source_event: str, changed_paths: Iterable[str] = (),
                 claims: str = "", plan: Optional[str] = None, data_class: str = "sensitive",
                 detail: Optional[Dict[str, Any]] = None, created: Optional[str] = None,
                 request_id: Optional[str] = None, not_before: Optional[str] = None,
                 root=None) -> Dict[str, Any]:
    """Build a request dict (claims truncated to 4000 chars). Does not write it. *not_before* (UTC ISO) is
    only set when given: the runner leaves the request queued until then (see is_ready)."""
    now = datetime.now(timezone.utc)
    created = created or utc_now_iso(now)
    req = {
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
    if not_before:
        req["not_before"] = utc_now_iso(parse_utc(not_before))
    return req


# ---------------------------------------------------------------- requests
def deferred_dir(root=None) -> Path:
    return _root(root) / "queue" / DEFERRED


def _home_dir(req: Dict[str, Any], root=None, now: Optional[datetime] = None) -> Path:
    """Where a pending request belongs: queue/deferred/ while its not_before lies ahead, else queue/."""
    return deferred_dir(root) if not is_ready(req, now) else _root(root) / "queue"


def pending_path(request_id: str, root=None) -> Optional[Path]:
    """queue/<id>.json, else queue/deferred/<id>.json, else None."""
    _check_request_id(request_id)
    for d in (_root(root) / "queue", deferred_dir(root)):
        p = d / f"{request_id}.json"
        if p.is_file():
            return p
    return None


def write_request(req: Dict[str, Any], root=None) -> Path:
    """Validate and write a new request: to queue/deferred/ when its not_before lies ahead (#40), else to
    queue/ (which starts judge-review.path)."""
    errs = validate_request(req)
    if errs:
        raise ValidationError(errs)
    ensure_dirs(root)
    return atomic_write_json(_home_dir(req, root) / f"{_check_request_id(req['id'])}.json", req)


def read_request(request_id: str, root=None) -> Dict[str, Any]:
    r = _root(root)
    _check_request_id(request_id)
    for sub in ("queue", f"queue/{DEFERRED}", "done"):
        p = r / sub / f"{request_id}.json"
        if p.is_file():
            return _read_json(p)
    raise FileNotFoundError(f"request {request_id} not in queue/, queue/{DEFERRED}/ or done/")


def _scan(d: Path) -> List[Tuple[Path, Dict[str, Any]]]:
    out = []
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        if not REQUEST_ID_RE.match(p.stem):
            continue
        try:
            req = _read_json(p)
        except (OSError, ValueError):
            continue
        if isinstance(req, dict) and not validate_request(req):
            out.append((p, req))
    return out


def list_pending(root=None) -> List[Dict[str, Any]]:
    """Every request not judged yet: queue/ and queue/deferred/, oldest (id) first. A request present in both
    (a release raced a merge) is listed once, from queue/."""
    seen: Dict[str, Tuple[Path, Dict[str, Any]]] = {}
    for p, req in _scan(deferred_dir(root)) + _scan(_root(root) / "queue"):
        seen[p.stem] = (p, req)  # queue/ comes last: it wins
    return [seen[k][1] for k in sorted(seen)]


def list_ready(root=None, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Requests run_judge.py --pending would judge now: queue/*.json that are ready (is_ready)."""
    return [req for _, req in _scan(_root(root) / "queue") if is_ready(req, now)]


def move_done(request_id: str, root=None) -> Path:
    r = ensure_dirs(root)
    _check_request_id(request_id)
    src, dst = r / "queue" / f"{request_id}.json", r / "done" / f"{request_id}.json"
    if not src.exists() and (deferred_dir(root) / f"{request_id}.json").exists():
        src = deferred_dir(root) / f"{request_id}.json"
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
    for sub in ("queue", f"queue/{DEFERRED}", "done"):
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


def _is_pre_verify(req: Dict[str, Any]) -> bool:
    return req.get("source_event") == "pre_verify" or (req.get("detail") or {}).get("hook") == "pre_verify"


def merge_into_pending_completion(req: Dict[str, Any], since: Optional[str] = None,
                                  root=None) -> Optional[str]:
    """Fold an on_session_end completion request *req* (built, not written) into the same turn's pending
    pre_verify completion request instead of queueing a second review of the same turn (#12).

    Target: the newest request in queue/ with kind "completion", the same session, written by pre_verify
    (source_event or detail.hook), not merged before, and created at or after *since* (the previous
    on_session_end of the session, i.e. the start of this turn). A request whose evidence bundle already
    exists (evidence/<id>/: the runner is judging it) is left alone, as is one already in done/.

    Merge: changed_paths = union; since = the earlier; claims, plan, created (the turn's end, so the
    evidence window covers the whole turn) and detail fields (turn_id ...) from *req*, with the
    pre_verify detail kept underneath; data_class "sensitive" if either side is; id, kind, session and
    source_event stay. detail.merged records both creation times. Validated, written atomically.

    Returns the merged request's id, or None when nothing was merged (the caller then writes *req* as a
    new request, subject to is_duplicate)."""
    if req.get("kind") != "completion" or not req.get("session"):
        return None
    floor = None
    if since:
        try:
            floor = parse_utc(since)
        except ValueError:
            floor = None
    r = _root(root)
    best = None
    for e in list_pending(root):
        if (e.get("kind") != "completion" or e.get("session") != req.get("session")
                or not _is_pre_verify(e) or (e.get("detail") or {}).get("merged")):
            continue
        try:
            ec = parse_utc(e.get("created") or "")
        except ValueError:
            continue
        if floor is not None and ec < floor:
            continue
        if best is None or ec >= best[0]:
            best = (ec, e)
    if best is None:
        return None
    pv = best[1]
    rid = _check_request_id(str(pv["id"]))
    if (r / "evidence" / rid).exists():  # being judged right now: a merge would not be reviewed
        return None
    try:
        end_created = parse_utc(req.get("created") or "")
    except ValueError:
        end_created = datetime.now(timezone.utc)
    try:
        sinces = [parse_utc(pv["since"])]
    except (KeyError, ValueError):
        sinces = []
    try:
        sinces.append(parse_utc(req["since"]))
    except (KeyError, ValueError):
        pass
    detail = dict(pv.get("detail") or {})
    detail.update(req.get("detail") or {})
    detail["merged"] = {"from": ["pre_verify", "on_session_end"], "pre_verify_created": pv.get("created"),
                        "session_end_created": req.get("created")}
    merged = dict(pv)
    merged.update({
        "created": utc_now_iso(max(best[0], end_created)),
        "since": utc_now_iso(min(sinces)) if sinces else pv.get("since"),
        "changed_paths": sorted(set(pv.get("changed_paths") or []) | set(req.get("changed_paths") or [])),
        "claims": (req.get("claims") or pv.get("claims") or "")[:4000],
        "plan": req.get("plan") or pv.get("plan"),
        "data_class": "sensitive" if "sensitive" in (pv.get("data_class"), req.get("data_class")) else "infra",
        "detail": detail,
    })
    errs = validate_request(merged)
    if errs:
        raise ValidationError(errs)
    qp, dp = r / "queue" / f"{rid}.json", r / "done" / f"{rid}.json"
    if not qp.is_file() or dp.exists():
        return None
    atomic_write_json(qp, merged)
    if dp.exists() or (r / "evidence" / rid).exists():
        # The runner took the request between our check and our write: do not leave a second, stale copy
        # in queue/ (it would be judged again on the old evidence). The caller writes a new request.
        # If it is still in queue/ (judging), put the original back so is_duplicate sees what is judged.
        try:
            if dp.exists():
                os.unlink(qp)
            else:
                atomic_write_json(qp, pv)
        except OSError:
            pass
        return None
    return rid


# ---------------------------------------------------------------- deferred requests / plan coalescing (#34)
def is_ready(req: Union[Dict[str, Any], str, Path], now: Optional[datetime] = None) -> bool:
    """False while the request's `not_before` lies in the future; True otherwise (no field, unparsable
    field, or an unreadable file: never hold a request back by mistake). Accepts a request dict or the path
    of its queue file. run_judge.py --pending skips requests that are not ready."""
    if not isinstance(req, dict):
        try:
            req = _read_json(Path(req))
        except (OSError, ValueError):
            return True
        if not isinstance(req, dict):
            return True
    nb = req.get("not_before")
    if not isinstance(nb, str) or not nb.strip():
        return True
    try:
        return parse_utc(nb) <= (now or datetime.now(timezone.utc))
    except ValueError:
        return True


def _unlink(p: Path) -> None:
    try:
        os.unlink(p)
    except OSError:
        pass


def _rewrite_pending(new: Dict[str, Any], old: Dict[str, Any], root=None, now: Optional[datetime] = None) -> bool:
    """Replace the pending request (queue/<id>.json or queue/deferred/<id>.json) with *new* unless the runner
    has taken it (done/ or evidence/ exists). *new* goes where it belongs (_home_dir): a request that became
    ready moves from queue/deferred/ into queue/ (that wakes judge-review.path), one pushed out again moves
    back. Same race handling as merge_into_pending_completion; a concurrent release that moved the request
    into queue/ while we rewrote the deferred copy wins (our copy is dropped, False). True when *new* is
    what is pending."""
    errs = validate_request(new)
    if errs:
        raise ValidationError(errs)
    r = _root(root)
    rid = _check_request_id(str(new["id"]))
    dp, ev = r / "done" / f"{rid}.json", r / "evidence" / rid
    cur = pending_path(rid, root)
    if cur is None or dp.exists() or ev.exists():
        return False
    dst = _home_dir(new, root, now) / f"{rid}.json"
    ensure_dir(dst.parent)
    atomic_write_json(dst, new)
    if dst != cur:
        _unlink(cur)
    if dp.exists() or ev.exists():
        # The runner took the request between our check and our write: no second, stale copy.
        if dp.exists():
            _unlink(dst)
        else:  # being judged from queue/: put the original back where it was
            try:
                atomic_write_json(cur, old)
            except OSError:
                pass
            if dst != cur:
                _unlink(dst)
        return False
    qp = r / "queue" / f"{rid}.json"
    if dst != qp and qp.exists():  # released into queue/ by another process meanwhile: that copy wins
        _unlink(dst)
        return False
    return True


def _same_turn(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    ta, tb = _turn_id(a), _turn_id(b)
    return not (ta and tb) or ta == tb


MAX_COALESCED_IDS = 50


def merge_into_pending_plan(req: Dict[str, Any], root=None) -> Optional[str]:
    """Coalesce a plan request *req* (built, not written) into the pending plan request of the same session,
    plan path and turn (#34: one plan review per turn instead of one per `patch`).

    Target: a request in queue/ with kind "plan", the same session and plan, the same Hermes turn
    (detail.turn_id; a missing turn id on either side matches), whose turn has not ended (no
    detail.turn_ended) and which the runner has not taken (no evidence/<id>/, not in done/).

    Merge: changed_paths = union; since = the earlier; created = the later (the last plan write, so the
    evidence window covers every write); claims (the plan's current text), not_before and detail fields
    from *req*; data_class "sensitive" if either side is; id, kind, session, plan, source_event stay.
    detail.coalesced = {"writes", "first_created", "tool_call_ids"}. Returns the id, or None (the caller
    writes *req* as a new request)."""
    if req.get("kind") != "plan" or not req.get("session"):
        return None
    for e in list_pending(root):
        if (e.get("kind") != "plan" or e.get("session") != req.get("session") or e.get("plan") != req.get("plan")
                or (e.get("detail") or {}).get("turn_ended") or not _same_turn(e, req)):
            continue
        if (_root(root) / "evidence" / str(e.get("id"))).exists():
            continue
        try:
            sinces = [parse_utc(x) for x in (e.get("since"), req.get("since")) if x]
            createds = [parse_utc(x) for x in (e.get("created"), req.get("created")) if x]
        except ValueError:
            continue
        old_detail = dict(e.get("detail") or {})
        co = dict(old_detail.get("coalesced") or {})
        ids = list(co.get("tool_call_ids") or ([old_detail["tool_call_id"]] if old_detail.get("tool_call_id") else []))
        new_id = (req.get("detail") or {}).get("tool_call_id")
        if new_id and new_id not in ids:
            ids.append(new_id)
        detail = old_detail
        detail.update(req.get("detail") or {})
        detail["coalesced"] = {"writes": int(co.get("writes") or 1) + 1,
                               "first_created": co.get("first_created") or e.get("created"),
                               "tool_call_ids": ids[-MAX_COALESCED_IDS:]}
        merged = dict(e)
        merged.update({
            "since": utc_now_iso(min(sinces)) if sinces else e.get("since"),
            "created": utc_now_iso(max(createds)) if createds else e.get("created"),
            "changed_paths": sorted(set(e.get("changed_paths") or []) | set(req.get("changed_paths") or [])),
            "claims": (req.get("claims") or e.get("claims") or "")[:4000],
            "data_class": "sensitive" if "sensitive" in (e.get("data_class"), req.get("data_class")) else "infra",
            "detail": detail,
        })
        if req.get("not_before"):
            merged["not_before"] = req["not_before"]
        if _rewrite_pending(merged, e, root):
            return str(e["id"])
    return None


def release_plan_requests(session: str, root=None, now: Optional[datetime] = None,
                          refresh=None) -> List[str]:
    """The session's turn ended: every pending plan request of *session* not yet released gets
    not_before = now and detail.turn_ended = now (later plan writes start a new request). *refresh*
    (optional) is called with each request dict before it is written, e.g. to re-read the plan's final
    text. The rewrite also wakes judge-review.path. Returns the released ids."""
    if not session:
        return []
    now_s = utc_now_iso(now)
    out = []
    for e in list_pending(root):
        if e.get("kind") != "plan" or e.get("session") != session or (e.get("detail") or {}).get("turn_ended"):
            continue
        new = dict(e)
        new["detail"] = dict(e.get("detail") or {})
        new["detail"]["turn_ended"] = now_s
        if e.get("not_before"):
            new["not_before"] = min(now_s, str(e["not_before"]))
        if refresh is not None:
            try:
                refresh(new)
            except Exception:
                pass
        if _rewrite_pending(new, e, root, now):
            out.append(str(e["id"]))
    return out


def release_due(root=None, now: Optional[datetime] = None) -> List[str]:
    """Release each pending request whose not_before has passed and that was not released yet (adds
    detail.released): a deferred one moves from queue/deferred/ into queue/, a legacy one already in queue/
    is rewritten in place. judge-review.path only fires on queue/ changes: without this, a plan request whose
    debounce ran out with no turn end would never be judged. Called by every hook event, the runaway
    watcher's poll and run_judge.py --pending (so judge-review.timer releases them too). Returns the ids."""
    now = now or datetime.now(timezone.utc)
    out = []
    for e in list_pending(root):
        if not e.get("not_before") or not is_ready(e, now):
            continue
        d = e.get("detail") or {}
        if d.get("released") or d.get("turn_ended"):
            continue
        new = dict(e)
        new["detail"] = dict(d)
        new["detail"]["released"] = utc_now_iso(now)
        if _rewrite_pending(new, e, root, now):
            out.append(str(e["id"]))
    return out


def stall_minutes() -> float:
    """JUDGE_STALL_MINUTES (default 15; 0 or negative = check off)."""
    try:
        return float(config.get("JUDGE_STALL_MINUTES", "15"))
    except (ValueError, TypeError):
        return 15.0


def runner_busy(root=None) -> bool:
    """True when a run_judge.py holds $JUDGE_REVIEW_DIR/.runner.lock (non-blocking probe; False on errors)."""
    import fcntl
    p = _root(root) / ".runner.lock"
    if not p.exists():
        return False
    try:
        with open(p, "a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        return False
    return False


def stall_status(root=None, now: Optional[datetime] = None, minutes: Optional[float] = None
                 ) -> Optional[Dict[str, Any]]:
    """#41: None, or {"count", "oldest", "since", "minutes", "limit"} when the oldest READY request in queue/
    has waited more than *minutes* (JUDGE_STALL_MINUTES, default 15) and no runner is busy. Catches a failed
    judge-review.path/.service and any other stall (backend failing, units never installed). Cheap enough for
    a per-turn hook: one stat() per queue file; JSON is parsed only for files older than the limit.
    "Waited" counts from the file's mtime: when it landed in queue/ (or was last rewritten there)."""
    limit = stall_minutes() if minutes is None else float(minutes)
    if limit <= 0:
        return None
    q = _root(root) / "queue"
    try:
        entries = [e for e in os.scandir(q) if e.name.endswith(".json") and e.is_file()
                   and REQUEST_ID_RE.match(e.name[:-5])]
    except OSError:
        return None
    now = now or datetime.now(timezone.utc)
    cutoff = now.timestamp() - limit * 60
    old = []
    for e in entries:
        try:
            mt = e.stat().st_mtime
        except OSError:
            continue
        if mt <= cutoff and is_ready(e.path, now):
            old.append((mt, e.name[:-5]))
    if not old or runner_busy(root):
        return None
    mt, rid = min(old)
    since = datetime.fromtimestamp(mt, timezone.utc)
    return {"count": len(old), "oldest": rid, "since": utc_now_iso(since),
            "minutes": int((now - since).total_seconds() // 60), "limit": limit}


def stall_line(st: Dict[str, Any]) -> str:
    """One line for humans (judge-findings) about a stall_status() result."""
    return (f"WARNING: judge runner is not running or stalled: {st['count']} review request(s) waiting, the "
            f"oldest since {st['since']} ({st['minutes']} min > {st['limit']:g}). Check: systemctl --user status "
            "judge-review.path judge-review.service judge-review.timer; fix: systemctl --user reset-failed "
            "judge-review.service judge-review.path && systemctl --user start judge-review.path")


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
    if request_id is not None:  # the main finding and, for a sensitive request, the frontier claims stage
        rid = _check_request_id(request_id)
        paths = [d / f"{rid}.json", d / f"{rid}.claims.json"]
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
# An ack file acks/<request-id>.<item-id> holds one JSON object {"actor": "human"|"agent", "reason": str,
# "ts": "<UTC Z>"} plus an optional "via" provenance object written by bin/judge-ack. A legacy ack (empty
# or a plain-text reason) reads as actor "human".
#
# Semantics: ANY ack stops C5 re-injection (is_acked). An item is CLOSED only when a human acked it, or
# an agent acked it and its severity is not high. An agent ack of a high item leaves it "agent-acked":
# not injected any more, but still awaiting a human (judge-findings --needs-human).
#
# Forgery: the agent runs as the same user and the gate lets it write acks/, so it can write any ack
# content. agent_wrote_ack() catches the direct case: an ack file that one of the agent's recorded tool
# calls (snapshots/*/events.jsonl: write_file/patch targets, terminal path tokens) wrote to is read as
# actor "agent" whatever it claims. See bin/judge-ack for the process-side check.
ACK_ACTORS = ("human", "agent")
ACK_STATUSES = ("open", "agent-acked", "closed")
MAX_ACK_BYTES = 8192


def _ack_path(request_id: str, item_id: str, root=None) -> Path:
    _check_request_id(request_id)
    if not isinstance(item_id, str) or not ITEM_ID_RE.match(item_id):
        raise ValueError(f"bad item id: {item_id!r}")
    return _root(root) / "acks" / f"{request_id}.{item_id}"


def ack(request_id: str, item_id: str, reason: str = "", root=None, *, actor: str = "human",
        via: Optional[Dict[str, Any]] = None, now: Optional[datetime] = None) -> Path:
    """Write acks/<request-id>.<item-id> as {"actor", "reason", "ts"[, "via"]} (first line of reason,
    <= 500 chars). bin/judge-ack decides the actor; library callers default to "human"."""
    if actor not in ACK_ACTORS:
        raise ValueError(f"bad ack actor: {actor!r}")
    p = _ack_path(request_id, item_id, root)
    ensure_dirs(root)
    line = (reason or "").strip().splitlines()[0][:500] if (reason or "").strip() else ""
    obj: Dict[str, Any] = {"actor": actor, "reason": line, "ts": utc_now_iso(now)}
    if via:
        obj["via"] = via
    return atomic_write(p, json.dumps(obj, ensure_ascii=False) + "\n")


write_ack = ack


def is_acked(request_id: str, item_id: str, root=None) -> bool:
    """Any ack (human or agent, JSON or legacy). This is what stops C5 re-injection."""
    try:
        return _ack_path(request_id, item_id, root).exists()
    except ValueError:
        return False


def read_ack(request_id: str, item_id: str, root=None) -> Optional[Dict[str, Any]]:
    """The ack as written: {"actor", "reason", "ts", "legacy": bool[, "via"]}, or None when not acked.
    Legacy plain-text acks read as actor "human" (ts = file mtime). A JSON object without a valid actor
    reads as "agent" (not trusted)."""
    try:
        p = _ack_path(request_id, item_id, root)
        with open(p, "rb") as fh:
            raw = fh.read(MAX_ACK_BYTES).decode("utf-8", errors="replace")
        mtime = utc_now_iso(datetime.fromtimestamp(p.stat().st_mtime, timezone.utc))
    except (ValueError, OSError):
        return None
    text = raw.strip()
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            actor = obj.get("actor") if obj.get("actor") in ACK_ACTORS else "agent"
            out = {"actor": actor, "reason": str(obj.get("reason") or "")[:500],
                   "ts": str(obj.get("ts") or mtime), "legacy": False}
            if isinstance(obj.get("via"), dict):
                out["via"] = obj["via"]
            return out
    return {"actor": "human", "reason": (text.splitlines() or [""])[0][:500], "ts": mtime, "legacy": True}


def agent_event_paths(root=None) -> set:
    """Every path the agent's recorded tool calls named (all sessions' snapshots/*/events.jsonl)."""
    out: set = set()
    d = _root(root) / "snapshots"
    if not d.is_dir():
        return out
    for ev_file in d.glob("*/events.jsonl"):
        try:
            lines = ev_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict) and isinstance(ev.get("paths"), list):
                out.update(os.path.normpath(str(x)) for x in ev["paths"] if isinstance(x, str))
    return out


def agent_wrote_ack(request_id: str, item_id: str, root=None, _paths: Optional[set] = None) -> bool:
    """True when one of the agent's recorded tool calls named this ack file (a direct write_file/patch,
    or a terminal command with the ack path in it). Best effort: an obfuscated path is not caught."""
    try:
        p = os.path.normpath(str(_ack_path(request_id, item_id, root)))
    except ValueError:
        return False
    paths = _paths if _paths is not None else agent_event_paths(root)
    if p in paths:
        return True
    try:  # same file through a symlinked or differently spelled review dir
        rp = os.path.realpath(p)
        return any(os.path.realpath(x) == rp for x in paths if os.path.basename(x) == os.path.basename(p))
    except OSError:
        return False


def ack_actor(request_id: str, item_id: str, root=None, _paths: Optional[set] = None) -> Optional[str]:
    """Effective actor of the item's ack ("human" | "agent"), or None when not acked. An ack the agent
    wrote directly (agent_wrote_ack) is "agent" whatever the file claims."""
    a = read_ack(request_id, item_id, root)
    if a is None:
        return None
    if a["actor"] == "human" and agent_wrote_ack(request_id, item_id, root, _paths):
        return "agent"
    return a["actor"]


def _is_high(item: Dict[str, Any]) -> bool:
    return str(item.get("severity") or "").strip().lower() == "high"


def closure(actor: Optional[str], severity: Any) -> str:
    """The closure rule on its own: "open" (no ack), "closed" (human ack, or agent ack of a non-high
    item) or "agent-acked" (agent ack of a high item: awaiting a human)."""
    if actor is None:
        return "open"
    if actor == "human" or str(severity or "").strip().lower() != "high":
        return "closed"
    return "agent-acked"


def item_status(request_id: str, item: Dict[str, Any], root=None, _paths: Optional[set] = None) -> str:
    return closure(ack_actor(request_id, str(item.get("id")), root, _paths), item.get("severity"))


def is_closed(request_id: str, item: Dict[str, Any], root=None) -> bool:
    return item_status(request_id, item, root) == "closed"


def needs_human(status: str, item: Dict[str, Any]) -> bool:
    """A high item that no human has closed (open, or acked only by the agent)."""
    return status != "closed" and _is_high(item)


def unacked_items(root=None) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    out = []
    for f in read_findings(root):
        for it in f.get("items") or []:
            if not is_acked(f["request"], str(it.get("id")), root):
                out.append((f, it))
    return out


def items_by_status(root=None) -> Dict[str, List[Tuple[Dict[str, Any], Dict[str, Any]]]]:
    """{"open": [...], "agent-acked": [...], "closed": [...]} of (finding, item)."""
    out: Dict[str, List[Tuple[Dict[str, Any], Dict[str, Any]]]] = {s: [] for s in ACK_STATUSES}
    paths = agent_event_paths(root)
    for f in read_findings(root):
        for it in f.get("items") or []:
            out[item_status(f["request"], it, root, paths)].append((f, it))
    return out
