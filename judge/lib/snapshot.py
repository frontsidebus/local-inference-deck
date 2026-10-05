"""judge/lib/snapshot.py: watched-path snapshots taken at session start, and diffs against them.

Layout of snapshots/<session>/ (dir 700, files 600):
    meta.json      {"session", "started", "cwd", "roots": [...], "repos": [{"root","head","status"}],
                    "late": bool, "last_end": "...Z" | absent,
                    "dir_roots": [{"root", "files", "truncated", "too_large"}],   opted-in non-git dirs
                    "skipped_roots": [{"root", "reason"}], "snapshot_caps": {"max_files", "max_bytes"},
                    "noise_globs": [...]}
    index.json     {abs_path: {"sha256", "size"} | {"skipped": reason, "size"}}
    files/<abs path without leading />   copies of watched files
    events.jsonl   one line per executed tool call: {"t","tool","paths","status","call_id"?,"call_hash"?}

Watched: $HERMES_HOME/{config.yaml,skills/,memories/,plans/}, ~/.ssh/config, <cwd>/.hermes/plans/, the
git repos containing cwd, JUDGE_REPO_DIR and each JUDGE_INFRA_REPOS entry (HEAD + status recorded; diffs come
from git), and every JUDGE_INFRA_REPOS entry that is NOT a git repo (a "dir root", e.g. a sandbox): copied like
the watched roots but capped at JUDGE_SNAPSHOT_MAX_FILES files (default 2000; the rest is not indexed and the
root is marked truncated: additions there are not detected) and JUDGE_SNAPSHOT_MAX_BYTES per file (default
1 MB; larger files are hashed, not copied). .git, node_modules, __pycache__, .venv (SKIP_DIRS) are skipped.

Noise: Hermes' own bookkeeping files (NOISE_GLOBS, plus `$HERMES_HOME/*.lock` and JUDGE_NOISE_GLOBS) are never
indexed, diffed or attributed: they change on read-only turns too.

    watched_roots(cfg, cwd) -> list[str]
    noise_globs(cfg, meta=None) -> list[str];  is_noise(path, globs) -> bool
    dir_roots(cfg, cwd) -> (list[str] non-git JUDGE_INFRA_REPOS dirs, list[{"root","reason"}] skipped)
    take(session, cwd, cfg, root=None, late=False, now=None) -> Path      idempotent: returns existing dir
    load_meta(snapdir) -> dict;  save_meta(snapdir, meta)
    record_event(snapdir, tool, paths, status)
    events(snapdir) -> list[dict]
    changed_files(snapdir, cfg) -> list[(status, path)]   status in A|M|D ; includes repo changes
    agent_touched(snapdir, cfg, until=None, tools=(), commands=None) -> (paths, prefixes)
                                                          what the agent's tool calls touched (incl. cp/mv/install
                                                          destinations of its terminal commands, #45)
    copy_targets(command, cwd) -> (paths, prefixes)       files a cp/mv/install in *command* created
    write_targets(command, cwd) -> (paths, prefixes)      copy_targets + output redirection and tee targets
    confirmed_writes(snapdir, cfg, until=None, commands=None) -> (paths, prefixes)
                                                          what the session's calls wrote AS A TARGET
                                                          (write_file/patch, write_targets): confirms request
                                                          changed_paths in the collector
    terminal_commands(hermes_home, session) -> {call_id: (command, workdir)}   from state.db, attribution only
    attribute(changed, paths, prefixes=()) -> (agent, others)                split changed paths by who touched them
    diff_text(snapdir, cfg, sensitive, include=None, until=None) -> str
                                                          unified diff (infra) or stat summary (sensitive),
                                                          optionally restricted to *include* paths
    stat_lines(snapdir, cfg, paths) -> list[str]          "<status> <path> | +N -M" per path, never content

Attribution: snapshot diffs show every change to a watched path, whoever made it (the human, other tools,
a `git pull`). A changed path counts as the agent's only when one of the session's recorded tool events
(events.jsonl: write_file/patch targets, path-like tokens of terminal commands) names it, when it lies
under a HERMES_HOME dir written by a Hermes self-write tool the session ran (SELF_WRITE_TOOLS), or (#45) when
it is a file a `cp`/`mv`/`install` of one of the session's terminal commands created (copy_targets; the
command text is read from Hermes' state.db by tool_call_id, in memory only: it never reaches a bundle).
"""
from __future__ import annotations

import difflib
import fnmatch
import glob as _glob
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from . import queue as q

MAX_FILE = 2 * 1024 * 1024
MAX_TOTAL = 100 * 1024 * 1024
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".cache"}
GIT_TIMEOUT = 15
DEFAULT_DIR_MAX_FILES = 2000
DEFAULT_DIR_MAX_BYTES = 1024 * 1024
RESCAN_FACTOR = 4  # a dir root is rescanned up to RESCAN_FACTOR * max_files files at diff time

# Hermes bookkeeping that changes on every turn (skill usage counters, lock files, the curator's ledger and
# backups): never agent work. fnmatch patterns over absolute paths (`*` crosses `/`). `$HERMES_HOME/*.lock`
# is added by noise_globs(); JUDGE_NOISE_GLOBS (space- or comma-separated) extends the list.
NOISE_GLOBS = ("*/skills/.usage.json", "*/skills/.locks/*", "*/skills/.curator_ledger.jsonl",
               "*/skills/.curator_backups/*")


def _setting(cfg: Optional[Mapping[str, str]], key: str) -> str:
    v = (cfg or {}).get(key)
    return str(v) if v not in (None, "") else os.environ.get(key, "")


def _int_setting(cfg: Optional[Mapping[str, str]], key: str, default: int) -> int:
    try:
        v = int(float(_setting(cfg, key) or default))
        return v if v > 0 else default
    except ValueError:
        return default


def snapshot_caps(cfg: Optional[Mapping[str, str]] = None) -> Dict[str, int]:
    """Caps for dir roots: JUDGE_SNAPSHOT_MAX_FILES (per root), JUDGE_SNAPSHOT_MAX_BYTES (per file)."""
    return {"max_files": _int_setting(cfg, "JUDGE_SNAPSHOT_MAX_FILES", DEFAULT_DIR_MAX_FILES),
            "max_bytes": _int_setting(cfg, "JUDGE_SNAPSHOT_MAX_BYTES", DEFAULT_DIR_MAX_BYTES)}


def noise_globs(cfg: Optional[Mapping[str, str]] = None, meta: Optional[Mapping] = None) -> List[str]:
    """NOISE_GLOBS + `$HERMES_HOME/*.lock` + JUDGE_NOISE_GLOBS (+ the globs recorded in a snapshot's meta)."""
    out = list(NOISE_GLOBS)
    hh = (cfg or {}).get("HERMES_HOME") or os.environ.get("HERMES_HOME") or "~/.hermes"
    for h in {os.path.abspath(os.path.expanduser(hh)), _key(hh)}:
        out.append(h.rstrip("/") + "/*.lock")
    out += [g for g in re.split(r"[\s,]+", _setting(cfg, "JUDGE_NOISE_GLOBS")) if g]
    out += [g for g in ((meta or {}).get("noise_globs") or []) if isinstance(g, str) and g]
    return list(dict.fromkeys(out))


def is_noise(path: str, globs) -> bool:
    if not isinstance(path, str) or not path:
        return False
    cands = {os.path.abspath(os.path.expanduser(path)), _key(path)}
    return any(fnmatch.fnmatchcase(c, g) for c in cands for g in globs)


def dir_roots(cfg: Optional[Mapping[str, str]], cwd: Optional[str] = None) -> Tuple[List[str], List[Dict]]:
    """JUDGE_INFRA_REPOS entries that are plain directories (not inside a git repo): snapshotted with caps.
    Returns (roots, skipped) where skipped = [{"root", "reason"}] (missing, not a dir, or already covered)."""
    roots: List[str] = []
    skipped: List[Dict] = []
    covered = [os.path.realpath(r) for r in watched_roots(cfg or {"HERMES_HOME": "~/.hermes"}, cwd)]
    for raw in _setting(cfg, "JUDGE_INFRA_REPOS").split():
        r = os.path.realpath(os.path.expanduser(raw))
        if not os.path.exists(r):
            skipped.append({"root": r, "reason": "missing"})
        elif not os.path.isdir(r):
            skipped.append({"root": r, "reason": "not a directory"})
        elif _repo_root(r):
            continue  # a git repo: diffed via git (see _repos)
        elif r in roots or any(r == c or r.startswith(c.rstrip("/") + "/") for c in covered):
            continue
        else:
            roots.append(r)
    return roots, skipped


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


def _iter_files(root: str, noise=()):
    if os.path.isfile(root) and not os.path.islink(root):
        if not is_noise(root, noise):
            yield root
        return
    if not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for f in sorted(filenames):
            p = os.path.join(dirpath, f)
            if os.path.isfile(p) and not os.path.islink(p) and not is_noise(p, noise):
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


def _scan(roots: List[str], noise=(), limit: Optional[int] = None) -> Dict[str, Dict]:
    """{path: {sha256, size}} of the files under *roots* (noise excluded); at most *limit* files per root."""
    idx: Dict[str, Dict] = {}
    for r in roots:
        n = 0
        for p in _iter_files(r, noise):
            if limit is not None and n >= limit:
                break
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            idx[p] = {"sha256": _sha(p), "size": size}
            n += 1
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
    for cand in (cwd, cfg.get("JUDGE_REPO_DIR"), *_setting(cfg, "JUDGE_INFRA_REPOS").split()):
        cand = os.path.expanduser(cand) if cand else cand
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
    noise = noise_globs(cfg)
    caps = snapshot_caps(cfg)
    droots, skipped_roots = dir_roots(cfg, cwd)
    dinfo: List[Dict] = []
    idx: Dict[str, Dict] = {}
    total = 0
    plan = [(r, None, MAX_FILE) for r in roots] + [(r, caps["max_files"], caps["max_bytes"]) for r in droots]
    for r, max_files, max_bytes in plan:
        n = too_large = 0
        truncated = False
        for p in _iter_files(r, noise):
            if max_files is not None and n >= max_files:
                truncated = True
                break
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            n += 1
            entry = {"sha256": _sha(p), "size": size}
            if size > max_bytes:
                entry["skipped"] = "too large"
                too_large += 1
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
        if max_files is not None:
            dinfo.append({"root": r, "files": n, "truncated": truncated, "too_large": too_large})
    q.atomic_write_json(d / "index.json", idx)
    meta = {"session": session, "started": _now_iso(now), "cwd": cwd or "", "roots": roots,
            "repos": _repos(cfg, cwd), "late": bool(late), "dir_roots": dinfo, "skipped_roots": skipped_roots,
            "snapshot_caps": caps, "noise_globs": noise}
    save_meta(d, meta)  # written last: its presence marks a complete snapshot
    return d


def record_event(snapdir: Path, tool: str, paths: List[str], status: str = "", now: Optional[datetime] = None,
                 call_id: Optional[str] = None, call_hash: Optional[str] = None,
                 command: Optional[str] = None) -> None:
    """Append one executed tool call. call_id (Hermes tool_call_id) / call_hash (lib/redact.call_hash) mark
    the call for matching with gate.log; *command* is lib/toolcalls.command_word (an allowlisted program
    name or "(other)", never arguments). No command text is stored."""
    q.ensure_dir(snapdir)
    ev = {"t": _now_iso(now), "tool": tool, "paths": paths, "status": status}
    if call_id:
        ev["call_id"] = str(call_id)[:200]
    if call_hash:
        ev["call_hash"] = call_hash
    if command:
        ev["command"] = command
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
                  tools=(), commands=None) -> Tuple[set, List[str]]:
    """(paths, prefixes) touched by the agent's tool calls: every path recorded in events.jsonl with
    t <= *until* (all events when None), plus `HERMES_HOME/<dir>/` prefixes of SELF_WRITE_TOOLS that appear
    in those events or in *tools* (tool names from the Hermes log), plus (#45) the files created by a
    cp/mv/install of the session's terminal calls (copy_targets of the command; *commands* maps tool_call_id ->
    (command, workdir), default: read from Hermes' state.db). Paths are realpath-normalised."""
    paths, names = set(), {str(t) for t in (tools or ())}
    meta = load_meta(snapdir)
    noise = noise_globs(cfg, meta)
    hh = (cfg or {}).get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    prefixes: List[str] = []
    term_ids: List[Tuple[str, str]] = []
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
            if isinstance(p, str) and p.strip() and not is_noise(p, noise):
                paths.add(_key(p))
        names.add(str(ev.get("tool") or ""))
        if ev.get("tool") == "terminal" and ev.get("call_id"):
            term_ids.append((str(ev["call_id"]), str(ev.get("cwd") or "")))
    if term_ids:
        if commands is None:
            commands = terminal_commands(hh, str(meta.get("session") or ""))
        for cid, ev_cwd in term_ids:
            got = (commands or {}).get(cid)
            if not got:
                continue
            command, workdir = got if isinstance(got, tuple) else (got, "")
            try:  # best effort: attribution must never break a hook or a bundle
                cps, cpre = copy_targets(command, workdir or ev_cwd or str(meta.get("cwd") or "") or "/")
            except Exception:
                continue
            paths |= {p for p in cps if not is_noise(p, noise)}
            prefixes += [x for x in cpre if x not in prefixes]
    prefixes += [_key(os.path.join(hh, d)).rstrip("/") + "/" for n, d in SELF_WRITE_TOOLS.items() if n in names]
    return paths, prefixes


WRITE_TOOLS = ("write_file", "patch")


def confirmed_writes(snapdir: Path, cfg: Optional[Mapping[str, str]] = None, until: Optional[datetime] = None,
                     commands=None) -> Tuple[set, List[str]]:
    """(paths, prefixes) the session's executed tool calls (t <= *until*) WROTE as a target: write_file/patch
    paths, and write_targets (cp/mv/install destinations, output redirections, `tee` operands) of its terminal
    calls (*commands*: tool_call_id -> (command, workdir), default read from Hermes' state.db, in memory only).
    Unlike agent_touched, a path a terminal command merely names (an argument, a file it reads) is NOT here:
    this is what confirms a request's changed_paths in the collector (together with the snapshot changes
    agent_touched attributes). Realpath-normalised; noise excluded; best effort, never raises on a command."""
    meta = load_meta(snapdir)
    noise = noise_globs(cfg, meta)
    hh = (cfg or {}).get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    paths: set = set()
    prefixes: List[str] = []
    term_ids: List[Tuple[str, str]] = []
    for ev in events(snapdir):
        if not ran(ev):
            continue
        if until is not None:
            try:
                if q.parse_utc(str(ev.get("t") or "")) > until:
                    continue
            except ValueError:
                continue
        if ev.get("tool") in WRITE_TOOLS:
            paths |= {_key(p) for p in ev.get("paths") or [] if isinstance(p, str) and p.strip()
                      and not is_noise(p, noise)}
        elif ev.get("tool") == "terminal" and ev.get("call_id"):
            term_ids.append((str(ev["call_id"]), str(ev.get("cwd") or "")))
    if term_ids:
        if commands is None:
            commands = terminal_commands(hh, str(meta.get("session") or ""))
        for cid, ev_cwd in term_ids:
            got = (commands or {}).get(cid)
            if not got:
                continue
            command, workdir = got if isinstance(got, tuple) else (got, "")
            try:
                wp, wpre = write_targets(command, workdir or ev_cwd or str(meta.get("cwd") or "") or "/")
            except Exception:
                continue
            paths |= {p for p in wp if not is_noise(p, noise)}
            prefixes += [x for x in wpre if x not in prefixes]
    return paths, prefixes


# ---------------------------------------------------------------- cp/mv/install destinations (#45)
COPY_PROGRAMS = ("cp", "mv", "install")
COPY_WALK_MAX = 5000            # files walked per recursive source
_SEPARATORS = {"&&", "||", ";", "|", "&", "|&", ";;", "(", ")", "{", "}", "!"}
_REDIRECTS = {">", ">>", "<", "<<", "<<<", ">&", "&>", "&>>", ">|", "<>"}
_WRAPPERS = {"sudo", "command", "builtin", "exec", "nohup", "time", "env", "doas"}
_ARG_OPTS = {"cp": {"-S", "--suffix", "-t", "--target-directory"},
             "mv": {"-S", "--suffix", "-t", "--target-directory"},
             "install": {"-S", "--suffix", "-t", "--target-directory", "-m", "--mode", "-o", "--owner", "-g",
                         "--group", "--strip-program", "-Z", "--context"}}


def _simple_commands(command: str) -> List[List[str]]:
    """Shell words of each simple command in *command* (best effort: no expansion; a line that does not
    tokenize is skipped). Redirections and their targets are dropped."""
    return [w for w, _ in _parsed_commands(command) if w]


_OUT_REDIRECTS = {">", ">>", "&>", "&>>", ">|", "<>", ">&"}


def _parsed_commands(command: str) -> List[Tuple[List[str], List[str]]]:
    """(words, output redirection targets) of each simple command in *command*. A target is the word after
    `>`, `>>`, `>|`, `&>`, `&>>`, `<>`, `N>`, `N>>` (and `>&` / `N>&` when it is not an fd number or `-`);
    input redirections (`<`, `<<`, `<<<`) are dropped like before."""
    out: List[Tuple[List[str], List[str]]] = []
    for line in str(command or "").splitlines():
        try:
            lex = shlex.shlex(line, posix=True, punctuation_chars=True)
            lex.whitespace_split = True
            lex.commenters = "#"
            toks = list(lex)
        except ValueError:
            continue
        cur: List[str] = []
        redirs: List[str] = []
        skip = None
        for t in toks:
            if skip is not None:
                if skip and not (re.fullmatch(r"\d+|-", t) and skip == "dup"):
                    redirs.append(t)
                skip = None
                continue
            if t in _SEPARATORS:
                if cur or redirs:
                    out.append((cur, redirs))
                cur, redirs = [], []
            elif t in _REDIRECTS or re.fullmatch(r"\d+>>?|\d+<|\d*>&\d*", t):
                fd_dup = t.endswith(">&")
                if cur and cur[-1].isdigit():  # `2>/dev/null` tokenizes as `2`, `>`, `/dev/null`
                    cur.pop()
                if re.fullmatch(r"\d*>&\d+", t):  # `2>&1` in one token: no target word follows
                    continue
                skip = ("dup" if fd_dup else "file") if (t in _OUT_REDIRECTS or ">" in t) else ""
            else:
                cur.append(t)
        if cur or redirs:
            out.append((cur, redirs))
    return out


def _abs_from(path: str, cwd: str) -> str:
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(cwd or "/", p)
    return os.path.normpath(p)


def _parse_copy(prog: str, args: List[str]) -> Optional[Tuple[str, List[str], bool, bool]]:
    """(dest, sources, dest_is_dir_by_syntax, recursive) of a cp/mv/install argv, or None (no destination,
    `install -d`, unresolvable words)."""
    target = None
    no_target_dir = recursive = False
    operands: List[str] = []
    i = 0
    opts_done = False
    while i < len(args):
        a = args[i]
        i += 1
        if opts_done or not a.startswith("-") or a == "-":
            operands.append(a)
            continue
        if a == "--":
            opts_done = True
            continue
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if name in ("--target-directory",):
                if eq:
                    target = val
                elif i < len(args):
                    target = args[i]
                    i += 1
            elif name == "--no-target-directory":
                no_target_dir = True
            elif name in ("--recursive", "--archive"):
                recursive = True
            elif name == "--directory" and prog == "install":
                return None
            elif name in _ARG_OPTS.get(prog, set()) and not eq:
                i += 1
            continue
        # short option cluster, e.g. -rf, -t DIR, -tDIR, -m 0644
        for j, ch in enumerate(a[1:], 1):
            opt = "-" + ch
            if ch == "d" and prog == "install":
                return None
            if ch in "rRa" and prog == "cp":
                recursive = True
            if ch == "T":
                no_target_dir = True
            if opt in _ARG_OPTS.get(prog, set()):
                val = a[j + 1:]
                if not val and i < len(args):
                    val = args[i]
                    i += 1
                if opt == "-t":
                    target = val
                break
    if prog == "mv":
        recursive = True
    words = operands + ([target] if target else [])
    if any("$" in w or "`" in w for w in words):
        return None
    if target:
        return (target, operands, True, recursive) if operands else None
    if len(operands) < 2:
        return None
    dest = operands[-1]
    by_syntax = (dest.endswith("/") or len(operands) > 2) and not no_target_dir
    return dest, operands[:-1], by_syntax, recursive


def _walk(root: str, cap: int) -> List[str]:
    out: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for f in sorted(filenames):
            out.append(os.path.relpath(os.path.join(dirpath, f), root))
            if len(out) >= cap:
                return out
    return out


def copy_targets(command: str, cwd: str) -> Tuple[set, List[str]]:
    """(paths, prefixes) of the files that the cp/mv/install invocations in *command* create (#45). Best
    effort and attribution only: `cd DIR` is followed, `-t DIR` and trailing-slash/multi-source directory
    destinations are understood, a recursive copy maps every file now under the source to the destination
    (`D/rel` and `D/<src name>/rel`), a glob source is matched against the destination dir, and a moved
    directory (source gone) gives a `D/<src name>/` prefix (`D/` when it was a rename). Words with `$` or
    backticks are not resolved. Nothing is executed; the command text is never stored."""
    return _targets(command, cwd, writes=False)


def write_targets(command: str, cwd: str) -> Tuple[set, List[str]]:
    """(paths, prefixes) *command* writes as a destination: copy_targets (cp/mv/install, #45) plus the targets
    of output redirections (`>`, `>>`, `&>`, `N>`, ...) and `tee` operands. Words with `$` or backticks are
    not resolved; `/dev/*` targets are ignored. Used to CONFIRM a request's changed_paths (collector); nothing
    is executed and the command text is never stored."""
    return _targets(command, cwd, writes=True)


def _targets(command: str, cwd: str, writes: bool) -> Tuple[set, List[str]]:
    paths: set = set()
    prefixes: List[str] = []
    cur = cwd or "/"
    for words, redirs in _parsed_commands(command):
        if writes:
            for t in redirs:
                if t and "$" not in t and "`" not in t:
                    a = _abs_from(t, cur)
                    if not a.startswith("/dev/"):
                        paths.add(_key(a))
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words = words[1:]
        while words and os.path.basename(words[0]) in _WRAPPERS:
            words = words[1:]
            while words and (words[0].startswith("-") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0])):
                flag = words.pop(0)
                if flag in ("-u", "-g") and words:
                    words.pop(0)
        if not words:
            continue
        prog = os.path.basename(words[0])
        if prog in ("cd", "pushd"):
            if len(words) > 1 and "$" not in words[1] and words[1] != "-":
                cur = _abs_from(words[1], cur)
            continue
        if writes and prog == "tee":
            paths |= {_key(_abs_from(a, cur)) for a in words[1:]
                      if not a.startswith("-") and "$" not in a and "`" not in a
                      and not _abs_from(a, cur).startswith("/dev/")}
            continue
        if prog not in COPY_PROGRAMS:
            continue
        parsed = _parse_copy(prog, words[1:])
        if not parsed:
            continue
        dest, sources, dir_syntax, recursive = parsed
        d = _abs_from(dest, cur)
        dest_dir = dir_syntax or os.path.isdir(d)
        if not dest_dir:
            paths.add(_key(d))
        for src in sources:
            s_abs = _abs_from(src, cur)
            base = os.path.basename(s_abs.rstrip("/"))
            if any(c in base for c in "*?["):
                srcs = sorted(_glob.glob(s_abs))
                if os.path.isdir(d):
                    for name in sorted(os.listdir(d)):
                        if fnmatch.fnmatchcase(name, base):
                            full = os.path.join(d, name)
                            if os.path.isdir(full) and recursive:
                                x = _key(full).rstrip("/") + "/"
                                if x not in prefixes:
                                    prefixes.append(x)
                            else:
                                paths.add(_key(full))
                srcs = [x for x in srcs if os.path.isdir(x)] if recursive else []
            else:
                srcs = [s_abs]
                if dest_dir:
                    paths.add(_key(os.path.join(d, base)))
            for sp in srcs:
                b = os.path.basename(sp.rstrip("/"))
                if recursive and os.path.isdir(sp):
                    for rel in _walk(sp, COPY_WALK_MAX):
                        paths.add(_key(os.path.join(d, b, rel)))
                        if not dir_syntax:
                            paths.add(_key(os.path.join(d, rel)))
                elif recursive and prog == "mv" and not os.path.exists(sp):
                    gone = [_key(os.path.join(d, b)).rstrip("/") + "/"]
                    if not dir_syntax and len(sources) == 1 and not os.path.exists(os.path.join(d, b)):
                        gone.append(_key(d).rstrip("/") + "/")
                    prefixes += [x for x in gone if x not in prefixes]
    return paths, prefixes


def terminal_commands(hermes_home, session: str) -> Dict[str, Tuple[str, str]]:
    """{tool_call_id: (command, workdir)} for the terminal calls of *session* in Hermes' state.db (opened
    read-only; empty when it is missing). Used in memory for attribution only (copy_targets): callers must
    never write these strings anywhere."""
    p = Path(str(hermes_home or "")) / "state.db"
    out: Dict[str, Tuple[str, str]] = {}
    if not session or not p.is_file():
        return out
    con = None
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=2)
        con.execute("PRAGMA query_only = ON")
        rows = con.execute("SELECT tool_calls FROM messages WHERE session_id = ? AND tool_calls IS NOT NULL",
                           (session,)).fetchall()
    except sqlite3.Error:
        return out
    finally:
        if con is not None:
            con.close()
    for (raw,) in rows:
        try:
            calls = json.loads(raw)
        except (TypeError, ValueError):
            continue
        for c in calls if isinstance(calls, list) else []:
            if not isinstance(c, dict):
                continue
            fn = c.get("function") if isinstance(c.get("function"), dict) else {}
            if fn.get("name") != "terminal":
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(args, dict) or not isinstance(args.get("command"), str):
                continue
            wd = args.get("workdir") if isinstance(args.get("workdir"), str) else ""
            for key in ("id", "call_id"):
                if isinstance(c.get(key), str) and c[key]:
                    out[c[key]] = (args["command"], wd)
    return out


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


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _file_changes(snapdir: Path, meta: Dict, cfg: Optional[Mapping[str, str]] = None) -> List[Tuple[str, str]]:
    """(status, path) for watched files and dir roots vs the index; noise excluded. In a dir root that was
    truncated at snapshot time additions cannot be told apart from unindexed files, so only M/D are reported."""
    noise = noise_globs(cfg, meta)
    old = {p: v for p, v in _load_index(snapdir).items() if not is_noise(p, noise)}
    new = _scan(meta.get("roots") or [], noise)
    dinfo = [x for x in (meta.get("dir_roots") or []) if isinstance(x, dict) and x.get("root")]
    caps = meta.get("snapshot_caps") or {}
    limit = int(caps.get("max_files") or DEFAULT_DIR_MAX_FILES) * RESCAN_FACTOR
    new.update(_scan([x["root"] for x in dinfo], noise, limit=limit))
    truncated = [x["root"] for x in dinfo if x.get("truncated")]
    out = []
    for p in sorted(set(old) | set(new)):
        if p not in new:
            # not seen by the (possibly capped) rescan: deleted, or just beyond the rescan cap
            if os.path.isfile(p) and not os.path.islink(p):
                if _sha(p) != old[p].get("sha256"):
                    out.append(("M", p))
            else:
                out.append(("D", p))
        elif p not in old:
            if not any(_under(p, r) for r in truncated):
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
    noise = noise_globs(cfg, meta)
    seen: Dict[str, str] = {}
    for s, p in _file_changes(snapdir, meta, cfg):
        seen[p] = s
    for repo in meta.get("repos") or []:
        for s, p in _repo_changes(repo):
            if not is_noise(p, noise):
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


def _counts(a: Optional[List[str]], b: Optional[List[str]]) -> Optional[Tuple[int, int]]:
    """(added, removed) lines between *a* and *b*; None when either side is binary."""
    if a is None or b is None:
        return None
    plus = minus = 0
    for line in difflib.unified_diff(a, b, n=0):
        if line.startswith("+") and not line.startswith("+++"):
            plus += 1
        elif line.startswith("-") and not line.startswith("---"):
            minus += 1
    return plus, minus


def _stat(a: Optional[List[str]], b: Optional[List[str]]) -> str:
    c = _counts(a, b)
    return "binary" if c is None else f"+{c[0]} -{c[1]}"


STATUS_WORDS = {"A": "added", "M": "modified", "D": "deleted"}
WITHHELD_PREFIX = "# content withheld (data_class=sensitive): "
# #43: per-path withholding inside an infra (content) diff
WITHHELD_PREFIXES = {
    "sensitive": "# content withheld (sensitive path in an infra bundle; name not shown): ",
    "secret": "# content withheld (secret-shaped path): ",
    "scratch": "# content withheld (agent scratch/cache path: metadata only): ",
}


def withheld_line(path: str, status: str = "M", counts: Optional[Tuple[int, int]] = None, why: str = "",
                  kind: str = "") -> str:
    """The explicit marker for a change whose content a bundle does not show (a stat, never content):
    `# content withheld (data_class=sensitive): <path> — N lines changed (+a/-b) [modified]`. *kind*
    (sensitive | secret | scratch) selects the per-path marker of an infra bundle (#43); *path* is then the
    label to show (a placeholder for a sensitive path)."""
    word = STATUS_WORDS.get(status, status)
    if why:
        what = why
    elif counts is None:
        what = "binary file changed"
    else:
        what = f"{counts[0] + counts[1]} lines changed (+{counts[0]}/-{counts[1]})"
    prefix = WITHHELD_PREFIXES.get(kind, WITHHELD_PREFIX)
    return f"{prefix}{path} \u2014 {what} [{word}]\n"


def _literal(rels: List[str]) -> List[str]:
    """Pathspecs that match exactly these paths: a name with `*`, `?` or `[` must never widen the diff."""
    return [":(literal)" + r for r in rels]


def _numstat(root: str, head: str, rels: List[str]) -> List[Tuple[str, Optional[Tuple[int, int]]]]:
    """[(abs path, (added, removed) | None for binary)] from `git diff --numstat <head> [-- rels]`."""
    args = ["diff", "--numstat", "--no-renames", head] + (["--", *_literal(rels)] if rels else [])
    rc, txt = _git(root, *args)
    out = []
    if rc != 0:
        return out
    for line in txt.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        c = None if parts[0] == "-" else (int(parts[0]), int(parts[1]))
        out.append((os.path.join(root, parts[2]), c))
    return out


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
              include=None, until: Optional[datetime] = None, withhold=None) -> str:
    """Watched files vs snapshot + repo changes since the session-start HEAD.

    sensitive=True: `git diff --stat`-style lines only (path | +N -M), never file contents.
    include: only these paths (realpath-compared); None = every changed path.
    until: end of the review window; files modified after it get a NOTE line (their content is as observed
    at collection time).
    withhold (#43, used with sensitive=False): callable(path) -> None (show the content) or (kind, label): the
    path gets a withheld_line(label, kind=kind) instead of content (kind sensitive | secret | scratch). A repo
    whose every wanted path is withheld gets no header, no start-status listing and no git diff at all."""
    snapdir = Path(snapdir)
    meta = load_meta(snapdir)
    if not meta:
        return "# no snapshot for this session (on_session_start hook not installed or did not run)\n"
    keys = None if include is None else {_key(p) for p in include}
    noise = noise_globs(cfg, meta)

    def want(p: str) -> bool:
        return keys is None or _key(p) in keys

    def held(p: str):
        if sensitive or withhold is None:
            return None
        return withhold(p)

    idx = _load_index(snapdir)
    hdr = [f"# local diff vs snapshot taken {meta.get('started')} (session {meta.get('session')})"]
    if meta.get("late"):
        hdr.append("# NOTE: late snapshot (taken mid-session); earlier changes are not visible")
    if sensitive:
        hdr.append("# mode: CONTENT WITHHELD (data_class=sensitive): each changed path below gets a "
                   "`# content withheld` line with a line-count stat only. A withheld line means the file DID "
                   "change; its content is just not shown.")
    else:
        hdr.append("# mode: unified diff")
    if keys is not None:
        hdr.append(f"# restricted to paths attributed to the agent ({len(keys)} candidate path(s))")
    for x in meta.get("dir_roots") or []:
        if isinstance(x, dict) and x.get("truncated"):
            hdr.append(f"# NOTE: opted-in dir {x.get('root')} was truncated at {x.get('files')} files at session "
                       "start; files added there are not detected")
    for x in meta.get("skipped_roots") or []:
        if isinstance(x, dict):
            hdr.append(f"# NOTE: opted-in dir {x.get('root')} was not snapshotted ({x.get('reason')})")
    out = ["\n".join(hdr) + "\n"]
    for status, p in _file_changes(snapdir, meta, cfg):
        if not want(p):
            continue
        snap = snapdir / "files" / p.lstrip("/")
        h = held(p)
        out.append(_late_note(p, until) if h is None else "")
        if idx.get(p, {}).get("skipped"):
            why = f"snapshot skipped ({idx[p]['skipped']}); content diff unavailable"
            if h is not None:
                out.append(withheld_line(h[1], status, why=why, kind=h[0]))
            else:
                out.append(withheld_line(p, status, why=why) if sensitive else f"{status} {p} | {why}\n")
            continue
        a = _read_lines(snap) if status != "A" else []
        b = _read_lines(Path(p)) if status != "D" else []
        if sensitive:
            out.append(withheld_line(p, status, _counts(a, b)))
            continue
        if h is not None:
            out.append(withheld_line(h[1], status, _counts(a, b), kind=h[0]))
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
        changes = [(st, p) for st, p in _repo_changes(repo) if not is_noise(p, noise)]
        mine = [p for _, p in changes if want(p)]
        if keys is not None and not mine:
            continue
        status_of = {p: s for s, p in changes}
        hidden = {p: held(p) for p in mine}
        hidden = {p: h for p, h in hidden.items() if h is not None}
        shown = [p for p in mine if p not in hidden]
        if hidden and not shown:
            # #43: nothing of this repo is shown: no repo root, no start-status listing, only withheld lines
            for p in sorted(hidden):
                kind, label = hidden[p]
                c = _path_counts(root, head, p, status_of.get(p, "M"))
                out.append(withheld_line(label, status_of.get(p, "M"), c, kind=kind))
            continue
        rels: List[str] = []
        if keys is not None or hidden:
            rels = [os.path.relpath(p, root) for p in shown]
        pre = [ln for ln in (repo.get("status") or "").splitlines() if ln.strip()]
        out.append(f"\n# repo {root}: changes since session-start HEAD {head[:12]}\n")
        if pre:
            out.append(f"# {len(pre)} path(s) were already modified/untracked at session start:\n")
            out.extend(f"#   {ln}\n" for ln in pre[:50])
        out.extend(_late_note(p, until) for p in shown)
        if sensitive:
            for p, c in _numstat(root, head, rels):
                if want(p):
                    out.append(withheld_line(p, status_of.get(p, "M"), c))
        else:
            for p in sorted(hidden):
                kind, label = hidden[p]
                c = _path_counts(root, head, p, status_of.get(p, "M"))
                out.append(withheld_line(label, status_of.get(p, "M"), c, kind=kind))
            if shown or keys is None and not hidden:
                args = ["diff", "--no-color", "--no-ext-diff", head] + (["--", *_literal(rels)] if rels else [])
                rc, txt = _git(root, *args)
                out.append(txt if rc == 0 else f"# git diff failed (rc={rc})\n")
        new = [p for s, p in changes if s == "A" and want(p) and p not in hidden and not _tracked(root, p)]
        for p in new:
            lines = _read_lines(Path(p))
            if sensitive:
                out.append(withheld_line(p, "A", None if lines is None else (len(lines), 0)))
            elif lines is None:
                out.append(f"A {p} (untracked) | binary\n")
            else:
                out.extend(difflib.unified_diff([], lines, fromfile="/dev/null", tofile=f"b{p}"))
                if out and not out[-1].endswith("\n"):
                    out.append("\n")
    return "".join(out)


def _path_counts(root: str, head: str, path: str, status: str) -> Optional[Tuple[int, int]]:
    """(added, removed) of one repo path vs *head* (git numstat; an untracked file counts all its lines)."""
    for p, c in _numstat(root, head, [os.path.relpath(path, root)]):
        if _key(p) == _key(path):
            return c
    if status == "A":
        lines = _read_lines(Path(path))
        return None if lines is None else (len(lines), 0)
    return (0, 0)


def stat_lines(snapdir: Path, cfg: Optional[Mapping[str, str]] = None, paths=()) -> List[str]:
    """`<status> <path> | +N -M` for each of *paths* (as returned by changed_files). Never file contents."""
    snapdir = Path(snapdir)
    meta = load_meta(snapdir)
    if not meta:
        return []
    idx = _load_index(snapdir)
    files = {p: s for s, p in _file_changes(snapdir, meta, cfg)}
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
