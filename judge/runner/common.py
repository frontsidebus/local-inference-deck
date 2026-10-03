"""Shared helpers for the runner, the C5 injector and the judge-* CLIs (stdlib only).

Uses judge/lib/config.py and judge/lib/queue.py when they provide what is needed, and falls back to
small local implementations of the same contract (CONTRACT.md "Runtime directories") otherwise, so a
missing or still-changing helper never breaks a hook.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

JUDGE_DIR = Path(__file__).resolve().parent.parent
if str(JUDGE_DIR) not in sys.path:
    sys.path.insert(0, str(JUDGE_DIR))

try:  # shared helper (collector agent); optional at import time
    from lib import config as _config  # type: ignore
except Exception:  # pragma: no cover - depends on sibling work
    _config = None
try:
    from lib import queue as _queue  # type: ignore
except Exception:  # pragma: no cover
    _queue = None

SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}
REQUEST_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9]{1,6}-(plan|gate|completion|runaway)$")
ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def check_ids(request_id: str, item_id: Optional[str] = None) -> None:
    """Raise ValueError unless the ids match the contract (also keeps them path-safe)."""
    if not isinstance(request_id, str) or not REQUEST_ID_RE.match(request_id):
        raise ValueError(f"bad request id: {request_id!r}")
    if item_id is not None and (not isinstance(item_id, str) or not ITEM_ID_RE.match(item_id)):
        raise ValueError(f"bad item id: {item_id!r}")


# ---------------------------------------------------------------- config / paths
def _site() -> Dict[str, str]:
    """Merged config (defaults < site.env < env) from lib/config.load_config(); {} when unavailable.
    Not cached: hooks are short-lived and tests change the environment between calls."""
    if _config is not None:
        try:
            data = _config.load_config()
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items() if v is not None}
        except Exception:
            pass
    return {}


def setting(name: str, default: str = "") -> str:
    """Environment first, then site.env (through lib/config.py), then *default*."""
    val = os.environ.get(name)
    if val not in (None, ""):
        return val
    val = _site().get(name)
    return val if val not in (None, "") else default


def hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(os.path.expanduser(env))
    val = _site().get("HERMES_HOME")
    return Path(val) if val else Path(os.path.expanduser("~/.hermes"))


def review_dir() -> Path:
    env = os.environ.get("JUDGE_REVIEW_DIR")
    if env:
        return Path(os.path.expanduser(env))
    if not os.environ.get("HERMES_HOME"):
        val = _site().get("JUDGE_REVIEW_DIR")
        if val:
            return Path(val)
    return hermes_home() / "review"


def sub(name: str) -> Path:
    return review_dir() / name


def ensure_dirs() -> None:
    for d in (review_dir(), *(sub(n) for n in ("queue", "evidence", "findings", "acks", "done"))):
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass


# ---------------------------------------------------------------- time
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: Any) -> Optional[datetime]:
    if not isinstance(text, str) or not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- atomic io
def atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path: Path, obj: Any) -> None:
    atomic_write(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- findings / acks
def ack_path(request_id: str, item_id: str) -> Path:
    return sub("acks") / f"{request_id}.{item_id}"


def is_acked(request_id: str, item_id: str) -> bool:
    try:
        check_ids(request_id, item_id)
    except ValueError:
        return False
    if _queue is not None and callable(getattr(_queue, "is_acked", None)):
        try:
            return bool(_queue.is_acked(request_id, item_id))
        except Exception:
            pass
    return ack_path(request_id, item_id).exists()


def write_ack(request_id: str, item_id: str, reason: str) -> Path:
    check_ids(request_id, item_id)
    if _queue is not None and callable(getattr(_queue, "write_ack", None)):
        try:
            _queue.write_ack(request_id, item_id, reason)
            p = ack_path(request_id, item_id)
            if p.exists():
                return p
        except Exception:
            pass
    p = ack_path(request_id, item_id)
    atomic_write(p, (reason.strip().splitlines() or [""])[0][:500] + "\n")
    return p


def iter_findings() -> Iterator[Tuple[Path, Dict[str, Any]]]:
    """(path, finding) for every readable findings/*.json, newest file name last."""
    d = sub("findings")
    if not d.is_dir():
        return
    for p in sorted(d.glob("*.json")):
        try:
            data = read_json(p)
        except Exception:
            continue
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            yield p, data


def request_for(request_id: str) -> Optional[Dict[str, Any]]:
    if not REQUEST_ID_RE.match(request_id or ""):
        return None
    for d in ("queue", "done"):
        p = sub(d) / f"{request_id}.json"
        if p.exists():
            try:
                data = read_json(p)
                return data if isinstance(data, dict) else None
            except Exception:
                return None
    return None


def session_short(session: str) -> str:
    """Last 6 chars of a session id: the id component used in request ids."""
    return (session or "")[-6:]


def request_session_short(request_id: str) -> str:
    """`<ts>-<session short 6>-<kind>` -> the session part, '' if the id is not in that form."""
    parts = request_id.split("-")
    short = parts[1] if len(parts) >= 3 else ""
    return "" if short == "nosess" else short  # lib/queue.py uses "nosess" when there is no session


def finding_age_ok(finding: Dict[str, Any], path: Path, window: timedelta) -> bool:
    created = parse_iso(finding.get("created"))
    if created is None:
        try:
            created = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        except OSError:
            return False
    return utc_now() - created <= window


def log_error(name: str, message: str) -> None:
    """Append one line to $JUDGE_REVIEW_DIR/<name>; never raises."""
    try:
        p = review_dir() / name
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(f"{iso(utc_now())} {message.replace(chr(10), ' | ')[:4000]}\n")
    except Exception:
        pass


def items_sorted(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(items, key=lambda it: -SEVERITY_RANK.get(str(it.get("severity")), 0))
