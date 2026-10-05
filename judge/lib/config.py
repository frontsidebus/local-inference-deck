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
    classify(paths, cfg=None, cwd=None, max_mixed=None) -> "infra" | "sensitive"   per-path rules, see below (#43)
    classify_detail(paths, cfg=None, cwd=None, max_mixed=None) -> dict   the class plus each path's class
    path_class(path, cfg=None, cwd=None) -> "secret" | "scratch" | "infra" | "sensitive"
    is_scratch_path(path, cfg=None, cwd=None) -> bool  agent scratch/cache (SCRATCH_GLOBS + JUDGE_SCRATCH_GLOBS)
    is_infra_path(path, cfg=None, cwd=None) -> bool   False for every is_secret_path (#42)
    is_secret_path(path, cfg=None) -> bool            site.env, *.env, keys, secrets/ ... (SECRET_GLOBS + JUDGE_SECRET_GLOBS)
    git_common_dir(path) -> str | None               the repo's shared .git dir (worktrees: the main repo's)
    infra_git_dirs(cfg=None) -> set[str]              git_common_dir of JUDGE_REPO_DIR and JUDGE_INFRA_REPOS
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
    config.py --explain PATH..  print classify_detail() as JSON (per-path classes)
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
    "JUDGE_RUNAWAY_TOKENS": "24000",        # C6: n_decoded threshold, any n_predict (README)
    "JUDGE_RUNAWAY_MINUTES": "10",
    "JUDGE_PROBE_TIMEOUT": "20",           # seconds, per probe
    "JUDGE_SSH_CONNECT_TIMEOUT": "8",       # seconds, ssh ConnectTimeout
    "JUDGE_INFRA_REPOS": "",               # extra repo dirs whose files are infra (space-separated)
    "JUDGE_LOG_TZ": "",                    # tz of Hermes log timestamps: "" = system local, "UTC", "+02:00", IANA
    "JUDGE_COMPLETION_DEDUPE_SECONDS": "900",  # completion requests of a session within this window dedupe
    "JUDGE_WINDOW_GRACE_SECONDS": "10",    # evidence window = [request.since, request.created + this]
    "JUDGE_PLAN_DEBOUNCE_S": "120",        # plan review waits for turn end or this long with no plan write (#34)
    "JUDGE_STALL_MINUTES": "15",           # #41: warn when a ready queue request waited this long (0 = off)
    "JUDGE_SECRET_GLOBS": "",              # #42: extra basename globs of secret files (never infra-class)
    "JUDGE_SCRATCH_GLOBS": "",             # #43: extra globs of agent scratch/cache paths (never decide the class)
    "JUDGE_MIXED_MAX_SENSITIVE": "3",      # #43: an infra request may carry up to N withheld sensitive paths (0 = strict)
    # run-1 fixes (one place for every default; site.env and the environment override)
    "JUDGE_HOST_PROBES": "1",              # collector runs read-only host-state probes for host claims
    "JUDGE_NOISE_GLOBS": "",               # extra globs added to snapshot.NOISE_GLOBS
    "JUDGE_SNAPSHOT_MAX_FILES": "2000",    # per opted-in dir root
    "JUDGE_SNAPSHOT_MAX_BYTES": "1048576", # larger files are hashed, not copied
    "JUDGE_LOCAL_MAX_SEVERITY": "medium",  # cap for findings from the local judge
    "JUDGE_INJECT_LOCAL": "0",             # 1 = C5 also injects local-judge findings
    "JUDGE_SENSITIVE_FRONTIER_CLAIMS": "1", # 1 = sensitive completions also get a frontier claims-only review
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
    /etc, /srv.  Anything else (incl. ~/.hermes/.env, other repos, home files) is sensitive, except git
    worktrees and clones' worktrees that share a repo's .git with an infra repo (see is_infra_path, #37)."""
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


# ---------------------------------------------------------------- secret-shaped files (#42)
# Basename globs (fnmatch, case-insensitive) of files that hold secrets or site values. They are never
# infra-class, wherever they live (an infra repo or its worktree, /etc, /srv, a host path), so a change to
# one keeps the whole bundle sensitive: local judge, no file contents in any frontier bundle.
SECRET_GLOBS = (
    # the gate's secret_output.secret_names (policy/gate-policy.json.tmpl), so both agree on "secret-shaped"
    "*.key", "*.pem", ".env", "*.env", "api-key", "api_key", "apikey", "*-api-key", "*_api_key",
    "*secret*", "admin-password", "*password*", "shadow", "gshadow", "id_rsa", "id_ecdsa", "id_ed25519",
    "id_dsa", "*.p12", "*.pfx", "*.keystore", "credentials", "credentials.json", ".netrc", ".pgpass",
    "auth.json", "wg*.conf", "token", "*.token", "*-token", "*_token", "privkey*", "*.age", "*.kdbx",
    # plus env-file variants and backups (site.env.bak-*), more key stores, and the sanitizer's word lists
    ".env.*", "*.env.*", "*.jks", ".htpasswd", ".git-credentials", "id_*_sk", ".sanitize-*",
)
# Templates, examples, public keys, code and docs carry no values (the gate's not_secret_names, plus .dist):
# they stay classified by location (site.env.example, gen-secrets.sh, rotate-secrets.md).
SECRET_EXEMPT_SUFFIXES = (".pub", ".example", ".sample", ".tmpl", ".template", ".dist", ".md", ".sh", ".py", ".rs",
                          ".go", ".js", ".ts", ".d", ".service", ".j2", ".html", ".css")
SECRET_DIRS = ("secrets", ".secrets", "private")   # any path component -> secret (secrets/, /etc/ssl/private/)


def secret_globs(cfg: Optional[Mapping[str, str]] = None) -> List[str]:
    """SECRET_GLOBS plus JUDGE_SECRET_GLOBS (space-separated basename globs from site.env or the env)."""
    extra = (_cfg(cfg).get("JUDGE_SECRET_GLOBS") or "").split()
    return list(SECRET_GLOBS) + extra


def is_secret_path(path: str, cfg: Optional[Mapping[str, str]] = None) -> bool:
    """True when *path* (local, `walter:/x` or `covenant:/x`) names a secret-shaped file: its basename matches
    secret_globs() (and is not a template/example), or a directory component is secrets/, .secrets/ or
    private/. Purely lexical: no file is opened."""
    if not isinstance(path, str) or not path.strip():
        return False
    m = _REMOTE_RE.match(path.strip())
    p = m.group(2) if m else os.path.expanduser(path.strip())
    p = os.path.normpath(p)
    parts = [x for x in p.split(os.sep) if x]
    if not parts:
        return False
    if any(x.lower() in SECRET_DIRS for x in parts[:-1]):
        return True
    base = parts[-1].lower()
    if base.endswith(SECRET_EXEMPT_SUFFIXES):
        return False
    return any(fnmatch.fnmatchcase(base, g.lower()) for g in secret_globs(cfg))


# ---------------------------------------------------------------- git worktrees (#37)
def _read_gitdir_file(dotgit: str) -> Optional[str]:
    """The `gitdir:` target of a `.git` FILE (a worktree or submodule checkout), absolute; None if unreadable."""
    try:
        with open(dotgit, encoding="utf-8", errors="replace") as fh:
            first = fh.read(4096).splitlines()[:1]
    except OSError:
        return None
    if not first or not first[0].startswith("gitdir:"):
        return None
    target = first[0][len("gitdir:"):].strip()
    if not target:
        return None
    if not os.path.isabs(target):
        target = os.path.join(os.path.dirname(dotgit), target)
    return os.path.realpath(target)


def _common_from_gitdir(gitdir: str) -> Optional[str]:
    """Shared .git dir of a worktree's private gitdir (<main>/.git/worktrees/<name>): its `commondir` file,
    else the `worktrees/` layout. None when it is neither (e.g. a submodule's .git/modules/<x>)."""
    try:
        with open(os.path.join(gitdir, "commondir"), encoding="utf-8", errors="replace") as fh:
            rel = fh.read(4096).strip()
        if rel:
            common = rel if os.path.isabs(rel) else os.path.join(gitdir, rel)
            common = os.path.realpath(common)
            if os.path.isdir(common):
                return common
    except OSError:
        pass
    parent = os.path.dirname(gitdir)
    if os.path.basename(parent) == "worktrees" and os.path.isdir(gitdir):
        return os.path.realpath(os.path.dirname(parent))
    return None


_COMMON_CACHE: Dict[str, Optional[str]] = {}


def git_common_dir(path: str) -> Optional[str]:
    """The shared git dir (`git rev-parse --git-common-dir`, resolved) of the checkout holding *path*, found
    from the nearest ancestor with a `.git` entry: a `.git` directory is the common dir itself; a `.git`
    file (`gitdir: <main>/.git/worktrees/<name>`) is a linked worktree whose common dir is the main repo's
    .git. Parsed from the files (no subprocess). None when there is no checkout or the layout is unknown
    (submodules, broken gitdir): callers treat None as "not the same repo" (sensitive by default)."""
    if not isinstance(path, str) or not path:
        return None
    d = os.path.realpath(os.path.expanduser(path))
    while d and not os.path.isdir(d):  # a new or deleted file: start from its nearest existing directory
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    seen: List[str] = []
    out: Optional[str] = None
    while True:
        if d in _COMMON_CACHE:
            out = _COMMON_CACHE[d]
            break
        seen.append(d)
        dotgit = os.path.join(d, ".git")
        if os.path.isdir(dotgit):
            out = os.path.realpath(dotgit)
            break
        if os.path.isfile(dotgit):
            gitdir = _read_gitdir_file(dotgit)
            out = _common_from_gitdir(gitdir) if gitdir else None
            break
        if os.path.basename(d) == ".git":  # inside a repo's git dir itself
            out = d
            break
        parent = os.path.dirname(d)
        if parent == d:
            out = None
            break
        d = parent
    if len(_COMMON_CACHE) > 4096:
        _COMMON_CACHE.clear()
    for s in seen:
        _COMMON_CACHE[s] = out
    return out


def infra_git_dirs(cfg: Optional[Mapping[str, str]] = None) -> set:
    """git_common_dir of JUDGE_REPO_DIR and each JUDGE_INFRA_REPOS entry (those that are git checkouts)."""
    c = _cfg(cfg)
    out = set()
    for r in [c.get("JUDGE_REPO_DIR") or ""] + (c.get("JUDGE_INFRA_REPOS") or "").split():
        if r and os.path.isdir(os.path.expanduser(r)):
            g = git_common_dir(os.path.join(_real(r), ".judge-repo-probe"))
            if g:
                out.add(g)
    return out


def _normalize(path: str, cwd: Optional[str]) -> str:
    p = os.path.expanduser(str(path))
    if not os.path.isabs(p):
        p = os.path.join(cwd or os.getcwd(), p)
    return os.path.realpath(p)


def is_infra_path(path: str, cfg: Optional[Mapping[str, str]] = None, cwd: Optional[str] = None) -> bool:
    """True when *path* matches an infra rule. `walter:/etc/x` / `covenant:/srv/x` (host files) count when
    under /etc or /srv.  Paths are expanded (~), made absolute against *cwd*, and symlink-resolved.
    A path in a git worktree whose shared git dir (git_common_dir) is that of JUDGE_REPO_DIR or a
    JUDGE_INFRA_REPOS entry is infra too (#37: worktrees are classed like their main repo)."""
    if not isinstance(path, str) or not path.strip():
        return False
    if is_secret_path(path, cfg):
        # #42: site.env, *.env, keys, secrets/ ... never count as infra, even inside an infra repo or /etc.
        return False
    m = _REMOTE_RE.match(path.strip())
    if m:
        rp = os.path.normpath(m.group(2))
        return rp.startswith("/etc/") or rp.startswith("/srv/")
    p = _normalize(path.strip(), cwd)
    if p.endswith("/.env") or is_secret_path(p, cfg):
        # ~/.hermes/.env and similar secrets never count as infra, even if a rule were broadened; the
        # resolved path is checked too (a symlink named like a plain file that points at a key).
        return False
    rules = infra_rules(cfg)
    if any(fnmatch.fnmatchcase(p, r) for r in rules):
        return True
    # #37: another worktree (or the main clone) of an infra repo is the same repo: same class. Decided by
    # the shared git dir; anything undetermined stays sensitive.
    common = git_common_dir(p)
    return bool(common) and common in infra_git_dirs(cfg)


# ---------------------------------------------------------------- agent scratch / caches (#43)
# Absolute fnmatch globs (`*` crosses `/`; `~` and $HERMES_HOME expanded) of the agent's own scratch and cache
# dirs. A changed path there never decides a request's class (it is neither infra nor sensitive), and no bundle
# shows its content: at most its name and a line count. Secret-shaped files (is_secret_path) are secret wherever
# they live, scratch dirs included.
SCRATCH_GLOBS = ("{hh}/cache/*", "{hh}/tmp/*", "~/.cache/*", "*/__pycache__/*", "*/.pytest_cache/*",
                 "*/.mypy_cache/*", "*/.ruff_cache/*")


def scratch_globs(cfg: Optional[Mapping[str, str]] = None) -> List[str]:
    """SCRATCH_GLOBS (with $HERMES_HOME and ~ expanded, symlinks resolved) plus JUDGE_SCRATCH_GLOBS."""
    c = _cfg(cfg)
    hh = _real(c.get("HERMES_HOME") or "~/.hermes")
    out: List[str] = []
    for g in list(SCRATCH_GLOBS) + re.split(r"[\s,]+", c.get("JUDGE_SCRATCH_GLOBS") or ""):
        if not g:
            continue
        g = g.replace("{hh}", hh)
        if g.startswith("~"):
            g = os.path.expanduser(g)
        if g.startswith("/"):
            head, star, tail = g.partition("*")
            # resolve the literal leading directory (e.g. ~/.cache -> its real location), keep the pattern part
            d = os.path.dirname(head) if not head.endswith("/") else head.rstrip("/")
            if d and os.path.isabs(d):
                g = _real(d).rstrip("/") + head[len(d):] + star + tail
        out.append(g)
    return list(dict.fromkeys(out))


def is_scratch_path(path: str, cfg: Optional[Mapping[str, str]] = None, cwd: Optional[str] = None) -> bool:
    """True when *path* (resolved) is under an agent scratch/cache glob (scratch_globs). Host paths never are."""
    if not isinstance(path, str) or not path.strip() or _REMOTE_RE.match(path.strip()):
        return False
    p = _normalize(path.strip(), cwd)
    return any(fnmatch.fnmatchcase(p, g) for g in scratch_globs(cfg))


def path_class(path: str, cfg: Optional[Mapping[str, str]] = None, cwd: Optional[str] = None) -> str:
    """One path's class: "secret" (is_secret_path, of the path as given or resolved), else "scratch"
    (is_scratch_path), else "infra" (is_infra_path), else "sensitive"."""
    if not isinstance(path, str) or not path.strip():
        return "sensitive"
    raw = path.strip()
    if is_secret_path(raw, cfg):
        return "secret"
    if not _REMOTE_RE.match(raw):
        p = _normalize(raw, cwd)
        if p.endswith("/.env") or is_secret_path(p, cfg):
            return "secret"
    if is_scratch_path(raw, cfg, cwd):
        return "scratch"
    return "infra" if is_infra_path(raw, cfg, cwd) else "sensitive"


def mixed_max(cfg: Optional[Mapping[str, str]] = None) -> int:
    """JUDGE_MIXED_MAX_SENSITIVE (default 3; 0 = strict, any sensitive path makes the request sensitive)."""
    try:
        return max(0, int(float(_cfg(cfg).get("JUDGE_MIXED_MAX_SENSITIVE") or 3)))
    except ValueError:
        return 3


def classify_detail(paths: Iterable[str], cfg: Optional[Mapping[str, str]] = None, cwd: Optional[str] = None,
                    max_mixed: Optional[int] = None) -> Dict:
    """Per-path classification (#43). Returns {"class", "reason", "infra", "sensitive", "secret", "scratch"}
    (the four lists hold the given path strings).

    1. any secret path -> sensitive (#42: a secret file keeps the whole bundle away from the frontier);
    2. scratch paths are left out; with no other path the session *cwd* decides, as with no paths at all;
    3. every remaining path infra -> infra;
    4. infra plus a few sensitive paths (at most *max_mixed*, default JUDGE_MIXED_MAX_SENSITIVE, and no more
       than the infra paths) -> infra with those paths WITHHELD: the collector shows no content and no name
       for them (reason "mixed");
    5. otherwise -> sensitive."""
    plist = [p for p in (paths or []) if isinstance(p, str) and p.strip()]
    out: Dict = {"infra": [], "sensitive": [], "secret": [], "scratch": []}
    for p in plist:
        out[path_class(p, cfg, cwd)].append(p)
    limit = mixed_max(cfg) if max_mixed is None else max(0, int(max_mixed))
    if out["secret"]:
        out.update({"class": "sensitive", "reason": "secret path"})
    elif not out["infra"] and not out["sensitive"]:
        infra_cwd = bool(cwd) and is_infra_path(os.path.join(cwd, ".judge-cwd-probe"), cfg, cwd)
        out.update({"class": "infra" if infra_cwd else "sensitive",
                    "reason": ("only scratch paths: " if plist else "no paths: ")
                              + ("cwd is infra" if infra_cwd else "cwd is not infra")})
    elif not out["sensitive"]:
        out.update({"class": "infra", "reason": "all paths infra"})
    elif out["infra"] and len(out["sensitive"]) <= min(limit, len(out["infra"])):
        out.update({"class": "infra", "reason": "mixed"})
    else:
        out.update({"class": "sensitive", "reason": "sensitive path"})
    return out


def classify(paths: Iterable[str], cfg: Optional[Mapping[str, str]] = None, cwd: Optional[str] = None,
             max_mixed: Optional[int] = None) -> str:
    """'infra' | 'sensitive' by classify_detail(): secret paths make it sensitive, scratch paths do not count,
    a few sensitive paths next to infra ones are withheld (JUDGE_MIXED_MAX_SENSITIVE), any other sensitive path
    makes it sensitive. With no deciding path the session's *cwd* decides (infra only if cwd itself is inside an
    infra location, e.g. the deck repo); no paths and no cwd -> 'sensitive'."""
    return classify_detail(paths, cfg, cwd, max_mixed)["class"]


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
    if cmd == "--explain":
        import json
        print(json.dumps(classify_detail(rest, cfg, os.getcwd()), indent=2))
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
