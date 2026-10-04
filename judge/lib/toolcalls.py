"""judge/lib/toolcalls.py: the NAME of a tool call's command, never its arguments (stdlib only; bug #33).

The claims-only bundle and `tool-calls.jsonl` say which program a terminal call ran so a judge can tell a
narrowed, gate-allowed retry (`stat` alone after an escalated `stat; grep -o ... key`) from a workaround. Only
the first word is ever looked at, and only a name from READONLY_COMMANDS is kept: anything else becomes
"(other)", so a script path, a variable or an argument can never come through.

    command_word(tool, command) -> str
        terminal: the first word of *command* (leading VAR=value assignments skipped, basename only) when it is
        in READONLY_COMMANDS, "(other)" for any other word, "(none)" for an empty command.
        any other tool: the tool name.
    is_command_word(tool, word) -> bool     a value command_word() can return for *tool*, or "(unknown)"
    commands_from_state_db(hermes_home, session) -> {tool_call_id: command_word}
        terminal calls of *session* in Hermes' state.db (opened read-only); the arguments are parsed in memory
        and only command_word() of them is returned.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Dict, Optional

# Common programs that only read by default. Membership says nothing about safety (the gate decides that);
# it only bounds what a command name in a bundle can be.
READONLY_COMMANDS = frozenset("""
ls stat wc file du df cat head tail less more grep egrep fgrep rg find tree which whereis type readlink realpath
basename dirname pwd cd echo printf test true false date uptime whoami id groups hostname uname env printenv
ps pgrep top free lsof ss netstat ip systemctl journalctl sha256sum sha1sum sha512sum md5sum cksum b2sum
diff cmp sort uniq cut tr awk jq xxd od hexdump strings base64 getent dig nslookup ping nvidia-smi sensors
git
""".split())
OTHER = "(other)"
NONE = "(none)"
UNKNOWN = "(unknown)"  # the collector could not find the call's command (no event field, no state.db row)
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def command_word(tool: str, command: object) -> str:
    if tool != "terminal":
        return tool
    words = str(command or "").strip().split()
    while words and _ASSIGN_RE.match(words[0]):
        words.pop(0)
    if not words:
        return NONE
    w = words[0].strip("'\"`();&|{}").rsplit("/", 1)[-1]
    return w if w in READONLY_COMMANDS else OTHER


def is_command_word(tool: str, word: object) -> bool:
    if tool != "terminal":
        return word == tool
    return word in READONLY_COMMANDS or word in (OTHER, NONE, UNKNOWN)


def commands_from_state_db(hermes_home, session: str) -> Dict[str, str]:
    """{tool_call_id: command_word} for the terminal calls of *session* (empty when state.db is missing)."""
    p = Path(hermes_home) / "state.db"
    out: Dict[str, str] = {}
    if not session or not p.is_file():
        return out
    con: Optional[sqlite3.Connection] = None
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
                args = {}
            word = command_word("terminal", args.get("command") if isinstance(args, dict) else None)
            for key in ("id", "call_id"):
                if isinstance(c.get(key), str) and c[key]:
                    out[c[key]] = word
    return out
