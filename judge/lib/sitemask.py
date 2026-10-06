#!/usr/bin/env python3
"""judge/lib/sitemask.py: mask the site's real identifier values before ANY frontier judge call (stdlib only).

The frontier judge is a third-party service. Infra bundles carry the site's real identifiers in Hermes log lines,
diffs, probes and manifests (the domain in an API base URL, a backend IP in a compose file ...). Before a frontier
call the runner replaces every such value with a `${KEY}` placeholder named after its site.env variable, so the
judge can still reason about it ("`${SPARK_DIGEST_HOST}` serves the UI"), and maps the judge's finding back to
the real values locally. The reverse map exists only in memory, rebuilt from site.env at run time; it is never
written to disk and never sent. The local judge runs on site and gets the real values (nothing is masked).

    build(cfg) -> SiteMask              from a config dict (lib/config.load_config())
    from_config() -> SiteMask           the same, from site.env + environment
    SiteMask.mask(text) -> str          real values -> placeholders
    SiteMask.mask_obj(obj)              the same, recursively over the strings of dicts/lists
    SiteMask.unmask(text, keep=()) -> str   placeholders -> real values; placeholders in *keep* stay as they are
    SiteMask.unmask_obj(obj, keep=())
    SiteMask.residual(text) -> {key: n} real values left in *text* (the self-check: must be empty)
    SiteMask.literal_placeholders(text) -> set   placeholders that already occur in *text* (templates: ambiguous)
    SiteMask.keys -> sorted placeholder names (no values)

What is masked (each non-empty value of 3+ characters; one placeholder per distinct value):

    hosts      SPARK_DOMAIN, every SPARK_*_HOST, and SPARK_DOMAIN without its TLD as ${SPARK_DOMAIN:stem}
               (6+ chars); a subdomain the config does not list becomes `name.${SPARK_DOMAIN}`
    emails     every *_EMAIL
    addresses  every *_IP, the elements of every *_IPS list (${KEY[n]}), every *_SUBNET (the CIDR, and its
               network address as ${KEY:net}); an IPv4 address also in its dashed form (ip-10-0-0-1 / ec2-...)
               as ${KEY:dashed}
    accounts   every *_SSH_USER and the elements of SPARK_USERS. A generic account name (GENERIC_USERS: ubuntu,
               operator, root ...) is masked only where it is a login (`name@`, `/home/name`, `~name`, `-l name`,
               `User name`), because masking the word "ubuntu" everywhere would garble the bundle.
    aliases    the elements of JUDGE_SSH_ALIASES
    buckets    every *_BUCKET
    site name  SPARK_SITE_NAME (any whitespace between its words)
    ids        every *_PUBLIC_KEY, *_UUID and *_CLIENT_ID whose value holds a digit and is 16+ chars (a client id
               that is a plain word is not an identifier worth garbling the text for)

Matching: longest value first, case-insensitive, at word boundaries (a host or name is not part of a longer
`[A-Za-z0-9_-]` run; an address is not part of a longer dotted number), so it also works inside URLs
(`https://${SPARK_API_HOST}/v1`), `user@host` and JSON. A second sweep then replaces any non-address value still
present as a plain substring, so the self-check (`residual`) holds by construction: what is sent contains none of
the values. Placeholders are inserted through private-use sentinels, so a short value never matches inside an
earlier placeholder's text.

Unmasking: a placeholder that already occurs LITERALLY in the unmasked input (a template line with
`${SPARK_DOMAIN}`) is ambiguous: the judge may be quoting the template. Callers pass those as *keep*, and they stay
as placeholders in the finding (never turned into a real value the source did not contain). Unmasked text uses
the value as configured (case as in site.env).
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

MIN_LEN = 3
STEM_MIN = 6
ID_MIN = 16
# Public, generic account names: masked only in login position (see the module doc).
GENERIC_USERS = frozenset({"ubuntu", "operator", "root", "admin", "user", "debian", "centos", "fedora", "ec2-user",
                           "pi", "core", "ansible", "deploy"})
# Never masked: public codenames and loopback/any addresses.
KEEP = frozenset({"walter", "covenant", "localhost", "127.0.0.1", "0.0.0.0", "::1"})

_S0, _S1 = "", ""
_SENT_RE = re.compile(_S0 + r"([-]+)" + _S1)
PLACEHOLDER_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]*(?:\[\d+\]|:[a-z]+)?)\}")
_SPLIT_RE = re.compile(r"[\s,;]+")


@dataclass
class Entry:
    value: str          # as configured
    placeholder: str    # ${KEY}, ${KEY[2]}, ${KEY:net} ...
    kind: str           # host | email | ip | user | generic-user | alias | bucket | name | id
    pattern: "re.Pattern[str]" = field(repr=False, default=None)  # type: ignore[assignment]


def _enc(i: int) -> str:
    out = ""
    while True:
        out = chr(0xE100 + i % 0x1000) + out
        i //= 0x1000
        if not i:
            return _S0 + out + _S1


def _dec(s: str) -> int:
    n = 0
    for ch in s:
        n = n * 0x1000 + (ord(ch) - 0xE100)
    return n


def _is_ipv4(v: str) -> bool:
    try:
        ipaddress.IPv4Address(v)
        return True
    except ValueError:
        return False


_W = r"A-Za-z0-9_\-"


def _pattern(value: str, kind: str) -> "re.Pattern[str]":
    if kind == "ip":
        if "-" in value:  # dashed form ip-10-0-0-1
            # ec2-1-2-3-4 / ip-1-2-3-4, never the tail of a longer dashed number (9-1-2-3-4)
            return re.compile(r"(?:(?<=[A-Za-z]-)|(?<=[A-Za-z]\d-)|(?<=[A-Za-z]\d\d-)|(?<![\d-]))"
                              + re.escape(value) + r"(?!\d)(?!-\d)")
        return re.compile(r"(?<!\d)(?<!\d\.)" + re.escape(value) + r"(?!\d)(?!\.\d)")
    if kind == "name":
        body = r"\s+".join(re.escape(w) for w in value.split())
        return re.compile(r"(?i)(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])")
    if kind == "generic-user":
        v = re.escape(value)
        return re.compile(r"(?i)(?:(?<![" + _W + r".])" + v + r"(?=@)|(?<=/home/)" + v + r"(?![" + _W + r"])"
                          r"|(?<=~)" + v + r"(?![" + _W + r"])|(?<=-l )" + v + r"(?![" + _W + r"])"
                          r"|(?<=\bUser )" + v + r"(?![" + _W + r"]))")
    if kind == "id":
        return re.compile(r"(?i)(?<![A-Za-z0-9+/=_-])" + re.escape(value) + r"(?![A-Za-z0-9+/=_-])")
    return re.compile(r"(?i)(?<![" + _W + r"])" + re.escape(value) + r"(?![" + _W + r"])")


def _items(cfg: Mapping[str, str]) -> List[Tuple[str, str, str]]:
    """(value, placeholder, kind) candidates, in a deterministic order (first placeholder per value wins)."""
    out: List[Tuple[str, str, str]] = []
    keys = sorted(cfg)

    def val(k: str) -> str:
        return str(cfg.get(k) or "").strip()

    def listed(k: str, kind: str) -> None:
        parts = [p for p in _SPLIT_RE.split(val(k)) if p]
        for n, p in enumerate(parts, 1):
            out.append((p, f"${{{k}}}" if len(parts) == 1 else f"${{{k}[{n}]}}", kind))

    def ip(v: str, ph: str) -> None:
        out.append((v, ph, "ip"))
        if _is_ipv4(v):
            out.append((v.replace(".", "-"), ph[:-1] + ":dashed}", "ip"))

    dom = val("SPARK_DOMAIN").strip(".")
    if dom:
        out.append((dom, "${SPARK_DOMAIN}", "host"))
        stem = dom.rsplit(".", 1)[0] if "." in dom else ""
        if len(stem) >= STEM_MIN:
            out.append((stem, "${SPARK_DOMAIN:stem}", "host"))
    for k in keys:
        if k.startswith("SPARK_") and k.endswith("_HOST") and val(k):
            out.append((val(k).strip("."), f"${{{k}}}", "host"))
    for k in keys:
        if k.endswith("_EMAIL") and val(k):
            out.append((val(k), f"${{{k}}}", "email"))
    for k in keys:
        if k.endswith("_IP") and val(k):
            ip(val(k), f"${{{k}}}")
        elif k.endswith("_IPS") and val(k):
            parts = [p for p in _SPLIT_RE.split(val(k)) if p]
            for n, p in enumerate(parts, 1):
                ip(p, f"${{{k}}}" if len(parts) == 1 else f"${{{k}[{n}]}}")
        elif k.endswith("_SUBNET") and val(k):
            v = val(k)
            out.append((v, f"${{{k}}}", "ip"))
            if "/" in v and _is_ipv4(v.split("/", 1)[0]):
                out.append((v.split("/", 1)[0], f"${{{k}:net}}", "ip"))
    for k in keys:
        if k.endswith("_SSH_USER") and val(k):
            out.append((val(k), f"${{{k}}}", "user"))
    listed("SPARK_USERS", "user")
    listed("JUDGE_SSH_ALIASES", "alias")
    for k in keys:
        if k.endswith("_BUCKET") and val(k):
            out.append((val(k), f"${{{k}}}", "bucket"))
    if val("SPARK_SITE_NAME"):
        out.append((" ".join(val("SPARK_SITE_NAME").split()), "${SPARK_SITE_NAME}", "name"))
    for k in keys:
        v = val(k)
        if (k.endswith(("_PUBLIC_KEY", "_UUID", "_CLIENT_ID")) and len(v) >= ID_MIN and re.search(r"\d", v)):
            out.append((v, f"${{{k}}}", "id"))
    return out


class SiteMask:
    def __init__(self, entries: Iterable[Entry]):
        self.entries: List[Entry] = sorted(entries, key=lambda e: (-len(e.value), e.placeholder))
        self.by_placeholder: Dict[str, str] = {e.placeholder: e.value for e in self.entries}

    def __bool__(self) -> bool:
        return bool(self.entries)

    @property
    def keys(self) -> List[str]:
        return sorted(self.by_placeholder)

    # -------------------------------------------------------------- mask
    def mask_count(self, text: str) -> Tuple[str, int]:
        if not self.entries or not text:
            return text, 0
        # placeholders already in the text (templates) are protected, so no value matches inside one
        base = len(self.entries)
        protected: List[str] = []

        def protect(m: "re.Match[str]") -> str:
            protected.append(m.group(0))
            return _enc(base + len(protected) - 1)
        text = PLACEHOLDER_RE.sub(protect, text)
        n = 0
        for i, e in enumerate(self.entries):
            text, k = e.pattern.subn(_enc(i), text)
            n += k
        for i, e in enumerate(self.entries):  # sweep: any non-address value left as a plain substring
            if e.kind in ("ip", "generic-user"):
                continue
            rx = re.compile(r"(?i)" + (r"\s+".join(re.escape(w) for w in e.value.split()) if e.kind == "name"
                                       else re.escape(e.value)))
            text, k = rx.subn(_enc(i), text)
            n += k
        def expand(m: "re.Match[str]") -> str:
            i = _dec(m.group(1))
            return self.entries[i].placeholder if i < base else protected[i - base]
        return _SENT_RE.sub(expand, text), n

    def mask(self, text: str) -> str:
        return self.mask_count(text)[0]

    def mask_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.mask(obj)
        if isinstance(obj, list):
            return [self.mask_obj(x) for x in obj]
        if isinstance(obj, dict):
            return {self.mask(k) if isinstance(k, str) else k: self.mask_obj(v) for k, v in obj.items()}
        return obj

    # -------------------------------------------------------------- unmask
    def unmask(self, text: str, keep: Iterable[str] = ()) -> str:
        if not self.entries or not text or "${" not in text:
            return text
        keep = set(keep)
        return PLACEHOLDER_RE.sub(
            lambda m: m.group(0) if m.group(0) in keep else self.by_placeholder.get(m.group(0), m.group(0)), text)

    def unmask_obj(self, obj: Any, keep: Iterable[str] = ()) -> Any:
        keep = set(keep)
        if isinstance(obj, str):
            return self.unmask(obj, keep)
        if isinstance(obj, list):
            return [self.unmask_obj(x, keep) for x in obj]
        if isinstance(obj, dict):
            return {k: self.unmask_obj(v, keep) for k, v in obj.items()}
        return obj

    # -------------------------------------------------------------- checks
    def literal_placeholders(self, text: str) -> Set[str]:
        """Placeholders of this mask that already occur in *text* (before masking): ambiguous when unmasking."""
        return {m.group(0) for m in PLACEHOLDER_RE.finditer(text or "") if m.group(0) in self.by_placeholder}

    def residual(self, text: str) -> Dict[str, int]:
        """{placeholder: count} of real values still in *text*: the boundary match for addresses and generic
        accounts, a case-insensitive substring match for everything else. Names only, never values."""
        out: Dict[str, int] = {}
        text = PLACEHOLDER_RE.sub(" ", text or "")
        for e in self.entries:  # longest first; a counted value is blanked, so a shorter one inside it is not
            if e.kind in ("ip", "generic-user"):
                rx = e.pattern
            elif e.kind == "name":
                rx = re.compile(r"(?i)" + r"\s+".join(re.escape(w) for w in e.value.split()))
            else:
                rx = re.compile(r"(?i)" + re.escape(e.value))
            text, n = rx.subn(" ", text)
            if n:
                out[e.placeholder] = out.get(e.placeholder, 0) + n
        return out


def build(cfg: Mapping[str, str]) -> SiteMask:
    seen: Set[str] = set()
    entries: List[Entry] = []
    for value, ph, kind in _items(cfg):
        if len(value) < MIN_LEN or value.lower() in KEEP:
            continue
        if kind == "user" and value.lower() in GENERIC_USERS:
            kind = "generic-user"
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        entries.append(Entry(value=value, placeholder=ph, kind=kind, pattern=_pattern(value, kind)))
    return SiteMask(entries)


def from_config(cfg: Optional[Mapping[str, str]] = None) -> SiteMask:
    if cfg is None:
        try:
            from lib import config  # judge/ on sys.path
        except ImportError:  # pragma: no cover
            import importlib
            config = importlib.import_module("config")
        cfg = config.load_config()
    return build(cfg)
