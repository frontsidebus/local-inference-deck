"""Lab model registry (/models/registry.yaml) and the llama-swap config renderer.

The registry is site-local and is written only by spark-model-agent (root). This module:
  - loads and validates it strictly (unknown keys, bad ids, bad hashes, non-whitelisted flags are errors, and
    every problem is reported at once with its location);
  - saves it atomically (temp file in the same directory, fsync, rename);
  - renders the WHOLE llama-swap config: the base config (the repo template rendered by deploy.sh, installed as
    /etc/llama-swap/config.base.yaml) plus one entry per ENABLED lab model and the matching matrix vars/sets.

Rendering is text-level: the base is kept byte for byte and lab text is only inserted (after the last core
model, after the last matrix var, after the last matrix set). With no enabled lab model the output IS the base.
The result is re-parsed and checked (check_config) before anyone installs it.

Python 3.10+, stdlib + PyYAML.
"""
from __future__ import annotations

import copy
import datetime as _dt
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

SCHEMA = "spark-model-registry-v1"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,38}[a-z0-9]$")
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
SHA64_RE = re.compile(r"^[0-9a-f]{64}$")
# repo-relative path of a GGUF file: plain segments, no "..", no leading "/", ends in .gguf
FILE_RE = re.compile(r"^(?:[A-Za-z0-9_][A-Za-z0-9._+-]{0,127}/){0,4}[A-Za-z0-9_][A-Za-z0-9._+-]{0,159}\.gguf$")
# -ot regex for MoE experts on the CPU: regex characters only (no quotes, $, spaces, commas or "=")
CPU_EXPERTS_RE = re.compile(r"^[A-Za-z0-9_.\\|()\[\]*+?^-]{1,200}$")

PLACEMENTS = ("gpu0", "gpu1", "both")
CLASSES = ("uncensored", "abliterated", "specialized", "base")
ROLES = ("model", "mmproj", "draft")
IMAGES = ("image", "image-next")
SPLITS = ("layer", "tensor")
LICENSE_FLAGS = ("noncommercial", "other", "custom", "gated")
TEMPLATE_SOURCES = ("embedded", "none", "file")
SPEED_CLASSES = ("A", "B", "C", "D")

LAB_PREFIX = "lab-"                 # llama-swap model id = lab-<id>
CONTAINER_LAB_DIR = "/models/gguf/lab"   # where the model containers see lab files (MODELS_DIR/gguf is mounted there)
CORE_GPU0_VAR = "c"                 # base matrix var of the GPU0 core model (coder)
CORE_GPU1_VAR = "f"                 # base matrix var of the GPU1 core model (coder-fast)
PAIR_SET = "lab-gpu-pairs"          # generated set: (GPU0 group) & (GPU1 group)
VAR_KEY_RE = re.compile(r"^[a-zA-Z0-9.-]{1,32}$")   # llama-swap v260 internal/config/matrix.go:11

# sampling key -> (llama-server flag, type, min, max)
SAMPLING = {
    "temp": ("--temp", float, 0.0, 5.0),
    "top_p": ("--top-p", float, 0.0, 1.0),
    "top_k": ("--top-k", int, 0, 1000),
    "min_p": ("--min-p", float, 0.0, 1.0),
    "typical_p": ("--typical", float, 0.0, 1.0),
    "presence_penalty": ("--presence-penalty", float, -2.0, 2.0),
    "frequency_penalty": ("--frequency-penalty", float, -2.0, 2.0),
    "repeat_penalty": ("--repeat-penalty", float, 0.0, 5.0),
}


def _int_range(lo, hi):
    def ok(v, _e):
        return re.fullmatch(r"-?\d{1,7}", v) is not None and lo <= int(v) <= hi
    ok.desc = f"an integer {lo}..{hi}"
    return ok


def _choice(*vals):
    def ok(v, _e):
        return v in vals
    ok.desc = "one of " + "|".join(vals)
    return ok


def _template_file(v, e):
    return re.fullmatch(rf"{re.escape(CONTAINER_LAB_DIR)}/{re.escape(str(e.get('id', '')))}/[A-Za-z0-9._-]{{1,100}}\.jinja", v) is not None


_template_file.desc = f"{CONTAINER_LAB_DIR}/<id>/<name>.jinja"

# extra_args whitelist: flag -> value check (None = flag without a value). Everything else is refused.
EXTRA_ARGS = {
    "--ctx-checkpoints": _int_range(0, 64),
    "--cache-reuse": _int_range(0, 4096),
    "--spec-type": _choice("draft-mtp"),
    "--spec-draft-n-max": _int_range(1, 16),
    "-devd": _choice("CUDA0", "CUDA1"),
    "-ngld": _int_range(0, 999),
    "--reasoning-format": _choice("none", "auto", "deepseek", "deepseek-legacy"),
    "--reasoning-budget": _int_range(-1, 1000000),
    "--chat-template-file": _template_file,
    "--swa-full": None,
}
TOKEN_RE = re.compile(r"^[A-Za-z0-9_./:,+=-]{1,200}$")

ENTRY_KEYS = {
    # key: (required, default), in the order the registry is written
    "id": (True, None), "repo": (True, None), "revision": (True, None), "files": (True, None),
    "placement": (True, None), "ctx": (True, None), "n_max": (False, None), "image": (False, "image"),
    "split": (False, "layer"), "cpu_experts": (False, None), "sampling": (False, {}), "extra_args": (False, []),
    "ttl": (False, 900), "class": (True, None), "tools_allowed": (False, False), "license": (False, {}),
    "template": (False, {}), "fit": (False, {}), "enabled": (False, False), "pinned": (False, False),
    "added": (True, None), "last_used": (False, None),
}
FILE_KEYS = ("path", "bytes", "sha256", "role")
LICENSE_KEYS = ("id", "flag", "gated")
TEMPLATE_KEYS = ("source", "sha256", "bytes", "flags", "reviewed")
FIT_KEYS = ("vram_gb", "speed_class", "placement", "warnings")


class RegistryError(ValueError):
    """Invalid registry or base config. .problems lists every finding."""

    def __init__(self, problems, source="registry"):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        self.source = source
        super().__init__(f"{source}: " + ("; ".join(self.problems) if len(self.problems) < 2 else
                                          f"{len(self.problems)} problems:\n  - " + "\n  - ".join(self.problems)))


# ---------------------------------------------------------------------------------------- registry I/O
def empty() -> dict:
    return {"schema": SCHEMA, "models": []}


def _ts(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, _dt.datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=_dt.timezone.utc)
        return v.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(v, _dt.date):
        return v.strftime("%Y-%m-%dT00:00:00Z")
    if isinstance(v, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z", v):
        return v
    raise ValueError(f"{v!r} is not a UTC timestamp like 2026-10-14T00:00:00Z")


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v) -> bool:
    return (isinstance(v, (int, float))) and not isinstance(v, bool)


def _check_keys(d, allowed, where, errs) -> bool:
    if not isinstance(d, dict):
        errs.append(f"{where}: must be a mapping, got {type(d).__name__}")
        return False
    for k in d:
        if k not in allowed:
            errs.append(f"{where}: unknown key {k!r} (allowed: {', '.join(allowed)})")
    return True


def validate_entry(raw: Any, where: str = "entry") -> tuple[dict, list[str]]:
    """Normalize one registry model entry. Returns (entry with defaults, problems)."""
    errs: list[str] = []
    if not _check_keys(raw, tuple(ENTRY_KEYS), where, errs):
        return {}, errs
    e = {}
    for k, (req, default) in ENTRY_KEYS.items():
        if k in raw:
            e[k] = copy.deepcopy(raw[k])
        elif req:
            errs.append(f"{where}: missing required key {k!r}")
        else:
            e[k] = copy.deepcopy(default)
    eid = e.get("id")
    if eid is not None:
        if not isinstance(eid, str) or not ID_RE.match(eid):
            errs.append(f"{where}: id {eid!r} must match {ID_RE.pattern} (lowercase letters, digits, '.', '-'; 2-40 chars)")
        else:
            where = f"{where} (id={eid})"
    if "repo" in e and (not isinstance(e["repo"], str) or not REPO_RE.match(e["repo"])):
        errs.append(f"{where}: repo {e['repo']!r} must be <org>/<name>")
    if "revision" in e and (not isinstance(e["revision"], str) or not SHA40_RE.match(e["revision"])):
        errs.append(f"{where}: revision must be a 40-hex commit sha (resolved at pull), got {e['revision']!r}")
    if "placement" in e and e["placement"] not in PLACEMENTS:
        errs.append(f"{where}: placement {e['placement']!r} must be one of {'|'.join(PLACEMENTS)}")
    if "ctx" in e and not (_is_int(e["ctx"]) and 512 <= e["ctx"] <= 1048576):
        errs.append(f"{where}: ctx must be an integer 512..1048576, got {e['ctx']!r}")
    if "class" in e and e["class"] not in CLASSES:
        errs.append(f"{where}: class {e['class']!r} must be one of {'|'.join(CLASSES)}")
    for k in ("added", "last_used"):
        if k in e and e[k] is not None:
            try:
                e[k] = _ts(e[k])
            except ValueError as x:
                errs.append(f"{where}: {k}: {x}")
    if e.get("added") is None and "added" in e:
        errs.append(f"{where}: added must be a timestamp")
    # files
    files = e.get("files")
    if "files" in e:
        if not isinstance(files, list) or not files:
            errs.append(f"{where}: files must be a non-empty list")
        else:
            names, roles = {}, {"model": 0, "mmproj": 0, "draft": 0}
            for i, f in enumerate(files):
                fw = f"{where}: files[{i}]"
                if not _check_keys(f, FILE_KEYS, fw, errs):
                    continue
                for k in FILE_KEYS:
                    if k not in f:
                        errs.append(f"{fw}: missing {k!r}")
                p = f.get("path")
                if p is not None:
                    if not isinstance(p, str) or not FILE_RE.match(p) or ".." in p.split("/"):
                        errs.append(f"{fw}: path {p!r} must be a repo-relative *.gguf path (plain segments, no '..')")
                    else:
                        base = p.rsplit("/", 1)[-1]
                        if base in names:
                            errs.append(f"{fw}: file name {base!r} also used by files[{names[base]}] (files are stored flat in lab/<id>/)")
                        names[base] = i
                if "bytes" in f and not (_is_int(f["bytes"]) and f["bytes"] > 0):
                    errs.append(f"{fw}: bytes must be a positive integer")
                if "sha256" in f and not (isinstance(f["sha256"], str) and SHA64_RE.match(f["sha256"])):
                    errs.append(f"{fw}: sha256 must be 64 lowercase hex characters")
                if "role" in f:
                    if f["role"] not in ROLES:
                        errs.append(f"{fw}: role {f['role']!r} must be one of {'|'.join(ROLES)}")
                    else:
                        roles[f["role"]] += 1
            if not roles["model"]:
                errs.append(f"{where}: files has no role: model entry")
            for r in ("mmproj", "draft"):
                if roles[r] > 1:
                    errs.append(f"{where}: at most one {r} file")
    # optional scalars
    if e.get("n_max") is None and _is_int(e.get("ctx")):
        e["n_max"] = min(8192, e["ctx"])
    elif "n_max" in e and not (_is_int(e["n_max"]) and 1 <= e["n_max"] <= (e["ctx"] if _is_int(e.get("ctx")) else 1048576)):
        errs.append(f"{where}: n_max must be an integer 1..ctx, got {e['n_max']!r}")
    if e.get("image") not in IMAGES:
        errs.append(f"{where}: image must be one of {'|'.join(IMAGES)} (llama-swap macros), got {e.get('image')!r}")
    if e.get("split") not in SPLITS:
        errs.append(f"{where}: split must be one of {'|'.join(SPLITS)}, got {e.get('split')!r}")
    ce = e.get("cpu_experts")
    if ce is not None:
        if not isinstance(ce, str) or not CPU_EXPERTS_RE.match(ce):
            errs.append(f"{where}: cpu_experts must be a tensor-name regex (letters, digits, . _ \\ | ( ) [ ] * + ? ^ -)")
        elif e.get("placement") != "both":
            errs.append(f"{where}: cpu_experts needs placement: both")
    if not (_is_int(e.get("ttl")) and 60 <= e["ttl"] <= 86400):
        errs.append(f"{where}: ttl must be an integer 60..86400 seconds, got {e.get('ttl')!r}")
    for k in ("tools_allowed", "enabled", "pinned"):
        if not isinstance(e.get(k), bool):
            errs.append(f"{where}: {k} must be true or false, got {e.get(k)!r}")
    # sampling
    s = e.get("sampling")
    if s is None:
        e["sampling"] = s = {}
    if _check_keys(s, tuple(SAMPLING), f"{where}: sampling", errs):
        for k, v in s.items():
            if k not in SAMPLING:
                continue
            _flag, typ, lo, hi = SAMPLING[k]
            good = _is_int(v) if typ is int else _is_num(v)
            if not good or not lo <= v <= hi:
                errs.append(f"{where}: sampling.{k} must be {'an integer' if typ is int else 'a number'} {lo}..{hi}, got {v!r}")
    # extra_args
    xa = e.get("extra_args")
    if xa is None:
        e["extra_args"] = xa = []
    if not isinstance(xa, list) or not all(isinstance(t, str) for t in xa):
        errs.append(f"{where}: extra_args must be a list of strings")
    else:
        i = 0
        seen = set()
        while i < len(xa):
            t = xa[i]
            if t not in EXTRA_ARGS:
                errs.append(f"{where}: extra_args: {t!r} is not whitelisted (allowed: {', '.join(EXTRA_ARGS)})")
                i += 1
                continue
            if t in seen:
                errs.append(f"{where}: extra_args: {t} given twice")
            seen.add(t)
            check = EXTRA_ARGS[t]
            if check is None:
                i += 1
                continue
            if i + 1 >= len(xa):
                errs.append(f"{where}: extra_args: {t} needs a value ({check.desc})")
                break
            v = xa[i + 1]
            if not TOKEN_RE.match(v) or not check(v, e):
                errs.append(f"{where}: extra_args: {t} {v!r}: value must be {check.desc}")
            i += 2
    # license
    lic = e.get("license") or {}
    e["license"] = lic
    if _check_keys(lic, LICENSE_KEYS, f"{where}: license", errs):
        lic.setdefault("id", None)
        lic.setdefault("flag", None)
        if lic["id"] is not None and not (isinstance(lic["id"], str) and re.fullmatch(r"[A-Za-z0-9._+-]{1,64}", lic["id"])):
            errs.append(f"{where}: license.id must be a short licence identifier, got {lic['id']!r}")
        if lic["flag"] is not None and lic["flag"] not in LICENSE_FLAGS:
            errs.append(f"{where}: license.flag must be null or one of {'|'.join(LICENSE_FLAGS)}")
        if "gated" in lic and not isinstance(lic["gated"], bool):
            errs.append(f"{where}: license.gated must be true or false")
    # template
    tp = e.get("template") or {}
    e["template"] = tp
    if _check_keys(tp, TEMPLATE_KEYS, f"{where}: template", errs):
        tp.setdefault("source", "embedded")
        tp.setdefault("flags", [])
        tp.setdefault("reviewed", False)
        if tp["source"] not in TEMPLATE_SOURCES:
            errs.append(f"{where}: template.source must be one of {'|'.join(TEMPLATE_SOURCES)}")
        if tp.get("sha256") is not None and not (isinstance(tp["sha256"], str) and SHA64_RE.match(tp["sha256"])):
            errs.append(f"{where}: template.sha256 must be 64 hex characters")
        if tp.get("bytes") is not None and not (_is_int(tp["bytes"]) and tp["bytes"] >= 0):
            errs.append(f"{where}: template.bytes must be a non-negative integer")
        if not isinstance(tp["flags"], list) or not all(isinstance(x, str) and re.fullmatch(r"[a-z0-9_-]{1,40}", x) for x in tp["flags"]):
            errs.append(f"{where}: template.flags must be a list of short flag names")
        if not isinstance(tp["reviewed"], bool):
            errs.append(f"{where}: template.reviewed must be true or false")
    # fit
    ft = e.get("fit") or {}
    e["fit"] = ft
    if _check_keys(ft, FIT_KEYS, f"{where}: fit", errs):
        vg = ft.get("vram_gb")
        if vg is not None and not (isinstance(vg, list) and len(vg) == 2 and all(_is_num(x) and 0 <= x < 1000 for x in vg)):
            errs.append(f"{where}: fit.vram_gb must be [gpu0_gb, gpu1_gb]")
        if ft.get("speed_class") is not None and ft["speed_class"] not in SPEED_CLASSES:
            errs.append(f"{where}: fit.speed_class must be one of {'|'.join(SPEED_CLASSES)}")
        if ft.get("placement") is not None and not isinstance(ft["placement"], str):
            errs.append(f"{where}: fit.placement must be a string")
        if ft.get("warnings") is not None and not (isinstance(ft["warnings"], list) and all(isinstance(x, str) for x in ft["warnings"])):
            errs.append(f"{where}: fit.warnings must be a list of strings")
    # Phase 2 enable refuses flagged + unreviewed templates; the renderer enforces the same for hand edits.
    if e.get("enabled") is True and isinstance(tp, dict) and tp.get("flags") and tp.get("reviewed") is not True:
        errs.append(f"{where}: enabled with a flagged chat template ({', '.join(tp['flags'])}) that is not reviewed: run spark-model review {eid}")
    return e, errs


def validate(raw: Any, source: str = "registry") -> dict:
    """Validate a parsed registry; return a normalized copy (defaults filled). Raises RegistryError."""
    if raw is None:
        return empty()
    errs: list[str] = []
    if not isinstance(raw, dict):
        raise RegistryError(f"top level must be a mapping with schema: {SCHEMA} and models: [...]", source)
    for k in raw:
        if k not in ("schema", "models"):
            errs.append(f"unknown top-level key {k!r} (allowed: schema, models)")
    if raw.get("schema") != SCHEMA:
        errs.append(f"schema must be {SCHEMA!r}, got {raw.get('schema')!r}")
    models = raw.get("models")
    if models is None:
        models = []
    if not isinstance(models, list):
        errs.append("models must be a list")
        models = []
    out, seen = [], {}
    for i, m in enumerate(models):
        e, problems = validate_entry(m, f"models[{i}]")
        errs += problems
        eid = e.get("id")
        if isinstance(eid, str):
            if eid in seen:
                errs.append(f"models[{i}]: duplicate id {eid!r} (also models[{seen[eid]}])")
            seen[eid] = i
        out.append(e)
    if errs:
        raise RegistryError(errs, source)
    return {"schema": SCHEMA, "models": out}


def loads(text: str, source: str = "registry") -> dict:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as x:
        raise RegistryError(f"not valid YAML: {x}", source) from None
    return validate(raw, source)


def load(path) -> dict:
    """The registry at path; a missing file is an empty registry."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return empty()
    return loads(text, str(p))


HEADER = ("# spark-model lab registry (spark-model-registry-v1). Written by spark-model-agent; do not edit while a\n"
          "# pull is running. Schema and runbook: walter/models/README.md in the repo.\n")


def dumps(reg: dict) -> str:
    reg = validate(reg)
    return HEADER + yaml.safe_dump(reg, sort_keys=False, default_flow_style=None, allow_unicode=False, width=120)


def atomic_write(path, text: str, mode: int = 0o644, keep_prev: bool = False) -> bool:
    """Write text to path atomically (temp + fsync + rename). Returns False (and does not touch the file, so its
    mtime stays) when the content is already identical. keep_prev: copy the old content to <path>.prev first."""
    p = Path(path)
    data = text.encode("utf-8")
    try:
        old = p.read_bytes()
    except FileNotFoundError:
        old = None
    if old == data:
        return False
    st = p.stat() if old is not None else None
    if keep_prev and old is not None:
        _write_tmp_rename(p.with_name(p.name + ".prev"), old, st.st_mode & 0o777, st)
    _write_tmp_rename(p, data, (st.st_mode & 0o777) if st else mode, st)
    return True


def _write_tmp_rename(p: Path, data: bytes, mode: int, st=None) -> None:
    fd, tmp = tempfile.mkstemp(prefix="." + p.name + ".", dir=str(p.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        if st is not None and os.geteuid() == 0:
            os.chown(tmp, st.st_uid, st.st_gid)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    dfd = os.open(str(p.parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def save(path, reg: dict) -> bool:
    return atomic_write(path, dumps(reg), 0o644)


def find(reg: dict, eid: str) -> dict | None:
    return next((m for m in reg["models"] if m["id"] == eid), None)


def lab_bytes(reg: dict) -> int:
    return sum(f["bytes"] for m in reg["models"] for f in m["files"])


def model_id(e: dict) -> str:
    return LAB_PREFIX + e["id"]


def _flat(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def main_file(e: dict) -> dict:
    """The file passed to -m: the first shard of the model files (llama.cpp loads the others from its directory)."""
    return sorted((f for f in e["files"] if f["role"] == "model"), key=lambda f: _flat(f["path"]))[0]


# ---------------------------------------------------------------------------------------- base config text
_KEY_LINE = re.compile(r'^(?P<ind> *)(?P<key>"[^"]*"|\'[^\']*\'|[A-Za-z0-9_.-]+):(?:\s|$)')


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_content(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith("#")


def _unq(k: str) -> str:
    return k[1:-1] if k[:1] in "\"'" else k


def _block(lines: list[str], path: tuple[str, ...]) -> tuple[int, int, int]:
    """Locate a block mapping by key path in YAML text without parsing it (block style only).
    Returns (header_line, last_child_line, child_indent): last_child_line is the last non-blank line (content or
    comment) indented deeper than the header before the block ends."""
    lo, hi, child_ind = 0, len(lines), 0      # search range and the indentation of its direct children
    hdr = -1
    for depth, key in enumerate(path):
        hdr = -1
        for i in range(lo, hi):
            ln = lines[i]
            if not _is_content(ln) or _indent(ln) != child_ind:
                continue
            m = _KEY_LINE.match(ln)
            if m and _unq(m.group("key")) == key:
                hdr = i
                break
        where = ".".join(path[:depth + 1])
        if hdr < 0:
            raise RegistryError(f"no block {where}", "base config")
        rest = lines[hdr][_KEY_LINE.match(lines[hdr]).end():].split("#", 1)[0].strip()
        if rest:
            raise RegistryError(f"{where} must be a block mapping (found {rest!r} after the key)", "base config")
        ind = _indent(lines[hdr])
        lo, hi = hdr + 1, len(lines)
        for j in range(lo, len(lines)):
            if _is_content(lines[j]) and _indent(lines[j]) <= ind:
                hi = j
                break
        child_ind = next((_indent(lines[j]) for j in range(lo, hi) if _is_content(lines[j])), None)
        if child_ind is None:
            raise RegistryError(f"{where} is empty", "base config")
    last = max(j for j in range(lo, hi) if lines[j].strip() and _indent(lines[j]) > _indent(lines[hdr]))
    return hdr, last, child_ind


MATRIX_PATH = ("routing", "router", "settings", "matrix")


def _parse_base(base_text: str) -> dict:
    try:
        cfg = yaml.safe_load(base_text)
    except yaml.YAMLError as x:
        raise RegistryError(f"not valid YAML: {x}", "base config") from None
    if not isinstance(cfg, dict) or not isinstance(cfg.get("models"), dict):
        raise RegistryError("no models: mapping", "base config")
    return cfg


def matrix_of(cfg: dict) -> dict | None:
    m = cfg
    for k in MATRIX_PATH:
        m = m.get(k) if isinstance(m, dict) else None
    return m if isinstance(m, dict) else None


# ---------------------------------------------------------------------------------------- render
def _num(v) -> str:
    return str(v) if _is_int(v) else format(float(v), "g")


def lab_entry_lines(e: dict, ind: int) -> list[str]:
    """llama-swap model entry text for one lab model; ind = indentation of model keys under models:."""
    a, b, c = " " * ind, " " * (ind + 2), " " * (ind + 4)
    mid = model_id(e)
    d = f"{CONTAINER_LAB_DIR}/{e['id']}"
    pl = e["placement"]
    mem = "${mem-moe}" if e.get("cpu_experts") else "${mem}"
    tp = "${tp} " if pl == "both" and e["split"] == "tensor" else ""
    dev = {"gpu0": "0", "gpu1": "1", "both": "0,1"}[pl]
    if pl == "both":
        sm = "-sm tensor" if e["split"] == "tensor" else "-sm layer -ts 1,1"
    else:
        sm = "-sm none"
    roles = {f["role"]: f for f in e["files"]}
    cmd = [f'${{run}} {mem} {tp}--gpus "\\"device={dev}\\"" ${{{e["image"]}}}',
           f"-m {d}/{_flat(main_file(e)['path'])}"]
    if "mmproj" in roles:
        cmd.append(f"--mmproj {d}/{_flat(roles['mmproj']['path'])}")
    cmd.append(f"${{common}} -c {e['ctx']} -n {e['n_max']} {sm}")
    if e.get("cpu_experts"):
        cmd.append(f'-ot "{e["cpu_experts"]}=CPU"')
    if "draft" in roles:
        cmd.append(f"-md {d}/{_flat(roles['draft']['path'])}")
    samp = " ".join(f"{SAMPLING[k][0]} {_num(v)}" for k, v in e["sampling"].items())
    if samp:
        cmd.append(samp)
    if e["extra_args"]:
        cmd.append(" ".join(e["extra_args"]))
    where = {"gpu0": "GPU0", "gpu1": "GPU1", "both": "GPU0+GPU1"}[pl]
    ins = "[text, image]" if "mmproj" in roles else "[text]"
    out = [f"{a}# lab model from the spark-model registry ({e['repo']}@{e['revision'][:12]}); generated, do not edit here",
           f'{a}"{mid}":',
           f'{b}name: "Lab {e["id"]} ({where})"',
           f"{b}cmd: |"]
    out += [f"{c}{x}" for x in cmd]
    out += [f"{b}cmdStop: ${{stop}}",
            f"{b}proxy: http://127.0.0.1:${{PORT}}",
            f"{b}unloadTimeout: 30",
            f"{b}ttl: {e['ttl']}",
            f"{b}unlisted: true",
            f"{b}metadata: {{lab: true, class: {e['class']}}}",
            f"{b}capabilities: {{ in: {ins}, out: [text], tools: {'true' if e['tools_allowed'] else 'false'}, context: {e['ctx']} }}"]
    return out


def lab_matrix(enabled: list[dict], core_vars: dict, core_sets: list[str],
               gpu0_core: str = CORE_GPU0_VAR, gpu1_core: str = CORE_GPU1_VAR) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(vars, sets) to append for the enabled lab models. Vars l1..lN (skipping names the base uses), in registry
    order. GPU0 group = {gpu0_core} + gpu0 labs, GPU1 group = {gpu1_core} + gpu1 labs; one set
    "(g0 | ...) & (g1 | ...)" covers every pair (v260's DSL has | and parentheses; the core pair it also
    contains is already the core coding set, which stays first and wins ties). Each `both` model gets its own set."""
    vars_, sets = [], []
    n = 0
    g0, g1 = [gpu0_core], [gpu1_core]
    for e in enabled:
        n += 1
        while f"l{n}" in core_vars:
            n += 1
        v = f"l{n}"
        vars_.append((v, model_id(e)))
        if e["placement"] == "gpu0":
            g0.append(v)
        elif e["placement"] == "gpu1":
            g1.append(v)
        else:
            name = f"{LAB_PREFIX}{e['id']}"
            if name in core_sets:
                raise RegistryError(f"matrix set name {name!r} is already used by the base config", "render")
            sets.append((name, v))
    if len(g0) > 1 or len(g1) > 1:
        if PAIR_SET in core_sets:
            raise RegistryError(f"matrix set name {PAIR_SET!r} is already used by the base config", "render")
        grp = lambda g: g[0] if len(g) == 1 else "(" + " | ".join(g) + ")"   # noqa: E731
        sets.insert(0, (PAIR_SET, f"{grp(g0)} & {grp(g1)}"))
    return vars_, sets


def render(base_text: str, registry: dict) -> str:
    """The llama-swap config: base text + enabled lab models + their matrix vars/sets.
    With no enabled lab model the base is returned unchanged (byte for byte)."""
    reg = validate(registry)
    enabled = [m for m in reg["models"] if m["enabled"]]
    if not enabled:
        return base_text
    cfg = _parse_base(base_text)
    models = cfg["models"]
    for m in enabled:
        if model_id(m) in models:
            raise RegistryError(f"lab model id {model_id(m)!r} collides with a base model", "render")
    mx = matrix_of(cfg)
    if not mx or not isinstance(mx.get("vars"), dict) or not isinstance(mx.get("sets"), dict):
        raise RegistryError("base config has no routing.router.settings.matrix with vars and sets", "base config")
    for v, dev in ((CORE_GPU0_VAR, "0"), (CORE_GPU1_VAR, "1")):
        mm = mx["vars"].get(v)
        if mm not in models or f'device={dev}\\"' not in str(models[mm].get("cmd", "")):
            raise RegistryError(f"matrix var {v!r} must name the single-GPU core model on GPU{dev}", "base config")
    vars_, sets = lab_matrix(enabled, mx["vars"], list(mx["sets"]))

    nl = "\n"
    lines = base_text.split(nl)
    # Insertion points, computed on the original text, applied bottom-up so earlier indices stay valid.
    _, mlast, mind = _block(lines, ("models",))
    _, vlast, vind = _block(lines, MATRIX_PATH + ("vars",))
    _, slast, sind = _block(lines, MATRIX_PATH + ("sets",))
    model_text = []
    for e in enabled:
        model_text += [""] + lab_entry_lines(e, mind)
    var_text = [f"{' ' * vind}{k}: {v}" for k, v in vars_]
    set_text = [f'{" " * sind}{k}: "{v}"' for k, v in sets]
    edits = sorted([(mlast, model_text), (vlast, var_text), (slast, set_text)], key=lambda t: -t[0])
    for after, text in edits:
        lines[after + 1:after + 1] = text
    out = nl.join(lines)
    problems = check_config(out, base_text)
    if problems:
        raise RegistryError(problems, "rendered config")
    return out


# ---------------------------------------------------------------------------------------- checks
_DSL_TOKEN = re.compile(r"\s*(?:(?P<op>[&|()])|\+(?P<ref>[\w.-]+)|(?P<id>[\w.-]+)|(?P<bad>\S))")


def dsl_names(dsl: str) -> tuple[list[str], list[str]]:
    """(identifiers, +refs) of a llama-swap v260 matrix DSL expression; raises ValueError on a syntax error.
    Grammar (internal/matrix/dsl.go): or := and ('|' and)* ; and := atom ('&' atom)* ;
    atom := ident | '+' setname | '(' or ')' ; ident chars: letters, digits, _ - ."""
    toks = []
    pos = 0
    s = dsl.strip()
    if not s:
        raise ValueError("empty expression")
    while pos < len(s):
        m = _DSL_TOKEN.match(s, pos)
        if not m or m.group("bad"):
            raise ValueError(f"unexpected character at {pos}")
        pos = m.end()
        toks.append(m)
    ids, refs = [], []
    i = 0

    def atom():
        nonlocal i
        if i >= len(toks):
            raise ValueError("unexpected end")
        t = toks[i]
        i += 1
        if t.group("id"):
            ids.append(t.group("id"))
        elif t.group("ref"):
            refs.append(t.group("ref"))
        elif t.group("op") == "(":
            orx()
            if i >= len(toks) or toks[i].group("op") != ")":
                raise ValueError("missing closing parenthesis")
            i += 1
        else:
            raise ValueError(f"unexpected {t.group(0).strip()!r}")

    def andx():
        nonlocal i
        atom()
        while i < len(toks) and toks[i].group("op") == "&":
            i += 1
            atom()

    def orx():
        nonlocal i
        andx()
        while i < len(toks) and toks[i].group("op") == "|":
            i += 1
            andx()

    orx()
    if i != len(toks):
        raise ValueError(f"unexpected {toks[i].group(0).strip()!r} after expression")
    return ids, refs


def check_config(text: str, base_text: str | None = None) -> list[str]:
    """Problems with a rendered llama-swap config (empty list = ok). Mirrors what llama-swap v260 checks at load
    (matrix vars name known models, sets parse and reference known vars/models/sets) plus our own invariants:
    no duplicate ids/aliases, every ${macro} in a lab entry resolves, every lab model is in a matrix set, and
    (with base_text) everything that is not a lab entry is exactly the base."""
    p: list[str] = []
    try:
        cfg = yaml.safe_load(text)
    except yaml.YAMLError as x:
        return [f"not valid YAML: {x}"]
    if not isinstance(cfg, dict) or not isinstance(cfg.get("models"), dict):
        return ["no models: mapping"]
    models = cfg["models"]
    macros = cfg.get("macros") or {}
    names: dict[str, str] = {}
    for mid, mc in models.items():
        if mid in names:
            p.append(f"model id {mid!r} is also an alias of {names[mid]!r}")
        names[mid] = mid
        for al in (mc or {}).get("aliases") or []:
            if al in names:
                p.append(f"alias {al!r} of {mid!r} is already used by {names[al]!r}")
            names[al] = mid
    for mid, mc in models.items():
        if not str(mid).startswith(LAB_PREFIX):
            continue
        for fld in ("cmd", "cmdStop", "proxy"):
            for name in re.findall(r"\$\{([\w.-]+)\}", str(mc.get(fld, ""))):
                if name not in macros and name not in ("MODEL_ID", "PORT"):
                    p.append(f"{mid}: {fld} uses unknown macro ${{{name}}}")
        if mc.get("unlisted") is not True:
            p.append(f"{mid}: lab models must be unlisted")
    mx = matrix_of(cfg)
    if mx is not None:
        vars_ = mx.get("vars") or {}
        sets = mx.get("sets") or {}
        for k, v in vars_.items():
            if not VAR_KEY_RE.match(str(k)):
                p.append(f"matrix var key {k!r} must match {VAR_KEY_RE.pattern}")
            if v not in models:
                p.append(f"matrix var {k!r} references unknown model {v!r}")
        for k in (mx.get("evict_costs") or {}):
            if k not in vars_ and k not in models:
                p.append(f"matrix evict_costs: unknown var or model {k!r}")
        in_sets = set()
        for name, dsl in sets.items():
            try:
                ids, refs = dsl_names(str(dsl))
            except ValueError as x:
                p.append(f"matrix set {name!r}: {x}")
                continue
            for i in ids:
                if i in vars_:
                    in_sets.add(vars_[i])
                elif i in models:
                    in_sets.add(i)
                else:
                    p.append(f"matrix set {name!r}: unknown var or model {i!r}")
            for r in refs:
                if r not in sets:
                    p.append(f"matrix set {name!r} references undefined set {r!r}")
        for mid in models:
            if str(mid).startswith(LAB_PREFIX) and mid not in in_sets:
                p.append(f"{mid}: in no matrix set (it would evict everything)")
    if base_text is not None:
        try:
            base = yaml.safe_load(base_text)
        except yaml.YAMLError as x:
            return p + [f"base: not valid YAML: {x}"]
        core = {k: v for k, v in models.items() if not str(k).startswith(LAB_PREFIX)}
        if core != base.get("models"):
            p.append("core models differ from the base config")
        a, b = dict(cfg), dict(base)
        a.pop("models", None), b.pop("models", None)
        ma, mb = matrix_of(a), matrix_of(b)
        if ma is not None and mb is not None:
            a = copy.deepcopy(a)
            ma = matrix_of(a)
            ma["vars"] = {k: v for k, v in (ma.get("vars") or {}).items()
                          if k in (mb.get("vars") or {}) or not str(v).startswith(LAB_PREFIX)}
            ma["sets"] = {k: v for k, v in (ma.get("sets") or {}).items()
                          if k in (mb.get("sets") or {}) or not (str(k).startswith(LAB_PREFIX))}
            if list(ma["vars"]) != list(mb.get("vars") or {}) or list(ma["sets"]) != list(mb.get("sets") or {}):
                p.append("core matrix vars/sets differ from the base config (or changed order)")
        if a != b:
            p.append("settings outside models/matrix differ from the base config")
    return p
