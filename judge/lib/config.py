#!/usr/bin/env python3
"""judge/lib/config.py: site.env loader, judge defaults, path resolution, data_class rules, SSH argv.

Stdlib only. Import with judge/ on sys.path:  ``from lib import config``.

Public API (other judge parts depend on it; keep it stable):

    load_config(site_env=None, environ=None) -> dict[str, str]
        defaults < site.env < process environment.  Every value is a string.  Always present:
        HERMES_HOME, JUDGE_REVIEW_DIR (absolute, ~ expanded), JUDGE_REPO_DIR, JUDGE_DIR, SITE_ENV
        (path of the file that was read, or "" when none) and every key in DEFAULTS.
    hermes_home(cfg=None) -> Path
    review_dir(cfg=None, create=False) -> Path       create=True makes it (mode 700)
    classify(paths, cfg=None, cwd=None) -> "infra" | "sensitive"
    is_infra_path(path, cfg=None, cwd=None) -> bool
    host_ssh(name, cfg=None) -> list[str]             argv prefix; append ONE remote command string
    window_grace(cfg=None) -> int                     JUDGE_WINDOW_GRACE_SECONDS (evidence window end grace)
    HOSTS = ("walter", "covenant")
    HOST_RULES                                        C2 gate rules about commands aimed at Walter/Covenant
    parse_env_file(path) -> dict[str, str]
    get(key, default="", cfg=None) -> str

CLI (used by lib/config.sh):
    config.py --export          print `export KEY='value'` lines (shell-quoted) for eval
    config.py --get KEY         print one value
    config.py --review-dir      print the review dir
    config.py --classify PATH.. print infra|sensitive
    config.py --ssh HOST        print the ssh argv prefix (shell-quoted)
"""
from __future__ import annotations

import fnmatch
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional

JUDGE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = JUDGE_DIR.parent

HOSTS = ("walter", "covenant")

# C2 gate rules whose evidence is a command aimed at Walter/Covenant (no local paths): infra by definition.
# Shared by hooks/gate.py (classifies its own requests) and collector/collect.py (decides when to trust that
# classification for a request without changed paths).
HOST_RULES = frozenset({"remote-mutation", "remote-opaque", "remote-copy"})

# Judge defaults (contract: site.env variables under `# --- judge`), plus existing site vars the judge
# needs a fallback for.  Values are strings, like everything else loaded from site.env.
DEFAULTS: Dict[str, str] = {
    "JUDGE_MODE": "frontier",              # frontier|local
    "JUDGE_LOCAL_MODEL": "big",
    "JUDGE_FRONTIER_CMD": "claude",
    "JUDGE_SSH_ALIASES": "",               # ssh aliases that reach the edge (space-separated)
    "EDGE_SSH_USER": "ubuntu",
    "EDGE_SSH_KEY": "~/.ssh/edge.pem",
    "JUDGE_RUNAWAY_TOKENS": "20000",
    "JUDGE_RUNAWAY_MINUTES": "10",
    "JUDGE_PROBE_TIMEOUT": "20",           # seconds, per probe
    "JUDGE_SSH_CONNECT_TIMEOUT": "8",       # seconds, ssh ConnectTimeout
    "JUDGE_INFRA_REPOS": "",               # extra repo dirs whose files are infra (space-separated)
    "JUDGE_LOG_TZ": "",                    # tz of Hermes log timestamps: "" = system local, "UTC", "+02:00", IANA
    "JUDGE_COMPLETION_DEDUPE_SECONDS": "900",  # completion requests of a session within this window dedupe
    "JUDGE_WINDOW_GRACE_SECONDS": "10",    # evidence window = [request.since, request.created + this]
    "BACKEND_SSH_USER": "operator",
    "LLAMA_SWAP_PORT": "8080",
}

# Keys the process environment may override even when site.env does not define them.
_ENV_KEYS = set(DEFAULTS) | {
    "HERMES_HOME", "JUDGE_REVIEW_DIR", "JUDGE_REPO_DIR",
    "BACKEND_LAN_IP", "BACKEND_WG_IP", "EDGE_PUBLIC_IP", "EDGE_WG_IP", "SPARK_DOMAIN", "SPARK_API_HOST",
}

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


# ---------------------------------------------------------------- site.env
def parse_env_file(path) -> Dict[str, str]:
    """Parse KEY=VALUE lines. Supports `export`, single/double quotes, `#` comments (full-line, or after an
    unquoted value when preceded by whitespace). No variable expansion, no command substitution."""
    out: Dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        key, rest = m.group(1), m.group(2).strip()
        if rest[:1] in ("'", '"'):
            q = rest[0]
            end = rest.find(q, 1)
            val = rest[1:end] if end != -1 else rest[1:]
        else:
            val = re.split(r"\s+#", rest, maxsplit=1)[0].strip()
            if val.startswith("#"):
                val = ""
        out[key] = val
    return out


def site_env_path(environ: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    env = os.environ if environ is None else environ
    p = env.get("SITE_ENV")
    if p:
        p = Path(os.path.expanduser(p))
        return p if p.is_file() else None
    p = REPO_ROOT / "site.env"
    return p if p.is_file() else None


def _expand(p: str) -> str:
    return os.path.abspath(os.path.expanduser(p))


def load_config(site_env=None, environ: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Merged config dict: DEFAULTS < site.env < environment. All values are strings."""
    env = os.environ if environ is None else environ
    path = Path(os.path.expanduser(str(site_env))) if site_env else site_env_path(env)
    site = parse_env_file(path) if path and path.is_file() else {}
    cfg: Dict[str, str] = dict(DEFAULTS)
    cfg.update(site)
    for k in _ENV_KEYS | set(site):
        v = env.get(k)
        if v not in (None, ""):
            cfg[k] = v
    cfg["HERMES_HOME"] = _expand(cfg.get("HERMES_HOME") or "~/.hermes")
    cfg["JUDGE_REVIEW_DIR"] = _expand(cfg.get("JUDGE_REVIEW_DIR") or os.path.join(cfg["HERMES_HOME"], "review"))
    cfg["JUDGE_REPO_DIR"] = _expand(cfg.get("JUDGE_REPO_DIR") or str(REPO_ROOT))
    cfg["JUDGE_DIR"] = str(JUDGE_DIR)
    cfg["SITE_ENV"] = str(path) if path and path.is_file() else ""
    return cfg


def _cfg(cfg: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return cfg if cfg is not None else load_config()


def get(key: str, default: str = "", cfg: Optional[Mapping[str, str]] = None) -> str:
    v = _cfg(cfg).get(key)
    return v if v not in (None, "") else default


def hermes_home(cfg: Optional[Mapping[str, str]] = None) -> Path:
    return Path(_cfg(cfg)["HERMES_HOME"])


def review_dir(cfg: Optional[Mapping[str, str]] = None, create: bool = False) -> Path:
    d = Path(_cfg(cfg)["JUDGE_REVIEW_DIR"])
    if create:
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    return d


def window_grace(cfg: Optional[Mapping[str, str]] = None) -> int:
    """Seconds added to a request's `created` to close its evidence window (default 10, never negative)."""
    try:
        return max(0, int(float(_cfg(cfg).get("JUDGE_WINDOW_GRACE_SECONDS") or 10)))
    except ValueError:
        return 10


# ---------------------------------------------------------------- data_class rules
def _real(p: str) -> str:
    return os.path.realpath(os.path.expanduser(p))


def infra_rules(cfg: Optional[Mapping[str, str]] = None) -> List[str]:
    """Absolute glob patterns (fnmatch; `*` crosses `/`) whose matches are infra-class.

    Hermes config/skills/memories/plans, ~/.ssh/config, the deck repo (JUDGE_REPO_DIR plus JUDGE_INFRA_REPOS),
    /etc, /srv.  Anything else (incl. ~/.hermes/.env, other repos, home files) is sensitive."""
    c = _cfg(cfg)
    hh = _real(c["HERMES_HOME"])
    rules = [
        f"{hh}/config.yaml",
        f"{hh}/skills/*",
        f"{hh}/memories/*",
        f"{hh}/plans/*",
        "*/.hermes/plans/*",
        _real("~/.ssh/config"),
        "/etc/*",
        "/srv/*",
    ]
    repos = [c["JUDGE_REPO_DIR"]] + (c.get("JUDGE_INFRA_REPOS") or "").split()
    for r in repos:
        if r:
            rules.append(_real(r).rstrip("/") + "/*")
    return rules


_REMOTE_RE = re.compile(r"^(walter|covenant):(/.*)$")


def _normalize(path: str, cwd: Optional[str]) -> str:
    p = os.path.expanduser(str(path))
    if not os.path.isabs(p):
        p = os.path.join(cwd or os.getcwd(), p)
    return os.path.realpath(p)


def is_infra_path(path: str, cfg: Optional[Mapping[str, str]] = None, cwd: Optional[str] = None) -> bool:
    """True when *path* matches an infra rule. `walter:/etc/x` / `covenant:/srv/x` (host files) count when
    under /etc or /srv.  Paths are expanded (~), made absolute against *cwd*, and symlink-resolved."""
    if not isinstance(path, str) or not path.strip():
        return False
    m = _REMOTE_RE.match(path.strip())
    if m:
        rp = os.path.normpath(m.group(2))
        return rp.startswith("/etc/") or rp.startswith("/srv/")
    p = _normalize(path.strip(), cwd)
    rules = infra_rules(cfg)
    if any(fnmatch.fnmatchcase(p, r) for r in rules):
        # ~/.hermes/.env and similar secrets never count as infra, even if a rule were broadened.
        return not p.endswith("/.env")
    return False


def classify(paths: Iterable[str], cfg: Optional[Mapping[str, str]] = None, cwd: Optional[str] = None) -> str:
    """'infra' only when there is at least one path and EVERY path is infra; otherwise 'sensitive'.

    With no paths at all the session's *cwd* decides (infra only if cwd itself is inside an infra location,
    e.g. the deck repo); no paths and no cwd -> 'sensitive'."""
    plist = [p for p in (paths or []) if isinstance(p, str) and p.strip()]
    if not plist:
        if cwd and is_infra_path(os.path.join(cwd, ".judge-cwd-probe"), cfg, cwd):
            return "infra"
        return "sensitive"
    return "infra" if all(is_infra_path(p, cfg, cwd) for p in plist) else "sensitive"


# ---------------------------------------------------------------- ssh
def host_ssh(name: str, cfg: Optional[Mapping[str, str]] = None) -> List[str]:
    """ssh argv prefix for walter|covenant. Append exactly one remote command string.

    walter:   ssh <opts> BACKEND_SSH_USER@BACKEND_LAN_IP
    covenant: ssh <opts> -i EDGE_SSH_KEY EDGE_SSH_USER@EDGE_PUBLIC_IP when EDGE_SSH_KEY / EDGE_SSH_USER is set
              explicitly (site.env or env); else the first JUDGE_SSH_ALIASES alias; else the defaults.
    Options: BatchMode=yes (never prompt), ConnectTimeout, no agent/X11 forwarding. Raises ValueError for
    unknown hosts or missing addresses."""
    c = _cfg(cfg)
    ct = str(c.get("JUDGE_SSH_CONNECT_TIMEOUT") or "8")
    base = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={ct}", "-o", "ForwardAgent=no",
            "-o", "ForwardX11=no", "-o", "ClearAllForwardings=yes"]
    if name == "walter":
        ip = c.get("BACKEND_LAN_IP")
        if not ip:
            raise ValueError("BACKEND_LAN_IP is not set in site.env")
        return base + [f"{c.get('BACKEND_SSH_USER') or 'operator'}@{ip}"]
    if name == "covenant":
        site_file = c.get("SITE_ENV")
        site = parse_env_file(site_file) if site_file else {}
        explicit = any(site.get(k) or os.environ.get(k) for k in ("EDGE_SSH_KEY", "EDGE_SSH_USER"))
        aliases = (c.get("JUDGE_SSH_ALIASES") or "").split()
        if not explicit and aliases:
            return base + [aliases[0]]
        ip = c.get("EDGE_PUBLIC_IP")
        if not ip:
            raise ValueError("EDGE_PUBLIC_IP is not set in site.env")
        key = os.path.expanduser(c.get("EDGE_SSH_KEY") or DEFAULTS["EDGE_SSH_KEY"])
        return base + ["-o", "IdentitiesOnly=yes", "-i", key, f"{c.get('EDGE_SSH_USER') or 'ubuntu'}@{ip}"]
    raise ValueError(f"unknown host {name!r} (expected one of {', '.join(HOSTS)})")


# ---------------------------------------------------------------- CLI
def _main(argv: List[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cfg = load_config()
    cmd, rest = argv[0], argv[1:]
    if cmd == "--export":
        for k in sorted(cfg):
            if _KEY_RE.match(k):
                print(f"export {k}={shlex.quote(cfg[k])}")
        return 0
    if cmd == "--get" and len(rest) == 1:
        print(cfg.get(rest[0], ""))
        return 0
    if cmd == "--review-dir":
        print(review_dir(cfg))
        return 0
    if cmd == "--classify":
        print(classify(rest, cfg))
        return 0
    if cmd == "--ssh" and len(rest) == 1:
        try:
            print(" ".join(shlex.quote(a) for a in host_ssh(rest[0], cfg)))
        except ValueError as e:
            print(f"config: {e}", file=sys.stderr)
            return 2
        return 0
    print("usage: config.py --export | --get KEY | --review-dir | --classify PATH... | --ssh HOST", file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
