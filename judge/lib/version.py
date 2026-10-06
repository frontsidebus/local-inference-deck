"""judge/lib/version.py: which judge code wrote a request, built a bundle or judged it (code version stamp).

    code_version(judge_dir=None, refresh=False) -> {"sha", "commit", "dirty", "source"}
    label(v) -> "<sha12>" | "<sha12>-dirty" | "unknown"
    same(a, b) -> bool              both known, same sha, neither dirty
    mismatch_note(written, current, what="request", by="runner") -> str | None

`sha` is the git tree id of the judge/ directory as the checkout's index records it (equal to
`git rev-parse HEAD:judge` when nothing is staged), so commits that touch only docs or other components do not
change it. `commit` is HEAD's commit id (for humans; may be null). `dirty` is true when a tracked file under
judge/ differs from the index (content, or missing), a judge/ index entry is unmerged, or an untracked code file
(CODE_SUFFIXES) exists under judge/. Staged-but-uncommitted changes show in `sha` itself.

Computed from files only: `.git` (dir, or a worktree's `gitdir:` file), HEAD, the ref file or packed-refs, and
the binary index (versions 2-4). **No subprocess**: the hooks stay fast. Tracked files are stat-compared with
their index entry and hashed only when the stat differs (or is racy). The result is cached per process and judge
dir. Without a usable git checkout, the first line of `judge/VERSION` is the sha (`source: "file"`, never dirty);
without either, `{"sha": null, "source": "unknown"}`.
"""
from __future__ import annotations

import hashlib
import os
import struct
from pathlib import Path
from typing import Dict, List, Optional, Tuple

JUDGE_DIR = Path(__file__).resolve().parent.parent
VERSION_FILE = "VERSION"
CODE_SUFFIXES = (".py", ".sh", ".json", ".tmpl", ".md", ".service", ".timer", ".path")
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git"}
_cache: Dict[str, Dict] = {}


def _read(p: Path) -> Optional[str]:
    try:
        return p.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _git_dirs(start: Path) -> Optional[Tuple[Path, Path, Path]]:
    """(worktree root, gitdir, commondir) of the checkout holding *start*, from files only."""
    for d in [start, *start.parents]:
        g = d / ".git"
        if g.is_dir():
            gitdir = g
        elif g.is_file():
            text = _read(g) or ""
            if not text.startswith("gitdir:"):
                return None
            gitdir = Path(text[len("gitdir:"):].strip())
            if not gitdir.is_absolute():
                gitdir = (d / gitdir).resolve()
        else:
            continue
        common = gitdir
        cd = _read(gitdir / "commondir")
        if cd:
            common = Path(cd) if os.path.isabs(cd) else (gitdir / cd).resolve()
        return d, gitdir, common
    return None


def _head_commit(gitdir: Path, common: Path) -> Optional[str]:
    head = _read(gitdir / "HEAD")
    if not head:
        return None
    if not head.startswith("ref:"):
        return head if len(head) in (40, 64) else None
    ref = head[4:].strip()
    for base in (gitdir, common):
        v = _read(base / ref)
        if v:
            return v.split()[0]
    packed = _read(common / "packed-refs") or ""
    for line in packed.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == ref:
            return parts[0]
    return None


def _varint(data: bytes, i: int) -> Tuple[int, int]:
    """git's offset varint (index v4 path prefix lengths)."""
    c = data[i]
    i += 1
    val = c & 0x7F
    while c & 0x80:
        val += 1
        c = data[i]
        i += 1
        val = (val << 7) + (c & 0x7F)
    return val, i


def read_index(path: Path) -> List[Tuple[str, int, bytes, int, int, int, int]]:
    """[(name, mode, sha, mtime_s, mtime_ns, size, stage)] of a git index file (v2, v3, v4)."""
    data = path.read_bytes()
    if data[:4] != b"DIRC":
        raise ValueError("not a git index")
    ver, n = struct.unpack(">II", data[4:12])
    if ver not in (2, 3, 4):
        raise ValueError(f"index version {ver}")
    i, out, prev = 12, [], b""
    for _ in range(n):
        start = i
        (_cs, _cn, ms, mns, _dev, _ino, mode, _uid, _gid, size) = struct.unpack(">10I", data[i:i + 40])
        sha = data[i + 40:i + 60]
        flags = struct.unpack(">H", data[i + 60:i + 62])[0]
        i += 62
        if ver >= 3 and flags & 0x4000:
            i += 2
        if ver == 4:
            strip, i = _varint(data, i)
            end = data.index(b"\0", i)
            name = prev[:len(prev) - strip] + data[i:end]
            i = end + 1
        else:
            end = data.index(b"\0", i)
            name = data[i:end]
            i = start + ((end - start) // 8 + 1) * 8  # NUL padding to a multiple of 8
        prev = name
        out.append((name.decode("utf-8", errors="surrogateescape"), mode, sha, ms, mns, size, (flags >> 12) & 3))
    return out


def _tree_sha(entries: List[Tuple[str, int, bytes]]) -> str:
    """git tree id of *entries* [(relative path, mode, sha)] (nested trees built recursively)."""
    files: Dict[str, Tuple[int, bytes]] = {}
    dirs: Dict[str, List[Tuple[str, int, bytes]]] = {}
    for rel, mode, sha in entries:
        head, sep, rest = rel.partition("/")
        if sep:
            dirs.setdefault(head, []).append((rest, mode, sha))
        else:
            files[head] = (mode, sha)
    items = [(name, b"%o" % mode, sha) for name, (mode, sha) in files.items()]
    items += [(name, b"40000", bytes.fromhex(_tree_sha(sub))) for name, sub in dirs.items()]
    items.sort(key=lambda x: x[0].encode("utf-8", "surrogateescape") + (b"/" if x[1] == b"40000" else b""))
    body = b"".join(m + b" " + n.encode("utf-8", "surrogateescape") + b"\0" + s for n, m, s in items)
    return hashlib.sha1(b"tree %d\0" % len(body) + body).hexdigest()


def _blob_sha(path: str, mode: int) -> Optional[bytes]:
    try:
        data = os.readlink(path).encode("utf-8", "surrogateescape") if mode == 0o120000 else Path(path).read_bytes()
    except OSError:
        return None
    return hashlib.sha1(b"blob %d\0" % len(data) + data).digest()


def _from_git(judge_dir: Path) -> Optional[Dict]:
    found = _git_dirs(judge_dir)
    if not found:
        return None
    root, gitdir, common = found
    idx = gitdir / "index"
    try:
        entries = read_index(idx)
        idx_mtime = idx.stat().st_mtime_ns
    except (OSError, ValueError, struct.error, IndexError):
        return None
    try:
        prefix = judge_dir.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None
    prefix = "" if prefix == "." else prefix + "/"
    mine = [e for e in entries if e[0].startswith(prefix)]
    if not mine:
        return None
    dirty = any(e[6] for e in mine)
    tracked = set()
    tree_in: List[Tuple[str, int, bytes]] = []
    for name, mode, sha, ms, mns, size, stage in mine:
        tracked.add(name)
        if stage:
            continue
        tree_in.append((name[len(prefix):], mode, sha))
        if dirty or mode == 0o160000:
            continue
        p = os.path.join(str(root), name)
        try:
            st = os.lstat(p)
        except OSError:
            dirty = True
            continue
        same_stat = (int(st.st_mtime) & 0xFFFFFFFF) == ms and st.st_mtime_ns % 1_000_000_000 == mns \
            and (st.st_size & 0xFFFFFFFF) == size
        if same_stat and st.st_mtime_ns < idx_mtime:
            continue
        if _blob_sha(p, mode) != sha:
            dirty = True
    if not dirty:
        for dirpath, dirnames, filenames in os.walk(judge_dir):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            rel_dir = os.path.relpath(dirpath, root)
            for f in filenames:
                if f.endswith(CODE_SUFFIXES):
                    rel = f if rel_dir == "." else f"{rel_dir}/{f}".replace(os.sep, "/")
                    if rel not in tracked:
                        dirty = True
                        break
            if dirty:
                break
    return {"sha": _tree_sha(tree_in), "commit": _head_commit(gitdir, common), "dirty": dirty, "source": "git"}


def code_version(judge_dir=None, refresh: bool = False) -> Dict:
    """The judge code version (see module doc). Never raises; cached per process and *judge_dir*."""
    d = Path(judge_dir) if judge_dir else JUDGE_DIR
    key = str(d)
    if key in _cache and not refresh:
        return dict(_cache[key])
    try:
        v = _from_git(d)
    except Exception:  # a stamp must never break a hook
        v = None
    if v is None:
        line = ((_read(d / VERSION_FILE) or "").splitlines() or [""])[0].strip()[:80]
        v = ({"sha": line, "commit": None, "dirty": False, "source": "file"} if line else
             {"sha": None, "commit": None, "dirty": False, "source": "unknown"})
    _cache[key] = v
    return dict(v)


def label(v) -> str:
    if not isinstance(v, dict) or not v.get("sha"):
        return "unknown"
    return str(v["sha"])[:12] + ("-dirty" if v.get("dirty") else "")


def same(a, b) -> bool:
    return (isinstance(a, dict) and isinstance(b, dict) and bool(a.get("sha")) and a.get("sha") == b.get("sha")
            and not a.get("dirty") and not b.get("dirty"))


def mismatch_note(written, current, what: str = "request", by: str = "runner") -> Optional[str]:
    """A finding note when *written* (the version stamped on *what*) is not provably *current* (the version of
    the *by* code judging it now), else None. A warning only: never a failure."""
    if same(written, current):
        return None
    if not isinstance(written, dict) or not written.get("sha"):
        return (f"code version: the {what} carries no judge code version (written before stamps, or unknown); "
                f"judged by {by} {label(current)}")
    if isinstance(current, dict) and written.get("sha") == current.get("sha"):
        return (f"code version: {what} written by judge code {label(written)}, judged by {by} {label(current)} "
                "(same tree, uncommitted changes: behaviour may differ)")
    return (f"code version: {what} written by judge code {label(written)}, judged by {by} {label(current)}: "
            "hook, collector or prompt behaviour may differ between them")
