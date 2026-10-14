"""Pull a GGUF model from the Hugging Face Hub into the lab: plan, download, verify, promote, register.

  plan_pull()  -> Plan     resolve the revision to a 40-hex sha, list the files (LFS sha256 required), select the
                           GGUF files (--file patterns are matched against the Hub's list, never used as paths),
                           read the GGUF headers by HTTP Range (bounded gguf.parse_header), capture the licence,
                           extract and flag the chat template, check the quota and free space. Nothing is written.
  execute(plan) -> entry   download with resume into <models>/quarantine/<id>/ (root 0700), sha256 + size check,
                           GGUF magic + bounded header parse + shard count == split.count, promote to
                           <models>/gguf/lab/<id>/ (dirs 0755, files 0444) and append the registry entry
                           (enabled: false). A failure keeps the quarantine copy and says why.

Directories are opened without following symlinks and every file operation is relative to a verified directory
fd, because /models and /models/gguf belong to the operator user while this runs as root.

The HF token (optional, /etc/spark-hf/token) is read by hf.py from its path and sent only as an Authorization
header to Hub hosts; redirects to CDNs never get it, and it is never printed or logged.
"""
from __future__ import annotations

import fnmatch
import hashlib
import http.client
import math
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Callable

import registry as R

try:
    import gguf  # owner: FIT agent (bounded GGUF header parser)
except ImportError:  # pragma: no cover - the lib ships both
    gguf = None
try:
    import hf  # owner: FIT agent (Hub HTTP over urllib)
except ImportError:  # pragma: no cover
    hf = None
try:
    import fit as fitmod  # owner: FIT agent (VRAM / placement estimate); optional for a pull
except ImportError:  # pragma: no cover
    fitmod = None

GB = 1000 ** 3
PATTERN_RE = re.compile(r"^[A-Za-z0-9._*?\[\]/-]{1,200}$")
SHARD_RE = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$")
HEADER_MAX = 256 << 20          # gguf.parse_header cap (tokenizer arrays can be >10 MB)
TEMPLATE_MAX = 32 * 1024        # flag templates larger than this
CHUNK = 4 << 20
USER_AGENT = "spark-model-agent/1 (urllib)"
MAX_REDIRECTS = 10
SLEEP = time.sleep                # tests replace it


class FetchError(Exception):
    """A refused or failed pull. .kept = quarantine dir kept for inspection (or None)."""

    def __init__(self, msg: str, kept: str | None = None):
        super().__init__(msg)
        self.kept = kept


@dataclass
class Settings:
    models_dir: Path
    registry: Path
    orgs: tuple[str, ...]
    quota_gb: float = 1500
    reserve_gb: float = 300
    free_factor: float = 1.05
    token_file: str | None = None
    endpoint: str = "https://huggingface.co"
    timeout: float = 60

    @property
    def quarantine(self) -> Path:
        return Path(self.models_dir) / "quarantine"

    @property
    def lab(self) -> Path:
        return Path(self.models_dir) / "gguf" / "lab"


@dataclass
class Plan:
    id: str
    repo: str
    rev: str
    sha: str
    files: list[dict]
    placement: str
    ctx: int
    klass: str
    klass_source: str
    total_bytes: int
    lab_bytes: int
    quota_bytes: int
    free_bytes: int
    need_free: int
    license: dict
    card: dict
    template: dict
    template_text: str | None
    arch: str | None
    fit: dict
    warnings: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    split: str = "layer"               # both: from the fit (layer | tensor)
    cpu_experts: str | None = None     # both: the fit's -ot regex when it puts experts on the CPU

    @property
    def ok(self) -> bool:
        return not self.refusals

    def summary(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if k not in ("template_text", "card")}
        d["class"] = d.pop("klass")
        d["quota_after_bytes"] = self.lab_bytes + self.total_bytes
        d["ok"] = self.ok
        d["card"] = {k: self.card.get(k) for k in ("license", "license_name", "gated", "base_model", "pipeline_tag")}
        return d


# ------------------------------------------------------------------------------------------------ helpers
def parse_repo_arg(arg: str) -> tuple[str, str]:
    repo, _, rev = arg.partition("@")
    if not R.REPO_RE.match(repo) or ".." in repo:
        raise FetchError(f"repo {repo!r} must be <org>/<name>")
    rev = rev or "main"
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", rev) or ".." in rev:
        raise FetchError(f"revision {rev!r}: a branch, tag or commit sha")
    return repo, rev


def check_org(repo: str, orgs) -> None:
    org = repo.split("/", 1)[0]
    if org not in set(orgs or ()):
        allowed = " ".join(sorted(orgs)) if orgs else "(none: LAB_HF_ORGS is empty)"
        raise FetchError(f"org {org!r} is not in LAB_HF_ORGS ({allowed}); the owner adds orgs in site.env and redeploys")


def role_of(path: str) -> str:
    name = path.rsplit("/", 1)[-1].lower()
    if "mmproj" in name:
        return "mmproj"
    if re.match(r"(mtp|draft)[-_.]", name):
        return "draft"
    return "model"


def select_files(files: list[dict], patterns: list[str]) -> list[dict]:
    """GGUF files to pull. Patterns are fnmatch globs matched against the Hub's file list."""
    for p in patterns:
        if not PATTERN_RE.match(p) or ".." in p.split("/"):
            raise FetchError(f"--file {p!r}: a glob of letters, digits, . _ - / * ? [ ] matched against the repo's files")
    gg = [f for f in files if f["path"].lower().endswith(".gguf")]
    if not gg:
        raise FetchError("the repo has no .gguf files (GGUF only: safetensors, .bin and .pt are never downloaded)")
    if patterns:
        chosen = [f for f in gg if any(fnmatch.fnmatchcase(f["path"], p) for p in patterns)]
        nongguf = [f["path"] for f in files if not f["path"].lower().endswith(".gguf")
                   and any(fnmatch.fnmatchcase(f["path"], p) for p in patterns)]
        if not chosen:
            raise FetchError("no .gguf file matches --file " + " ".join(patterns) + _listing(gg))
        if nongguf:
            raise FetchError(f"--file also matches non-GGUF files ({', '.join(nongguf[:5])}); GGUF only")
    else:
        models = [f for f in gg if role_of(f["path"]) == "model"]
        groups = {SHARD_RE.sub("", f["path"]) for f in models}
        if len(groups) != 1:
            raise FetchError(f"the repo has {len(gg)} GGUF files: pick with --file PATTERN" + _listing(gg))
        chosen = models
    out = []
    for f in sorted(chosen, key=lambda f: f["path"]):
        if not R.FILE_RE.match(f["path"]) or ".." in f["path"].split("/"):
            raise FetchError(f"{f['path']!r}: file path has characters this tool does not accept")
        if not f.get("sha256"):
            raise FetchError(f"{f['path']}: the Hub reports no LFS sha256, so it cannot be verified; refused")
        out.append({"path": f["path"], "bytes": int(f["size"]), "sha256": f["sha256"].lower(), "role": role_of(f["path"])})
    models = [f for f in out if f["role"] == "model"]
    if not models:
        raise FetchError("the selection has no model file (only mmproj/draft)" + _listing(gg))
    if sum(f["role"] == "mmproj" for f in out) > 1 or sum(f["role"] == "draft" for f in out) > 1:
        raise FetchError("at most one mmproj and one draft file per model" + _listing([{"path": f["path"]} for f in out]))
    names = [f["path"].rsplit("/", 1)[-1] for f in out]
    if len(set(names)) != len(names):
        raise FetchError("two selected files have the same name (files are stored flat in lab/<id>/)")
    shards = [SHARD_RE.search(f["path"]) for f in models]
    if len(models) > 1 or any(shards):
        if not all(shards) or len({SHARD_RE.sub("", f["path"]) for f in models}) != 1:
            raise FetchError("more than one model file selected and they are not the shards of one split GGUF"
                             + _listing([{"path": f["path"]} for f in models]))
        n = int(shards[0].group(2))
        idx = sorted(int(m.group(1)) for m in shards)
        if idx != list(range(1, n + 1)) or any(int(m.group(2)) != n for m in shards):
            raise FetchError(f"incomplete shard set: have {idx}, the names say {n} shards; select every shard")
    return out


def _listing(files) -> str:
    lines = [f"\n  {f['path']}" + (f"  ({f['size'] / GB:.2f} GB)" if "size" in f else "") for f in files[:40]]
    more = f"\n  ... and {len(files) - 40} more" if len(files) > 40 else ""
    return "".join(lines) + more


OSI = {"apache-2.0", "mit", "bsd-2-clause", "bsd-3-clause", "bsd", "isc", "unlicense", "cc0-1.0", "mpl-2.0",
       "lgpl-2.1", "lgpl-3.0", "lgpl-lr", "gpl-2.0", "gpl-3.0", "agpl-3.0", "artistic-2.0", "zlib", "ecl-2.0",
       "epl-2.0", "afl-3.0", "osl-3.0", "cc-by-4.0", "cc-by-sa-4.0", "cc-by-3.0", "cc-by-sa-3.0", "odc-by"}


def license_info(card: dict) -> dict:
    """{id, flag, gated}. flag: noncommercial (NC terms), custom (model-specific licences such as llama*/gemma/
    openrail), other (licence "other" or none given), gated (an OSI-style licence behind a gate), else null."""
    lid = card.get("license")
    if isinstance(lid, list):
        lid = lid[0] if lid else None
    lid = lid.strip().lower() if isinstance(lid, str) and lid.strip() else None
    if lid is not None and not re.fullmatch(r"[a-z0-9._+-]{1,64}", lid):
        lid = "other"
    gated = bool(card.get("gated")) and card.get("gated") not in ("false", False)
    if lid and re.search(r"(^|-)nc($|-)|noncommercial|non-commercial|research", lid):
        flag = "noncommercial"
    elif lid in OSI:
        flag = "gated" if gated else None
    elif lid in (None, "other", "unknown"):
        flag = "other"
    else:
        flag = "custom"
    return {"id": lid, "flag": flag, "gated": gated}


_FOR_MESSAGES = re.compile(r"\{%-?\s*for\s+\w+\s+in\s+(?:loop_)?messages\b.*?%\}", re.S)
_TAG = re.compile(r"\{%.*?%\}|\{\{.*?\}\}|\{#.*?#\}", re.S)


def template_flags(t: str | None) -> list[str]:
    """Simple, documented heuristics (walter/models/README.md "Chat template flags"):
      missing        no tokenizer.chat_template in the header
      large          more than 32 KB
      large-range    a range() with a literal bound above 1000 (CPU/DoS by template)
      raise          raise_exception or similar tricks that abort rendering on chosen inputs
      dunder         access to __attributes__ (sandbox-escape attempts)
      system-text    literal text outside the messages loop that looks like an injected instruction
                     ("you are", "always", "never", "ignore", "must") or is longer than 200 characters"""
    if t is None:
        return ["missing"]
    flags = []
    if len(t.encode("utf-8")) > TEMPLATE_MAX:
        flags.append("large")
    if any(int(n) > 1000 for n in re.findall(r"range\(\s*(\d+)", t)):
        flags.append("large-range")
    if re.search(r"raise_exception|\braise\b", t):
        flags.append("raise")
    if "__" in "".join(_TAG.findall(t)):
        flags.append("dunder")
    m = _FOR_MESSAGES.search(t)
    if m:   # outside = before the loop and after the last endfor (good enough for real templates)
        tail = t.rfind("endfor")
        outside = t[:m.start()] + (t[tail:] if tail > m.end() else "")
    else:
        outside = t
    literal = _TAG.sub("", outside)
    strings = re.findall(r"'([^']*)'|\"([^\"]*)\"", " ".join(_TAG.findall(outside)))
    text = literal + " " + " ".join(a or b for a, b in strings)
    if len(literal.strip()) > 200 or re.search(r"\b(you are|always|never|ignore|must)\b", text, re.I):
        flags.append("system-text")
    return flags


def template_info(t: str | None) -> dict:
    if t is None:
        return {"source": "none", "sha256": None, "bytes": 0, "flags": ["missing"], "reviewed": False}
    b = t.encode("utf-8")
    return {"source": "embedded", "sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b),
            "flags": template_flags(t), "reviewed": False}


def class_from_name(repo: str, files: list[dict]) -> str | None:
    s = (repo + " " + " ".join(f["path"] for f in files)).lower()
    if "abliterat" in s:
        return "abliterated"
    if "uncensor" in s:
        return "uncensored"
    return None


def fit_summary(obj) -> dict:
    """The registry fit: block from a fit.estimate() result (dataclass, dict or object)."""
    if obj is None:
        return {}
    if is_dataclass(obj):
        d = asdict(obj)
    elif isinstance(obj, dict):
        d = dict(obj)
    else:
        d = dict(vars(obj))
    out: dict[str, Any] = {}
    v = next((d[k] for k in ("vram_gb", "per_gpu_gb", "gpu_gb") if k in d), None)
    if isinstance(v, (list, tuple)) and len(v) == 2 and all(isinstance(x, (int, float)) for x in v):
        out["vram_gb"] = [round(float(x), 1) for x in v]
    if d.get("speed_class") in R.SPEED_CLASSES:
        out["speed_class"] = d["speed_class"]
    if isinstance(d.get("placement"), str):
        out["placement"] = d["placement"]
    w = d.get("warnings")
    if isinstance(w, (list, tuple)):
        out["warnings"] = [str(x) for x in w][:20]
    return out


def free_bytes(path) -> int:
    st = os.statvfs(str(path))
    return st.f_bavail * st.f_frsize


# ------------------------------------------------------------------------------------------------ the Hub
class Hub:
    """The Hub calls a pull needs (hf.py) plus a streaming, resumable download."""

    def __init__(self, endpoint: str, token_file: str | None = None, timeout: float = 60, hf_fetch=None):
        if hf is None:
            raise FetchError("hf.py is missing from the spark-model lib")
        self.endpoint = endpoint.rstrip("/")
        self.token_file = token_file
        self.timeout = timeout
        self.kw = {"endpoint": self.endpoint, "token_file": token_file}
        if hf_fetch is not None:
            self.kw["fetch"] = hf_fetch

    def resolve_revision(self, repo, rev):
        return hf.resolve_revision(repo, rev, **self.kw)

    def list_files(self, repo, sha):
        return hf.list_files(repo, sha, **self.kw)

    def model_card(self, repo, sha):
        return hf.model_card(repo, sha, **self.kw)

    def range_reader(self, repo, sha, path):
        return hf.range_reader(repo, sha, path, max_bytes=HEADER_MAX, **self.kw)

    # -- download: urllib without automatic redirects, so the token only ever goes to Hub hosts
    def _hub_hosts(self):
        return set(getattr(hf, "HUB_HOSTS", {"huggingface.co", "hf.co"})) | {urllib.parse.urlsplit(self.endpoint).hostname or ""}

    def open(self, repo: str, sha: str, path: str, start: int):
        url = f"{self.endpoint}/{repo}/resolve/{sha}/{urllib.parse.quote(path, safe='/')}"
        token = hf.read_token(self.token_file) if self.token_file else None
        opener = urllib.request.build_opener(_NoRedirect())
        for _ in range(MAX_REDIRECTS + 1):
            hdrs = {"User-Agent": USER_AGENT}
            if start:
                hdrs["Range"] = f"bytes={start}-"
            if token and (urllib.parse.urlsplit(url).hostname or "") in self._hub_hosts():
                hdrs["Authorization"] = f"Bearer {token}"
            req = urllib.request.Request(url, headers=hdrs)
            try:
                resp = opener.open(req, timeout=self.timeout)
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308) and e.headers.get("location"):
                    nxt = urllib.parse.urljoin(url, e.headers["location"])
                    sch = urllib.parse.urlsplit(nxt).scheme
                    if sch not in ("https", "http") or (url.startswith("https:") and sch == "http"):
                        raise FetchError(f"refusing redirect to a {sch} URL") from None
                    url = nxt
                    e.close()
                    continue
                if e.code == 416:
                    e.close()
                    return None, 416
                code = e.headers.get("x-error-code") or ""
                e.close()
                raise FetchError(f"download of {path} failed: HTTP {e.code} {code}".rstrip()) from None
            return resp, resp.status
        raise FetchError(f"too many redirects downloading {path}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# ------------------------------------------------------------------------------------------------ plan
def plan_pull(hub, reg: dict, settings: Settings, repo_arg: str, eid: str, placement: str, *,
              patterns: list[str] | None = None, ctx: int | None = None, klass: str | None = None,
              free: Callable[[], int] | None = None) -> Plan:
    repo, rev = parse_repo_arg(repo_arg)
    if not isinstance(eid, str) or not R.ID_RE.match(eid):
        raise FetchError(f"--id {eid!r} must match {R.ID_RE.pattern}")
    if placement not in R.PLACEMENTS:
        raise FetchError(f"--placement must be one of {'|'.join(R.PLACEMENTS)}")
    if klass is not None and klass not in R.CLASSES:
        raise FetchError(f"--class must be one of {'|'.join(R.CLASSES)}")
    if ctx is not None and not 512 <= ctx <= 1048576:
        raise FetchError("--ctx must be 512..1048576")
    check_org(repo, settings.orgs)
    if R.find(reg, eid):
        raise FetchError(f"id {eid!r} is already in the registry (spark-model rm {eid} first, or pick another --id)")
    try:
        sha = hub.resolve_revision(repo, rev)
        files = select_files(hub.list_files(repo, sha), list(patterns or []))
        card = hub.model_card(repo, sha) or {}
    except FetchError:
        raise
    except Exception as e:  # hf.HFError and transport errors
        raise FetchError(f"Hub: {e}") from None
    if card.get("disabled"):
        raise FetchError("the repo is disabled on the Hub")
    refusals, warnings = [], []
    # headers by HTTP Range (bounded): template, arch, shard count, fit
    headers = []
    for f in [f for f in files if f["role"] == "model"]:
        try:
            headers.append(gguf.parse_header(hub.range_reader(repo, sha, f["path"]), max_bytes=HEADER_MAX))
        except Exception as e:
            raise FetchError(f"{f['path']}: GGUF header check failed before download: {e}") from None
    h0 = headers[0]
    try:
        _check_shards(files, headers)
    except FetchError as e:
        refusals.append(str(e))
    tmpl = h0.kv.get("tokenizer.chat_template")
    tmpl = tmpl if isinstance(tmpl, str) else None
    arch = h0.arch
    if ctx is None:
        cl = h0.kv.get(f"{arch}.context_length") if arch else None
        ctx = min(int(cl), 32768) if isinstance(cl, int) and cl >= 512 else 32768
    src = "--class"
    if klass is None:
        klass = class_from_name(repo, files)
        src = "repo name"
        if klass is None:
            raise FetchError("pass --class uncensored|abliterated|specialized|base (not detectable from the repo name)")
    fitd, split, cpu_experts = {}, "layer", None
    if fitmod is not None:
        try:
            byrole = {r: sum(f["bytes"] for f in files if f["role"] == r) for r in ("mmproj", "draft")}
            raw = fitmod.estimate(headers, ctx, mmproj_bytes=byrole["mmproj"], draft_bytes=byrole["draft"])
            fitd = fit_summary(raw)
            if placement == "both":
                if getattr(raw, "split", None) in R.SPLITS:
                    split = raw.split
                ov = getattr(raw, "cpu_override", None)
                if fitd.get("placement") == "both+cpu-experts" and isinstance(ov, str) and ov.endswith("=CPU") \
                        and R.CPU_EXPERTS_RE.match(ov[:-4]):
                    cpu_experts = ov[:-4]
        except Exception as e:  # the estimate is advisory at pull time
            warnings.append(f"fit estimate failed: {e}")
    else:
        warnings.append("fit.py not installed: no VRAM estimate recorded")
    fp = fitd.get("placement")
    if fp == "none":
        refusals.append("fit: the model does not fit at this context (try a smaller quant or --ctx)")
    elif fp and fp != placement and not (placement == "both" and fp.startswith("both")):
        warnings.append(f"fit suggests placement {fp}, you asked for {placement}")
    for w in fitd.get("warnings", []):
        warnings.append(f"fit: {w}")
    lic = license_info(card)
    tinfo = template_info(tmpl)
    total = sum(f["bytes"] for f in files)
    lab = R.lab_bytes(reg)
    quota = int(settings.quota_gb * GB)
    fb = (free or (lambda: free_bytes(_existing(settings.models_dir))))()
    need = int(math.ceil(total * settings.free_factor + settings.reserve_gb * GB))
    if lab + total > quota:
        refusals.append(f"quota: lab files {lab / GB:.1f} GB + this pull {total / GB:.1f} GB > LAB_MODELS_QUOTA_GB "
                        f"{settings.quota_gb:g} GB (spark-model rm --suggest lists what could go)")
    if fb < need:
        refusals.append(f"free space: {fb / GB:.1f} GB free on {settings.models_dir}, need {need / GB:.1f} GB "
                        f"(pull x {settings.free_factor:g} + {settings.reserve_gb:g} GB reserve)")
    if (settings.lab / eid).exists() or os.path.lexists(settings.lab / eid):
        refusals.append(f"{settings.lab / eid} already exists (an orphan? see spark-model ls)")
    for m in reg["models"]:
        if m["repo"] == repo and m["revision"] == sha and {f["sha256"] for f in m["files"]} & {f["sha256"] for f in files}:
            warnings.append(f"the same files are already registered as {m['id']!r}")
    if tinfo["flags"]:
        warnings.append(f"chat template flagged ({', '.join(tinfo['flags'])}): Phase 2 enable needs spark-model review {eid}")
    if lic["flag"]:
        warnings.append(f"licence {lic['id'] or '(none)'}: flag {lic['flag']}" + (" (gated repo)" if lic["gated"] else ""))
    return Plan(id=eid, repo=repo, rev=rev, sha=sha, files=files, placement=placement, ctx=ctx, klass=klass,
                klass_source=src, total_bytes=total, lab_bytes=lab, quota_bytes=quota, free_bytes=fb, need_free=need,
                license=lic, card=card, template=tinfo, template_text=tmpl, arch=arch, fit=fitd,
                warnings=warnings, refusals=refusals, split=split, cpu_experts=cpu_experts)


def _existing(p) -> Path:
    p = Path(p)
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def _check_shards(files: list[dict], headers: list) -> None:
    models = [f for f in files if f["role"] == "model"]
    infos = [gguf.split_info(h) for h in headers]
    if len(models) == 1:
        si = infos[0]
        if si is not None and si[1] != 1:
            raise FetchError(f"{models[0]['path']}: split.count is {si[1]} but only one file is selected; select every shard")
        return
    counts = {si[1] if si else None for si in infos}
    if counts != {len(models)}:
        raise FetchError(f"shard count mismatch: {len(models)} files selected, split.count says {sorted(map(str, counts))}")
    nos = sorted(si[0] for si in infos)
    if nos != list(range(len(models))):
        raise FetchError(f"split.no values {nos} are not 0..{len(models) - 1}")


def entry_from_plan(p: Plan) -> dict:
    return {
        "id": p.id, "repo": p.repo, "revision": p.sha, "files": [dict(f) for f in p.files],
        "placement": p.placement, "ctx": p.ctx, "n_max": min(8192, p.ctx), "image": "image", "split": p.split,
        "cpu_experts": p.cpu_experts, "sampling": {}, "extra_args": [], "ttl": 900, "class": p.klass,
        "tools_allowed": p.klass not in ("uncensored", "abliterated"),
        "license": dict(p.license), "template": dict(p.template), "fit": dict(p.fit), "enabled": False,
        "pinned": False, "added": R.now(), "last_used": None,
    }


# ------------------------------------------------------------------------------------------------ secure dirs
def _dir_fd(parent_fd: int | None, path_or_name, mode: int, *, create: bool, private: bool) -> int:
    """Open (creating if asked) a directory without following symlinks; it must be ours and not writable by
    others (private: no group/other access at all)."""
    name = str(path_or_name)
    made = False
    if create:
        try:
            os.mkdir(name, mode, dir_fd=parent_fd)
            made = True
        except FileExistsError:
            pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    if made:
        os.fchmod(fd, mode)          # mkdir's mode is filtered by the umask
    st = os.fstat(fd)
    bad = None
    if st.st_uid != os.geteuid():
        bad = f"is owned by uid {st.st_uid}, not {os.geteuid()}"
    elif st.st_mode & 0o022:
        bad = f"is group/other-writable ({stat.filemode(st.st_mode)})"
    elif private and st.st_mode & 0o077:
        if create:
            os.fchmod(fd, 0o700)
        else:
            bad = f"must be mode 0700 ({stat.filemode(st.st_mode)})"
    if bad:
        os.close(fd)
        raise FetchError(f"refusing to use {name}: it {bad}")
    return fd


def open_lab_dirs(settings: Settings, create: bool = True) -> tuple[int, int, int]:
    """(models_fd, quarantine_fd, lab_fd)."""
    mfd = os.open(str(settings.models_dir), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        qfd = _dir_fd(mfd, "quarantine", 0o700, create=create, private=True)
        gfd = os.open("gguf", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=mfd)
        try:
            lfd = _dir_fd(gfd, "lab", 0o755, create=create, private=False)
            if create:
                os.fchmod(lfd, 0o755)
        finally:
            os.close(gfd)
    except BaseException:
        os.close(mfd)
        raise
    return mfd, qfd, lfd


# ------------------------------------------------------------------------------------------------ execute
def _progress(out, name: str, done: int, size: int, t0: float, start: int):
    rate = (done - start) / max(time.monotonic() - t0, 1e-6)
    out.write(f"  {name}: {done / GB:.2f} / {size / GB:.2f} GB ({100 * done / max(size, 1):.0f}%) {rate / 1e6:.0f} MB/s\n")
    out.flush()


def download_file(hub, repo: str, sha: str, f: dict, dfd: int, *, out=sys.stderr, retries: int = 5) -> None:
    """Download one file into the directory dfd as <name>.part (resuming with HTTP Range), verify size and
    sha256, then rename to <name> (0444). An already complete <name> is re-verified, not re-downloaded."""
    name = f["path"].rsplit("/", 1)[-1]
    part = name + ".part"
    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        if stat.S_ISREG(st.st_mode) and st.st_size == f["bytes"] and _sha_of(dfd, name) == f["sha256"]:
            out.write(f"  {name}: already downloaded, sha256 ok\n")
            return
        os.unlink(name, dir_fd=dfd)
    except FileNotFoundError:
        pass
    attempt = 0
    while True:
        fd = os.open(part, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise FetchError(f"{part} is not a regular file")
            have = os.fstat(fd).st_size
            if have > f["bytes"]:
                os.ftruncate(fd, 0)
                have = 0
            h = hashlib.sha256()
            os.lseek(fd, 0, os.SEEK_SET)
            left = have
            while left:
                b = os.read(fd, min(CHUNK, left))
                if not b:
                    break
                h.update(b)
                left -= len(b)
            if have < f["bytes"]:
                resp, status = hub.open(repo, sha, f["path"], have)
                if status == 416:          # the server has nothing past `have`: start over
                    os.ftruncate(fd, 0)
                    raise OSError("server answered 416 (range not satisfiable); restarting the file")
                with resp:
                    if have and status != 206:
                        out.write(f"  {name}: server ignored Range, restarting from 0\n")
                        os.ftruncate(fd, 0)
                        have = 0
                        h = hashlib.sha256()
                    if status == 206:
                        m = re.match(r"bytes (\d+)-", resp.headers.get("content-range", ""))
                        if not m or int(m.group(1)) != have:
                            raise FetchError(f"{name}: bad Content-Range {resp.headers.get('content-range')!r}")
                    os.lseek(fd, have, os.SEEK_SET)
                    done = start = have
                    t0 = last = time.monotonic()
                    while True:
                        b = resp.read(CHUNK)
                        if not b:
                            break
                        if done + len(b) > f["bytes"]:
                            raise FetchError(f"{name}: server sent more than the {f['bytes']} bytes the Hub lists")
                        os.write(fd, b)
                        h.update(b)
                        done += len(b)
                        if time.monotonic() - last > 30:
                            _progress(out, name, done, f["bytes"], t0, start)
                            last = time.monotonic()
                    have = done
            os.fsync(fd)
        except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException) as e:
            os.close(fd)
            attempt += 1
            if attempt > retries:
                raise FetchError(f"{name}: download failed after {retries} retries: {e}") from None
            out.write(f"  {name}: {e}; retry {attempt}/{retries} (resumes)\n")
            SLEEP(min(2 ** attempt, 30))
            continue
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)
        if have != f["bytes"]:
            attempt += 1
            if attempt > retries:
                raise FetchError(f"{name}: got {have} bytes, the Hub lists {f['bytes']}")
            out.write(f"  {name}: short read ({have} of {f['bytes']}); retry {attempt}/{retries}\n")
            continue
        digest = h.hexdigest()
        if digest != f["sha256"]:
            os.rename(part, name + ".bad", src_dir_fd=dfd, dst_dir_fd=dfd)
            raise FetchError(f"{name}: sha256 mismatch: got {digest}, the Hub's LFS sha256 is {f['sha256']} "
                             f"(kept as {name}.bad)")
        _fchmod_at(dfd, part, 0o444)
        os.rename(part, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        out.write(f"  {name}: {f['bytes'] / GB:.2f} GB, sha256 ok\n")
        return


def _fchmod_at(dfd, name, mode):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
    try:
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _sha_of(dfd: int, name: str) -> str:
    h = hashlib.sha256()
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
    try:
        while True:
            b = os.read(fd, CHUNK)
            if not b:
                break
            h.update(b)
    finally:
        os.close(fd)
    return h.hexdigest()


def verify_local(files: list[dict], dfd: int) -> list:
    """GGUF magic, bounded header parse (and that the file is long enough for its tensors) of every file;
    shard count == split.count for the model files. Returns the model headers."""
    headers = []
    for f in files:
        name = f["path"].rsplit("/", 1)[-1]
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
        try:
            size = os.fstat(fd).st_size

            def read(off, n, fd=fd):
                return os.pread(fd, n, off)
            if read(0, 4) != b"GGUF":
                raise FetchError(f"{name}: not a GGUF file (bad magic)")
            try:
                h = gguf.parse_header(read, max_bytes=HEADER_MAX)
            except Exception as e:
                raise FetchError(f"{name}: GGUF header rejected: {e}") from None
            last = max((t.offset + t.nbytes for t in h.tensors), default=0)
            if h.header_bytes + last > size:
                raise FetchError(f"{name}: truncated: tensors need {h.header_bytes + last} bytes, file has {size}")
        finally:
            os.close(fd)
        if f["role"] == "model":
            headers.append(h)
    _check_shards(files, headers)
    return headers


def execute(plan: Plan, hub, settings: Settings, *, out=sys.stderr, save_entry: Callable[[dict], None]) -> dict:
    """Download, verify, promote, register. save_entry(entry) appends to the registry (the caller holds the lock)."""
    if not plan.ok:
        raise FetchError("plan refused: " + "; ".join(plan.refusals))
    try:
        mfd, qfd, lfd = open_lab_dirs(settings, create=True)
    except OSError as e:     # ELOOP: a symlink where quarantine/ or gguf/lab/ should be
        raise FetchError(f"refusing the lab directories under {settings.models_dir}: {e}") from None
    kept = str(settings.quarantine / plan.id)
    try:
        dfd = _dir_fd(qfd, plan.id, 0o700, create=True, private=True)
        try:
            out.write(f"pull {plan.repo}@{plan.sha[:12]} -> quarantine/{plan.id}/ ({plan.total_bytes / GB:.2f} GB)\n")
            for f in plan.files:
                download_file(hub, plan.repo, plan.sha, f, dfd, out=out)
            verify_local(plan.files, dfd)
            out.write("  GGUF headers ok, shard count ok\n")
            # promote: files into lab/.<id>.incoming, then rename the directory into place
            tmpname = f".{plan.id}.incoming"
            try:
                os.stat(plan.id, dir_fd=lfd, follow_symlinks=False)
                raise FetchError(f"lab/{plan.id} already exists; nothing promoted")
            except FileNotFoundError:
                pass
            ifd = _dir_fd(lfd, tmpname, 0o755, create=True, private=False)
            try:
                for f in plan.files:
                    name = f["path"].rsplit("/", 1)[-1]
                    _fchmod_at(dfd, name, 0o444)
                    os.rename(name, name, src_dir_fd=dfd, dst_dir_fd=ifd)
                os.fchmod(ifd, 0o755)
                os.fsync(ifd)
            finally:
                os.close(ifd)
            os.rename(tmpname, plan.id, src_dir_fd=lfd, dst_dir_fd=lfd)
            os.fsync(lfd)
        finally:
            os.close(dfd)
        try:
            os.rmdir(plan.id, dir_fd=qfd)
        except OSError:
            pass
        entry = entry_from_plan(plan)
        save_entry(entry)
        out.write(f"  promoted to {settings.lab / plan.id}/ and registered as {plan.id} (enabled: false)\n")
        return entry
    except FetchError as e:
        e.kept = e.kept or kept
        raise
    except OSError as e:
        raise FetchError(f"pull failed: {e}", kept) from None
    finally:
        for fd in (lfd, qfd, mfd):
            os.close(fd)


# ------------------------------------------------------------------------------------------------ removal
def check_lab_dir(settings: Settings) -> None:
    """Raise FetchError unless <models>/gguf/lab is a usable directory (ours, not a symlink, not writable by others)."""
    try:
        mfd = os.open(str(settings.models_dir), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            gfd = os.open("gguf", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=mfd)
            try:
                os.close(_dir_fd(gfd, "lab", 0o755, create=False, private=False))
            except FileNotFoundError:
                pass
            finally:
                os.close(gfd)
        finally:
            os.close(mfd)
    except OSError as e:
        raise FetchError(f"{settings.lab}: {e}") from None


def remove_lab_dir(settings: Settings, eid: str) -> int:
    """Delete <models>/gguf/lab/<eid>/ (flat directory of regular files). Returns bytes freed. Never follows
    symlinks; refuses subdirectories. Only ever touches lab/<eid>."""
    if not R.ID_RE.match(eid):
        raise FetchError(f"bad id {eid!r}")
    mfd = os.open(str(settings.models_dir), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        gfd = os.open("gguf", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=mfd)
        try:
            try:
                lfd = _dir_fd(gfd, "lab", 0o755, create=False, private=False)
            except FileNotFoundError:
                return 0
        finally:
            os.close(gfd)
        try:
            try:
                dfd = os.open(eid, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=lfd)
            except FileNotFoundError:
                return 0
            freed = 0
            try:
                for name in os.listdir(dfd):
                    st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                    if stat.S_ISDIR(st.st_mode):
                        raise FetchError(f"lab/{eid}/{name} is a directory; not removing (inspect by hand)")
                for name in os.listdir(dfd):
                    st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                    freed += st.st_size if stat.S_ISREG(st.st_mode) else 0
                    os.unlink(name, dir_fd=dfd)
            finally:
                os.close(dfd)
            os.rmdir(eid, dir_fd=lfd)
            return freed
        finally:
            os.close(lfd)
    finally:
        os.close(mfd)
