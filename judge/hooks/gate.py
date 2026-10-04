#!/usr/bin/env python3
"""judge/hooks/gate.py: C2 synchronous safety gate (Hermes ``pre_tool_call`` shell hook).

Deterministic policy, no model calls, stdlib only. Installed with ``fail_closed: true`` and matcher
``terminal|write_file|patch|read_file``.

stdin   {"hook_event_name","tool_name","tool_input","session_id","cwd","profile","extra"}
          terminal:   tool_input = {"command": str, "workdir"?: str, "background"?: bool, ...}
          write_file: tool_input = {"path": str, "content": str}
          patch:      tool_input = {"path","old_string","new_string","replace_all"?}  (replace mode)
                      or {"mode": "patch", "patch": "*** Begin Patch ..."}            (V4A mode)
          read_file:  tool_input = {"path": str, "offset"?: int, "limit"?: int}  (tools/file_tools.py
                      READ_FILE_SCHEMA). Only the path matters: a secret-shaped path escalates
                      (secret-output, rule_key per resolved path, so paging with offset/limit or a
                      `[a]lways` answer covers that one file only); every other read passes.
stdout  {}                                                    pass, exit 0
        {"action":"approve","message":...,"rule_key":...}     escalate to the human, exit 0
        {"action":"block","message":...}                      refuse, exit 2 (and the message on stderr)
Internal error / malformed stdin -> block JSON + exit 2 (fail closed; Hermes also blocks on exit 2 alone).

Every non-pass decision appends one JSONL line to $JUDGE_REVIEW_DIR/gate.log and writes a `gate` review
request (lib/queue.py). Rules live in judge/policy/gate-policy.json.tmpl (see its _doc); the pre-rendered
$JUDGE_REVIEW_DIR/gate-policy.json wins when present.

Debug: ``gate.py --explain < payload.json`` prints the decision plus every rule hit, without logging.
"""
from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

JUDGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(JUDGE_DIR, "policy", "gate-policy.json.tmpl")
SITE_VARS = ("BACKEND_LAN_IP", "BACKEND_WG_IP", "EDGE_PUBLIC_IP", "EDGE_WG_IP", "SPARK_DOMAIN",
             "BACKEND_SSH_USER", "EDGE_SSH_USER", "JUDGE_SSH_ALIASES")
KNOWN_KINDS = ("oversight_config", "oversight_path", "hermes_config", "sensitive_path", "remote_mutation",
               "remote_copy", "remote_opaque", "public_push", "secret_output", "secret_output_unknown")
# #27: added after the first policies were rendered; an older pre-rendered gate-policy.json gets this default.
DEFAULT_SECRET_UNKNOWN_RULE = {
    "id": "secret-output-unknown", "kind": "secret_output_unknown", "action": "approve",
    "description": "A printing command reads a file the gate cannot resolve in a command that names a secret "
                   "path or secret-shaped file; it may print that secret"}
GATED_TOOLS = ("terminal", "write_file", "patch", "read_file")
# Fallbacks when an older pre-rendered $JUDGE_REVIEW_DIR/gate-policy.json lacks these keys (the template has them).
DEFAULT_HERMES_CLI = {
    "config_readonly": ["", "show", "get", "path", "env-path", "check"], "config_block": ["edit"],
    "hooks_readonly": ["", "list", "ls", "doctor"],
    "hooks_block": ["revoke", "remove", "rm", "approve", "accept", "allow"],
    "block_subcommands": ["import"], "approve_subcommands": ["setup", "migrate"],
}
DEFAULT_SECRET_PATHS = ["~/.config/spark/**", "$HERMES_HOME/.env", "~/.ssh/id_*", "/etc/llama-swap/api-key",
                        "/etc/wireguard/**", "/etc/ssh/ssh_host_*_key", "~/.git-credentials",
                        "~/.docker/config.json", "~/.config/gh/hosts.yml"]
MAX_DEPTH = 8
MAX_SCRIPT_PEEK = 512 * 1024
DEV_SINKS = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "-")


class GateError(Exception):
    """Policy/config problem: the gate cannot decide, so it blocks (fail closed)."""


# =============================================================== config / policy
def _runtime_paths():
    home = os.environ.get("HOME") or os.path.expanduser("~")
    hh = os.environ.get("HERMES_HOME") or os.path.join(home, ".hermes")
    hh = os.path.abspath(os.path.expanduser(hh))
    rd = os.environ.get("JUDGE_REVIEW_DIR") or os.path.join(hh, "review")
    rd = os.path.abspath(os.path.expanduser(rd))
    return {"HOME": home, "HERMES_HOME": hh, "JUDGE_REVIEW_DIR": rd, "JUDGE_DIR": JUDGE_DIR}


def _lib():
    if JUDGE_DIR not in sys.path:
        sys.path.insert(0, JUDGE_DIR)
    import lib  # noqa: F401  (judge/lib package)
    return lib


def _site_vars():
    """Site identifiers: judge/lib/config.py (defaults < site.env < environment)."""
    try:
        _lib()
        from lib import config as jconfig
        cfg = jconfig.load_config()
    except Exception as e:  # missing/broken config helper: fall back to the environment only
        sys.stderr.write(f"gate: lib/config unavailable ({e}); using environment for site values\n")
        cfg = os.environ
    return {k: str(cfg.get(k) or "") for k in SITE_VARS}


def _render(src, values):
    return re.sub(r"\$\{([A-Z][A-Z0-9_]*)\}",
                  lambda m: json.dumps(values.get(m.group(1), ""))[1:-1] if m.group(1) in values else m.group(0),
                  src)


def load_policy(rt=None):
    rt = rt or _runtime_paths()
    pre = os.path.join(rt["JUDGE_REVIEW_DIR"], "gate-policy.json")
    try:
        if os.path.isfile(pre):
            with open(pre, encoding="utf-8") as fh:
                text, source = fh.read(), pre
        else:
            with open(TEMPLATE, encoding="utf-8") as fh:
                text, source = _render(fh.read(), _site_vars()), TEMPLATE
        pol = json.loads(text)
    except (OSError, ValueError) as e:
        raise GateError(f"cannot load gate policy: {e}") from e
    if not isinstance(pol, dict) or not isinstance(pol.get("rules"), list):
        raise GateError(f"gate policy {source}: missing rules list")
    seen = set()
    for r in pol["rules"]:
        if not isinstance(r, dict) or not r.get("id") or r.get("kind") not in KNOWN_KINDS \
                or r.get("action") not in ("approve", "block"):
            raise GateError(f"gate policy {source}: bad rule {str(r)[:120]}")
        if r["kind"] in seen:
            raise GateError(f"gate policy {source}: duplicate rule kind {r['kind']}")
        seen.add(r["kind"])
    if "secret_output" in seen and "secret_output_unknown" not in seen:
        pol["rules"].append(dict(DEFAULT_SECRET_UNKNOWN_RULE))
    pol["_source"] = source
    return pol


def _clean(values):
    out = []
    for v in values or []:
        for part in re.split(r"[\s,]+", str(v)):
            part = part.strip()
            if part and "${" not in part:
                out.append(part)
    return out


class Hosts:
    """Recognises ssh/scp/rsync destinations that reach Walter or Covenant."""

    def __init__(self, hosts_cfg, home):
        self.addr, self.domains, self.users, self.aliases = {}, [], {}, {}
        for h in (hosts_cfg or {}).values():
            label = h.get("label") or "remote host"
            for a in _clean(h.get("addresses")):
                self.addr[a.lower()] = label
            for d in _clean(h.get("domains")):
                self.domains.append((d.lower().lstrip("."), label))
            for u in _clean(h.get("users")):
                self.users[u] = label
            for a in _clean(h.get("aliases")):
                self.aliases[a.lower()] = label
        self.empty = not (self.addr or self.domains or self.aliases)
        self._ssh_config(os.path.join(home, ".ssh", "config"))

    def _ssh_config(self, path):
        """Host aliases whose HostName/User reach a target count as targets too."""
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            return
        block, hostname, user = [], None, None

        def flush():
            label = self._label_for(hostname) if hostname else None
            label = label or (self.users.get(user) if user else None)
            if label:
                for b in block:
                    if not any(ch in b for ch in "*?!"):
                        self.aliases.setdefault(b.lower(), label)

        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = re.split(r"\s*=\s*|\s+", line, maxsplit=1)
            key, val = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
            if key in ("host", "match"):
                flush()
                block, hostname, user = (val.split() if key == "host" else []), None, None
            elif key == "hostname":
                hostname = val
            elif key == "user":
                user = val

        flush()

    def _label_for(self, host):
        h = host.lower().strip("[]")
        if h in self.addr:
            return self.addr[h]
        if h in self.aliases:
            return self.aliases[h]
        for d, label in self.domains:
            if h == d or h.endswith("." + d):
                return label
        return None

    def match(self, user, host):
        """Label of the target reached, or None for a host the gate does not guard."""
        if not host:
            return None
        if any(t in host for t in ("$", "`", "__GATE_SUB")):
            return "an unresolved host (assumed Walter/Covenant)"
        label = self._label_for(host)
        if label:
            return label
        if user and user in self.users:
            return self.users[user]
        if self.empty:
            return "an unknown host (no site identifiers configured)"
        return None


# =============================================================== shell parsing
_HD_RE = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|\\?([A-Za-z0-9_.-]+))")
_OPS = ("&>>", "<<<", "&&", "||", ";;", ">>", ">&", "<&", "&>", ">|", "|&", "<>", "<<",
        ";", "&", "|", "<", ">", "(", ")")
SEPS = {";", ";;", "&&", "||", "|", "|&", "&", "(", ")"}
OUT_REDIRS = {">", ">>", ">|", "&>", "&>>", "<>"}
SUB_RE = re.compile(r"__GATE_SUB(\d+)__")
HD_TOKEN_RE = re.compile(r"__GATE_HD(\d+)__")
ARITH = "__GATE_ARITH__"      # an arithmetic command (( ... )) (runs nothing)
ARITH_NUM = "__GATE_NUM__"    # an arithmetic expansion $(( ... )) (a number)
LOOPVAR = "__GATE_LOOPVAR__"  # value of a for/select loop variable (unknown)
XARG = "__GATE_XARG__"        # an argument supplied at run time by xargs / find -exec {}
# #27 shell variables: $NAME / ${NAME} are substituted when NAME holds a known literal value
VAR_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
# a word still holding something the gate can't resolve: a variable/parameter, a substitution, an xargs/find arg
# ($0 is the script/shell name, not data: `sed -n '2,20p' "$0"` prints the script's own usage)
UNRES_RE = re.compile(r"\$[{A-Za-z_1-9@*#?!]|__GATE_SUB\d+__|__GATE_XARG__|__GATE_LOOPVAR__|`")
DECL_BUILTINS = ("export", "declare", "local", "readonly", "typeset")
STDOUT_FILES = ("/dev/stdout", "/dev/stderr", "/dev/tty", "-", "/proc/self/fd/1", "/proc/self/fd/2", "/dev/fd/1",
                "/dev/fd/2")
HASH_SINKS = ("sha256sum", "sha1sum", "sha512sum", "sha224sum", "sha384sum", "md5sum", "b2sum", "cksum", "sum",
              "openssl", "xxh64sum", "xxhsum")
INTERPRETERS = ("python", "python3", "perl", "ruby", "node", "php")
# Reserved words (recognized only in command position). Openers are followed by a command; closers end a
# compound command and may only be followed by redirects/separators.
KW_OPEN = {"if", "then", "else", "elif", "while", "until", "do", "!", "{", "time"}
KW_CLOSE = {"fi", "done", "esac", "}"}


class ProcSub(str):
    """Source of a process substitution <(...) / >(...): its placeholder stands for a pipe, not a file name."""


def _match_close(s, i):
    """Index of the ')' closing a '(' that ends just before s[i] (quote-aware); len(s) if unbalanced."""
    depth, sq, dq, n = 1, False, False, len(s)
    while i < n:
        c = s[i]
        if sq:
            if c == "'":
                sq = False
        elif c == "\\":
            i += 2
            continue
        elif dq:
            if c == '"':
                dq = False
        elif c == "'":
            sq = True
        elif c == '"':
            dq = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def preprocess(s):
    """Pull out command substitutions ($(...), `...`, <(...), >(...)) and heredoc bodies.

    Returns (text with placeholders, [substitution sources], [heredoc bodies]). Single-quoted text is left
    alone (no local expansion there), comments and line continuations are dropped."""
    out, substs, heredocs, pending = [], [], [], []
    i, n, sq, dq = 0, len(s), False, False
    arith = True  # after one `((` that does not close as `))`, stop trying (keeps odd input linear)
    while i < n:
        c = s[i]
        if sq:
            out.append(c)
            if c == "'":
                sq = False
            i += 1
            continue
        if c == "\\":
            if i + 1 < n and s[i + 1] == "\n":
                i += 2
                continue
            out.append(s[i:i + 2])
            i += 2
            continue
        if arith and c == "$" and s.startswith("$((", i):
            j = _match_close(s, i + 2)
            if j < n and _match_close(s, i + 3) == j - 1:
                # arithmetic expansion: a number; only substitutions inside it can run anything
                inner = s[i + 3:j - 1]
                if "$(" in inner or "`" in inner:
                    substs.extend(preprocess(inner)[1])
                out.append(ARITH_NUM)
                i = j + 1
                continue
            arith = False
        if c == "$" and s.startswith("$(", i):
            j = _match_close(s, i + 2)
            substs.append(s[i + 2:j])
            out.append(f"__GATE_SUB{len(substs) - 1}__")
            i = j + 1
            continue
        if c == "`":
            j = i + 1
            while j < n and s[j] != "`":
                j += 2 if s[j] == "\\" else 1
            substs.append(s[i + 1:j])
            out.append(f"__GATE_SUB{len(substs) - 1}__")
            i = j + 1
            continue
        if dq:
            out.append(c)
            if c == '"':
                dq = False
            i += 1
            continue
        if c == "'":
            sq = True
        elif c == '"':
            dq = True
        elif c == "(" and arith and s.startswith("((", i):
            # arithmetic command `(( ... ))` / `for (( ...; ...; ... ))`: one inert word; substitutions inside it
            # are still pulled out and analyzed. `( (a) )`-style nested subshells don't close with `))`.
            j = _match_close(s, i + 1)
            if j < n and _match_close(s, i + 2) == j - 1:
                inner = s[i + 2:j - 1]
                if "$(" in inner or "`" in inner:
                    substs.extend(preprocess(inner)[1])
                out.append(f" {ARITH} ")
                i = j + 1
                continue
            arith = False
        elif c in "<>" and i + 1 < n and s[i + 1] == "(":
            j = _match_close(s, i + 2)
            substs.append(ProcSub(s[i + 2:j]))
            out.append(f" __GATE_SUB{len(substs) - 1}__ ")
            i = j + 1
            continue
        elif c == "<" and s.startswith("<<", i) and not s.startswith("<<<", i):
            m = _HD_RE.match(s, i)
            if m:
                delim = next(g for g in m.groups()[1:] if g is not None)
                heredocs.append("")
                pending.append((len(heredocs) - 1, delim, m.group(1) == "-"))
                out.append(f" __GATE_HD{len(heredocs) - 1}__ ")
                i = m.end()
                continue
        elif c == "#" and (i == 0 or s[i - 1] in " \t\n;&|()"):
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        elif c == "\n":
            out.append("\n")
            i += 1
            for k, delim, dash in pending:
                body = []
                while i < n:
                    j = s.find("\n", i)
                    line = s[i:] if j < 0 else s[i:j]
                    i = n if j < 0 else j + 1
                    if (line.lstrip("\t") if dash else line).strip() == delim:
                        break
                    body.append(line)
                heredocs[k] = "\n".join(body)
            pending = []
            continue
        out.append(c)
        i += 1
    return "".join(out), substs, heredocs


def tokenize(s):
    """Shell words and operators: [("w", word) | ("op", op)]. Quotes are removed from words; quoted
    operator characters stay words (grep '>' is not a redirect). Unquoted newlines become ';'."""
    toks, buf, inword, i, n = [], [], False, 0, len(s)

    def flush():
        nonlocal buf, inword
        if inword:
            toks.append(("w", "".join(buf)))
        buf, inword = [], False

    while i < n:
        c = s[i]
        if c == "'":
            j = s.find("'", i + 1)
            j = n if j < 0 else j
            buf.append(s[i + 1:j])
            inword, i = True, j + 1
            continue
        if c == "$" and i + 1 < n and s[i + 1] == "'":
            j = i + 2
            while j < n and s[j] != "'":
                j += 2 if s[j] == "\\" else 1
            buf.append(s[i + 2:j].replace("\\n", "\n").replace("\\'", "'"))
            inword, i = True, j + 1
            continue
        if c == '"':
            i += 1
            inword = True
            while i < n and s[i] != '"':
                if s[i] == "\\" and i + 1 < n and s[i + 1] in '"\\$`\n':
                    buf.append(s[i + 1])
                    i += 2
                    continue
                buf.append(s[i])
                i += 1
            i += 1
            continue
        if c == "\\":
            if i + 1 < n and s[i + 1] != "\n":
                buf.append(s[i + 1])
                inword = True
            i += 2
            continue
        if c in " \t\r":
            flush()
            i += 1
            continue
        if c == "\n":
            flush()
            toks.append(("op", ";"))
            i += 1
            continue
        if c in ";&|<>()":
            fd = ""
            if c in "<>" and inword and "".join(buf).isdigit():
                fd = "".join(buf)  # fd number of a redirect (2>, 1>>): kept on the op as "2>"
                buf, inword = [], False
            else:
                flush()
            for op in _OPS:
                if s.startswith(op, i):
                    toks.append(("op", (fd + op) if fd and fd not in ("0", "1") else op))
                    i += len(op)
                    break
            continue
        buf.append(c)
        inword = True
        i += 1
    flush()
    return toks


def shell_structure(toks):
    """Reduce compound commands (if/while/until/for/select/case, { }, [[ ]], (( )), function definitions) to
    the simple commands they run, so each body command is judged on its own.

    Returns (tokens, function names defined). Reserved words count only in command position, as in bash
    (`echo done` keeps its argument). Loop/case headers (`for x in a b`, `case $v in`, case patterns) are
    dropped: they run nothing except substitutions, which preprocess() already pulled out and analyzes.
    Anything that does not parse as expected is left in place, so it is judged as an (unknown) command."""
    out, funcs, stack = [], set(), []  # stack: "pattern" | "body" per open `case`
    i, n, cmdpos = 0, len(toks), True
    close_dbl, nxt = [None] * n, None  # index of the next `]]` word (one backward pass: linear on big scripts)
    for k in range(n - 1, -1, -1):
        close_dbl[k] = nxt
        if toks[k] == ("w", "]]"):
            nxt = k
    while i < n:
        kind, val = toks[i]
        if stack and stack[-1] == "pattern":
            if kind == "w" and val == "esac":
                stack.pop()
                i, cmdpos = i + 1, False
                continue
            if kind == "op" and val == ")":
                stack[-1] = "body"
                i, cmdpos = i + 1, True
                continue
            if kind == "w" or val in ("(", "|", ";"):
                i += 1
                continue
            stack.pop()  # malformed pattern list: stop treating it as a case
        if kind == "op":
            if stack and stack[-1] == "body" and (val == ";;" or (val == ";" and toks[i + 1:i + 2] == [("op", "&")])):
                stack[-1] = "pattern"  # ;; / ;& / ;;& end a case arm
                out.append(("op", ";"))
                i += 1 if val == ";;" else 2
                if toks[i:i + 1] == [("op", "&")]:
                    i += 1
                cmdpos = True
                continue
            out.append((kind, val))
            cmdpos = val in SEPS
            i += 1
            continue
        if not cmdpos:
            out.append((kind, val))
            i += 1
            continue
        if val in KW_OPEN:
            i += 1
            continue
        if val in KW_CLOSE or val == ARITH:
            if val == "esac" and stack and stack[-1] == "body":
                stack.pop()
            i, cmdpos = i + 1, False
            continue
        if val in ("for", "select") and i + 1 < n and toks[i + 1][0] == "w":
            j = i + 2  # `for x do`, `for x in a b ...`, `for (( ... ))` (ARITH): drop the header words
            if j < n and toks[j] != ("w", "do"):
                while j < n and toks[j][0] == "w":
                    j += 1
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", toks[i + 1][1]):
                out.append(("lv", toks[i + 1][1]))  # #27: the loop variable no longer holds a known value
            i = j
            continue
        if val == "case":
            j = i + 1
            while j < n and toks[j][0] == "w" and toks[j][1] != "in":
                j += 1
            if j < n and toks[j] == ("w", "in") and j > i + 1:
                stack.append("pattern")
                i = j + 1
                continue
        if val == "[[":
            j = close_dbl[i]
            if j is not None:  # a conditional expression: its && || < > ( ) are not shell operators
                i, cmdpos = j + 1, False
                continue
        if val == "function" and i + 1 < n and toks[i + 1][0] == "w":
            funcs.add(toks[i + 1][1])
            i += 2
            if toks[i:i + 2] == [("op", "("), ("op", ")")]:
                i += 2
            continue
        if toks[i + 1:i + 3] == [("op", "("), ("op", ")")] and re.fullmatch(r"[A-Za-z_][\w.:-]*", val):
            funcs.add(val)  # name() { body; }: the body is judged where it is defined
            i += 3
            continue
        out.append((kind, val))
        cmdpos = False
        i += 1
    return out, funcs


class SC:
    """One simple command."""
    __slots__ = ("argv", "assigns", "redirects", "fds", "stdin", "pipe_in", "pipe_out")

    def __init__(self):
        self.argv, self.assigns, self.redirects = [], [], []
        self.fds = []  # fd number of each redirect ("" = default; "2" for 2>/dev/null)
        self.stdin, self.pipe_in, self.pipe_out = None, False, False

    def out_targets(self):
        return [t for op, t in self.redirects if op in OUT_REDIRS and t not in DEV_SINKS]

    def stdout_captured(self):
        # `2>/dev/null` hides only stderr: the secret still reaches the transcript on stdout
        return any(op in (">", ">>", ">|", "&>", "&>>") and (op.startswith("&") or fd == "")
                   for (op, _), fd in zip(self.redirects, self.fds + [""] * len(self.redirects)))


_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")


def simple_commands(toks, heredocs, inherited_stdin=None):
    cmds, cur, expect = [], SC(), None

    def close(pipe):
        nonlocal cur
        if cur.argv or cur.assigns or cur.redirects:
            cur.pipe_out = pipe
            cmds.append(cur)
        elif pipe and cmds:
            cmds[-1].pipe_out = True
        cur = SC()
        cur.pipe_in = pipe

    for kind, val in toks:
        if kind == "lv":  # loop variable (shell_structure): an assignment of an unknown value
            close(False)
            cur.assigns.append(f"{val}={LOOPVAR}")
            close(False)
            continue
        if kind == "op":
            if val in SEPS:
                expect = None
                close(val in ("|", "|&"))
            else:
                expect = val
            continue
        if expect:
            m = HD_TOKEN_RE.fullmatch(val)
            fd, op = re.match(r"(\d*)(.*)", expect, re.S).groups()
            if m:
                cur.stdin = heredocs[int(m.group(1))]
            else:
                cur.redirects.append((op, val))
                cur.fds.append(fd)
            expect = None
            continue
        m = HD_TOKEN_RE.fullmatch(val)
        if m:
            cur.stdin = heredocs[int(m.group(1))]
            continue
        if not cur.argv and _ASSIGN_RE.match(val):
            cur.assigns.append(val)
            continue
        cur.argv.append(val)
    close(False)
    if inherited_stdin is not None:
        for c in cmds:
            if c.stdin is None and not c.pipe_in:
                c.stdin = inherited_stdin
                break
    return cmds


# ---------------------------------------------------------------- argv helpers
def positional(args, takes=(), long_takes=()):
    """Non-option arguments. `takes` = short options that consume a value; `long_takes` = long ones."""
    out, i, n = [], 0, len(args)
    while i < n:
        a = args[i]
        if a == "--":
            out.extend(args[i + 1:])
            break
        if a.startswith("--") and len(a) > 2:
            if "=" not in a and a[2:] in long_takes:
                i += 1
        elif a.startswith("-") and len(a) > 1:
            for k, ch in enumerate(a[1:]):
                if ch in takes:
                    if k == len(a) - 2:
                        i += 1
                    break
        else:
            out.append(a)
        i += 1
    return out


def split_after_opts(args, takes="", long_takes=()):
    """(first positional, raw words after it); leading options (and their values) are skipped."""
    i, n = 0, len(args)
    while i < n:
        a = args[i]
        if a == "--":
            i += 1
            break
        if a.startswith("--") and len(a) > 2:
            if "=" not in a and a[2:] in long_takes:
                i += 1
        elif a.startswith("-") and len(a) > 1:
            for k, ch in enumerate(a[1:]):
                if ch in takes:
                    if k == len(a) - 2:
                        i += 1
                    break
        else:
            break
        i += 1
    return (args[i], args[i + 1:]) if i < n else (None, [])


def opt_values(args, short, long_names=()):
    """Values of the given short option chars (clustered or separate) / long option names."""
    vals, i, n = [], 0, len(args)
    while i < n:
        a = args[i]
        if a == "--":
            break
        if a.startswith("--"):
            name, eq, v = a[2:].partition("=")
            if name in long_names:
                if eq:
                    vals.append(v)
                elif i + 1 < n:
                    vals.append(args[i + 1])
                    i += 1
        elif a.startswith("-") and len(a) > 1 and short:
            body = a[1:]
            for k, ch in enumerate(body):
                if ch in short:
                    if k + 1 < len(body):
                        vals.append(body[k + 1:])
                    elif i + 1 < n:
                        vals.append(args[i + 1])
                        i += 1
                    break
        i += 1
    return vals


def has_flag(args, shorts="", longs=()):
    for a in args:
        if a == "--":
            break
        if a.startswith("--"):
            if a[2:].split("=", 1)[0] in longs:
                return True
        elif a.startswith("-") and len(a) > 1 and shorts and any(ch in shorts for ch in a[1:]):
            return True
    return False


def base(word):
    return os.path.basename(word) if "/" in word else word


_WRAPPERS = {
    "sudo": ("ugCDphrtTU", ("user", "group", "close-from", "chdir", "prompt", "host", "role", "type",
                           "other-user", "command-timeout")),
    "doas": ("uC", ()),
    "env": ("uCS", ("unset", "chdir", "split-string")),
    "nohup": ("", ()), "nice": ("n", ("adjustment",)), "ionice": ("cnp", ("class", "classdata", "pid")),
    "stdbuf": ("ioe", ("input", "output", "error")), "time": ("fo", ("format", "output")),
    "command": ("", ()), "exec": ("a", ()), "builtin": ("", ()), "chronic": ("", ()),
    "timeout": ("sk", ("signal", "kill-after")), "flock": ("wcEn", ("wait", "timeout", "conflict-exit-code")),
    "unbuffer": ("", ()), "watch": ("nqd", ("interval", "differences")), "caffeinate": ("", ()), "setsid": ("", ()), "runuser": ("ugl", ("user", "group")),
}


def unwrap(argv):
    """Strip sudo/env/timeout/... prefixes. Returns (argv, assigns, notes) where notes may hold
    'shell' for `sudo -i` / `sudo -s` without a command (an interactive root shell)."""
    argv, assigns, notes = list(argv), [], set()
    for _ in range(12):
        if not argv:
            break
        name = base(argv[0])
        if name in ("!", "{", "}", "then", "do", "else", "elif", "if", "while", "until", "time"):
            argv = argv[1:]
            continue
        if name not in _WRAPPERS:
            break
        takes, long_takes = _WRAPPERS[name]
        if name == "command" and len(argv) > 1 and argv[1] in ("-v", "-V"):
            break
        i = 1
        while i < len(argv):
            a = argv[i]
            if a == "--":
                i += 1
                break
            if name == "env" and _ASSIGN_RE.match(a):
                assigns.append(a)
                i += 1
                continue
            if a.startswith("--"):
                if "=" not in a and a[2:] in long_takes:
                    i += 1
                if name == "sudo" and a in ("--login", "--shell"):
                    notes.add("shell")
                i += 1
                continue
            if a.startswith("-") and len(a) > 1:
                if name == "sudo" and any(ch in "is" for ch in a[1:]):
                    notes.add("shell")
                for k, ch in enumerate(a[1:]):
                    if ch in takes:
                        if k == len(a) - 2:
                            i += 1
                        break
                i += 1
                continue
            if name == "timeout" or (name == "flock" and not a.startswith("-")):
                i += 1  # duration / lock file
            break
        argv = argv[i:]
        while argv and _ASSIGN_RE.match(argv[0]):
            assigns.append(argv.pop(0))
        if argv:
            notes.discard("shell")
    return argv, assigns, notes


_REMOTE_ARG_RE = re.compile(r"^(?:([^@/:\s]+)@)?(\[[^\]]+\]|[^/:@\s]+):(.*)$")


def remote_arg(word):
    """(user, host, path) for scp/rsync `[user@]host:path` operands, else None."""
    if word.startswith(("/", "./", "../", "~")):
        return None
    if word.startswith("rsync://"):
        rest = word[8:]
        user, _, rest = rest.rpartition("@") if "@" in rest.split("/", 1)[0] else ("", "", rest)
        return (user or None, rest.split("/", 1)[0].split(":")[0], "/" + rest.split("/", 1)[-1])
    m = _REMOTE_ARG_RE.match(word)
    if not m or m.group(2).isdigit():
        return None
    return m.group(1), m.group(2), m.group(3)


def split_ssh_dest(word):
    if word.startswith("ssh://"):
        word = word[6:].split("/", 1)[0]
        if word.count(":") == 1:
            word = word.split(":")[0]
    user, _, host = word.rpartition("@")
    return (user or None), host


# =============================================================== path helpers
def expand(p, rt, cwd=None):
    """~ / $HOME / $HERMES_HOME / $JUDGE_REVIEW_DIR / $JUDGE_DIR expansion, made absolute against cwd."""
    if not p:
        return ""
    for var in ("HERMES_HOME", "JUDGE_REVIEW_DIR", "JUDGE_DIR", "HOME"):
        p = p.replace("${%s}" % var, rt[var])
        p = re.sub(r"\$%s(?![A-Za-z0-9_])" % var, lambda _m, v=rt[var]: v, p)
    if p == "~" or p.startswith("~/"):
        p = rt["HOME"] + p[1:]
    if not os.path.isabs(p):
        p = os.path.join(cwd or rt["HOME"], p)
    return os.path.normpath(p)


def variants(p):
    out = {p}
    try:
        out.add(os.path.realpath(p))
    except (OSError, ValueError):
        pass
    return out


def path_match(p, pattern):
    if pattern.endswith("/**"):
        root = pattern[:-3]
        return p == root or p.startswith(root.rstrip("/") + "/")
    return fnmatch.fnmatchcase(p, pattern)


# =============================================================== the gate
class Gate:
    def __init__(self, policy, rt=None):
        self.rt = rt or _runtime_paths()
        self.rules = {r["kind"]: r for r in policy["rules"]}
        self.hosts = Hosts(policy.get("hosts"), self.rt["HOME"])
        rm = self.rules.get("remote_mutation", {})
        self.ro_cmds = set(rm.get("readonly_commands") or ())
        self.ro_sub = {k: set(v) for k, v in (rm.get("readonly_subcommands") or {}).items()}
        so = self.rules.get("secret_output", {})
        self.secret_names = so.get("secret_names") or []
        self.not_secret = so.get("not_secret_names") or []
        self.print_cmds = set(so.get("print_commands") or ())
        self.safe_sinks = set(so.get("safe_sinks") or ())
        self.secret_grep = re.compile(so.get("secret_grep_pattern") or r"(?!x)x")
        oc = self.rules.get("oversight_config", {})
        self.config_path = expand(oc.get("path") or "$HERMES_HOME/config.yaml", self.rt)
        self.config_keys = list(oc.get("keys") or [])
        tops = [k for k in self.config_keys if "." not in k]
        nested = [k.rsplit(".", 1)[-1] for k in self.config_keys if "." in k]
        self.config_top = set(tops)
        self.config_nested = set(nested)
        self.config_token_re = re.compile(r"(?<![A-Za-z0-9_-])(%s)(?![A-Za-z0-9_-])"
                                          % "|".join(map(re.escape, tops + nested))) if tops + nested else None
        self.config_key_re = re.compile(r"(?<![A-Za-z0-9_-])[\"']?(%s)[\"']?\s*:"
                                        % "|".join(map(re.escape, tops + nested))) if tops + nested else None
        self.hits = []
        self._pat_cache = {}
        self.full_text = ""
        self.subject = None  # rule_key subject override (read_file: the resolved path)
        self.tainted = set()     # #27: copies of a secret made earlier in this command (abs paths)
        self.extra_texts = []    # script files read while analyzing (for the secret-mention scan)
        self._mention = None     # cached result of _line_mentions_secret()

    # ------------------------------------------------------------ hit bookkeeping
    def hit(self, kind, reason, subject=""):
        r = self.rules.get(kind)
        if not r:
            return
        self.hits.append({"rule": r["id"], "kind": kind, "action": r["action"], "reason": reason,
                          "subject": subject})

    # ------------------------------------------------------------ path classification
    def _pats(self, kind, key="paths"):
        ck = (kind, key)
        if ck not in self._pat_cache:
            vals = (self.rules.get(kind) or {}).get(key) or []
            if not vals and (kind, key) == ("secret_output", "secret_paths") and kind in self.rules:
                vals = DEFAULT_SECRET_PATHS
            out = []
            for v in ([vals] if isinstance(vals, str) else vals):
                glob = v.endswith("/**")
                for x in variants(expand(v[:-3] if glob else v, self.rt)):
                    out.append(x.rstrip("/") + "/**" if glob else x)
            self._pat_cache[ck] = out
        return self._pat_cache[ck]

    def _in_judge_dir(self, pv):
        markers = (self.rules.get("oversight_path") or {}).get("judge_dir_markers") or []
        for p in pv:
            d = p
            for _ in range(64):
                if os.path.basename(d) == "judge" and any(os.path.isfile(os.path.join(d, m)) for m in markers):
                    return True
                parent = os.path.dirname(d)
                if parent == d:
                    break
                d = parent
        return False

    def _ancestor_of(self, pv, kind):
        roots = [x[:-3] if x.endswith("/**") else x for x in self._pats(kind)]
        if kind == "oversight_path":
            roots.append(self.config_path)
        return any(r.startswith(p.rstrip("/") + "/") for p in pv for r in roots)

    def classify_path(self, abspath, tree=False):
        """'config' | 'oversight' | 'sensitive' | None for a normalized absolute path. tree=True: the
        operation is recursive/destructive (rm -r, mv, chmod -R), so a parent of a protected path counts."""
        pv = variants(abspath)
        if tree:
            if "oversight_path" in self.rules and self._ancestor_of(pv, "oversight_path"):
                return "oversight"
            if "sensitive_path" in self.rules and self._ancestor_of(pv, "sensitive_path"):
                return "sensitive"
        cfgv = variants(self.config_path)
        if "oversight_config" in self.rules or "hermes_config" in self.rules:
            if pv & cfgv:
                return "config"
        if "oversight_path" in self.rules:
            exc = self._pats("oversight_path", "except")
            if not any(path_match(p, x) for p in pv for x in exc):
                if any(path_match(p, x) for p in pv for x in self._pats("oversight_path")) or self._in_judge_dir(pv):
                    return "oversight"
        if "sensitive_path" in self.rules:
            if any(path_match(p, x) for p in pv for x in self._pats("sensitive_path")):
                return "sensitive"
        return None

    def is_secret_file(self, word, cwd=None, names=True):
        """Secret-shaped file: basename in secret_names, or the path under secret_paths (~/.config/spark/**,
        ~/.ssh/id_*, ...); not_secret_names (*.pub, *.example, ...) wins. The symlink target counts too.
        Shared by the terminal secret-output checks and read_file."""
        if not word or word in DEV_SINKS:
            return False
        b = os.path.basename(word.rstrip("/"))
        if names and b and not any(fnmatch.fnmatch(b, x) for x in self.not_secret) and \
                any(fnmatch.fnmatch(b, x) for x in self.secret_names):
            return True
        if any(t in word for t in ("__GATE_SUB", "`")):
            return False
        pats = self._pats("secret_output", "secret_paths")
        pvs = variants(expand(word, self.rt, cwd))
        if self.tainted and pvs & self.tainted:
            return True
        for p in pvs:
            pb = os.path.basename(p)
            if not pb or any(fnmatch.fnmatch(pb, x) for x in self.not_secret):
                continue
            if (names and any(fnmatch.fnmatch(pb, x) for x in self.secret_names)) or \
                    any(path_match(p, x) for x in pats):
                return True
        return False

    # ------------------------------------------------------------ write classification
    def local_write(self, raw, cwd, tool, how, text_for_keys=None, tree=False):
        """A local write of `raw` (path as written). text_for_keys: the edit text for config.yaml checks."""
        if not raw or raw in DEV_SINKS or raw.startswith("/dev/") or raw.startswith("/proc/self/fd"):
            return
        p = expand(raw, self.rt, cwd)
        cls = self.classify_path(p, tree)
        if cls == "config":
            self.config_edit_terminal(p, how, text_for_keys)
        elif cls == "oversight":
            self.hit("oversight_path", f"{how} {p}", p)
        elif cls == "sensitive":
            self.hit("sensitive_path", f"{how} {p}", p)

    def config_edit_terminal(self, p, how, text):
        """Terminal edit of config.yaml: the full change can't be diffed, so look for oversight key names
        anywhere in the whole terminal command (a staged copy edited earlier in the chain counts)."""
        if text is not None or self.full_text:
            text = "\n".join(x for x in (text, self.full_text) if x)
        if text is not None and self.config_token_re and self.config_token_re.search(text):
            key = self.config_token_re.search(text).group(1)
            self.hit("oversight_config", f"{how} {p} mentioning `{key}`", p)
        elif text is None:
            self.hit("hermes_config", f"{how} {p} (content not inspectable; may touch oversight keys)", p)
        else:
            self.hit("hermes_config", f"{how} {p}", p)

    # ------------------------------------------------------------ script analysis
    def analyze(self, text, ctx, depth=0, stdin=None):
        """Walk a shell script. ctx: {'remote': label|None, 'captured': bool, 'cwd': str}.
        Returns True when the script reads a secret (used for `$(cat key)` tracking)."""
        if depth > MAX_DEPTH:
            if ctx["remote"]:
                self.hit("remote_mutation", "command nesting too deep to analyze", text[:80])
            return False
        if not text or not text.strip():
            return False
        ctx.setdefault("secret_vars", set())
        if "vars" not in ctx:
            ctx["vars"] = self._seed_vars()
        body, substs, heredocs = preprocess(text)
        toks, funcs = shell_structure(tokenize(body))
        ctx.setdefault("functions", set()).update(funcs)
        cmds = simple_commands(toks, heredocs, stdin)
        # Command substitutions run in order with the commands that hold them (#27: `f=key; echo "$(head
        # "$f")"` must see f). Substitutions in dropped loop/case headers and [[ ]] run first.
        sub_secret = {}
        procsubs = {k for k, src in enumerate(substs) if isinstance(src, ProcSub)}

        def run_sub(k):
            if k < len(substs) and k not in sub_secret:
                sub_secret[k] = False
                sub_secret[k] = self.analyze(substs[k], dict(ctx, captured=True), depth + 1)

        held = {int(m) for sc in cmds for w in sc.assigns + sc.argv + [t for _, t in sc.redirects]
                for m in SUB_RE.findall(w)}
        for k in range(len(substs)):
            if k not in held:
                run_sub(k)
        reads_secret = False
        cwd = ctx["cwd"]
        secret_vars = ctx["secret_vars"]
        for i, sc in enumerate(cmds):
            self._bind(sc, ctx, run_sub, sub_secret)
            sink = cmds[i + 1] if sc.pipe_out and i + 1 < len(cmds) else None
            captured = ctx["captured"] or sc.stdout_captured() or (sink is not None and self._safe_sink(sink))
            sctx = dict(ctx, captured=captured, cwd=cwd, procsubs=procsubs, sub_secret=sub_secret)
            if sc.pipe_in and i > 0 and not sctx.get("maybe_secret"):
                # `find <dir holding a secret> | xargs cat`: the names on stdin may be secrets
                sctx["maybe_secret"] = self._listing_maybe(cmds[i - 1], cwd)
            r = self.simple(sc, sctx, depth, sub_secret, sink)
            reads_secret = reads_secret or r
            if r:
                self._after_secret_read(sc, sink, sctx)
            cwd = sctx["cwd"]
        return reads_secret

    # ------------------------------------------------------------ shell variables (#27)
    def _seed_vars(self):
        return {k: self.rt[k] for k in ("HOME", "HERMES_HOME", "JUDGE_REVIEW_DIR", "JUDGE_DIR")}

    @staticmethod
    def subst(word, env):
        """$X / ${X} replaced by the known literal value of X; anything else is left in place."""
        if "$" not in word:
            return word

        def rep(m):
            v = env.get(m.group(1) or m.group(2))
            return m.group(0) if v is None else v
        return VAR_RE.sub(rep, word)

    def _bind(self, sc, ctx, run_sub, sub_secret):
        """Run the substitutions this command holds, substitute known variables into its words and record
        its assignments (prefix assignments, X=..., export/local/declare/readonly/typeset X=..., read X, unset X).
        Over-approximates bash on purpose: a prefix assignment (`X=1 cmd`) is kept for later commands too."""
        env, secret_vars = ctx["vars"], ctx["secret_vars"]

        def assign(a):
            for m in SUB_RE.findall(a):
                run_sub(int(m))
            name, _, raw = a.partition("=")
            name = name.rstrip("+")
            arr = "[" in name
            name = name.split("[", 1)[0]
            val = self.subst(raw, env)
            if val == "~" or val.startswith("~/"):
                val = self.rt["HOME"] + val[1:]
            tainted = any(sub_secret.get(int(m)) for m in SUB_RE.findall(a)) or \
                self._mentions_secret_var([raw], ctx, {})
            if tainted:
                secret_vars.add(name)
            else:
                secret_vars.discard(name)
            env[name] = None if (arr or a.split("=", 1)[0].endswith("+") or UNRES_RE.search(val)) else val
            return f"{name}={val}" if not arr else a

        sc.assigns = [assign(a) for a in sc.assigns]
        for w in sc.argv + [t for _, t in sc.redirects]:
            for m in SUB_RE.findall(w):
                run_sub(int(m))
        sc.argv = [self.subst(w, env) for w in sc.argv]
        sc.redirects = [(op, self.subst(t, env)) for op, t in sc.redirects]
        argv, _, _ = unwrap(sc.argv)
        if not argv:
            return
        name = argv[0]
        if name in DECL_BUILTINS:
            for a in argv[1:]:
                if _ASSIGN_RE.match(a):
                    assign(a)
                elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", a) and name in ("local", "declare", "typeset"):
                    env[a] = None if a not in env else env[a]
        elif name in ("read", "mapfile", "readarray", "getopts") or (name == "printf" and "-v" in argv):
            names = opt_values(argv[1:], "v") if name == "printf" else positional(argv[1:], "adnNptuOsCc")
            for v in names:
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v):
                    env[v] = None
        elif name == "unset":
            for v in argv[1:]:
                env.pop(v, None)
                secret_vars.discard(v)

    def _line_mentions_secret(self):
        """First secret-shaped word anywhere in the command (and the scripts it runs), or None.
        Used only when a printing command has an argument the gate can't resolve."""
        if self._mention is None:
            self._mention = ""
            seen = set()
            # the command line itself: any secret-shaped word; script files it runs (often long, with comments
            # and site.env plumbing): configured secret paths only, to avoid approval fatigue
            for text, names in [(self.full_text, True)] + [(t, False) for t in self.extra_texts]:
                for frag in re.split(r"[\s'\"`;|&<>(){}=,:\[\]]+", self.subst(text, self._seed_vars())):
                    frag = frag.lstrip("-+?!@")
                    if not frag or (frag, names) in seen or len(frag) > 512 or UNRES_RE.search(frag):
                        continue
                    seen.add((frag, names))
                    if self.is_secret_file(frag, None, names=names):
                        self._mention = frag
                        return frag
        return self._mention or None

    def _covers_secret(self, word, cwd):
        """A directory that holds a configured secret path (`~/.config`, `/etc`, `~`), as a find root."""
        if not word or UNRES_RE.search(word) or any(c in word for c in "*?"):
            return False
        roots = [x[:-3] if x.endswith("/**") else x for x in self._pats("secret_output", "secret_paths")]
        for p in variants(expand(word, self.rt, cwd)):
            pre = p.rstrip("/") + "/"
            if any(r.startswith(pre) or r == p for r in roots):
                return True
        return False

    def _maybe_secret(self, ctx):
        return ctx.get("maybe_secret") or self._line_mentions_secret()

    def _listing_maybe(self, sc, cwd):
        """Producer of a pipeline (`find ... | xargs cat`): why its output may name a secret file, or None."""
        argv, _, _ = unwrap(sc.argv)
        if not argv or base(argv[0]) != "find":
            return None
        return self._find_maybe(argv[1:], cwd)

    def _find_maybe(self, args, cwd):
        roots = []
        for a in args:
            if a.startswith(("-", "(", "!")):
                break
            roots.append(a)
        filters = [args[k + 1] for k, a in enumerate(args[:-1]) if a in ("-name", "-iname", "-path", "-ipath",
                                                                          "-wholename", "-regex", "-iregex")]
        for f in filters:
            if self.is_secret_file(f, cwd):
                return f
        exempt = filters and all(any(fnmatch.fnmatch(f, x) for x in self.not_secret) for f in filters)
        for r in roots or ["."]:
            if self.is_secret_file(r, cwd):
                return r
            if not exempt and self._covers_secret(r, cwd):
                return r
        return None

    def _after_secret_read(self, sc, sink, ctx):
        """A command that read a secret: a file it writes is a copy of the secret (taint), and a digest of a
        byte-limited part of it is brute-forceable."""
        for t in sc.out_targets():
            if not re.fullmatch(r"&?\d+", t) and not ctx["remote"]:
                self.tainted |= variants(expand(t, self.rt, ctx["cwd"]))
        if sink is None:
            return
        sargv, _, _ = unwrap(sink.argv)
        if not sargv or base(sargv[0]) not in HASH_SINKS:
            return
        argv, _, _ = unwrap(sc.argv)
        name, args = (base(argv[0]), argv[1:]) if argv else ("", [])
        partial = (name in ("head", "tail") and has_flag(args, "c", ("bytes",))) or \
                  (name == "cut" and has_flag(args, "cb", ("bytes", "characters"))) or \
                  (name == "dd" and any(a.startswith(("count=", "skip=")) for a in args))
        if partial:
            self.hit("secret_output", f"{name} of part of a secret piped to {base(sargv[0])}: a digest of a few "
                     "bytes is brute-forceable", " ".join(argv)[:120])

    def _safe_sink(self, sink):
        argv, _, _ = unwrap(sink.argv)
        if not argv:
            return False
        name = base(argv[0])
        if name in self.safe_sinks:
            return True
        if name in ("grep", "egrep", "fgrep", "rg") and has_flag(argv[1:], "qlLc", ("quiet", "silent", "count",
                                                                                     "files-with-matches")):
            return True
        return False

    def _mentions_secret_var(self, args, ctx, sub_secret):
        sv = ctx.get("secret_vars") or set()
        for a in args:
            if any(sub_secret.get(int(m)) for m in SUB_RE.findall(a)):
                return True
            for v in sv:
                if re.search(r"\$\{?%s(?![A-Za-z0-9_])" % re.escape(v), a):
                    return True
        return False

    def simple(self, sc, ctx, depth, sub_secret, sink):
        argv, assigns, notes = unwrap(sc.argv)
        sc_assigns = sc.assigns + assigns
        sc.assigns = sc_assigns
        remote = ctx["remote"]
        if not argv:
            if "shell" in notes:
                if remote:
                    self.hit("remote_opaque", f"interactive root shell on {remote} (sudo -i/-s)", "sudo -s")
                return False
            # `$(<file)` / `x=$(<file)`: a bare input redirect reads the file into the capture.
            reads = [t for op, t in sc.redirects if op == "<" and self._file_kind(t, ctx) == "secret"]
            if reads and not ctx["captured"]:
                self.hit("secret_output", f"prints {reads[0]}", reads[0])
            unk = [t for op, t in sc.redirects if op == "<" and self._file_kind(t, ctx) == "unknown"]
            if not reads and unk and self._maybe_secret(ctx):
                reads = unk
                if not ctx["captured"]:
                    self.hit("secret_output_unknown", f"reads {unk[0]} (not resolvable) in a command that "
                             f"names {self._maybe_secret(ctx)}", unk[0])
            for t in sc.out_targets():
                if remote:
                    self.hit("remote_mutation", f"redirect > {t} on {remote}", t)
                else:
                    self._write_target(t, ctx, "redirect >", sc)
            return bool(reads)
        name = base(argv[0])
        args = argv[1:]
        if name in (ctx.get("functions") or ()) and "/" not in argv[0]:
            # call of a function defined in this script: its body was judged at the definition; the
            # call's own redirects still count.
            for t in sc.out_targets():
                if remote:
                    self.hit("remote_mutation", f"redirect > {t} on {remote}", t)
                else:
                    self._write_target(t, ctx, "redirect >", sc)
            return False

        # Shells and eval: recurse into the code they run.
        if name in ("bash", "sh", "zsh", "dash", "ksh", "ash", "busybox") or name == "eval":
            if name == "busybox" and args and args[0] in ("sh", "ash"):
                args = args[1:]
            if name == "eval":
                return self.analyze(" ".join(args), ctx, depth + 1)
            code = opt_values(args, "c")
            if code or has_flag(args, "c"):
                # bash -c 'script' arg0 args...: the script is the first positional after -c
                src = code[0] if code else (positional(args)[0] if positional(args) else "")
                return self.analyze(src, ctx, depth + 1)
            pos = positional(args, "oO", ("rcfile", "init-file"))
            if sc.stdin is not None and (not pos or has_flag(args, "s")):
                return self.analyze(sc.stdin, ctx, depth + 1)
            if pos:
                return self._run_script_file(pos[0], pos[1:], ctx, depth)
            if remote:
                if sc.pipe_in or any(op == "<" for op, _ in sc.redirects):
                    self.hit("remote_opaque", f"remote shell on {remote} fed from a local pipe/file", name)
                else:
                    self.hit("remote_opaque", f"interactive shell on {remote}", name)
            return False

        if name in ("ssh", "scp", "rsync", "sftp", "mosh"):
            return self._ssh_family(name, args, sc, ctx, depth, sink)

        reads_secret = self._secret_check(name, args, sc, ctx, sub_secret, depth)

        if remote:
            for reason in self.remote_reasons(name, args, sc, ctx, depth):
                self.hit("remote_mutation", f"{reason} on {remote}", " ".join(argv)[:200])
            for t in sc.out_targets():
                if not re.fullmatch(r"&?\d+|-", t):
                    self.hit("remote_mutation", f"redirect > {t} on {remote}", " ".join(argv)[:200])
            return reads_secret

        self._local_rules(name, args, argv, sc, ctx, depth)
        return reads_secret

    # ------------------------------------------------------------ script files
    def _run_script_file(self, path, args, ctx, depth):
        if ctx["remote"]:
            self.hit("remote_mutation", f"runs script {path} (contents not visible) on {ctx['remote']}", path)
            return False
        p = expand(path, self.rt, ctx["cwd"])
        if self.classify_path(p) == "oversight" and os.path.basename(p) == "install.sh" and "--apply" in args:
            self.hit("oversight_path", f"runs {p} --apply (rewrites the Hermes hooks config)", p)
        try:
            if os.path.isfile(p) and os.path.getsize(p) <= MAX_SCRIPT_PEEK:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    head = fh.read(MAX_SCRIPT_PEEK)
                self.extra_texts.append(head)
                self._mention = None
                first = head.split("\n", 1)[0]
                if not first.startswith("#!") or re.search(r"\b(ba|z|da|k)?sh\b", first):
                    return self.analyze(head, dict(ctx, captured=ctx["captured"]), depth + 1)
        except OSError:
            pass
        return False

    # ------------------------------------------------------------ ssh / scp / rsync
    def _ssh_family(self, name, args, sc, ctx, depth, sink):
        remote = ctx["remote"]
        if name in ("ssh", "mosh"):
            takes = "BbcDEeFIiJLlmOopQRSWw"
            i, user_opt, dest, no_cmd, ctl = 0, None, None, False, False
            while i < len(args):
                a = args[i]
                if a == "--":
                    i += 1
                    break
                if a.startswith("-") and len(a) > 1 and not a.startswith("--"):
                    body = a[1:]
                    for k, ch in enumerate(body):
                        if ch == "N":
                            no_cmd = True
                        if ch in takes:
                            val = body[k + 1:] or (args[i + 1] if i + 1 < len(args) else "")
                            if not body[k + 1:]:
                                i += 1
                            if ch == "l":
                                user_opt = val
                            elif ch == "o" and val.lower().startswith("user"):
                                user_opt = re.split(r"[= ]", val, maxsplit=1)[-1]
                            elif ch in "OW":
                                ctl = True
                            break
                    i += 1
                    continue
                if a.startswith("--"):
                    i += 1
                    continue
                break
            if i >= len(args):
                return False
            dest, rest = args[i], args[i + 1:]
            # OpenSSH also accepts options after the destination (`ssh host -t -- cmd`)
            while rest and rest[0].startswith("-") and len(rest[0]) > 1:
                opt = rest.pop(0)
                if opt == "--":
                    break
                body = opt[1:]
                for k, ch in enumerate(body):
                    if ch == "N":
                        no_cmd = True
                    if ch in takes:
                        val = body[k + 1:] or (rest.pop(0) if rest else "")
                        if ch == "l":
                            user_opt = val
                        elif ch == "o" and val.lower().startswith("user"):
                            user_opt = re.split(r"[= ]", val, maxsplit=1)[-1]
                        break
            user, host = split_ssh_dest(dest)
            label = self.hosts.match(user or user_opt, host)
            if not label:
                if remote:  # hop from a guarded host to an unguarded one: unknown effect
                    self.hit("remote_mutation", f"ssh hop to {host} from {remote}", dest)
                return False
            rctx = dict(ctx, remote=label, cwd="/")
            if rest:
                self.analyze(" ".join(rest), rctx, depth + 1, stdin=sc.stdin)
                if sc.stdin is None and (sc.pipe_in or any(op == "<" for op, _ in sc.redirects)):
                    first = base(rest[0])
                    if first in ("bash", "sh", "zsh", "dash", "sudo", "python3", "python", "perl") and \
                            not has_flag(rest[1:], "c"):
                        self.hit("remote_opaque", f"script fed to {label} from a local pipe/file", dest)
                return False
            if sc.stdin is not None:
                self.analyze(sc.stdin, rctx, depth + 1)
                return False
            if no_cmd or ctl:
                return False
            if sc.pipe_in or any(op == "<" for op, _ in sc.redirects):
                self.hit("remote_opaque", f"script fed to {label} from a local pipe/file", dest)
            else:
                self.hit("remote_opaque", f"interactive ssh session on {label}", dest)
            return False

        if name == "sftp":
            pos = positional(args, "BbcDFiJlPRSso")
            for d in pos[:1]:
                user, host = split_ssh_dest(d.split(":", 1)[0] if ":" in d and "@" in d.split(":")[0] else d)
                label = self.hosts.match(user, host)
                if label:
                    self.hit("remote_copy", f"sftp session to {label} (can upload)", d)
            return False

        if name == "scp":
            pos = positional(args, "cDFiJlOoPS")
            if len(pos) < 2:
                return False
            dest = pos[-1]
            ra = remote_arg(dest)
            if ra:
                label = self.hosts.match(ra[0], ra[1])
                if label:
                    self.hit("remote_copy", f"scp to {label}:{ra[2]}", dest)
            else:
                self.local_write(dest, ctx["cwd"], "scp", "scp into")
                if remote:
                    self.hit("remote_mutation", f"scp writes {dest} on {remote}", dest)
            return False

        if name == "rsync":
            pos = positional(args, "efBTM", ("rsh", "rsync-path", "exclude", "include", "filter", "temp-dir",
                                              "log-file", "password-file", "port", "files-from", "exclude-from",
                                              "include-from", "out-format", "chmod", "chown", "usermap",
                                              "groupmap", "backup-dir", "suffix", "compare-dest", "copy-dest",
                                              "link-dest", "partial-dir", "timeout", "contimeout", "bwlimit",
                                              "max-size", "min-size", "info", "debug", "sockopts"))
            if len(pos) < 2:
                return False
            dest, srcs = pos[-1], pos[:-1]
            ra = remote_arg(dest)
            if ra:
                label = self.hosts.match(ra[0], ra[1])
                if label:
                    self.hit("remote_copy", f"rsync to {label}:{ra[2]}", dest)
                return False
            for s in srcs:
                rs = remote_arg(s)
                if rs and has_flag(args, "", ("remove-source-files",)):
                    label = self.hosts.match(rs[0], rs[1])
                    if label:
                        self.hit("remote_mutation", f"rsync --remove-source-files from {label}", s)
            self.local_write(dest, ctx["cwd"], "rsync", "rsync into")
            if remote:
                self.hit("remote_mutation", f"rsync writes {dest} on {remote}", dest)
            return False
        return False

    # ------------------------------------------------------------ secrets in output
    def _print_files(self, name, args, sc, ctx):
        """Files a printing command reads (positional operands and `<` redirects); [] when it prints none."""
        files = []
        if name in ("grep", "egrep", "fgrep", "rg", "zgrep"):
            pos = positional(args, "efABCmd", ("regexp", "file", "max-count", "after-context",
                                               "before-context", "context"))
            explicit = opt_values(args, "e", ("regexp",))
            pats = explicit or pos[:1]
            files = pos if explicit else pos[1:]
            quiet = has_flag(args, "qlLc", ("quiet", "silent", "count", "files-with-matches",
                                            "files-without-match"))
            if not quiet and any(self.secret_grep.search(p) for p in pats) and not ctx["captured"]:
                self.hit("secret_output", f"{name} for secret-looking pattern prints matching lines",
                         " ".join([name] + args)[:120])
            if quiet:
                return []
        elif name == "sed":
            if has_flag(args, "i", ("in-place",)):
                return []
            pos = positional(args, "ef", ("expression", "file"))
            files = pos if opt_values(args, "e", ("expression",)) else pos[1:]
        elif name in ("awk", "gawk", "mawk"):
            pos = positional(args, "fvF")
            files = pos if opt_values(args, "f") else pos[1:]
        elif name == "dd":
            files = [a[3:] for a in args if a.startswith("if=")]
        elif name == "jq":
            pos = positional(args, "", ("arg", "argjson", "slurpfile", "rawfile", "indent"))
            files = pos[1:]
        else:
            files = positional(args, "nNcwsk" if name in ("head", "tail", "base64", "cut", "fold") else "")
        return files + [t for op, t in sc.redirects if op == "<"]

    def _secret_or_unknown(self, name, files, ctx, verb="prints"):
        """Shared tail of the read checks: a secret operand escalates as secret-output; an operand the gate can't
        resolve escalates as secret-output-unknown when the command names a secret somewhere (#27).
        Returns True when the command (may have) read a secret."""
        kinds = [(f, self._file_kind(f, ctx)) for f in files]
        hits = [f for f, k in kinds if k == "secret"]
        where = f" on {ctx['remote']}" if ctx["remote"] else ""
        if hits:
            if not ctx["captured"]:
                shown = hits[0] if not SUB_RE.fullmatch(hits[0]) else "a process substitution that reads a secret"
                self.hit("secret_output", f"{name} {shown} {verb} a secret{where}", hits[0])
            return True
        unk = [f for f, k in kinds if k == "unknown"]
        why = self._maybe_secret(ctx) if unk else None
        if why:
            if not ctx["captured"]:
                shown = unk[0].replace(XARG, "{xargs/find argument}").replace(LOOPVAR, "{loop variable}")
                self.hit("secret_output_unknown", f"{name} {verb} {shown} (not resolvable) in a command that "
                         f"names {why}{where}", unk[0])
            return True
        return False

    def _secret_check(self, name, args, sc, ctx, sub_secret, depth=0):
        """Returns True when the command reads a secret file (printed or captured)."""
        reads = False
        if depth > MAX_DEPTH:
            return False
        if name in self.print_cmds:
            reads = self._secret_or_unknown(name, self._print_files(name, args, sc, ctx), ctx)
        elif name in ("echo", "printf") and not ctx["captured"]:
            if self._mentions_secret_var(args, ctx, sub_secret):
                self.hit("secret_output", f"{name} of a value read from a secret file", name)
        elif name == "wg" and not ctx["captured"]:
            if (args and args[0] == "showconf") or any(a in ("private-key", "preshared-keys") for a in args):
                self.hit("secret_output", "wg prints private key material", "wg " + " ".join(args[:2]))
        elif name == "wg-quick" and args[:1] == ["strip"] and not ctx["captured"]:
            self.hit("secret_output", "wg-quick strip prints private key material", "wg-quick strip")
        elif name == "openssl" and args and args[0] in ("rsa", "pkey", "ec", "pkcs12") and not ctx["captured"]:
            if not any(a in ("-pubout", "-noout") for a in args):
                ins = [args[k + 1] for k, a in enumerate(args[:-1]) if a == "-in"]
                if any(self.is_secret_file(x, ctx["cwd"]) for x in ins):
                    self.hit("secret_output", f"openssl {args[0]} prints a private key", args[0])
                elif any(UNRES_RE.search(x) for x in ins):
                    self._secret_or_unknown(f"openssl {args[0]}", ins, ctx)
        elif name in ("cp", "install", "ln", "mv") and not ctx["remote"]:
            reads = self._copy_check(name, args, ctx)
        elif name == "xargs":
            reads = self._xargs_check(args, ctx, sub_secret, depth)
        elif name == "find":
            reads = self._find_check(args, ctx, sub_secret, depth)
        elif name in INTERPRETERS or re.fullmatch(r"python3\.\d+", name):
            reads = self._interp_check(name, args, sc, ctx)
        if name == "read":
            for op, t in sc.redirects:
                if op == "<" and (self.is_secret_file(t, ctx["cwd"]) or
                                  (UNRES_RE.search(t) and self._maybe_secret(ctx))):
                    for v in positional(args, "adnNptu"):
                        ctx.setdefault("secret_vars", set()).add(v)
                    reads = True
        return reads

    def _file_kind(self, word, ctx):
        """'secret' | 'unknown' | None for a file operand. A process substitution <(cmd) is a pipe: it is a
        secret when cmd read one, and never unknown (cmd itself was analyzed)."""
        m = SUB_RE.fullmatch(word)
        if m and int(m.group(1)) in (ctx.get("procsubs") or ()):
            return "secret" if (ctx.get("sub_secret") or {}).get(int(m.group(1))) else None
        if self.is_secret_file(word, ctx["cwd"]):
            return "secret"
        return "unknown" if UNRES_RE.search(word) else None

    def _copy_check(self, name, args, ctx):
        """cp/install/ln/mv of a secret: to stdout prints it; to a file makes a copy that counts as the secret for
        the rest of this command (`cp key /tmp/x; cat /tmp/x`)."""
        tdir = opt_values(args, "t", ("target-directory",))
        pos = positional(args, "tSmogb" if name == "install" else "tS",
                         ("target-directory", "suffix", "mode", "owner", "group", "backup"))
        if tdir:
            srcs, dest = pos, tdir[0]
        elif len(pos) >= 2:
            srcs, dest = pos[:-1], pos[-1]
        else:
            return False
        if dest in STDOUT_FILES:
            return self._secret_or_unknown(name, srcs, ctx, verb=f"to {dest} prints")
        secret = [s for s in srcs if self.is_secret_file(s, ctx["cwd"])]
        if secret:
            d = expand(dest, self.rt, ctx["cwd"])
            self.tainted |= variants(d)
            for s in secret:
                self.tainted.add(os.path.join(d, os.path.basename(s.rstrip("/"))))
            return True
        return False

    def _inner_check(self, inner, ctx, sub_secret, depth):
        """A command run by xargs / find -exec, whose XARG words are filled in at run time."""
        inner, _, _ = unwrap(inner)
        if not inner:
            return False
        iname, iargs = base(inner[0]), inner[1:]
        if iname in ("bash", "sh", "zsh", "dash", "ksh", "ash"):
            code = opt_values(iargs, "c")
            if code:
                # `sh -c 'cat "$1"' _ {}`: $1 stays unresolved; `sh -c 'cat {}'`: the XARG word does
                return self.analyze(code[0], ctx, depth + 1)
            return False
        if iname in ("xargs", "find"):
            return False
        fake = SC()
        fake.argv = inner
        return self._secret_check(iname, iargs, fake, ctx, sub_secret, depth + 1)

    def _xargs_check(self, args, ctx, sub_secret, depth):
        """`... | xargs cat`: the file names come from stdin. `echo ~/.config/spark/k | xargs cat` escalates
        (the line names a secret), as does `find <dir holding a secret> | xargs cat`."""
        first, more = split_after_opts(args, "IinPdLsEa", ("replace", "max-args", "max-procs", "delimiter",
                                                           "max-lines", "max-chars", "eof", "arg-file"))
        if not first:
            return False
        rep = opt_values(args, "I", ("replace",)) or (["{}"] if has_flag(args, "i") else [])
        inner = [first] + more
        if rep and rep[0]:
            inner = [w.replace(rep[0], XARG) for w in inner]
        else:
            inner = inner + [XARG]
        return self._inner_check(inner, ctx, sub_secret, depth)

    def _find_check(self, args, ctx, sub_secret, depth):
        """`find <roots> ... -exec cat {} +`: each {} is a file under the roots."""
        reads = False
        maybe = None
        i = 0
        while i < len(args):
            if args[i] in ("-exec", "-execdir", "-ok", "-okdir"):
                j = i + 1
                while j < len(args) and args[j] not in (";", "+"):
                    j += 1
                inner = [w.replace("{}", XARG) for w in args[i + 1:j]]
                if maybe is None:
                    maybe = self._find_maybe(args, ctx["cwd"]) or ""
                sctx = dict(ctx, maybe_secret=ctx.get("maybe_secret") or maybe or None)
                reads = self._inner_check(inner, sctx, sub_secret, depth) or reads
                i = j
            i += 1
        return reads

    def _interp_check(self, name, args, sc, ctx):
        """python/perl/ruby/node -c/-e code (or a script on stdin) that reads a secret file and prints it.
        Write detection for the same code lives in _inline_code."""
        flag = "r" if name == "php" else "ce"
        code = opt_values(args, flag)
        if not code and sc.stdin is not None and (not positional(args) or "-" in args):
            code = [sc.stdin]
        operands = [a for a in positional(args, flag) if a not in code]
        if not code:
            return False
        c = code[0]
        cands = set(re.findall(r"(?:~|\$\{?HOME\}?|\$\{?HERMES_HOME\}?)?/[A-Za-z0-9_./@+-]+", c))
        cands |= set(re.findall(r"['\"]([^'\"\s]{1,300})['\"]", c))
        cands |= set(operands)
        reads_code = re.search(r"\.read\w*\(|read_text|read_bytes|readlines|readFileSync|readFile\(|File\.read|"
                               r"IO\.read|file_get_contents|open\(|<\s*\$?\w*\s*>|\bslurp\b", c)
        implicit = name in ("perl", "ruby") and has_flag(args, "np")  # -n/-p read the operands line by line
        if not (reads_code or implicit):
            return False
        files = [x for x in cands if "/" in x or "." in x or x in operands]
        hits = [x for x in files if self.is_secret_file(x, ctx["cwd"])]
        if hits:
            if not ctx["captured"]:
                self.hit("secret_output", f"{name} code reads {hits[0]} and may print it", hits[0])
            return True
        unk = [x for x in operands if UNRES_RE.search(x)]
        if unk:
            return self._secret_or_unknown(name, unk, ctx, verb="code reads")
        return False

    # ------------------------------------------------------------ remote classification
    def remote_reasons(self, name, args, sc, ctx, depth):
        """Reasons a remote simple command mutates state; [] = read-only."""
        sub_ro = self.ro_sub
        if name in ("systemctl", "ufw", "certbot", "apt", "wg", "fail2ban-client", "timedatectl", "hostnamectl",
                    "resolvectl", "loginctl", "nft"):
            pos = positional(args, "HMtpnosC" if name == "systemctl" else "",
                             ("host", "machine", "type", "state", "property", "lines", "output", "signal",
                              "kill-whom", "root", "what", "cert-name", "config-dir", "work-dir", "logs-dir"))
            sub = pos[0] if pos else ""
            if name == "certbot":
                flags_only = not pos and all(a in ("--version", "-h", "--help") for a in args)
                if flags_only:
                    return []
                if sub in sub_ro.get("certbot", ()):
                    return []
                if "--dry-run" in args:
                    return [f"certbot {sub or ''} --dry-run (pre/post hooks still run)".replace("  ", " ")]
                return [f"certbot {sub}".strip()]
            if name == "ufw" and sub == "app":
                return [] if (pos[1:2] and pos[1] in ("list", "info")) else ["ufw app " + " ".join(pos[1:2])]
            if name == "systemctl" and has_flag(args, "", ("version",)) and not pos:
                return []
            if sub in sub_ro.get(name, ()):
                return []
            return [f"{name} {sub}".strip()]
        if name in ("docker", "docker-compose", "podman"):
            return self._docker(name, args, sc, ctx, depth)
        if name in ("iptables", "ip6tables", "iptables-legacy", "iptables-nft", "ip6tables-legacy",
                    "ip6tables-nft"):
            mut_long = ("append", "insert", "delete", "replace", "flush", "delete-chain", "new-chain", "policy",
                        "rename-chain", "zero")
            if has_flag(args, "AIDRFXNPEZ", mut_long):
                return [f"{name} (rule change)"]
            if has_flag(args, "LSC", ("list", "list-rules", "check")):
                return []
            return [f"{name} (unrecognized operation)"]
        if name == "nginx":
            if has_flag(args, "s", ("signal",)):
                return ["nginx -s " + " ".join(opt_values(args, "s"))]
            if has_flag(args, "tTvV?h"):
                return []
            return ["nginx (starts/reloads the server)"]
        if name == "journalctl":
            if any(a.startswith(("--vacuum", "--rotate", "--flush", "--sync", "--relinquish-var",
                                 "--smart-relinquish-var", "--setup-keys", "--update-catalog")) for a in args):
                return ["journalctl maintenance"]
            return []
        if name == "find":
            out = []
            i = 0
            while i < len(args):
                a = args[i]
                if a in ("-delete", "-fprint", "-fprint0", "-fprintf", "-fls"):
                    out.append(f"find {a}")
                if a in ("-exec", "-execdir", "-ok", "-okdir"):
                    j = i + 1
                    while j < len(args) and args[j] not in (";", "+"):
                        j += 1
                    inner = [x for x in args[i + 1:j]]
                    inner, _, _ = unwrap(inner)
                    if inner:
                        sub_reasons = self._inner_remote(inner, ctx, depth)
                        out.extend(f"find {a} {r}" for r in sub_reasons)
                    i = j
                i += 1
            return out
        if name == "sed":
            if has_flag(args, "i", ("in-place",)):
                return ["sed -i"]
            pos = positional(args, "ef", ("expression", "file"))
            scripts = opt_values(args, "e", ("expression",)) or pos[:1]
            if any(re.search(r"(^|[;}\s])[wW]\s*\S|(^|[;}\s])e(\s|$)|/[wW]\s+\S", s) for s in scripts):
                return ["sed w/e command (writes a file or runs a command)"]
            return []
        if name in ("awk", "gawk", "mawk"):
            if has_flag(args, "i", ("inplace",)) or any(a == "inplace" for a in opt_values(args, "i")):
                return [f"{name} -i inplace"]
            pos = positional(args, "fvF")
            prog = " ".join(pos[:1])
            if re.search(r"system\s*\(|print[f]?\b[^;}]*(>|\|)|\|\s*getline", prog):
                return [f"{name} program writes files or runs commands"]
            return []
        if name == "tee":
            files = [f for f in positional(args) if f not in DEV_SINKS]
            return [f"tee {files[0]}"] if files else []
        if name in ("curl",):
            return self._curl(args)
        if name in ("wget",):
            outs = opt_values(args, "O", ("output-document",))
            if has_flag(args, "", ("post-data", "post-file", "body-data", "body-file", "method")):
                return ["wget sends data"]
            if has_flag(args, "", ("spider",)) or (outs and all(o in DEV_SINKS for o in outs)):
                return []
            return ["wget (downloads to a file)"]
        if name == "openssl":
            pos = positional(args, "", ())
            sub = args[0] if args else ""
            outs = [args[k + 1] for k, a in enumerate(args[:-1]) if a in ("-out", "-keyout")]
            if any(o not in DEV_SINKS for o in outs) or sub in ("genrsa", "genpkey", "ca", "gendsa", "dhparam"):
                return [f"openssl {sub} writes a file"]
            del pos
            return []
        if name == "ip":
            if any(a in ("add", "del", "delete", "set", "flush", "change", "replace", "append", "prepend", "save",
                         "restore", "exec") for a in args):
                return ["ip (network change)"]
            return []
        if name == "hostname":
            return ["hostname (sets the hostname)"] if positional(args, "F") else []
        if name == "date":
            return ["date --set"] if has_flag(args, "s", ("set",)) else []
        if name == "dmesg":
            return ["dmesg (clears/changes the kernel log)"] if has_flag(args, "cCnDE", (
                "clear", "read-clear", "console-level", "console-off", "console-on")) else []
        if name == "sysctl":
            if has_flag(args, "wp", ("write", "load", "system")) or any("=" in a for a in positional(args)):
                return ["sysctl write"]
            return []
        if name == "mount":
            return ["mount"] if positional(args, "tOoLU") else []
        if name == "crontab":
            return [] if has_flag(args, "l", ("list",)) and not has_flag(args, "er") else ["crontab change"]
        if name == "tar":
            first = args[0] if args else ""
            cluster = first.lstrip("-") if first and not first.startswith("--") else ""
            if has_flag(args, "", ("list",)) or ("t" in cluster and not any(c in cluster for c in "cxruA")):
                return []
            return ["tar (creates or extracts files)"]
        if name == "git":
            return self._git_remote(args)
        if name == "nvidia-smi":
            mut = ("-pm", "--persistence-mode", "-pl", "--power-limit", "-r", "--gpu-reset", "-e", "--ecc-config",
                   "-c", "--compute-mode", "-ac", "--applications-clocks", "-rac", "--reset-applications-clocks",
                   "-lgc", "--lock-gpu-clocks", "-rgc", "--reset-gpu-clocks", "-lmc", "--lock-memory-clocks",
                   "-rmc", "--reset-memory-clocks", "-mig", "--multi-instance-gpu", "-am", "--accounting-mode",
                   "-caa", "--clear-accounted-apps", "-p", "--reset-ecc-errors", "-f", "--filename", "-dm",
                   "--driver-model", "-cc", "--cuda-clocks")
            if any(a.split("=", 1)[0] in mut for a in args):
                return ["nvidia-smi (changes GPU settings)"]
            return []
        if name in ("python", "python3") or re.fullmatch(r"python3\.\d+", name):
            return [] if args and all(a in ("--version", "-V") for a in args) else [f"{name} (runs code)"]
        if name == "xargs":
            first, more = split_after_opts(args, "IinPdLsEa", ("replace", "max-args", "max-procs", "delimiter",
                                                               "max-lines", "max-chars", "eof", "arg-file"))
            return self._inner_remote([first] + more, ctx, depth) if first else []
        if name == "env":
            return []
        if name == "yq":
            return ["yq -i"] if has_flag(args, "i", ("inplace",)) else []
        if name == "wg-quick":
            return [] if args[:1] == ["strip"] else [f"wg-quick {' '.join(args[:1])}"]
        if name in ("dpkg",):
            ro = ("l", "L", "s", "S", "p", "C", "V")
            if has_flag(args, "".join(ro), ("list", "listfiles", "status", "search", "print-avail", "audit",
                                            "verify", "get-selections", "print-architecture")):
                return []
            return ["dpkg change"]
        if name in self.ro_cmds:
            return []
        if name in ("cp", "mv", "rm", "rmdir", "ln", "mkdir", "touch", "chmod", "chown", "chgrp", "truncate",
                    "dd", "install", "useradd", "usermod", "userdel", "groupadd", "groupdel", "passwd", "chpasswd",
                    "kill", "pkill", "killall", "reboot", "shutdown", "poweroff", "halt", "apt-get", "aptitude",
                    "pip", "pip3", "npm", "snap", "visudo", "a2ensite", "a2dissite", "shred", "unlink",
                    "update-alternatives", "usermod", "setfacl", "chattr", "fail2ban-regex"):
            return [name]
        return [f"{name} (not on the read-only list)"]

    def _inner_remote(self, inner, ctx, depth):
        name = base(inner[0])
        if name in ("bash", "sh", "zsh", "dash"):
            code = opt_values(inner[1:], "c")
            if code:
                self.analyze(code[0], ctx, depth + 1)  # hits land directly, labelled with ctx["remote"]
                return []
            return [f"{name} (script not visible)"]
        fake = SC()
        fake.argv = inner
        return self.remote_reasons(name, inner[1:], fake, ctx, depth + 1)

    def _docker(self, name, args, sc, ctx, depth):
        compose = name == "docker-compose"
        g_takes, g_long = "Hcl", ("host", "context", "config", "log-level", "tlscacert", "tlscert", "tlskey")
        c_takes, c_long = "fp", ("file", "project-name", "project-directory", "env-file", "profile", "ansi",
                                 "progress", "parallel")
        rest = list(args)
        pos = positional(rest, g_takes, g_long) if not compose else []
        sub = "compose" if compose else (pos[0] if pos else "")
        if sub == "compose":
            if not compose:
                rest = rest[rest.index("compose") + 1:]
            cpos = positional(rest, c_takes, c_long)
            csub = cpos[0] if cpos else ""
            if csub in self.ro_sub.get("docker compose", ()):
                return []
            if csub == "exec":
                _, inner = split_after_opts(rest[rest.index("exec") + 1:], "uwe", ("user", "workdir", "env",
                                                                                    "index"))
                return self._inner_remote(inner, ctx, depth) if inner else []
            return [f"docker compose {csub}".strip()]
        sub2 = f"{sub} {pos[1]}" if len(pos) > 1 else sub
        if sub in ("exec",) or sub2 == "container exec":
            _, inner = split_after_opts(rest[rest.index("exec") + 1:], "uwe", ("user", "workdir", "env",
                                                                                "env-file", "detach-keys"))
            return self._inner_remote(inner, ctx, depth) if inner else []
        ro = self.ro_sub.get("docker", ())
        if sub in ("image", "container", "network", "volume", "system", "context", "buildx", "plugin"):
            return [] if sub2 in ro else [f"docker {sub2}"]
        if sub in ro:
            return []
        return [f"docker {sub}".strip()]

    def _curl(self, args):
        takes = "AbcCdDeEFHKmoPQrTuUwxXyYz"
        i, out = 0, []
        while i < len(args):
            a = args[i]
            if a.startswith("--"):
                nm, eq, v = a[2:].partition("=")
                val = v if eq else (args[i + 1] if i + 1 < len(args) else "")
                if nm in ("request",):
                    if val.upper() not in ("GET", "HEAD"):
                        out.append(f"curl -X {val}")
                elif nm.startswith("data") or nm in ("form", "form-string", "upload-file", "json"):
                    out.append(f"curl --{nm}")
                elif nm in ("output", "dump-header", "cookie-jar", "trace", "trace-ascii", "stderr"):
                    if val not in DEV_SINKS:
                        out.append(f"curl --{nm} {val}")
                elif nm in ("remote-name", "remote-name-all"):
                    out.append("curl --remote-name")
                if not eq and nm in ("request", "data", "data-raw", "data-binary", "data-urlencode", "form",
                                     "form-string", "upload-file", "json", "output", "dump-header", "cookie-jar",
                                     "header", "user-agent", "user", "url", "connect-timeout", "max-time",
                                     "resolve", "cacert", "cert", "key", "proxy", "write-out", "retry",
                                     "trace", "trace-ascii", "stderr", "config", "cookie", "referer"):
                    i += 1
            elif a.startswith("-") and len(a) > 1:
                body = a[1:]
                for k, ch in enumerate(body):
                    if ch == "O":
                        out.append("curl -O")
                    if ch in takes:
                        val = body[k + 1:] or (args[i + 1] if i + 1 < len(args) else "")
                        if not body[k + 1:]:
                            i += 1
                        if ch == "X" and val.upper() not in ("GET", "HEAD"):
                            out.append(f"curl -X {val}")
                        elif ch in "dFT":
                            out.append(f"curl -{ch}")
                        elif ch in "oDc" and val not in DEV_SINKS:
                            out.append(f"curl -{ch} {val}")
                        break
            i += 1
        return out

    def _git_remote(self, args):
        pos = positional(args, "Cc", ("git-dir", "work-tree", "namespace"))
        if not pos:
            return []
        sub = pos[0]
        rest = args[args.index(sub) + 1:] if sub in args else []
        rpos = positional(rest)
        if sub not in self.ro_sub.get("git", ()):
            return [f"git {sub}"]
        if sub == "branch" and (has_flag(rest, "dDmMcC", ("delete", "move", "copy", "set-upstream-to",
                                                           "unset-upstream", "edit-description")) or rpos):
            return ["git branch (changes refs)"]
        if sub == "tag" and (has_flag(rest, "dasfu", ("delete", "annotate", "sign")) or
                             (rpos and not has_flag(rest, "l", ("list", "contains", "points-at")))):
            return ["git tag (creates/deletes a tag)"]
        if sub == "remote" and rpos and rpos[0] not in ("show", "get-url", "-v"):
            return [f"git remote {rpos[0]}"]
        if sub == "config" and not has_flag(rest, "l", ("get", "list", "get-all", "get-regexp", "show-origin")):
            return ["git config (write)"]
        if sub == "reflog" and rpos and rpos[0] in ("expire", "delete"):
            return [f"git reflog {rpos[0]}"]
        return []

    # ------------------------------------------------------------ local rules
    def _write_target(self, raw, ctx, how, sc, text=None, tree=False):
        self.local_write(raw, ctx["cwd"], "terminal", how, text, tree)

    def _local_rules(self, name, args, argv, sc, ctx, depth):
        cwd = ctx["cwd"]
        cmd_text = " ".join(argv) + ("\n" + sc.stdin if sc.stdin else "")
        if name in ("cd", "pushd") and args:
            tgt = positional(args)
            if tgt and not tgt[0].startswith("-"):
                ctx["cwd"] = expand(tgt[0], self.rt, cwd)
            return
        for t in sc.out_targets():
            if not re.fullmatch(r"&?\d+", t):
                self._write_target(t, ctx, "redirect >", sc, cmd_text)
        if name == "tee":
            for f in positional(args):
                self._write_target(f, ctx, "tee", sc, cmd_text)
        elif name == "sed" and has_flag(args, "i", ("in-place",)):
            pos = positional(args, "ef", ("expression", "file"))
            files = pos if opt_values(args, "e", ("expression",)) or opt_values(args, "f") else pos[1:]
            for f in files:
                self._write_target(f, ctx, "sed -i", sc, cmd_text)
        elif name in ("perl", "ruby") and has_flag(args, "i"):
            for f in positional(args, "eEIMm"):
                self._write_target(f, ctx, f"{name} -i", sc, cmd_text)
        elif name in ("cp", "mv", "install", "ln", "rsync"):
            tdir = opt_values(args, "t", ("target-directory",))
            pos = positional(args, "tSmogb" if name == "install" else "tS",
                             ("target-directory", "suffix", "mode", "owner", "group", "backup"))
            if name == "install" and has_flag(args, "d", ("directory",)):
                dests = pos
            else:
                dests = tdir or pos[-1:]
            for d in dests:
                if len(pos) + len(tdir) >= 2 or name == "install":
                    self._write_target(d, ctx, name + " into", sc, cmd_text)
            if name == "mv":
                for s in (pos if tdir else pos[:-1]):
                    self._write_target(s, ctx, "mv away", sc, cmd_text, tree=True)
        elif name in ("rm", "rmdir", "unlink", "shred", "touch", "truncate", "chmod", "chown", "chgrp", "mkdir",
                      "setfacl", "chattr"):
            pos = positional(args, "sm" if name in ("truncate", "mkdir") else "", ("size", "reference", "mode"))
            if name in ("chmod", "chown", "chgrp", "setfacl", "chattr") and pos:
                pos = pos[1:]
            tree = name in ("rm", "rmdir", "shred") or has_flag(args, "R", ("recursive",))
            for f in pos:
                self._write_target(f, ctx, name, sc, cmd_text, tree=tree)
        elif name == "dd":
            for a in args:
                if a.startswith("of="):
                    self._write_target(a[3:], ctx, "dd of=", sc, cmd_text)
        elif name in ("curl", "wget"):
            for f in opt_values(args, "o" if name == "curl" else "O", ("output", "output-document")):
                self._write_target(f, ctx, f"{name} -o", sc, cmd_text)
        elif name == "yq" and has_flag(args, "i", ("inplace",)):
            for f in positional(args)[1:]:
                self._write_target(f, ctx, "yq -i", sc, cmd_text)
        elif name == "ssh-keygen":
            if not has_flag(args, "ylFBe"):
                for f in opt_values(args, "f"):
                    self._write_target(f, ctx, "ssh-keygen -f", sc, cmd_text)
                if has_flag(args, "R"):
                    self._write_target("~/.ssh/known_hosts", ctx, "ssh-keygen -R", sc, cmd_text)
        elif name == "ssh-copy-id":
            pass
        elif name == "tar" and args and ("x" in args[0].lstrip("-") or has_flag(args, "", ("extract", "get"))):
            for d in opt_values(args, "C", ("directory",)) or ["."]:
                self._write_target(d, ctx, "tar -x into", sc, cmd_text)
        elif name == "find":
            if any(a in ("-delete",) for a in args) or any(a in ("-exec", "-execdir") and k + 1 < len(args) and
                                                          base(args[k + 1]) in ("rm", "chmod", "chown", "mv",
                                                                                "sed", "truncate", "shred")
                                                          for k, a in enumerate(args)):
                roots = []
                for a in args:
                    if a.startswith(("-", "(", "!")):
                        break
                    roots.append(a)
                for r in roots or ["."]:
                    self._write_target(r, ctx, "find -delete/-exec under", sc, cmd_text, tree=True)
        elif name == "git":
            self._git_local(args, ctx, sc, cmd_text)
        elif name == "gh":
            self._gh(args)
        elif name == "hermes":
            self._hermes_cli(args, sc, ctx)
        elif name in ("python", "python3", "perl", "ruby", "node", "php") or re.fullmatch(r"python3\.\d+", name):
            code = opt_values(args, "ce" if name != "php" else "r")
            if not code and sc.stdin is not None and (not positional(args) or "-" in args):
                code = [sc.stdin]
            for c in code:
                self._inline_code(c, ctx, name)
        elif "/" in argv[0] or name.endswith(".sh"):
            self._run_script_file(argv[0], args, ctx, depth)
        # oversight via env assignments (HERMES_ACCEPT_HOOKS=1 hermes ...)
        oc = self.rules.get("oversight_config") or {}
        for a in sc.assigns:
            k, _, v = a.partition("=")
            if k in (oc.get("hermes_cli_block_env") or ()) and v not in ("", "0", "false", "no"):
                if name == "hermes" or base(name).startswith("hermes"):
                    self.hit("oversight_config", f"{k}={v} bypasses hook consent/approvals", a)

    def _inline_code(self, code, ctx, lang):
        write_ind = re.search(r"open\([^)]*['\"][wax+]|write_text|write_bytes|\.write\(|shutil\.|os\.(remove|"
                              r"unlink|rename|replace|chmod|truncate)|unlink|rmtree|dump\(|>\s*['\"/~$]|"
                              r"writeFile|appendFile|File\.write|-i\b|fs\.", code)
        if not write_ind:
            return
        cands = set(re.findall(r"(?:~|\$\{?HOME\}?|\$\{?HERMES_HOME\}?|\$\{?JUDGE_REVIEW_DIR\}?)?/[A-Za-z0-9_./@+-]+",
                               code))
        for nm in ("config.yaml", "shell-hooks-allowlist.json"):
            if nm in code and not any(c.endswith(nm) for c in cands):
                cands.add(os.path.join(self.rt["HERMES_HOME"], nm))
        for c in cands:
            self.local_write(c, ctx["cwd"], "terminal", f"{lang} code writing", code)

    def _git_local(self, args, ctx, sc, cmd_text):
        pos = positional(args, "Cc", ("git-dir", "work-tree", "namespace", "exec-path"))
        if not pos:
            return
        sub = pos[0]
        rest = args[args.index(sub) + 1:]
        if sub == "push":
            if not has_flag(rest, "n", ("dry-run",)):
                tgt = " ".join(positional(rest, "o", ("repo", "push-option", "receive-pack", "exec"))[:2])
                self.hit("public_push", f"git push {tgt}".strip() + " (the deck repo is PUBLIC: run "
                         "scripts/check-sanitized.sh first)", "git push " + tgt)
            return
        gcwd = (opt_values(args[:args.index(sub)], "C") or [None])[0]
        wcwd = expand(gcwd, self.rt, ctx["cwd"]) if gcwd else ctx["cwd"]
        if sub in ("checkout", "restore", "rm", "mv", "clean", "apply", "am"):
            paths = rest[rest.index("--") + 1:] if "--" in rest else positional(rest, "bBsp", ("source",))
            for p in paths:
                if sub == "checkout" and "--" not in rest and not os.path.exists(expand(p, self.rt, wcwd)):
                    continue
                self.local_write(p, wcwd, "terminal", f"git {sub}", cmd_text)

    def _gh(self, args):
        pos = positional(args, "R", ("repo",))
        if not pos:
            return
        muts = (self.rules.get("public_push") or {}).get("gh_mutations") or {}
        sub, sub2 = pos[0], (pos[1] if len(pos) > 1 else "")
        if sub2 in muts.get(sub, ()):
            self.hit("public_push", f"gh {sub} {sub2}", f"gh {sub} {sub2}")
        elif sub == "api":
            method = opt_values(args, "X", ("method",))
            if (method and method[0].upper() not in ("GET", "HEAD")) or \
                    has_flag(args, "fF", ("field", "raw-field", "input")):
                self.hit("public_push", "gh api write request", "gh api")

    def _hermes_cli(self, args, sc, ctx):
        """`hermes ...` on the terminal. Read-only forms pass (see the hermes_cli map in the oversight-config
        rule, verified against `hermes <sub> --help` and hermes_cli/{config,hooks}.py); mutating forms on
        oversight state block; any other config/hooks action escalates (when in doubt, escalate)."""
        oc = self.rules.get("oversight_config") or {}
        for f in oc.get("hermes_cli_block_flags") or ():
            if f in args:
                self.hit("oversight_config", f"hermes {f} bypasses approvals/hook consent", f"hermes {f}")
        opts = args[:args.index("--")] if "--" in args else args
        if any(a in ("-h", "--help") for a in opts):
            return  # argparse prints help and exits before any action runs
        cli = oc.get("hermes_cli") or DEFAULT_HERMES_CLI
        pos = positional(args, "zmtsr", ("usage-file", "model", "provider", "reasoning", "resume", "in",
                                           "toolsets", "skills", "continue", "apply", "days", "min-count",
                                           "limit", "db", "for-tool", "payload-file", "env-type"))
        if not pos:
            return  # bare `hermes` / `hermes --version`
        sub, act = pos[0], (pos[1] if len(pos) > 1 else "")
        if sub == "config":
            key = pos[2] if len(pos) > 2 else ""
            if act in ("set", "unset"):
                top = key.split(".", 1)[0]
                if top in self.config_top or key in self.config_keys or key.rsplit(".", 1)[-1] in self.config_nested:
                    self.hit("oversight_config", f"hermes config {act} {key}", key)
                else:
                    self.hit("hermes_config", f"hermes config {act} {key}".rstrip(), key or act)
            elif act in (cli.get("config_block") or ()):
                self.hit("oversight_config", f"hermes config {act} (can change any setting)", act)
            elif act == "get" and has_flag(args, "", ("raw",)):
                if not ctx["captured"]:
                    self.hit("secret_output", "hermes config get --raw prints credential values unmasked",
                             "hermes config get --raw")
            elif act in (cli.get("config_readonly") or ()):
                pass
            else:
                self.hit("hermes_config", f"hermes config {act} (not on the read-only list)", act)
        elif sub == "hooks":
            if act in (cli.get("hooks_block") or ()):
                self.hit("oversight_config", f"hermes hooks {act} changes hook consent", "hooks " + act)
            elif act not in (cli.get("hooks_readonly") or ()):
                self.hit("hermes_config", f"hermes hooks {act} (not on the read-only list; `test` fires the "
                         "oversight hooks with a synthetic payload)", "hooks " + act)
        elif sub == "approvals" and act == "suggest" and has_flag(args, "", ("apply",)):
            self.hit("oversight_config", "hermes approvals suggest --apply writes command_allowlist",
                     "approvals suggest --apply")
        elif sub in (cli.get("block_subcommands") or ()):
            self.hit("oversight_config", f"hermes {sub} overwrites the Hermes home (config, hook consent)", sub)
        elif sub in (cli.get("approve_subcommands") or ()):
            self.hit("hermes_config", f"hermes {sub} rewrites the Hermes config", sub)

    # ------------------------------------------------------------ file tools
    def file_write(self, tool, path, cwd, new_content=None, old_new=None):
        if not isinstance(path, str) or not path.strip():
            raise GateError(f"{tool}: tool_input.path missing")
        p = expand(path.strip(), self.rt, cwd)
        cls = self.classify_path(p)
        if cls == "config":
            self.config_file_edit(tool, p, new_content, old_new)
        elif cls == "oversight":
            self.hit("oversight_path", f"{tool} {p}", p)
        elif cls == "sensitive":
            self.hit("sensitive_path", f"{tool} {p}", p)
        return p

    def config_file_edit(self, tool, p, new_content, old_new):
        try:
            with open(p, encoding="utf-8") as fh:
                current = fh.read()
        except OSError:
            current = None
        if old_new is not None:
            touched = self._patch_touches(current, old_new)
        else:
            touched = self._write_touches(current, new_content or "")
        if touched is None:
            self.hit("hermes_config", f"{tool} {p} (could not verify oversight keys are unchanged)", p)
        elif touched:
            self.hit("oversight_config", f"{tool} {p} changes {', '.join(sorted(touched))}", p)
        else:
            self.hit("hermes_config", f"{tool} {p}", p)

    @staticmethod
    def _top_blocks(text):
        """{top-level key: [lines]} or None when the YAML is not plain block style we can trust."""
        blocks, cur, dupes = {}, None, False
        for line in text.split("\n"):
            if line.startswith("\t"):
                return None
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s in ("---", "...") or s.startswith(("{", "[", "? ", "%")):
                return None
            if not line[0].isspace():
                m = re.match(r"""^["']?([^"':#]+?)["']?\s*:(\s|$)""", line)
                if not m:
                    return None
                cur = m.group(1).strip()
                if cur in blocks:
                    dupes = True
                blocks[cur] = []
            if cur is None:
                return None
            blocks[cur].append(line.rstrip())
            if re.search(r"(^|\s)(<<\s*:|[&*][A-Za-z0-9_-]+(\s|$))", s):
                blocks.setdefault("__anchors__", []).append(s)
        return None if dupes else blocks

    def _write_touches(self, current, new):
        nb = self._top_blocks(new)
        ob = self._top_blocks(current) if current is not None else {}
        if nb is None or ob is None:
            return None
        if nb.get("__anchors__") != ob.get("__anchors__"):
            return None
        touched = set()
        for k in self.config_top:
            if nb.get(k) != ob.get(k):
                touched.add(k)
        for k in self.config_keys:
            if "." in k:
                top, leaf = k.split(".", 1)
                pick = lambda b: [x.strip() for x in (b or {}).get(top, []) if re.match(r"\s*%s\s*:" % re.escape(leaf), x)]
                if pick(nb) != pick(ob):
                    touched.add(k)
        return touched

    def _patch_touches(self, current, old_new):
        """old_new = list of (old_text, new_text) edits. Changed lines mentioning an oversight key, or
        changed lines located inside an oversight block of the current file, count."""
        touched = set()
        unsure = False
        lines = current.split("\n") if current is not None else []
        tops = self._line_tops(lines)
        for old, new in old_new:
            ol, nl = (old or "").split("\n"), (new or "").split("\n")
            start = None
            if current is not None and old:
                idx = current.find(old)
                if idx < 0:
                    stripped = [x.strip() for x in ol]
                    for li in range(len(lines) - len(ol) + 1):
                        if [x.strip() for x in lines[li:li + len(ol)]] == stripped:
                            start = li
                            break
                else:
                    start = current[:idx].count("\n")
            if start is None and old:
                unsure = True
            sm = difflib.SequenceMatcher(a=ol, b=nl, autojunk=False)
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag == "equal":
                    continue
                changed = "\n".join(ol[i1:i2] + nl[j1:j2])
                if self.config_key_re:
                    for m in self.config_key_re.finditer(changed):
                        touched.add(m.group(1))
                if start is not None:
                    span = range(start + i1, start + max(i2, i1 + 1)) if i2 > i1 else [start + i1 - 1]
                    for L in span:
                        if 0 <= L < len(tops) and tops[L] in self.config_top:
                            # inserted lines at column 0 start their own block; indented ones extend L's block
                            if i2 > i1 or any(x[:1].isspace() for x in nl[j1:j2] if x.strip()):
                                touched.add(tops[L])
        if touched:
            return touched
        return None if unsure else set()

    @staticmethod
    def _line_tops(lines):
        tops, cur = [], None
        for line in lines:
            if line and not line[0].isspace() and not line.startswith("#"):
                m = re.match(r"""^["']?([^"':#]+?)["']?\s*:""", line)
                cur = m.group(1).strip() if m else cur
            tops.append(cur)
        return tops

    def v4a(self, patch, cwd):
        hdr = re.compile(r"^\*\*\*\s*(Update|Add|Delete)\s+File:\s*(.+?)\s*$")
        mv = re.compile(r"^\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+?)\s*$")
        ops, cur = [], None
        for line in patch.split("\n"):
            m, m2 = hdr.match(line), mv.match(line)
            if m or m2:
                cur = {"op": m.group(1) if m else "Move", "path": (m or m2).group(2) if m else m2.group(1),
                       "dest": m2.group(2) if m2 else None, "lines": []}
                ops.append(cur)
            elif line.startswith("***"):
                cur = None if line.strip().startswith("*** End") else cur
            elif cur is not None:
                cur["lines"].append(line)
        if not ops:
            raise GateError("patch: V4A content has no file headers")
        paths = []
        for op in ops:
            if op["op"] == "Update":
                old = "\n".join(x[1:] for x in op["lines"] if x[:1] in (" ", "-"))
                new = "\n".join(x[1:] for x in op["lines"] if x[:1] in (" ", "+"))
                paths.append(self.file_write("patch", op["path"], cwd, old_new=[(old, new)]))
            elif op["op"] == "Add":
                content = "\n".join(x[1:] for x in op["lines"] if x[:1] == "+")
                paths.append(self.file_write("patch", op["path"], cwd, new_content=content))
            else:
                for pth in filter(None, (op["path"], op.get("dest"))):
                    p = expand(pth, self.rt, cwd)
                    paths.append(p)
                    cls = self.classify_path(p)
                    if cls == "config":
                        self.hit("oversight_config", f"patch {op['op'].lower()}s {p}", p)
                    elif cls == "oversight":
                        self.hit("oversight_path", f"patch {op['op'].lower()}s {p}", p)
                    elif cls == "sensitive":
                        self.hit("sensitive_path", f"patch {op['op'].lower()}s {p}", p)
        return paths

    # ------------------------------------------------------------ entry
    def evaluate(self, payload):
        tool = payload.get("tool_name")
        ti = payload.get("tool_input")
        cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) and payload.get("cwd") else self.rt["HOME"]
        paths = []
        if tool not in GATED_TOOLS:
            return paths
        if not isinstance(ti, dict):
            raise GateError(f"{tool}: tool_input is not an object")
        if tool == "terminal":
            cmd = ti.get("command")
            if not isinstance(cmd, str):
                raise GateError("terminal: tool_input.command missing")
            wd = ti.get("workdir")
            if isinstance(wd, str) and wd:
                cwd = expand(wd, self.rt, cwd)
            self.full_text = cmd
            self.analyze(cmd, {"remote": None, "captured": False, "cwd": cwd})
        elif tool == "write_file":
            content = ti.get("content")
            paths.append(self.file_write("write_file", ti.get("path"), cwd,
                                         new_content=content if isinstance(content, str) else ""))
        elif tool == "read_file":
            path = ti.get("path")
            if not isinstance(path, str) or not path.strip():
                raise GateError("read_file: tool_input.path missing")
            p = expand(path.strip(), self.rt, cwd)
            paths.append(p)
            self.subject = p
            if self.is_secret_file(p):
                self.hit("secret_output", f"read_file {p} returns a secret into the transcript", p)
        else:
            if ti.get("mode") == "patch" or (isinstance(ti.get("patch"), str) and not ti.get("path")):
                if not isinstance(ti.get("patch"), str):
                    raise GateError("patch: mode=patch without patch content")
                paths.extend(self.v4a(ti["patch"], cwd))
            else:
                paths.append(self.file_write("patch", ti.get("path"), cwd,
                                             old_new=[(ti.get("old_string") or "", ti.get("new_string") or "")]))
        return paths


# =============================================================== decision, logging, queue
_REDACTIONS = [
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S),
     "<redacted-key>"),
    (re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 <redacted>"),
    (re.compile(r"(?i)((?:authorization|x-api-key|api-key|cookie)\s*:\s*)[^\"'\n]+"), r"\1<redacted>"),
    (re.compile(r"(?i)\b([A-Za-z0-9_.-]*(?:pass(?:word|wd)?|secret|token|api[_-]?key|apikey|master[_-]?key|"
                r"private[_-]?key|credential)[A-Za-z0-9_.-]*\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s\"'&;]+)"),
     r"\1<redacted>"),
    (re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+:)[^\s/@]+@"), r"\1<redacted>@"),
    (re.compile(r"\b(sk|ghp|gho|ghs|github_pat|xox[abprs])[-_][A-Za-z0-9_-]{10,}"), "<redacted>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<redacted>"),
    # long opaque tokens: 32+ chars of one word mixing letters and digits (paths keep their '/' and '.')
    (re.compile(r"(?<![A-Za-z0-9/_.+-])(?=[A-Za-z_+-]*[0-9])(?=[0-9_+-]*[A-Za-z])[A-Za-z0-9+_-]{32,}={0,2}"
                r"(?![A-Za-z0-9/_.+-])"), "<redacted>"),
]


def redact(text, limit=300):
    t = text or ""
    try:
        _lib()
        from lib import redact as jredact
        t = jredact.redact(t)
    except Exception:
        pass
    for rx, rep in _REDACTIONS:
        t = rx.sub(rep, t)
    t = re.sub(r"\s*\n\s*", " ⏎ ", t).strip()
    return t if len(t) <= limit else t[:limit - 1] + "…"


def decide(gate, tool, ti):
    """(decision, message, rule_key, rule ids)."""
    hits = gate.hits
    if not hits:
        return "pass", "", "", []
    blocks = [h for h in hits if h["action"] == "block"]
    chosen = blocks or [h for h in hits if h["action"] == "approve"]
    decision = "block" if blocks else "approve"
    rules, reasons = [], []
    for h in chosen:
        if h["rule"] not in rules:
            rules.append(h["rule"])
        if h["reason"] not in reasons:
            reasons.append(h["reason"])
    descr = {r["id"]: r.get("description", "") for r in gate.rules.values()}
    head = "; ".join(f"[{r}] {descr.get(r, '')}" for r in rules[:3])
    detail = "; ".join(reasons[:4]) + (f" (+{len(reasons) - 4} more)" if len(reasons) > 4 else "")
    if decision == "block":
        msg = (f"BLOCKED by the judge gate: {head}. Detail: {detail}. This changes the agent's own oversight; "
               "the human must make this change directly. Do not retry or work around it; tell the human what "
               "change you wanted and why.")
    else:
        msg = f"Judge gate escalation: {head}. Detail: {detail}."
        if "public-push" in rules:
            msg += " Note: the deck repo is PUBLIC; confirm scripts/check-sanitized.sh is clean."
    subject = gate.subject or (ti.get("command") if tool == "terminal"
                               else (ti.get("path") or str(ti.get("patch") or "")[:400]))
    digest = hashlib.sha256(f"{tool}\0{subject}".encode("utf-8", "replace")).hexdigest()[:12]
    rule_key = f"judge-gate:{rules[0]}:{digest}"
    return decision, msg, rule_key, rules


def excerpt(tool, ti, paths):
    if tool == "terminal":
        return redact(ti.get("command") or "")
    return redact(", ".join(paths) or str(ti.get("path") or ""), 300)


def _append_log(rd, line):
    os.makedirs(rd, mode=0o700, exist_ok=True)
    path = os.path.join(rd, "gate.log")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def _hook_error(rd, msg):
    try:
        os.makedirs(rd, mode=0o700, exist_ok=True)
        fd = os.open(os.path.join(rd, "hook-errors.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps({"ts": _now(), "hook": "gate", "error": msg[:2000]}) + "\n").encode())
        finally:
            os.close(fd)
    except OSError:
        pass


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gate_data_class(jconfig, rules, paths, cwd):
    """'infra' only when every fired rule is a host rule (lib/config.HOST_RULES) and every involved local
    path (if any) is infra.

    Anything else (secret-output, sensitive-path, public-push, oversight, unknown) stays 'sensitive',
    which keeps the request with the local judge.
    """
    try:
        if not rules or not set(rules) <= jconfig.HOST_RULES:
            return "sensitive"
        if paths and jconfig.classify(paths, cwd=cwd) != "infra":
            return "sensitive"
        return "infra"
    except Exception:
        return "sensitive"


def enqueue_review(rd, session, tool, decision, rules, rule_key, exc, paths, cwd):
    """Write a `gate` review request via judge/lib/queue.py (the only place that touches the queue)."""
    _lib()
    from lib import config as jconfig
    from lib import queue as jqueue
    data_class = gate_data_class(jconfig, rules, paths, cwd)
    now = _now()
    req = jqueue.make_request(
        "gate", session or "", now, source_event="pre_tool_call", changed_paths=paths,
        claims=f"C2 gate {decision} ({', '.join(rules)}): {exc}", data_class=data_class,
        detail={"tool": tool, "decision": decision, "rules": rules, "rule_key": rule_key, "excerpt": exc},
        created=now, root=rd)
    return str(jqueue.write_request(req, root=rd))


def run(payload, rt=None, side_effects=True):
    """Evaluate one payload. Returns (stdout_obj, exit_code, record)."""
    t0 = time.monotonic()
    rt = rt or _runtime_paths()
    if not isinstance(payload, dict):
        raise GateError("payload is not a JSON object")
    event = payload.get("hook_event_name")
    if event not in (None, "pre_tool_call"):
        return {}, 0, None
    tool = payload.get("tool_name")
    if tool not in GATED_TOOLS:
        return {}, 0, None
    gate = Gate(load_policy(rt), rt)
    paths = gate.evaluate(payload)
    ti = payload.get("tool_input") or {}
    decision, msg, rule_key, rules = decide(gate, tool, ti)
    if decision == "pass":
        return {}, 0, {"decision": "pass", "hits": []}
    exc = excerpt(tool, ti, paths)
    record = {"ts": _now(), "session": payload.get("session_id") or "", "tool": tool,
              "rule": rules[0], "rules": rules, "decision": decision, "excerpt": exc, "rule_key": rule_key,
              "elapsed_ms": 0, "hits": gate.hits}
    if side_effects:
        rd = rt["JUDGE_REVIEW_DIR"]
        try:
            record["request"] = enqueue_review(rd, record["session"], tool, decision, rules, rule_key, exc,
                                               paths, payload.get("cwd"))
        except Exception as e:  # the decision stands; the missing review is logged
            _hook_error(rd, f"gate: could not write review request: {type(e).__name__}: {e}")
        record["elapsed_ms"] = round((time.monotonic() - t0) * 1000, 1)
        line = {k: record[k] for k in ("ts", "session", "tool", "rule", "rules", "decision", "excerpt",
                                       "rule_key", "elapsed_ms")}
        line.update(_call_markers(payload, tool, ti))
        if record.get("request"):
            line["request"] = os.path.basename(record["request"])
        try:
            _append_log(rd, line)
        except OSError as e:
            _hook_error(rd, f"gate: could not append gate.log: {e}")
    if decision == "block":
        return {"action": "block", "message": msg}, 2, record
    return {"action": "approve", "message": msg, "rule_key": rule_key}, 0, record


def _call_markers(payload, tool, ti):
    """tool_call_id (when Hermes sends one) and lib/redact.call_hash, so the collector can match this
    decision with the post_tool_call event of the call if it ran. Best effort: never fails the gate."""
    out = {}
    extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
    cid = extra.get("tool_call_id")
    if isinstance(cid, (str, int)) and str(cid).strip():
        out["tool_call_id"] = str(cid)[:200]
    try:
        _lib()
        from lib import redact as jredact
        out["call_hash"] = jredact.call_hash(tool, ti)
    except Exception:
        pass
    return out


def _fail_closed(reason):
    msg = (f"judge gate failed closed ({reason}); the tool call was blocked. Ask the human to check "
           "$JUDGE_REVIEW_DIR/hook-errors.log and judge/policy.")
    return {"action": "block", "message": msg}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    explain = "--explain" in argv
    rt = None
    try:
        rt = _runtime_paths()
        raw = sys.stdin.buffer.read()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw.strip() else None
        except (UnicodeDecodeError, ValueError) as e:
            raise GateError(f"malformed stdin JSON: {e}") from e
        if payload is None:
            raise GateError("empty stdin")
        out, code, record = run(payload, rt, side_effects=not explain)
        if explain:
            print(json.dumps({"output": out, "exit": code, "record": record}, indent=2, ensure_ascii=False))
            return 0
    except BaseException as e:  # noqa: BLE001  fail closed on anything, incl. KeyboardInterrupt
        reason = f"{type(e).__name__}: {e}" if isinstance(e, GateError) else f"internal error {type(e).__name__}: {e}"
        if rt:
            import traceback
            _hook_error(rt["JUDGE_REVIEW_DIR"], f"gate: {reason}\n{traceback.format_exc()[-1500:]}")
        out, code = _fail_closed(reason[:300]), 2
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    if code == 2:
        sys.stderr.write(out.get("message", "blocked by judge gate") + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
