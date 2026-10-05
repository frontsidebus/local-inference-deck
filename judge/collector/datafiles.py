#!/usr/bin/env python3
"""judge/collector/datafiles.py: data files the agent's code reads, for the R8 code review (stdlib only).

Some code defects only show against real data: pilot-2 B2's pipeline checked `seen.events` while the seed state
files keep ids in `seen.items`. The diff alone looks fine, and the seed files were only *read* by the agent, so
no bundle carried them. This module picks a few such files and writes excerpts of them to `data-files.txt`.

    collect_data_files(req, cfg, root, since, until, diff, agent_paths, data_class, now=None)
        -> (artifact text or None, manifest record)

Data boundary (all must hold for a file's content to be included):
  - relevant: the agent read it in the window (a `read_file` call, or a `cat`/`jq`/`head`/`tail`/`less`/`yq`
    target of a terminal call: tool_call_id from events.jsonl, arguments from Hermes' state.db, read-only, as
    #45 attribution reads them), and/or its path or basename is named in an added line of agent-diff.patch
    (a string literal, `Path(...)` argument);
  - lib/config.path_class says `infra` (not secret-shaped, not scratch, an infra location);
  - a structured data/config type the code would parse (DATA_SUFFIXES); plain `.txt` only when the diff names
    it; never `.env`, keys or certs (secret-shaped names are never read);
  - not changed by the agent (its content is already in agent-diff.patch);
  - at most READ_MAX bytes on disk, valid UTF-8, no private-key block;
  - at most JUDGE_DATA_FILES_MAX files (default 3), each excerpt at most JUDGE_DATA_FILE_BYTES (default 8192):
    JSON keeps its structure (every key of the top level, long objects/arrays elided with a count marker),
    anything else is middle-truncated at line boundaries with a marker.
Content is redact()ed before and after excerpting. Bundles that are not `infra` (sensitive, gate, claims-only)
get no data file content: the caller passes data_class and the function returns no artifact for them.
Withheld candidates are recorded in the manifest record: infra ones by name and reason, non-infra ones as
counts per reason only (their names never enter an infra bundle). Never raises for a single bad file.
"""
from __future__ import annotations

import glob as _glob
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from lib import config, snapshot
from lib import queue as q
from lib.redact import redact

ARTIFACT = "data-files.txt"
DATA_SUFFIXES = (".json", ".jsonl", ".ndjson", ".yaml", ".yml", ".toml", ".csv", ".tsv", ".ini", ".xml", ".txt")
TEXT_SUFFIXES = (".txt",)          # plain text samples: only when the diff names them
READ_MAX = 512 * 1024              # bytes on disk; bigger files are withheld ("too big")
DEFAULT_MAX_FILES = 3
DEFAULT_MAX_BYTES = 8192
KINDS = ("completion", "plan")
READ_PROGRAMS = ("cat", "jq", "yq", "head", "tail", "less", "more", "bat")
_JQ_ARG1 = {"-f", "--from-file", "--indent", "-L"}
_JQ_ARG2 = {"--arg", "--argjson", "--slurpfile", "--rawfile", "--args", "--jsonargs"}
_HEAD_ARG = {"-n", "-c", "--lines", "--bytes"}
_WRAPPERS = {"sudo", "command", "builtin", "exec", "nohup", "time", "env", "doas"}
_LITERAL_RE = re.compile(r"""(?P<q>["'])(?P<s>[^"'\n]{1,300})(?P=q)""")
_PEM_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
MAX_LITERALS = 200
GLOB_MAX = 20
OMIT_MARK = "[... {n} line(s) omitted by the collector ...]"

# withheld reasons
R_SECRET = "secret-shaped file"
R_SENSITIVE = "non-infra location"
R_SCRATCH = "agent scratch/cache"
R_TYPE = "plain text not named in agent-diff.patch"
R_BIG = "too big"
R_BINARY = "not UTF-8 text"
R_KEY = "private key material"
R_CAP = "over JUDGE_DATA_FILES_MAX"
R_MISSING = "missing or unreadable at collection time"
NAMELESS = (R_SECRET, R_SENSITIVE, R_SCRATCH)  # never named in a bundle


def _setting(cfg: Mapping[str, str], key: str, default: int) -> int:
    raw = (cfg or {}).get(key) or os.environ.get(key) or ""
    try:
        return max(0, int(str(raw).strip())) if str(raw).strip() else default
    except ValueError:
        return default


def settings(cfg: Mapping[str, str]) -> Tuple[bool, int, int]:
    """(enabled, max files, max bytes per excerpt) from JUDGE_DATA_FILES (1), JUDGE_DATA_FILES_MAX (3) and
    JUDGE_DATA_FILE_BYTES (8192): site.env, else the process environment."""
    on = str((cfg or {}).get("JUDGE_DATA_FILES") or os.environ.get("JUDGE_DATA_FILES") or "1").strip() != "0"
    return on, _setting(cfg, "JUDGE_DATA_FILES_MAX", DEFAULT_MAX_FILES), \
        max(512, _setting(cfg, "JUDGE_DATA_FILE_BYTES", DEFAULT_MAX_BYTES))


# ---------------------------------------------------------------- what the agent read
def read_file_calls(hermes_home, session: str) -> Dict[str, Tuple[str, str]]:
    """{tool_call_id: (path, workdir)} of the read_file calls of *session* in Hermes' state.db (read-only;
    empty when it is missing). Used in memory only."""
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
            if fn.get("name") != "read_file":
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(args, dict) or not isinstance(args.get("path"), str):
                continue
            for key in ("id", "call_id"):
                if isinstance(c.get(key), str) and c[key]:
                    out[c[key]] = (args["path"], "")
    return out


def _abs(path: str, cwd: str) -> str:
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(cwd or "/", p)
    return os.path.normpath(p)


def _expand(word: str, cwd: str) -> List[str]:
    if "$" in word or "`" in word or not word:
        return []
    p = _abs(word, cwd)
    if any(c in os.path.basename(p) for c in "*?["):
        return sorted(_glob.glob(p))[:GLOB_MAX]
    return [p]


def read_targets(command: str, cwd: str) -> List[str]:
    """Files a terminal command reads with cat/jq/yq/head/tail/less/more/bat (best effort, nothing executed).
    `cd DIR` is followed; jq/yq's filter argument and option values are skipped; `$`/backtick words are not
    resolved; a glob is matched (at most GLOB_MAX files)."""
    out: List[str] = []
    cur = cwd or "/"
    for words in snapshot._simple_commands(command):
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
                cur = _abs(words[1], cur)
            continue
        if prog not in READ_PROGRAMS:
            continue
        args, pos, i = words[1:], [], 0
        filter_given = False
        while i < len(args):
            a = args[i]
            if a == "--":
                pos += args[i + 1:]
                break
            if prog in ("jq", "yq"):
                if a in _JQ_ARG2:
                    i += 3
                    continue
                if a in _JQ_ARG1:
                    filter_given = filter_given or a in ("-f", "--from-file")
                    i += 2
                    continue
            elif prog in ("head", "tail") and a in _HEAD_ARG:
                i += 2
                continue
            if a.startswith("-") and a != "-":
                i += 1
                continue
            pos.append(a)
            i += 1
        if prog in ("jq", "yq") and not filter_given and pos:
            pos = pos[1:]  # the filter
        for w in pos:
            if w != "-":
                out += _expand(w, cur)
    return out


def agent_reads(req: Dict, cfg: Mapping[str, str], root: Path, since: datetime, until: datetime) -> Dict[str, int]:
    """{realpath: times read} for the session's read_file calls and terminal read targets that ran in the
    window. The call ids come from events.jsonl; the arguments from state.db (never stored)."""
    d = q.snapshot_dir(req["session"], root)
    meta = snapshot.load_meta(d)
    hh = (cfg or {}).get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    session = str(req.get("session") or "")
    base_cwd = str((req.get("detail") or {}).get("cwd") or meta.get("cwd") or "/")
    reads: Dict[str, int] = {}
    rf: Optional[Dict] = None
    tc: Optional[Dict] = None
    for ev in snapshot.events(d):
        if not snapshot.ran(ev) or not ev.get("call_id"):
            continue
        try:
            t = q.parse_utc(str(ev.get("t") or ""))
        except ValueError:
            continue
        if not since <= t <= until:
            continue
        cid, tool = str(ev["call_id"]), ev.get("tool")
        found: List[str] = []
        try:
            if tool == "read_file":
                if rf is None:
                    rf = read_file_calls(hh, session)
                got = rf.get(cid)
                if got:
                    found = [_abs(got[0], str(ev.get("cwd") or base_cwd))]
            elif tool == "terminal":
                if tc is None:
                    tc = snapshot.terminal_commands(hh, session)
                got = tc.get(cid)
                if got:
                    command, workdir = got if isinstance(got, tuple) else (got, "")
                    found = read_targets(command, workdir or str(ev.get("cwd") or base_cwd))
        except Exception:  # best effort: never break a bundle
            found = []
        for p in found:
            k = snapshot._key(p)
            reads[k] = reads.get(k, 0) + 1
    return reads


# ---------------------------------------------------------------- what the changed code names
def diff_literals(diff: str) -> List[str]:
    """String literals on the added code lines of agent-diff.patch (`Path("x.json")`, `"state/seed.yaml"`)."""
    out: List[str] = []
    for ln in (diff or "").splitlines():
        if not ln.startswith("+") or ln.startswith("+++"):
            continue
        for m in _LITERAL_RE.finditer(ln[1:]):
            s = m.group("s").strip()
            if s and s not in out and not s.startswith("#"):
                out.append(s)
            if len(out) >= MAX_LITERALS:
                return out
    return out


def _named(path: str, literals: List[str]) -> bool:
    base = os.path.basename(path)
    for s in literals:
        s = s.rstrip("/")
        if not s:
            continue
        if s == base or s.endswith("/" + base) or (("/" in s) and path.endswith("/" + s.lstrip("./"))):
            return True
    return False


def _has_suffix(path: str, suffixes) -> bool:
    return os.path.basename(path).lower().endswith(tuple(suffixes))


def resolve_literals(literals: List[str], bases: List[str]) -> List[str]:
    """Existing files named by data-typed literals: absolute or ~ literals as they are, relative ones against
    *bases* (the repo roots, the session cwd, the changed files' dirs). Nothing is globbed."""
    out: List[str] = []
    for s in literals:
        if not _has_suffix(s, DATA_SUFFIXES) or any(c in s for c in "*?[{$`") or "\0" in s:
            continue
        cands = [os.path.expanduser(s)] if s.startswith(("/", "~")) else [os.path.join(b, s) for b in bases]
        for c in cands:
            c = os.path.normpath(c)
            if os.path.isfile(c):
                k = snapshot._key(c)
                if k not in out:
                    out.append(k)
    return out


# ---------------------------------------------------------------- excerpts
def _elide(x, k: int, slen: int, depth: int = 0):
    if isinstance(x, dict):
        keep = len(x) if depth == 0 else k
        items = list(x.items())
        out = {str(kk): _elide(v, k, slen, depth + 1) for kk, v in items[:keep]}
        if len(items) > keep:
            out["…"] = f"{len(items) - keep} more key(s) elided by the collector"
        return out
    if isinstance(x, list):
        keep = max(1, k // 2)
        out = [_elide(v, k, slen, depth + 1) for v in x[:keep]]
        if len(x) > keep:
            out.append(f"… {len(x) - keep} more item(s) elided by the collector")
        return out
    if isinstance(x, str) and len(x) > slen:
        return x[:slen] + f"… (+{len(x) - slen} chars)"
    return x


def middle_cut(text: str, max_bytes: int) -> str:
    """At most about *max_bytes*: whole lines from the head and the tail, a marker in the middle."""
    if len(text) <= max_bytes:
        return text
    lines = text.split("\n")
    lines = [ln if len(ln) <= 400 else ln[:400] + f"… (+{len(ln) - 400} chars)" for ln in lines]
    head, tail, used = [], [], 60
    i, j = 0, len(lines) - 1
    while i <= j:
        if used + len(lines[i]) + 1 > max_bytes:
            break
        head.append(lines[i])
        used += len(lines[i]) + 1
        i += 1
        if i > j or used + len(lines[j]) + 1 > max_bytes:
            break
        tail.insert(0, lines[j])
        used += len(lines[j]) + 1
        j -= 1
    n = j - i + 1
    return "\n".join(head + ([OMIT_MARK.format(n=n)] if n > 0 else []) + tail)


def excerpt(text: str, path: str, max_bytes: int) -> Tuple[str, str]:
    """(excerpt, how): `whole`, `json-structure` (every top-level key; long objects/arrays/strings elided with
    count markers) or `middle-cut`."""
    if len(text) <= max_bytes:
        return text, "whole"
    if _has_suffix(path, (".json",)):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if data is not None:
            for k, slen in ((10, 200), (6, 120), (4, 80), (2, 60), (1, 40)):
                s = json.dumps(_elide(data, k, slen), indent=1, ensure_ascii=False)
                if len(s) <= max_bytes:
                    return s, "json-structure"
            return middle_cut(s, max_bytes), "json-structure, middle-cut"
    return middle_cut(text, max_bytes), "middle-cut"


# ---------------------------------------------------------------- selection
def collect_data_files(req: Dict, cfg: Mapping[str, str], root: Path, since: datetime, until: datetime,
                       diff: str, agent_paths, data_class: str,
                       now: Optional[datetime] = None) -> Tuple[Optional[str], Dict]:
    """(data-files.txt text or None, manifest record). See the module docstring for the boundary."""
    on, max_files, max_bytes = settings(cfg)
    rec: Dict = {"included": [], "withheld": [], "withheld_counts": {}}
    if data_class != "infra":
        rec["skipped"] = f"data_class={data_class}: no data file content"
        return None, rec
    if req.get("kind") not in KINDS:
        rec["skipped"] = f"kind={req.get('kind')}: data files are for code review of completion/plan bundles"
        return None, rec
    if not on or max_files == 0:
        rec["skipped"] = "disabled (JUDGE_DATA_FILES=0 or JUDGE_DATA_FILES_MAX=0)"
        return None, rec
    if not agent_paths:
        rec["skipped"] = "no agent change"
        return None, rec
    cwd = (req.get("detail") or {}).get("cwd") or None
    try:
        reads = agent_reads(req, cfg, root, since, until)
    except Exception:
        reads = {}
    literals = diff_literals(diff)
    meta = snapshot.load_meta(q.snapshot_dir(req["session"], root))
    changed = {snapshot._key(p) for p in agent_paths}
    bases = [str(r.get("root")) for r in meta.get("repos") or [] if isinstance(r, dict) and r.get("root")]
    bases += ([cwd] if cwd else []) + sorted({os.path.dirname(p) for p in changed})
    try:
        named = set(resolve_literals(literals, list(dict.fromkeys(bases))))
    except Exception:
        named = set()
    cands = []
    for p in sorted(set(reads) | named):
        if p in changed:
            continue  # its content is in agent-diff.patch already
        if not (_has_suffix(p, DATA_SUFFIXES) or config.is_secret_path(p, cfg)):
            continue  # code, docs, assets: not data
        is_named = p in named or _named(p, literals)
        cands.append((0 if (is_named and p in reads) else 1 if is_named else 2, -reads.get(p, 0), p, is_named))
    cands.sort(key=lambda c: (c[0], c[1], os.path.getsize(c[2]) if os.path.isfile(c[2]) else 0, c[2]))
    counts: Dict[str, int] = {}
    parts: List[str] = []

    def withhold(p: str, why: str) -> None:
        if why in NAMELESS:
            counts[why] = counts.get(why, 0) + 1
        else:
            rec["withheld"].append({"path": p, "reason": why})

    for _, neg_reads, p, is_named in cands:
        kind = config.path_class(p, cfg, cwd)
        if kind != "infra":
            withhold(p, {"secret": R_SECRET, "scratch": R_SCRATCH}.get(kind, R_SENSITIVE))
            continue
        if _has_suffix(p, TEXT_SUFFIXES) and not is_named:
            withhold(p, R_TYPE)
            continue
        try:
            size = os.path.getsize(p)
        except OSError:
            withhold(p, R_MISSING)
            continue
        if size > READ_MAX:
            withhold(p, f"{R_BIG} ({size} bytes > {READ_MAX})")
            continue
        if len(rec["included"]) >= max_files:
            withhold(p, R_CAP)
            continue
        try:
            raw = Path(p).read_bytes()[:READ_MAX]
            text = raw.decode("utf-8")
        except OSError:
            withhold(p, R_MISSING)
            continue
        except UnicodeDecodeError:
            withhold(p, R_BINARY)
            continue
        if _PEM_RE.search(text):
            withhold(p, R_KEY)
            continue
        body, how = excerpt(redact(text), p, max_bytes)
        body = redact(body)
        why = (["read by the agent" + (f" ({-neg_reads}x)" if -neg_reads > 1 else "")] if -neg_reads else []) + \
            (["named in agent-diff.patch"] if is_named else [])
        late = False
        try:
            late = datetime.fromtimestamp(os.path.getmtime(p), timezone.utc) > until
        except OSError:
            pass
        entry = {"path": p, "bytes": size, "shown_chars": len(body), "excerpt": how, "why": why}
        if late:
            entry["modified_after_window"] = True
        rec["included"].append(entry)
        hdr = f"=== data file: {p} ({size} bytes; {', '.join(why)}; excerpt: {how}) ==="
        parts.append(hdr + ("\nNOTE: modified after the window end; this may differ from what the agent read"
                            if late else "") + "\n" + body.rstrip("\n") + "\n")
    rec["withheld_counts"] = counts
    if not parts:
        return None, rec
    head = ("# Data files the changed code reads (context for the R8 code review): files the agent read in the "
            "window or that agent-diff.patch names,\n# infra locations only, redacted, content at collection "
            f"time. At most {max_files} file(s), {max_bytes} chars each; long JSON objects/arrays are elided "
            "with a count marker,\n# other files cut in the middle. Not the agent's changes; never evidence of "
            "what the agent did.\n")
    return head + "\n".join(parts), rec
