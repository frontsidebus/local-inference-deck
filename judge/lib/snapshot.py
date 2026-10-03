"""judge/lib/snapshot.py: watched-path snapshots taken at session start, and diffs against them.

Layout of snapshots/<session>/ (dir 700, files 600):
    meta.json      {"session", "started", "cwd", "roots": [...], "repos": [{"root","head","status"}],
                    "late": bool, "last_end": "...Z" | absent}
    index.json     {abs_path: {"sha256", "size"} | {"skipped": reason, "size"}}
    files/<abs path without leading />   copies of watched files
    events.jsonl   one line per executed tool call: {"t","tool","paths","status","call_id"?,"call_hash"?}

Watched: $HERMES_HOME/{config.yaml,skills/,memories/,plans/}, ~/.ssh/config, <cwd>/.hermes/plans/, and the
git repos containing cwd and JUDGE_REPO_DIR (HEAD + status recorded; diffs come from git).

    watched_roots(cfg, cwd) -> list[str]
    take(session, cwd, cfg, root=None, late=False, now=None) -> Path      idempotent: returns existing dir
    load_meta(snapdir) -> dict;  save_meta(snapdir, meta)
    record_event(snapdir, tool, paths, status)
    events(snapdir) -> list[dict]
    changed_files(snapdir, cfg) -> list[(status, path)]   status in A|M|D ; includes repo changes
    agent_touched(snapdir, cfg, until=None, tools=()) -> (paths, prefixes)   what the agent's tool calls touched
    attribute(changed, paths, prefixes=()) -> (agent, others)                split changed paths by who touched them
    diff_text(snapdir, cfg, sensitive, include=None, until=None) -> str
                                                          unified diff (infra) or stat summary (sensitive),
                                                          optionally restricted to *include* paths
    stat_lines(snapdir, cfg, paths) -> list[str]          "<status> <path> | +N -M" per path, never content

Attribution: snapshot diffs show every change to a watched path, whoever made it (the human, other tools,
a `git pull`). A changed path counts as the agent's only when one of the session's recorded tool events
(events.jsonl: write_file/patch targets, path-like tokens of terminal commands) names it, or when it lies
under a HERMES_HOME dir written by a Hermes self-write tool the session ran (SELF_WRITE_TOOLS).
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from . import queue as q

MAX_FILE = 2 * 1024 * 1024
MAX_TOTAL = 100 * 1024 * 1024
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".cache"}
GIT_TIMEOUT = 15


def watched_roots(cfg: Mapping[str, str], cwd: Optional[str]) -> List[str]:
    hh = cfg["HERMES_HOME"]
    roots = [os.path.join(hh, "config.yaml"), os.path.join(hh, "skills"), os.path.join(hh, "memories"),
             os.path.join(hh, "plans"), os.path.expanduser("~/.ssh/config")]
    if cwd:
        roots.append(os.path.join(cwd, ".hermes", "plans"))
    out: List[str] = []
    for r in roots:
        r = os.path.abspath(r)
        if r not in out:
            out.append(r)
    return out


def _iter_files(root: str):
    if os.path.isfile(root) and not os.path.islink(root):
        yield root
        return
    if not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for f in sorted(filenames):
            p = os.path.join(dirpath, f)
            if os.path.isfile(p) and not os.path.islink(p):
                yield p


def _sha(path: str) -> Optional[str]:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _scan(roots: List[str]) -> Dict[str, Dict]:
    idx: Dict[str, Dict] = {}
    for r in roots:
        for p in _iter_files(r):
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            idx[p] = {"sha256": _sha(p), "size": size}
    return idx


def _git(root: str, *args: str) -> Tuple[int, str]:
    try:
        cp = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=GIT_TIMEOUT,
                            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat"})
        return cp.returncode, cp.stdout
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def _repo_root(path: Optional[str]) -> Optional[str]:
    if not path or not os.path.isdir(path):
        return None
    rc, out = _git(path, "rev-parse", "--show-toplevel")
    return out.strip() if rc == 0 and out.strip() else None


def _repos(cfg: Mapping[str, str], cwd: Optional[str]) -> List[Dict[str, str]]:
    out, seen = [], set()
    for cand in (cwd, cfg.get("JUDGE_REPO_DIR")):
        root = _repo_root(cand)
        if not root or root in seen:
            continue
        seen.add(root)
        rc, head = _git(root, "rev-parse", "HEAD")
        _, status = _git(root, "status", "--porcelain=v1", "-uall")
        out.append({"root": root, "head": head.strip() if rc == 0 else "", "status": status})
    return out


def _now_iso(now: Optional[datetime] = None) -> str:
    return q.utc_now_iso(now)


def load_meta(snapdir: Path) -> Dict:
    try:
        return json.loads((Path(snapdir) / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_meta(snapdir: Path, meta: Dict) -> None:
    q.atomic_write_json(Path(snapdir) / "meta.json", meta)


def take(session: str, cwd: Optional[str], cfg: Mapping[str, str], root=None, late: bool = False,
         now: Optional[datetime] = None) -> Path:
    d = q.snapshot_dir(session, root)
    if (d / "meta.json").is_file():
        return d
    q.ensure_dir(d)
    roots = watched_roots(cfg, cwd)
    idx: Dict[str, Dict] = {}
    total = 0
    for r in roots:
        for p in _iter_files(r):
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            entry = {"sha256": _sha(p), "size": size}
            if size > MAX_FILE:
                entry["skipped"] = "too large"
            elif total + size > MAX_TOTAL:
                entry["skipped"] = "snapshot size cap"
            else:
                dst = d / "files" / p.lstrip("/")
                q.ensure_dir(dst.parent)
                try:
                    shutil.copyfile(p, dst)
                    os.chmod(dst, 0o600)
                    total += size
                except OSError as e:
                    entry["skipped"] = f"copy failed: {e.__class__.__name__}"
            idx[p] = entry
    q.atomic_write_json(d / "index.json", idx)
    meta = {"session": session, "started": _now_iso(now), "cwd": cwd or "", "roots": roots,
            "repos": _repos(cfg, cwd), "late": bool(late)}
    save_meta(d, meta)  # written last: its presence marks a complete snapshot
    return d


def record_event(snapdir: Path, tool: str, paths: List[str], status: str = "", now: Optional[datetime] = None,
                 call_id: Optional[str] = None, call_hash: Optional[str] = None) -> None:
    """Append one executed tool call. call_id (Hermes tool_call_id) / call_hash (lib/redact.call_hash) mark
    the call for matching with gate.log; no command text is stored."""
    q.ensure_dir(snapdir)
    ev = {"t": _now_iso(now), "tool": tool, "paths": paths, "status": status}
    if call_id:
        ev["call_id"] = str(call_id)[:200]
    if call_hash:
        ev["call_hash"] = call_hash
    line = json.dumps(ev) + "\n"
    fd = os.open(str(Path(snapdir) / "events.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def events(snapdir: Path) -> List[Dict]:
    out = []
    try:
        for line in (Path(snapdir) / "events.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out


# Hermes tools that write the agent's own state without a path argument (so no write_file/patch event):
# a change under HERMES_HOME/<dir> is the agent's when the session ran the tool (seen in events.jsonl if the
# post_tool_call matcher includes it, or in the session's agent.log tool_executor lines).
SELF_WRITE_TOOLS = {"memory": "memories", "skill_manage": "skills"}


def _key(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


# post_tool_call fires for every tool call, including ones that never ran: Hermes emits
# status="blocked" for a denied/timed-out approval or a block, "cancelled"/"aborted" for interrupted
# calls. Those must not count as the agent touching a file, nor as an executed call.
NOT_RUN_STATUSES = frozenset({"blocked", "denied", "rejected", "cancelled", "canceled", "aborted", "not_approved"})


def ran(ev) -> bool:
    """True unless the event's recorded status says the tool call did not run."""
    return str((ev or {}).get("status") or "").lower() not in NOT_RUN_STATUSES


def agent_touched(snapdir: Path, cfg: Optional[Mapping[str, str]] = None, until: Optional[datetime] = None,
                  tools=()) -> Tuple[set, List[str]]:
    """(paths, prefixes) touched by the agent's tool calls: every path recorded in events.jsonl with
    t <= *until* (all events when None), plus `HERMES_HOME/<dir>/` prefixes of SELF_WRITE_TOOLS that appear
    in those events or in *tools* (tool names from the Hermes log). Paths are realpath-normalised."""
    paths, names = set(), {str(t) for t in (tools or ())}
    for ev in events(snapdir):
        if not ran(ev):
            continue
        if until is not None:
            try:
                if q.parse_utc(str(ev.get("t") or "")) > until:
                    continue
            except ValueError:
                continue
        for p in ev.get("paths") or []:
            if isinstance(p, str) and p.strip():
                paths.add(_key(p))
        names.add(str(ev.get("tool") or ""))
    hh = (cfg or {}).get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    prefixes = [_key(os.path.join(hh, d)).rstrip("/") + "/" for n, d in SELF_WRITE_TOOLS.items() if n in names]
    return paths, prefixes


def attribute(changed, paths, prefixes=()) -> Tuple[List[str], List[str]]:
    """Split *changed* paths into (agent, others): a path is the agent's when its realpath is in *paths*
    (compared realpath-normalised) or starts with one of *prefixes*."""
    keys = {_key(p) for p in paths}
    agent: List[str] = []
    others: List[str] = []
    for p in changed:
        k = _key(p)
        (agent if k in keys or any(k.startswith(x) for x in prefixes) else others).append(p)
    return agent, others


def _load_index(snapdir: Path) -> Dict[str, Dict]:
    try:
        return json.loads((Path(snapdir) / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _file_changes(snapdir: Path, meta: Dict) -> List[Tuple[str, str]]:
    old = _load_index(snapdir)
    new = _scan(meta.get("roots") or [])
    out = []
    for p in sorted(set(old) | set(new)):
        if p not in new:
            out.append(("D", p))
        elif p not in old:
            out.append(("A", p))
        elif old[p].get("sha256") != new[p].get("sha256"):
            out.append(("M", p))
    return out


def _repo_changes(repo: Dict[str, str]) -> List[Tuple[str, str]]:
    root, head = repo.get("root"), repo.get("head")
    if not root or not os.path.isdir(root):
        return []
    out: Dict[str, str] = {}
    if head:
        rc, txt = _git(root, "diff", "--name-status", "--no-renames", head)
        if rc == 0:
            for line in txt.splitlines():
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    out[os.path.join(root, parts[1])] = parts[0][:1]
    start_untracked = {ln[3:] for ln in (repo.get("status") or "").splitlines() if ln.startswith("?? ")}
    rc, txt = _git(root, "ls-files", "--others", "--exclude-standard")
    if rc == 0:
        for rel in txt.splitlines():
            if rel and rel not in start_untracked:
                out[os.path.join(root, rel)] = "A"
    return sorted((s, p) for p, s in out.items())


def changed_files(snapdir: Path, cfg: Optional[Mapping[str, str]] = None) -> List[Tuple[str, str]]:
    meta = load_meta(snapdir)
    if not meta:
        return []
    seen: Dict[str, str] = {}
    for s, p in _file_changes(snapdir, meta):
        seen[p] = s
    for repo in meta.get("repos") or []:
        for s, p in _repo_changes(repo):
            seen.setdefault(p, s)
    return sorted(((s, p) for p, s in seen.items()), key=lambda x: x[1])


def _read_lines(path: Path) -> Optional[List[str]]:
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace").splitlines(keepends=True)


def _stat(a: Optional[List[str]], b: Optional[List[str]]) -> str:
    if a is None or b is None:
        return "binary"
    plus = minus = 0
    for line in difflib.unified_diff(a, b, n=0):
        if line.startswith("+") and not line.startswith("+++"):
            plus += 1
        elif line.startswith("-") and not line.startswith("---"):
            minus += 1
    return f"+{plus} -{minus}"


def _late_note(path: str, until: Optional[datetime]) -> str:
    if until is None:
        return ""
    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
    except OSError:
        return ""
    if mtime <= until:
        return ""
    return (f"# NOTE: {path} was modified after the review window ended (mtime {q.utc_now_iso(mtime)}, window "
            f"end {q.utc_now_iso(until)}); what follows shows it as observed at collection time\n")


def diff_text(snapdir: Path, cfg: Optional[Mapping[str, str]] = None, sensitive: bool = True,
              include=None, until: Optional[datetime] = None) -> str:
    """Watched files vs snapshot + repo changes since the session-start HEAD.

    sensitive=True: `git diff --stat`-style lines only (path | +N -M), never file contents.
    include: only these paths (realpath-compared); None = every changed path.
    until: end of the review window; files modified after it get a NOTE line (their content is as observed
    at collection time)."""
    snapdir = Path(snapdir)
    meta = load_meta(snapdir)
    if not meta:
        return "# no snapshot for this session (on_session_start hook not installed or did not run)\n"
    keys = None if include is None else {_key(p) for p in include}

    def want(p: str) -> bool:
        return keys is None or _key(p) in keys

    idx = _load_index(snapdir)
    hdr = [f"# local diff vs snapshot taken {meta.get('started')} (session {meta.get('session')})"]
    if meta.get("late"):
        hdr.append("# NOTE: late snapshot (taken mid-session); earlier changes are not visible")
    hdr.append("# mode: " + ("stat only (data_class=sensitive)" if sensitive else "unified diff"))
    if keys is not None:
        hdr.append(f"# restricted to paths attributed to the agent ({len(keys)} candidate path(s))")
    out = ["\n".join(hdr) + "\n"]
    for status, p in _file_changes(snapdir, meta):
        if not want(p):
            continue
        snap = snapdir / "files" / p.lstrip("/")
        out.append(_late_note(p, until))
        if idx.get(p, {}).get("skipped"):
            out.append(f"{status} {p} | snapshot skipped ({idx[p]['skipped']}); content diff unavailable\n")
            continue
        a = _read_lines(snap) if status != "A" else []
        b = _read_lines(Path(p)) if status != "D" else []
        if sensitive:
            out.append(f"{status} {p} | {_stat(a, b)}\n")
            continue
        if a is None or b is None:
            out.append(f"Binary files {p} differ\n")
            continue
        out.extend(difflib.unified_diff(a, b, fromfile=f"a{p}", tofile=f"b{p}"))
        if out and not out[-1].endswith("\n"):
            out.append("\n")
    for repo in meta.get("repos") or []:
        root, head = repo.get("root"), repo.get("head")
        if not root or not head or not os.path.isdir(root):
            continue
        changes = _repo_changes(repo)
        rels: List[str] = []
        if keys is not None:
            mine = [p for _, p in changes if want(p)]
            if not mine:
                continue
            rels = [os.path.relpath(p, root) for p in mine]
        pre = [ln for ln in (repo.get("status") or "").splitlines() if ln.strip()]
        out.append(f"\n# repo {root}: changes since session-start HEAD {head[:12]}\n")
        if pre:
            out.append(f"# {len(pre)} path(s) were already modified/untracked at session start:\n")
            out.extend(f"#   {ln}\n" for ln in pre[:50])
        out.extend(_late_note(p, until) for _, p in changes if want(p))
        args = ["diff", "--stat", head] if sensitive else ["diff", "--no-color", "--no-ext-diff", head]
        if rels:
            args += ["--", *rels]
        rc, txt = _git(root, *args)
        out.append(txt if rc == 0 else f"# git diff failed (rc={rc})\n")
        new = [p for s, p in changes if s == "A" and want(p) and not _tracked(root, p)]
        for p in new:
            lines = _read_lines(Path(p))
            if sensitive or lines is None:
                out.append(f"A {p} (untracked) | {'binary' if lines is None else f'+{len(lines)}'}\n")
            else:
                out.extend(difflib.unified_diff([], lines, fromfile="/dev/null", tofile=f"b{p}"))
                if out and not out[-1].endswith("\n"):
                    out.append("\n")
    return "".join(out)


def stat_lines(snapdir: Path, cfg: Optional[Mapping[str, str]] = None, paths=()) -> List[str]:
    """`<status> <path> | +N -M` for each of *paths* (as returned by changed_files). Never file contents."""
    snapdir = Path(snapdir)
    meta = load_meta(snapdir)
    if not meta:
        return []
    idx = _load_index(snapdir)
    files = {p: s for s, p in _file_changes(snapdir, meta)}
    in_repo: Dict[str, Tuple[str, Dict]] = {}
    for repo in meta.get("repos") or []:
        for s, p in _repo_changes(repo):
            in_repo.setdefault(p, (s, repo))
    out = []
    for p in paths:
        if p in files:
            s = files[p]
            if idx.get(p, {}).get("skipped"):
                stat = f"snapshot skipped ({idx[p]['skipped']})"
            else:
                stat = _stat(_read_lines(snapdir / "files" / p.lstrip("/")) if s != "A" else [],
                             _read_lines(Path(p)) if s != "D" else [])
        elif p in in_repo:
            s, repo = in_repo[p]
            root, head = repo.get("root") or "", repo.get("head") or ""
            stat = "?"
            rc, txt = _git(root, "diff", "--numstat", head, "--", os.path.relpath(p, root)) if head else (1, "")
            parts = txt.split("\t") if rc == 0 and txt.strip() else []
            if len(parts) >= 2:
                stat = "binary" if parts[0] == "-" else f"+{parts[0]} -{parts[1]}"
            elif s == "A":
                lines = _read_lines(Path(p))
                stat = "binary" if lines is None else f"+{len(lines)} (untracked)"
        else:
            s, stat = "?", "?"
        out.append(f"{s} {p} | {stat}")
    return out


def _tracked(root: str, path: str) -> bool:
    rc, _ = _git(root, "ls-files", "--error-unmatch", "--", os.path.relpath(path, root))
    return rc == 0
