#!/usr/bin/env python3
"""C3 done gate: Hermes ``pre_verify`` shell hook (stdin JSON -> stdout JSON).

Fires when the agent is about to finish a turn in which it edited files. Runs fast, deterministic
verifiers on ``extra.changed_paths`` and, if any fail, keeps the agent going once with
``{"action": "continue", "message": ...}``. Otherwise prints ``{}``.

Wire format (verified against hermes-agent agent/shell_hooks.py + agent/turn_stop_gates.py):
  stdin  {"hook_event_name": "pre_verify", "tool_name": null, "tool_input": null, "session_id": "...",
          "cwd": "...", "profile": "...",
          "extra": {"platform", "model", "coding", "attempt", "final_response", "changed_paths"}}
  stdout {"action": "continue", "message": "..."}  |  {}

Rules:
- One-shot: ``extra.attempt > 0`` -> ``{}`` (Hermes re-fires after every nudge; the
  ``agent.max_verify_nudges`` bound is only a backstop). The verifiers re-run record-only (see "C3 results").
- Total budget ~45 s (``JUDGE_VERIFY_BUDGET``); configure the hook with ``timeout: 60``.
- Always (on attempt 0) enqueue a ``completion`` review request, deduplicated (see ``is_duplicate``).
- Never crashes: any internal error is logged to ``$JUDGE_REVIEW_DIR/hook-errors.log`` and ``{}`` is printed.

Verifiers (chosen by path):
  *.sh / *.bash / sh-or-bash shebang  bash -n, plus ``shellcheck -S error`` when installed
  *.py                                 compile() in memory (no __pycache__ written anywhere)
  *.json                               json parse
  *.yml / *.yaml                       PyYAML safe_load_all via the Hermes venv python (or any python
                                       with PyYAML; ``JUDGE_YAML_PYTHON`` overrides); skipped with a note
  inside the deck repo                 <repo>/scripts/check-sanitized.sh, once per repo root (a repo root
                                       is the nearest ancestor holding CONVENTIONS.md and that script)
  covenant/nginx/**.tmpl               not run (nginx -t in a container is slow): noted for the judge
  ~/.ssh/config                        ``ssh -G`` parse check, then probes/probe.py ssh_alias_test for each
                                       Host alias whose block changed (baseline: collector snapshot)
  final_response claims                light heuristic, see ``check_claims``

C3 results (evidence for the judge, interface "C3 results" in the run-1 integration notes):
  Every verifier run, passing or failing, appends one JSON line to
  ``$JUDGE_REVIEW_DIR/snapshots/<session>/c3-results.jsonl``:
      {"t": "<UTC Z>", "attempt": int, "path": "<abs>", "check": "<name>", "ok": bool,
       "detail": "<= 300 chars, redacted>"}
  Checks: bash-n, shellcheck, py-compile, json, yaml, check-sanitized (path = repo root), ssh-config,
  ssh-alias:<alias>, claim (path = the claimed path). On ``attempt > 0`` (Hermes re-fires after our nudge)
  the verifiers run again in record-only mode: the results are appended, the hook still prints ``{}`` and
  enqueues nothing, so the judge sees the post-fix state. collector/extras.py ``c3_results`` reads the file.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

sys.dont_write_bytecode = True  # never leave __pycache__ in the judge/ tree
JUDGE_DIR = Path(__file__).resolve().parent.parent
PROBE = JUDGE_DIR / "probes" / "probe.py"
MAX_MESSAGE = 3500
MAX_CLAIMS = 4000


# ------------------------------------------------------------------ config / queue adapters
def _import(name: str) -> Any:
    """Import judge/lib/<name>.py; None if missing or broken (each import isolated)."""
    try:
        if str(JUDGE_DIR) not in sys.path:
            sys.path.insert(0, str(JUDGE_DIR))
        import importlib

        return importlib.import_module(f"lib.{name}")
    except Exception:
        return None


_site_cache: Optional[Dict[str, str]] = None


def _site() -> Dict[str, str]:
    global _site_cache
    if _site_cache is None:
        _site_cache = {}
        cfg = _import("config")
        for fn_name in ("load", "load_config", "load_site_env", "site_env"):
            fn = getattr(cfg, fn_name, None) if cfg else None
            if callable(fn):
                try:
                    data = fn()
                    if isinstance(data, dict):
                        _site_cache = {str(k): str(v) for k, v in data.items() if v is not None}
                        break
                except Exception:
                    continue
    return _site_cache


def setting(name: str, default: str = "") -> str:
    """Environment first, then site.env via lib/config.py, then *default*."""
    val = os.environ.get(name)
    if val:
        return val
    val = _site().get(name)
    return val if val else default


def _num(name: str, default: float) -> float:
    try:
        return float(setting(name, str(default)))
    except ValueError:
        return default


def hermes_home() -> Path:
    return Path(os.path.expanduser(os.environ.get("HERMES_HOME") or "~/.hermes"))


def review_dir() -> Path:
    env = os.environ.get("JUDGE_REVIEW_DIR")
    return Path(os.path.expanduser(env)) if env else hermes_home() / "review"


def log_error(where: str, exc: BaseException) -> None:
    try:
        d = review_dir()
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        p = d / "hook-errors.log"
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as fh:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-2000:]
            fh.write(f"{_utc_iso()} {where}: {exc!r}\n{tb}\n")
    except Exception:
        pass


def _utc_iso(dt: Optional[datetime] = None) -> str:
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: Any) -> Optional[datetime]:
    try:
        return datetime.strptime(str(s), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def request_id(session: str, kind: str, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    q = _import("queue")
    fn = getattr(q, "new_request_id", None) if q else None
    if callable(fn):
        try:
            return str(fn(kind, session, now))
        except Exception:
            pass
    short = re.sub(r"[^A-Za-z0-9]", "", session)[-6:] or "nosess"
    return f"{now.strftime('%Y%m%dT%H%M%SZ')}-{short}-{kind}"


def data_class(paths: Sequence[str]) -> str:
    """Delegate to lib/config.py's infra path rules; default to the safe value 'sensitive'."""
    cfg = _import("config")
    for fn_name in ("classify", "data_class", "classify_paths"):
        fn = getattr(cfg, fn_name, None) if cfg else None
        if callable(fn):
            try:
                v = fn(list(paths))
                if v in ("infra", "sensitive"):
                    return v
            except Exception:
                continue
    return "sensitive"


def _fallback_write(req: Dict[str, Any]) -> str:
    q = review_dir() / "queue"
    q.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=q, prefix=".tmp-", suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(req, fh, indent=2)
    os.chmod(tmp, 0o600)
    dst = q / f"{req['id']}.json"
    os.replace(tmp, dst)
    return str(dst)


def write_request(req: Dict[str, Any]) -> str:
    """lib/queue.write_request when present (schema-validated), else a local atomic write.

    The contract's source_event enum does not list ``pre_verify``; if the queue rejects it we retry
    once with ``on_session_end`` and keep the true origin in ``detail.hook``.
    """
    q = _import("queue")
    fn = getattr(q, "write_request", None) if q else None
    if not callable(fn):
        return _fallback_write(req)
    try:
        return str(fn(req))
    except Exception:
        if req.get("source_event") == "pre_verify":
            retry = dict(req, source_event="on_session_end")
            return str(fn(retry))
        raise


# ------------------------------------------------------------------ dedupe
def _iter_requests(kind: str) -> Iterable[Dict[str, Any]]:
    for sub in ("queue", "done"):
        d = review_dir() / sub
        if not d.is_dir():
            continue
        for p in d.glob(f"*-{kind}.json"):
            try:
                data = json.loads(p.read_text())
                if isinstance(data, dict):
                    yield data
            except Exception:
                continue


def is_duplicate(req: Dict[str, Any], window_s: float) -> Optional[str]:
    """Dedupe rule (shared with C4 on_session_end, which fires after pre_verify in the same turn):

    A completion request R duplicates an existing request E (pending in queue/ or already in done/) when
      E.kind == "completion" and E.session == R.session
      and |E.created - R.created| <= JUDGE_COMPLETION_DEDUPE_SECONDS (default 900)
      and set(R.changed_paths) is a subset of set(E.changed_paths) (an empty list is a subset).
    A duplicate is not written. A later turn that touches new paths is NOT a duplicate.
    Returns the id of E, or None.
    """
    created = _parse_iso(req.get("created")) or datetime.now(timezone.utc)
    mine = set(req.get("changed_paths") or [])
    for e in _iter_requests("completion"):
        if e.get("session") != req.get("session"):
            continue
        ec = _parse_iso(e.get("created"))
        if ec is None or abs((ec - created).total_seconds()) > window_s:
            continue
        if mine <= set(e.get("changed_paths") or []):
            return str(e.get("id") or "?")
    return None


# ------------------------------------------------------------------ runner
class Budget:
    def __init__(self, seconds: float):
        self.deadline = time.monotonic() + seconds

    def left(self) -> float:
        return self.deadline - time.monotonic()


def run(cmd: Sequence[str], budget: Budget, cwd: Optional[str] = None, timeout: float = 20.0,
        env: Optional[Dict[str, str]] = None) -> Optional[Tuple[int, str]]:
    """Run *cmd*; (rc, combined output) or None when the budget is spent. rc 124 = timed out."""
    t = min(timeout, budget.left() - 0.5)
    if t <= 1:
        return None
    try:
        cp = subprocess.run(list(cmd), cwd=cwd, capture_output=True, text=True, timeout=t,
                            stdin=subprocess.DEVNULL, env=env)
        return cp.returncode, (cp.stdout or "") + (cp.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {t:.0f}s"
    except FileNotFoundError as e:
        return 127, str(e)


def _excerpt(text: str, n: int = 400) -> str:
    text = text.strip()
    lines = text.splitlines()
    out = "\n    ".join(lines[:6])
    return (out[:n] + "...") if len(out) > n else out


class Result:
    def __init__(self) -> None:
        self.failures: List[str] = []       # shown to the agent (may quote tool output)
        self.fail_tags: List[str] = []      # short, content-free; go into the review request
        self.notes: List[str] = []
        self.ran: List[str] = []
        self.claim_flags: List[Dict[str, str]] = []
        self.soft_claims: List[Dict[str, str]] = []
        self.records: List[Dict[str, Any]] = []  # one per verifier run -> c3-results.jsonl

    def record(self, path: str, check: str, ok: bool, detail: str = "") -> None:
        self.records.append({"t": _utc_iso(), "path": str(path), "check": check, "ok": bool(ok),
                             "detail": str(detail or "")})

    def fail(self, tag: str, msg: str) -> None:
        self.fail_tags.append(tag)
        self.failures.append(msg)


# ------------------------------------------------------------------ path helpers
def norm(p: str, cwd: str) -> str:
    p = os.path.expanduser(str(p).strip())
    if not os.path.isabs(p):
        p = os.path.join(cwd or os.getcwd(), p)
    return os.path.normpath(p)


def _real(p: str) -> str:
    try:
        return os.path.realpath(p)
    except Exception:
        return p


def is_shell(p: str) -> bool:
    if p.endswith((".sh", ".bash", ".sh.tmpl")):
        return True
    if os.path.splitext(p)[1]:
        return False
    try:
        with open(p, "rb") as fh:
            first = fh.readline(200)
        return bool(re.match(rb"#!\s*\S*(/|env\s+)(ba)?sh\b", first))
    except Exception:
        return False


def find_repo(p: str) -> Optional[str]:
    d = os.path.dirname(p)
    while True:
        if os.path.isfile(os.path.join(d, "CONVENTIONS.md")) and os.path.isfile(
                os.path.join(d, "scripts", "check-sanitized.sh")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def ssh_config_path() -> str:
    return os.path.normpath(os.path.expanduser("~/.ssh/config"))


# ------------------------------------------------------------------ verifiers
def check_shell(paths: List[str], res: Result, budget: Budget) -> None:
    sc = shutil.which("shellcheck")
    for p in paths:
        r = run(["bash", "-n", p], budget, timeout=10)
        res.ran.append(f"bash -n {p}")
        if r is None:
            res.notes.append(f"budget exhausted before bash -n {p}")
            return
        res.record(p, "bash-n", r[0] == 0, r[1] if r[0] != 0 else "syntax OK")
        if r[0] != 0:
            res.fail("bash-syntax", f"bash -n {p} failed:\n    {_excerpt(r[1])}")
            continue
        if sc and not p.endswith(".tmpl"):
            r = run([sc, "-S", "error", "-f", "gcc", p], budget, timeout=15)
            res.ran.append(f"shellcheck {p}")
            if r and r[0] == 124:
                res.record(p, "shellcheck", True, "timed out (not counted as a failure)")
            elif r:
                res.record(p, "shellcheck", r[0] == 0, r[1] if r[0] != 0 else "no errors")
            if r and r[0] not in (0, 124):
                res.fail("shellcheck", f"shellcheck -S error {p}:\n    {_excerpt(r[1])}")
    if paths and not sc:
        res.notes.append("shellcheck not installed; only bash -n was run")


def check_python(paths: List[str], res: Result) -> None:
    for p in paths:
        res.ran.append(f"py compile {p}")
        try:
            with open(p, "rb") as fh:
                compile(fh.read(), p, "exec", dont_inherit=True)
            res.record(p, "py-compile", True, "compiles")
        except SyntaxError as e:
            res.record(p, "py-compile", False, f"line {e.lineno}: {e.msg}")
            res.fail("python-syntax", f"Python syntax error in {p} line {e.lineno}: {e.msg}")
        except (OSError, ValueError) as e:
            res.record(p, "py-compile", False, f"cannot compile: {e}")
            res.fail("python-syntax", f"cannot compile {p}: {e}")


def check_json(paths: List[str], res: Result) -> None:
    for p in paths:
        res.ran.append(f"json parse {p}")
        try:
            with open(p, encoding="utf-8") as fh:
                json.load(fh)
            res.record(p, "json", True, "parses")
        except json.JSONDecodeError as e:
            res.record(p, "json", False, f"line {e.lineno} col {e.colno}: {e.msg}")
            res.fail("json", f"invalid JSON in {p} line {e.lineno} col {e.colno}: {e.msg}")
        except (OSError, UnicodeDecodeError) as e:
            res.record(p, "json", False, f"cannot read: {e}")
            res.fail("json", f"cannot read {p}: {e}")


_YAML_SCRIPT = r"""
import json, sys, yaml
errs = {}
for p in sys.argv[1:]:
    try:
        with open(p, encoding="utf-8") as fh:
            list(yaml.safe_load_all(fh))
    except Exception as e:
        errs[p] = str(e).replace("\n", " ")[:300]
print(json.dumps(errs))
"""


def yaml_python(budget: Budget) -> Optional[str]:
    cands = [os.environ.get("JUDGE_YAML_PYTHON", ""),
             str(hermes_home() / "hermes-agent" / "venv" / "bin" / "python"),
             os.path.expanduser("~/.hermes/hermes-agent/venv/bin/python")]
    for c in cands:
        if c and os.access(c, os.X_OK):
            r = run([c, "-c", "import yaml"], budget, timeout=10)
            if r and r[0] == 0:
                return c
    return None


def check_yaml(paths: List[str], res: Result, budget: Budget) -> None:
    py = yaml_python(budget)
    if not py:
        res.notes.append("YAML not parsed (no python with PyYAML found): " + ", ".join(paths))
        return
    r = run([py, "-c", _YAML_SCRIPT, *paths], budget, timeout=15)
    res.ran.append(f"yaml parse {len(paths)} file(s)")
    if r is None or r[0] != 0:
        res.notes.append("YAML parse check did not complete: " + (_excerpt(r[1], 200) if r else "budget"))
        return
    try:
        errs = json.loads(r[1].strip().splitlines()[-1])
    except Exception:
        res.notes.append("YAML parse check gave unreadable output")
        return
    for p in paths:
        if p in errs:
            res.record(p, "yaml", False, str(errs[p]))
        else:
            res.record(p, "yaml", True, "parses")
    for p, msg in errs.items():
        res.fail("yaml", f"invalid YAML in {p}: {msg}")


def check_sanitized(roots: List[str], res: Result, budget: Budget) -> None:
    for root in roots:
        r = run(["bash", "scripts/check-sanitized.sh"], budget, cwd=root, timeout=30)
        res.ran.append(f"check-sanitized.sh in {root}")
        if r is None:
            res.notes.append(f"budget exhausted before check-sanitized.sh in {root}")
        else:
            res.record(root, "check-sanitized", r[0] == 0, r[1] if r[0] != 0 else "clean")
        if r is not None and r[0] != 0:
            res.fail("check-sanitized",
                     f"{root}/scripts/check-sanitized.sh failed (public repo: no real hosts/IPs/names/"
                     f"secrets; use site.env variables and example values):\n    {_excerpt(r[1], 600)}")


# --- ssh config
def parse_ssh_hosts(text: str) -> Dict[str, str]:
    """Map each concrete Host alias (no wildcards/negations) to its block text (comments stripped)."""
    blocks: Dict[str, str] = {}
    cur: List[str] = []
    cur_lines: List[str] = []

    def flush() -> None:
        body = "\n".join(cur_lines)
        for a in cur:
            blocks[a] = body

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"(?i)^(host|match)\s*(?:=|\s)\s*(.*)$", line)
        if m:
            flush()
            cur_lines = [line]
            cur = ([a for a in m.group(2).split() if not re.search(r"[*?!]", a)]
                   if m.group(1).lower() == "host" else [])
            continue
        cur_lines.append(" ".join(line.split()))
    flush()
    return blocks


def snapshot_dir(session: str) -> Optional[Path]:
    """snapshots/<session>/ written by the collector at session start (lib/snapshot.py), if any."""
    if not session:
        return None
    q = _import("queue")
    fn = getattr(q, "snapshot_dir", None) if q else None
    try:
        base = Path(fn(session)) if callable(fn) else review_dir() / "snapshots" / session
    except Exception:
        base = review_dir() / "snapshots" / session
    return base if base.is_dir() else None


# ------------------------------------------------------------------ C3 results (evidence for the judge)
C3_RESULTS = "c3-results.jsonl"
C3_DETAIL_MAX = 300
C3_MAX_RECORDS = 200  # per hook run
_SESSION_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")  # same rule as lib/queue.safe_session
_redact_fn: Optional[Any] = None


def _redactor() -> Any:
    """lib/redact.redact; when it cannot be loaded, a redactor that drops the text (never leak a detail)."""
    global _redact_fn
    if _redact_fn is None:
        fn = getattr(_import("redact"), "redact", None)
        if not callable(fn):
            try:
                import importlib.util

                spec = importlib.util.spec_from_file_location("judge_c3_redact", JUDGE_DIR / "lib" / "redact.py")
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)  # type: ignore[union-attr]
                fn = mod.redact
            except Exception:
                fn = None
        _redact_fn = fn if callable(fn) else (lambda _t: "")
    return _redact_fn


def c3_results_path(session: str) -> Optional[Path]:
    """$JUDGE_REVIEW_DIR/snapshots/<session>/c3-results.jsonl (None without a session id)."""
    if not session:
        return None
    q = _import("queue")
    fn = getattr(q, "snapshot_dir", None) if q else None
    try:
        base = Path(fn(session)) if callable(fn) else None
    except Exception:
        base = None
    if base is None:
        safe = _SESSION_SAFE_RE.sub("_", session).strip(".")[:120] or "nosession"
        base = review_dir() / "snapshots" / safe
    return base / C3_RESULTS


def c3_line(rec: Dict[str, Any], attempt: int) -> str:
    detail = " ".join(str(rec.get("detail") or "").split())  # one line; whitespace collapsed
    try:
        detail = str(_redactor()(detail))
    except Exception:
        detail = ""
    if len(detail) > C3_DETAIL_MAX:  # truncate AFTER redaction so a cut cannot defeat a pattern
        detail = detail[:C3_DETAIL_MAX - 3] + "..."
    return json.dumps({"t": str(rec.get("t") or _utc_iso()), "attempt": int(attempt),
                       "path": str(rec.get("path") or ""), "check": str(rec.get("check") or ""),
                       "ok": bool(rec.get("ok")), "detail": detail}, ensure_ascii=False)


def write_c3_results(session: str, attempt: int, records: Sequence[Dict[str, Any]]) -> Optional[Path]:
    """Append one line per verifier run. Never raises (errors go to hook-errors.log)."""
    try:
        path = c3_results_path(session)
        if path is None or not records:
            return None
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        text = "".join(c3_line(r, attempt) + "\n" for r in list(records)[:C3_MAX_RECORDS])
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(text)
        return path
    except Exception as e:
        log_error("verify.py c3-results", e)
        return None


def snapshot_index(session: str) -> Dict[str, Dict[str, Any]]:
    """{abs_path: {"sha256", "size"}} from snapshots/<session>/index.json; {} when absent."""
    base = snapshot_dir(session)
    try:
        data = json.loads((base / "index.json").read_text()) if base else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _sha256(path: str) -> Optional[str]:
    import hashlib

    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def find_ssh_baseline(session: str) -> Optional[str]:
    """The collector's session-start snapshot of ~/.ssh/config, if one exists."""
    base = snapshot_dir(session)
    if base is None:
        return None
    exact = base / "files" / ssh_config_path().lstrip("/")
    if exact.is_file():
        try:
            return exact.read_text(errors="replace")
        except OSError:
            return None
    for p in base.rglob("*"):
        sp = str(p)
        if p.is_file() and (sp.endswith("/.ssh/config") or sp.endswith("/ssh/config")
                            or p.name in ("ssh_config", "ssh-config", ".ssh_config")):
            try:
                return p.read_text(errors="replace")
            except Exception:
                return None
    return None


def run_probe(name: str, args: Sequence[str], budget: Budget) -> Optional[Tuple[int, str]]:
    if not PROBE.is_file():
        return 127, "probe.py not installed"
    return run([sys.executable, str(PROBE), name, *args], budget, timeout=25)


def check_ssh_config(path: str, session: str, final_response: str, res: Result, budget: Budget) -> None:
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError as e:
        res.fail("ssh-config", f"{path} is in changed_paths but cannot be read: {e}")
        return
    hosts = parse_ssh_hosts(text)
    if shutil.which("ssh"):
        probe_alias = next(iter(hosts), "judge-parse-check")
        r = run(["ssh", "-G", "-F", path, probe_alias], budget, timeout=5)
        res.ran.append("ssh -G parse")
        if r and r[0] not in (0, 124):
            res.record(path, "ssh-config", False, r[1])
            res.fail("ssh-config", f"ssh cannot parse {path}:\n    {_excerpt(r[1])}")
            return
        if r and r[0] == 0:
            res.record(path, "ssh-config", True, "ssh -G parses")
    baseline = find_ssh_baseline(session)
    if baseline is not None:
        old = parse_ssh_hosts(baseline)
        aliases = [a for a, body in hosts.items() if old.get(a) != body]
    else:
        wanted = set(setting("JUDGE_SSH_ALIASES", "").split())
        aliases = [a for a in hosts if a in wanted or re.search(
            rf"(?<![\w.-]){re.escape(a)}(?![\w.-])", final_response or "")]
        res.notes.append("no session-start snapshot of ~/.ssh/config; tested aliases named in the answer "
                         "or JUDGE_SSH_ALIASES only")
    for a in aliases[:8]:
        r = run_probe("ssh_alias_test", [a], budget)
        res.ran.append(f"probe ssh_alias_test {a}")
        if r is None:
            res.notes.append(f"budget exhausted before ssh_alias_test {a}")
            break
        if r[0] == 127:
            res.notes.append(f"ssh_alias_test {a} not run: {r[1][:120]}")
        elif r[0] == 64:
            res.notes.append(f"probe refused alias {a!r} (exit 64)")
        elif r[0] == 0:
            res.record(path, f"ssh-alias:{a}", True, "connects")
        elif r[0] != 0:
            res.record(path, f"ssh-alias:{a}", False, f"probe ssh_alias_test exit {r[0]}")
            res.fail("ssh-alias", f"ssh alias {a!r} from {path} does not connect "
                                  f"(probe ssh_alias_test exit {r[0]}):\n    {_excerpt(r[1], 300)}")
    if len(aliases) > 8:
        res.notes.append(f"{len(aliases) - 8} more changed ssh aliases not tested")


# --- claims heuristic
EDIT_VERBS = (r"fixed|updated|edited|changed|patched|modified|corrected|repaired|rewrote|rewritten|wrote|"
              r"written|added|created|configured|replaced|removed|deleted|renamed|applied")
SOFT_VERBS = r"verified|confirmed|checked|tested|validated|done|completed?"
_EDIT_RE = re.compile(rf"\b({EDIT_VERBS})\b", re.I)
_SOFT_RE = re.compile(rf"\b({SOFT_VERBS})\b", re.I)
_NEG_RE = re.compile(r"\b(not|no|never|without|unchanged|untouched|nothing|instead of)\b|n't\b", re.I)
_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.I)
_PATH_RE = re.compile(
    r"(?:~|\.{1,2})?/[\w.@%+=,~-][\w.@%+=,~/-]*"      # /abs, ~/x, ./x, ../x
    r"|[\w.-]+(?:/[\w.@+-]+)+"                          # rel/dir/file
    r"|[\w-]+(?:\.[\w-]+)*\.(?:sh|py|json|ya?ml|conf|tmpl|md|toml|service|timer|env|ini|cfg|js|ts|css|html)\b")


def _claim_tokens(sentence: str) -> List[str]:
    s = _URL_RE.sub(" ", sentence)
    out = []
    for m in _PATH_RE.finditer(s):
        tok = m.group(0).rstrip(".,:;)]}'\"`")
        if len(tok) > 2:
            out.append(tok)
    return out


def check_claims(final_response: str, changed: List[str], cwd: str, res: Result,
                 window_s: float, now: Optional[float] = None,
                 snap_index: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
    """Flag file paths the answer says were fixed/updated/... that are not in changed_paths.

    - Split the answer into sentences/lines; skip any with a negation ("not", "unchanged", "n't", ...).
    - Edit verbs (fixed, updated, edited, added, ...) + a path -> hard claim; verify-only verbs
      (verified, checked, done, ...) + a path -> soft claim (recorded for the judge, never a nudge,
      since reading/testing a file legitimately leaves it unchanged).
    - A path counts as changed if it equals a changed path, is a directory containing one, or (bare
      file name) shares a changed path's basename.
    - Only paths that exist, or would be a new file with an extension in an existing directory, are
      considered (so URL paths such as /healthz are ignored). Directories are ignored.
    - Hermes only lists edits made by its file tools in changed_paths, so a terminal ``sed -i`` would
      otherwise look like a false claim. When the collector's session-start snapshot (index.json) has a
      hash for the file, that decides: hash changed -> not flagged, hash equal -> flagged. Without a
      snapshot entry, a file modified within ``window_s`` is not flagged.
    """
    if not final_response:
        return
    now = now if now is not None else time.time()
    changed_real = {_real(p) for p in changed}
    changed_base = {os.path.basename(p) for p in changed}
    seen = set()
    for sentence in re.split(r"\n+|(?<=[.!?])\s+", final_response):
        if not sentence.strip() or _NEG_RE.search(sentence):
            continue
        hard = bool(_EDIT_RE.search(sentence))
        soft = bool(_SOFT_RE.search(sentence))
        if not (hard or soft):
            continue
        for tok in _claim_tokens(sentence):
            p = norm(tok, cwd)
            rp = _real(p)
            if rp in seen:
                continue
            seen.add(rp)
            if rp in changed_real or any(c.startswith(rp.rstrip("/") + "/") for c in changed_real):
                if hard:
                    res.record(p, "claim", True, "claimed changed; in changed_paths")
                continue
            if "/" not in tok and tok in changed_base:
                if hard:
                    res.record(p, "claim", True, "claimed changed; a changed path has this name")
                continue
            if os.path.isdir(p):
                continue
            exists = os.path.isfile(p)
            if not exists and not (os.path.splitext(p)[1] and os.path.isdir(os.path.dirname(p))):
                continue
            entry = {"path": tok, "sentence": sentence.strip()[:200]}
            if not hard:
                res.soft_claims.append(entry)
                continue
            snap = (snap_index or {}).get(p) or (snap_index or {}).get(rp) or {}
            if exists and snap.get("sha256"):
                if _sha256(p) != snap["sha256"]:
                    res.record(p, "claim", True, "claimed changed; differs from the session-start snapshot")
                    res.notes.append(f"{tok} claimed changed, not in changed_paths, but differs from the "
                                     f"session-start snapshot (edited outside file tools?)")
                    continue
            elif exists:
                try:
                    if now - os.path.getmtime(p) <= window_s:
                        res.record(p, "claim", True, "claimed changed; modified recently (no snapshot entry)")
                        res.notes.append(f"{tok} claimed changed, not in changed_paths, but modified "
                                         f"recently (edited outside file tools?)")
                        continue
                except OSError:
                    pass
            res.claim_flags.append(entry)
            if not exists:
                state = "it does not exist"
            elif snap.get("sha256"):
                state = "it is identical to its session-start snapshot"
            else:
                state = "it is unchanged (not in this turn's edited files, not modified recently)"
            res.record(p, "claim", False, f"claimed changed, but {state}")
            res.fail("claim-mismatch", f"your answer claims {tok} was changed, but {state}. "
                                       f"Make the change, or correct the claim in your answer.")


# ------------------------------------------------------------------ main logic
def verify(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], Result, Dict[str, Any]]:
    extra = payload.get("extra") or {}
    cwd = str(payload.get("cwd") or os.getcwd())
    session = str(payload.get("session_id") or "")
    final_response = str(extra.get("final_response") or "")
    raw_paths = [str(p) for p in (extra.get("changed_paths") or []) if p]
    paths = sorted({norm(p, cwd) for p in raw_paths})
    budget = Budget(_num("JUDGE_VERIFY_BUDGET", 45.0))
    res = Result()
    existing = [p for p in paths if os.path.isfile(p)]
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        res.notes.append("deleted/moved (not checked): " + ", ".join(missing[:10]))

    steps = [
        ("python", lambda: check_python([p for p in existing if p.endswith(".py")], res)),
        ("json", lambda: check_json([p for p in existing if p.endswith(".json")], res)),
        ("shell", lambda: check_shell([p for p in existing if is_shell(p)], res, budget)),
        ("yaml", lambda: [check_yaml(y, res, budget) for y in
                          [[p for p in existing if p.endswith((".yml", ".yaml"))]] if y]),
        ("claims", lambda: check_claims(final_response, paths, cwd, res,
                                        _num("JUDGE_CLAIM_MTIME_WINDOW", 3600.0),
                                        snap_index=snapshot_index(session))),
        ("ssh", lambda: [check_ssh_config(p, session, final_response, res, budget)
                         for p in existing if _real(p) == _real(ssh_config_path())]),
        ("sanitized", lambda: check_sanitized(
            sorted({r for r in (find_repo(p) for p in paths) if r}), res, budget)),
    ]
    for name, step in steps:
        if budget.left() <= 1:
            res.notes.append(f"time budget exhausted; skipped {name} check")
            continue
        try:
            step()
        except Exception as e:  # one broken verifier never hides the others
            log_error(f"verify.py step {name}", e)
            res.notes.append(f"{name} check errored internally (see hook-errors.log)")

    nginx = [p for p in paths if p.endswith(".tmpl") and "/covenant/nginx/" in p]
    if nginx:
        res.notes.append("nginx templates changed; nginx -t (container) skipped here, judge should render "
                         "and test: " + ", ".join(nginx[:10]))

    if res.failures:
        body = "\n".join(f"- {f}" for f in res.failures)
        msg = ("Done-gate (C3) found problems with this turn's changes:\n" + body +
               "\nFix each item (or correct your answer if a claim was wrong), re-run the failing check "
               "yourself, then finish.")
        if len(msg) > MAX_MESSAGE:
            msg = msg[:MAX_MESSAGE - 20] + "\n...(truncated)"
        out: Dict[str, Any] = {"action": "continue", "message": msg}
    else:
        out = {}
    ctx = {"session": session, "paths": paths, "final_response": final_response, "extra": extra}
    return out, res, ctx


def session_started(session: str) -> Optional[datetime]:
    """`started` from the session-start snapshot's meta.json (lib/snapshot.py), or None."""
    base = snapshot_dir(session)
    if base is None:
        return None
    try:
        meta = json.loads((base / "meta.json").read_text(encoding="utf-8"))
        return datetime.strptime(str(meta.get("started") or ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (OSError, ValueError, AttributeError):
        return None


def since_for(paths: List[str], now: datetime, session_start: Optional[datetime] = None) -> str:
    """Start of a completion request's evidence window: earliest mtime of the changed paths - 5 min (no paths
    readable: now - 1 h), never more than 24 h back, and clamped to the session start when it is known
    (bug #20: the 5-minute margin used to reach into earlier sessions). A late snapshot (`started` after the
    earliest edit) never cuts off an edit: the clamp is then the edit's own mtime."""
    mtimes = []
    for p in paths:
        try:
            mtimes.append(os.path.getmtime(p))
        except OSError:
            pass
    if mtimes:
        first = datetime.fromtimestamp(min(mtimes), timezone.utc)
        start = first - timedelta(minutes=5)
        start = max(start, now - timedelta(hours=24))
        if session_start is not None:
            start = max(start, min(session_start, first.replace(microsecond=0)))
    else:
        start = now - timedelta(hours=1)
        if session_start is not None:
            start = max(start, min(session_start, now))
    return _utc_iso(start)


def enqueue(res: Result, ctx: Dict[str, Any]) -> Optional[str]:
    now = datetime.now(timezone.utc)
    session = ctx["session"] or "unknown"
    extra = ctx["extra"]
    req = {
        "id": request_id(session, "completion", now),
        "kind": "completion",
        "session": session,
        "created": _utc_iso(now),
        "since": since_for(ctx["paths"], now, session_started(session)),
        "changed_paths": ctx["paths"],
        "claims": ctx["final_response"][:MAX_CLAIMS],
        "plan": None,
        "data_class": data_class(ctx["paths"]),
        "source_event": "pre_verify",
        "detail": {
            "hook": "pre_verify",
            "platform": extra.get("platform"), "model": extra.get("model"), "coding": extra.get("coding"),
            "verify": {"failed": sorted(set(res.fail_tags)), "ran": res.ran[:50], "notes": res.notes[:20]},
            "claim_flags": res.claim_flags[:20], "soft_claims": res.soft_claims[:20],
        },
    }
    dup = is_duplicate(req, _num("JUDGE_COMPLETION_DEDUPE_SECONDS", 900.0))
    if dup:
        return None
    return write_request(req)


def main(stdin: Any = None, stdout: Any = None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    out: Dict[str, Any] = {}
    try:
        raw = stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        extra = payload.get("extra") or {}
        try:
            attempt = int(extra.get("attempt") or 0)
        except (TypeError, ValueError):
            attempt = 0
        if not extra.get("changed_paths"):
            stdout.write("{}\n")
            return 0
        if attempt > 0:
            # re-fire after our nudge: record-only (post-fix state for the judge), never nudge again
            try:
                _, res, ctx = verify(payload)
                write_c3_results(ctx["session"], attempt, res.records)
            except Exception as e:
                log_error("verify.py record-only", e)
            stdout.write("{}\n")
            return 0
        out, res, ctx = verify(payload)
        write_c3_results(ctx["session"], attempt, res.records)
        try:
            enqueue(res, ctx)
        except Exception as e:
            log_error("verify.py enqueue", e)
    except Exception as e:
        log_error("verify.py", e)
        out = {}
    try:
        stdout.write(json.dumps(out) + "\n")
    except Exception:
        stdout.write("{}\n")
    return 0


if __name__ == "__main__":
    try:
        main()
    except BaseException:  # absolutely never crash the hook
        print("{}")
    sys.exit(0)
