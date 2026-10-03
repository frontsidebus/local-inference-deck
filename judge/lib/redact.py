"""judge/lib/redact.py: mask secret-looking values in text before it lands in evidence or requests.

    redact(text) -> str

Conservative: masks values, keeps keys and structure so the judge can still see *that* something was set.
Covers URL userinfo passwords, Bearer/Basic auth, well-known token shapes, PEM private keys, and
`<secret-ish key> = / : <value>` assignments (env, YAML, JSON, query strings, WireGuard).
"""
from __future__ import annotations

import re

MASK = "<redacted>"

_PEM_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S)
# scheme://user:password@host  (password may itself contain '@'; take up to the last '@' before '/' or space)
_URL_USERINFO_RE = re.compile(r"(?P<pre>\b[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+:)(?P<pw>[^\s/]*)@")
_AUTH_HDR_RE = re.compile(r"(?i)\b(authorization|proxy-authorization|x-api-key|api-key|cookie|set-cookie)"
                          r"(\s*[:=]\s*)(\"?)((?:(?:bearer|basic|token)\s+)?[^\s\"',;]+)")
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_TOKEN_SHAPES = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),  # JWT
]
_KEYWORDS = (r"pass(?:word|wd|phrase)?|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|privatekey|"
             r"presharedkey|master[_-]?key|client[_-]?secret|auth[_-]?key|credentials?|session[_-]?key|salt|"
             r"cookie[_-]?secret|signing[_-]?key|encryption[_-]?key")
# KEY = value | "key": "value" | key: value | ?key=value ; the key may have a prefix (LITELLM_MASTER_KEY)
_KV_RE = re.compile(
    r"(?i)(?P<key>[\"']?[A-Za-z0-9_.-]*(?:" + _KEYWORDS + r")[A-Za-z0-9_.-]*[\"']?)"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<q>[\"']?)(?P<val>[^\s\"'&,;}]{4,})")


def _kv_sub(m: re.Match) -> str:
    val = m.group("val")
    if (val == MASK or val.startswith("${") or val.upper() in ("CHANGEME", "NULL", "NONE", "TRUE", "FALSE")
            or re.fullmatch(r"[~\d.,%/()-]+", val)):  # numbers (max_tokens=4096, tokens=~3,545) are not secrets
        return m.group(0)
    return f"{m.group('key')}{m.group('sep')}{m.group('q')}{MASK}"


def redact(text: str) -> str:
    if not text:
        return text
    t = _PEM_RE.sub("-----BEGIN PRIVATE KEY-----" + MASK + "-----END PRIVATE KEY-----", text)
    t = _URL_USERINFO_RE.sub(lambda m: f"{m.group('pre')}{MASK}@", t)
    t = _AUTH_HDR_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{MASK}", t)
    t = _BEARER_RE.sub(lambda m: f"{m.group(1)} {MASK}", t)
    for rx in _TOKEN_SHAPES:
        t = rx.sub(MASK, t)
    t = _KV_RE.sub(_kv_sub, t)
    return t
