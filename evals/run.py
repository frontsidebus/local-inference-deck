#!/usr/bin/env python3
"""Run security eval suites against the local models (or, optionally, a frontier baseline).

    python3 evals/run.py --suite evals/samples --model coder-fast --model coder --limit 5
    python3 evals/run.py --suite ctibench-mcq --model coder --thinking off,on
    python3 evals/run.py --suite evals/samples --model claude --yes-frontier      # frontier baseline (costs money)

Suites are JSONL files in the task format (evals/README.md). ``--suite`` takes a file, a directory of
*.jsonl files, or a bare suite name, which resolves to evals/data/<name>.jsonl.

Each (model, thinking mode) gets its own directory ``evals/results/<run-name>-<model>[-think|-thinkdefault]/``:
- ``run.json``: provenance (model, sampling, max_tokens, git SHA, dataset checksums, llama-swap cmd, grader);
- ``responses.jsonl``: the raw responses, one line per attempt-complete item;
- ``scores.jsonl``: per-item scores;
- ``summary.json``, ``report.md``: written at the end (and on Ctrl-C).

Resume: run the same command with the same ``--run-name``. Items that already have a successful response
are skipped; failed ones are retried. ``--rescore`` re-scores the existing responses without calling the
model.

llm_judge items are graded after every model has finished generating, so a grader that lives on other
GPUs (the default, ``big``) is loaded once rather than swapped in and out per item.

The gateway key is read from a file (default ~/.config/spark/hermes.key) and never printed or recorded.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import os
import platform
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

EVALS_DIR = Path(__file__).resolve().parent
REPO_DIR = EVALS_DIR.parent
sys.path.insert(0, str(EVALS_DIR))
# No bytecode from this process: a stale or planted .pyc under evals/ must not stand in for the frozen source.
# Confirmatory runs also refuse to start while any __pycache__ exists under evals/ (check "bytecode").
sys.dont_write_bytecode = True
import grading as G  # noqa: E402
import report as R  # noqa: E402
import scorers as S  # noqa: E402

SCHEMA = 1
TYPES = ("mcq", "extract", "classify", "freeform", "code")
DEFAULT_KEY_FILE = "~/.config/spark/hermes.key"
DATA_DIR = EVALS_DIR / "data"
RESULTS_DIR = EVALS_DIR / "results"
GRADER_PROMPT = EVALS_DIR / "prompts" / "llm_judge.md"
LLAMA_SWAP_TEMPLATE = REPO_DIR / "walter" / "llama-swap" / "config.yaml.tmpl"
FRONTIER_PREFIX = "claude"
PLAN_C1 = EVALS_DIR / "plans" / "comparisons-C1.json"          # the C1 comparisons (contestants, primaries)
HELDOUT_MANIFEST = EVALS_DIR / "datasets" / "heldout" / "heldout-v1.json"
MODEL_FAMILIES = EVALS_DIR / "prompts" / "model_families.json"
INTERVENTIONS = EVALS_DIR / "plans" / "c1-interventions.json"   # the pinned D6 prompt and D8 server commands
ATTEMPTS_LEDGER = EVALS_DIR / "plans" / "attempts-C1.jsonl"   # append-only: one line per confirmatory attempt   # written by the judge area; read here
SWAPPING_ALIASES = {"big", "vision", "hermes", "flash"}   # these take both GPUs and evict the coding pair

DEFAULT_SYSTEM = {
    "mcq": ("You are a security expert answering a multiple-choice question. Choose the single best option. "
            "Keep any reasoning brief. End your reply with a line of the form 'Answer: X', where X is the option letter."),
    "extract": ("You extract information from security text. Follow the requested output format exactly and do "
                "not add commentary. When a single value is asked for, end with a line 'Answer: <value>'."),
    "classify": "You classify security data. End your reply with a line of the form 'Answer: <label>'.",
    "freeform": "You are a security expert. Answer accurately and concisely.",
    "code": ("You are an application security reviewer. Analyse the code as asked and follow the requested "
             "output format."),
}
# Output caps per task type with thinking off. Thinking on adds --think-budget (reasoning counts against max_tokens).
MAX_TOKENS = {"mcq": 1024, "classify": 1024, "extract": 2048, "freeform": 2048, "code": 3072}
DEFAULT_THINK_BUDGET = 8192
HARD_MAX_TOKENS = 32768   # the gateway/llama-server cap for the coding models

# Frontier cost estimate (USD per million tokens, first-party API rates, 2026-09). The CLI's default model is
# assumed to be Opus-class unless --frontier-model names another.
FRONTIER_PRICES = {"opus": (4.0, 20.0), "sonnet": (2.0, 10.0), "haiku": (1.0, 5.0)}
FRONTIER_OVERHEAD_IN = 2000           # rough per-call input added by the CLI itself
FRONTIER_EST_OUT = {"mcq": 400, "classify": 400, "extract": 800, "freeform": 1200, "code": 1500}

# Same strip list as judge/runner/run_judge.py: nothing may point the frontier CLI at the local gateway.
_ENDPOINT_VARS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_URL", "ANTHROPIC_BEDROCK_BASE_URL",
                  "ANTHROPIC_VERTEX_BASE_URL", "ANTHROPIC_FOUNDRY_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE")
_STRIP_VARS = _ENDPOINT_VARS + (
    "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_SIMPLE",
    "OPENAI_API_KEY",
)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def rel(path: Path) -> str:
    """Repo-relative path when possible, so recorded files hold no home-directory paths."""
    try:
        return str(Path(path).resolve().relative_to(REPO_DIR))
    except ValueError:
        return Path(path).name


# ------------------------------------------------------------------------------------------- settings
def load_site_env(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if "#" in v and not v.startswith(("'", '"')):
            v = v.split("#", 1)[0].strip()
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def setting(name: str, site: Dict[str, str], default: str = "") -> str:
    return os.environ.get(name) or site.get(name) or default


def read_key(path: str) -> str:
    p = Path(os.path.expanduser(path))
    try:
        key = p.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"cannot read the gateway key file {p.name}: {exc.strerror}") from None
    if not key:
        raise SystemExit(f"the gateway key file {p.name} is empty")
    return key


# ------------------------------------------------------------------------------------------- datasets
class DatasetError(ValueError):
    pass


def resolve_suite_paths(specs: List[str]) -> List[Path]:
    out: List[Path] = []
    for spec in specs:
        p = Path(spec)
        if p.is_dir():  # the full sets only: <suite>.sampleN.jsonl copies would duplicate their ids
            out += sorted(f for f in p.glob("*.jsonl") if not re.search(r"\.sample\d+\.jsonl$", f.name))
        elif p.is_file():
            out.append(p)
        elif (DATA_DIR / f"{spec}.jsonl").is_file():
            out.append(DATA_DIR / f"{spec}.jsonl")
        else:
            raise DatasetError(f"no suite file, directory or evals/data/{spec}.jsonl for {spec!r}")
    if not out:
        raise DatasetError("no .jsonl suite files found")
    return out


def validate_item(it: Any, where: str) -> Dict[str, Any]:
    if not isinstance(it, dict):
        raise DatasetError(f"{where}: not a JSON object")
    for k in ("id", "suite", "type", "prompt", "scorer"):
        if not isinstance(it.get(k), str) or not it[k].strip():
            raise DatasetError(f"{where}: missing or empty {k!r}")
    if "answer" not in it:
        raise DatasetError(f"{where}: missing 'answer'")
    if it["type"] not in TYPES:
        raise DatasetError(f"{where}: unknown type {it['type']!r}")
    if it["scorer"] not in S.SCORERS:
        raise DatasetError(f"{where}: unknown scorer {it['scorer']!r}")
    if it["type"] == "mcq" or it["scorer"] == "mcq_letter":
        if not isinstance(it.get("choices"), list) or len(it["choices"]) < 2:
            raise DatasetError(f"{where}: mcq items need a 'choices' list")
    if "system" in it and not isinstance(it["system"], str):
        raise DatasetError(f"{where}: 'system' must be a string")
    return it


def load_datasets(paths: List[Path], limit: Optional[int], only: Optional[List[str]]):
    """-> (items, datasets). --limit is per suite, in file order."""
    items: List[Dict[str, Any]] = []
    datasets = []
    seen: Dict[str, str] = {}
    per_suite: Dict[str, int] = {}
    for path in paths:
        n_file = 0
        suites = set()
        with path.open(encoding="utf-8") as fh:
            for ln, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                where = f"{rel(path)}:{ln}"
                try:
                    it = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise DatasetError(f"{where}: invalid JSON ({exc.msg})") from None
                it = validate_item(it, where)
                if it["id"] in seen:
                    raise DatasetError(f"{where}: duplicate id {it['id']!r} (also in {seen[it['id']]})")
                seen[it["id"]] = where
                n_file += 1
                suites.add(it["suite"])
                if only and it["suite"] not in only:
                    continue
                if limit is not None and per_suite.get(it["suite"], 0) >= limit:
                    continue
                per_suite[it["suite"]] = per_suite.get(it["suite"], 0) + 1
                it = dict(it)
                it["_system_default"] = suite_system(path, it["suite"], it["type"])
                items.append(it)
        rec = {"path": rel(path), "sha256": sha256_file(path), "items": n_file, "suites": sorted(suites)}
        prov = {}
        for suite in sorted(suites):  # the sidecar written by evals/datasets/<dir>/fetch.py
            pf = path.parent / f"{suite}.provenance.json"
            if pf.is_file() and pf.stat().st_size < 65536:
                try:
                    prov[suite] = json.loads(pf.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    prov[suite] = {"error": "unreadable provenance sidecar"}
        if prov:
            rec["provenance"] = prov
        datasets.append(rec)
    if not items:
        raise DatasetError("no items selected")
    return items, datasets


_SYS_CACHE: Dict[Tuple[str, str], Optional[str]] = {}


def suite_system(path: Path, suite: str, typ: str) -> str:
    """Suite system prompt: <file>.system.txt or <suite>.system.txt next to the JSONL, else the per-type default."""
    key = (str(path), suite)
    if key not in _SYS_CACHE:
        found = None
        for cand in (path.with_suffix(".system.txt"), path.parent / f"{suite}.system.txt"):
            if cand.is_file():
                found = cand.read_text(encoding="utf-8").strip()
                break
        _SYS_CACHE[key] = found
    return _SYS_CACHE[key] or DEFAULT_SYSTEM[typ]


def user_content(item: Dict[str, Any]) -> str:
    text = item["prompt"].rstrip()
    choices = item.get("choices")
    if choices:
        lines = []
        for i, c in enumerate(choices):
            c = str(c).strip()
            letter = S._LETTERS[i]
            lines.append(c if re.match(rf"^\(?{letter}[\).:]", c) else f"{letter}) {c}")
        text += "\n\n" + "\n".join(lines)
    return text


def build_messages(item: Dict[str, Any]) -> List[Dict[str, str]]:
    system = item.get("system") or item.get("_system_default") or DEFAULT_SYSTEM[item["type"]]
    return [{"role": "system", "content": system}, {"role": "user", "content": user_content(item)}]


THINK_OFF_TEMPERATURE = 0.0
THINK_TEMPERATURE = 0.6
THINK_TOP_P = 0.95

# ---- Controlled sampling: the one place the sampler settings are declared. -------------------------------------
# Every knob that llama-server (or the llama-swap cmd) can default differently is sent explicitly on every request,
# so a server default cannot leak into a comparison. Example: coder-fast's cmd sets --presence-penalty 1.5, big sets
# --top-k 40 --min-p 0.01, vision --top-k 64 (and llama.cpp's own min_p default is 0.05). Penalties apply even under
# greedy decoding, so they matter for thinking-off runs too.
# "off" (thinking off) is greedy, with every truncation sampler and penalty disabled.
# "thinking" (thinking on/default) is the Qwen3 model-card recommendation for thinking mode (temperature 0.6,
# top_p 0.95, top_k 20, min_p 0), applied identically to every model; penalties are neutral. Greedy decoding makes
# reasoning loop (eval run (a): 9 of 50 coder CTIBench MCQ items at temperature 0 hit the cap).
# Values use llama.cpp semantics: top_k 0 = off, top_p 1.0 = off, min_p 0 = off, repeat_penalty 1.0 = off,
# dry_multiplier 0 = off, xtc_probability 0 = off.
NEUTRAL_PENALTIES: Dict[str, float] = {"presence_penalty": 0.0, "frequency_penalty": 0.0, "repeat_penalty": 1.0,
                                       "dry_multiplier": 0.0, "xtc_probability": 0.0}
SAMPLING_PROFILES: Dict[str, Dict[str, Any]] = {
    "off": {"temperature": THINK_OFF_TEMPERATURE, "top_p": 1.0, "top_k": 0, "min_p": 0.0, **NEUTRAL_PENALTIES},
    "thinking": {"temperature": THINK_TEMPERATURE, "top_p": THINK_TOP_P, "top_k": 20, "min_p": 0.0, **NEUTRAL_PENALTIES},
}
SAMPLER_KEYS = tuple(SAMPLING_PROFILES["off"])
# llama-server cmd flags for the same knobs (to read a server's defaults from its llama-swap entry)
SERVER_FLAG_FOR = {"temperature": "--temp", "top_p": "--top-p", "top_k": "--top-k", "min_p": "--min-p",
                   "presence_penalty": "--presence-penalty", "frequency_penalty": "--frequency-penalty",
                   "repeat_penalty": "--repeat-penalty", "dry_multiplier": "--dry-multiplier",
                   "xtc_probability": "--xtc-probability"}


def sampling_profile_name(thinking: str) -> str:
    return "thinking" if thinking in ("on", "default") else "off"


def sampler_overrides(args) -> Dict[str, Any]:
    """The explicit per-invocation overrides: --temperature/--top-p/--presence-penalty and --sampler KEY=VALUE."""
    out: Dict[str, Any] = dict(getattr(args, "sampler_kv", None) or {})
    for k in ("temperature", "top_p", "presence_penalty"):
        v = getattr(args, k, None)
        if v is not None:
            out[k] = v
    return out


def sampling_for(args, thinking: str) -> Dict[str, Any]:
    """The sampler parameters sent on every request of one run (seed excluded). Explicit overrides win over the
    declared profile. With --legacy-sampling, the pre-2026-10 behaviour: temperature and top_p only (top_p unset with
    thinking off), presence_penalty only when given, so the server's defaults apply to everything else."""
    if getattr(args, "legacy_sampling", False):
        thinks = thinking in ("on", "default")
        out: Dict[str, Any] = {"temperature": args.temperature if args.temperature is not None
                               else (THINK_TEMPERATURE if thinks else THINK_OFF_TEMPERATURE)}
        top_p = args.top_p if args.top_p is not None else (THINK_TOP_P if thinks else None)
        if top_p is not None:
            out["top_p"] = top_p
        if args.presence_penalty is not None:
            out["presence_penalty"] = args.presence_penalty
        return out
    out = dict(SAMPLING_PROFILES[sampling_profile_name(thinking)])
    out.update(sampler_overrides(args))
    return out


# ---- Token budgets: one declared policy, identical for every model in a comparison. ----------------------------
# answer_tokens: the visible-answer budget per task type (thinking off gets exactly this).
# think_budget: added on top for thinking on/default, because llama-server counts reasoning against max_tokens. Every
#   model gets the same answer budget, and every thinking run the same additional reasoning budget. A model that
#   reasons past think_budget eats into its own answer budget and is recorded as truncated (finish_reason
#   "length"); it is never silently extended.
# suite_answer_tokens: optional per-suite answer budgets that replace the per-type value.
# Precedence per item: --max-tokens (flat, smoke runs) > item meta.max_tokens > suite > type; then + think_budget.
BUDGET_POLICY: Dict[str, Any] = {"name": "default-2026-10", "answer_tokens": dict(MAX_TOKENS),
                                 "suite_answer_tokens": {}, "think_budget": DEFAULT_THINK_BUDGET,
                                 "hard_max": HARD_MAX_TOKENS}


def load_budget_policy(path: Optional[str]) -> Dict[str, Any]:
    """BUDGET_POLICY, or a JSON file that overrides parts of it:
    {"name", "answer_tokens": {type: n}, "suite_answer_tokens": {suite: n}, "think_budget": n}."""
    pol = json.loads(json.dumps(BUDGET_POLICY))
    if not path:
        return pol
    try:
        given = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"--budget-policy {Path(path).name}: {exc}") from None
    if not isinstance(given, dict):
        raise SystemExit("--budget-policy: want a JSON object")
    for k in ("answer_tokens", "suite_answer_tokens"):
        if k in given:
            v = given[k]
            if not isinstance(v, dict) or not all(isinstance(n, int) and not isinstance(n, bool) and n > 0
                                                  for n in v.values()):
                raise SystemExit(f"--budget-policy: {k} must map names to positive integers")
            pol[k] = dict(pol[k], **v) if k == "answer_tokens" else dict(v)
    unknown_types = sorted(set(pol["answer_tokens"]) - set(TYPES))
    if unknown_types:
        raise SystemExit(f"--budget-policy: unknown task type(s) {unknown_types}")
    if "think_budget" in given:
        tb = given["think_budget"]
        if not isinstance(tb, int) or isinstance(tb, bool) or tb < 0:
            raise SystemExit("--budget-policy: think_budget must be a non-negative integer")
        pol["think_budget"] = tb
    pol["name"] = str(given.get("name") or Path(path).stem)
    return pol


def _policy(args) -> Dict[str, Any]:
    return getattr(args, "budget_policy", None) or BUDGET_POLICY


def answer_budget_for(item: Dict[str, Any], args) -> int:
    """The visible-answer budget for one item (before any thinking budget)."""
    if args.max_tokens:
        return args.max_tokens
    meta_cap = (item.get("meta") or {}).get("max_tokens")
    if isinstance(meta_cap, int) and not isinstance(meta_cap, bool) and meta_cap > 0:
        return meta_cap
    pol = _policy(args)
    return pol["suite_answer_tokens"].get(item.get("suite")) or pol["answer_tokens"].get(item["type"]) \
        or MAX_TOKENS[item["type"]]


def max_tokens_for(item: Dict[str, Any], args, thinking: str) -> int:
    if args.max_tokens:  # a flat cap (smoke runs): no thinking budget added, as before
        return min(args.max_tokens, HARD_MAX_TOKENS)
    base = answer_budget_for(item, args)
    if thinking != "off":
        base += args.think_budget
    return min(base, HARD_MAX_TOKENS)


# ------------------------------------------------------------------------------------------- provenance
def git_info() -> Dict[str, Any]:
    def g(*a: str) -> str:
        try:
            return subprocess.run(["git", *a], cwd=REPO_DIR, capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    status = g("status", "--porcelain", "--untracked-files=no")
    untracked = [l[3:] for l in g("status", "--porcelain", "--untracked-files=normal").splitlines()
                 if l.startswith("??")]
    return {"sha": g("rev-parse", "HEAD") or "unknown", "branch": g("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(status), "untracked": bool(untracked), "untracked_files": untracked[:20],
            "dirty_files": [l[3:] for l in status.splitlines()][:20]}


def git_rev(ref: str) -> Optional[str]:
    """The commit SHA a ref (tag, branch or SHA) points at, or None."""
    try:
        out = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=REPO_DIR,
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None


def llama_swap_entry(alias: str, path: Path) -> Optional[Dict[str, Any]]:
    """The llama-swap model entry serving ``alias``, from a config file (default: the repo template), with the
    repo's macros expanded. Returns None when the alias is not found. A template is what the repo deploys; the
    live config can differ if Walter has not been redeployed."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    macros: Dict[str, str] = {}
    msec = re.search(r"^macros:\n(.*?)^\S", text, re.S | re.M)
    if msec:
        for k, v in re.findall(r'^\s+"([\w-]+)":\s*"(.*)"\s*$', msec.group(1), re.M):
            macros[k] = v
    mods = re.search(r"^models:\n(.*?)(?=^\S|\Z)", text, re.S | re.M)
    if not mods:
        return None
    for m in re.finditer(r'^  "([^"]+)":\n((?:^(?:    .*|\s*)\n)+)', mods.group(1), re.M):
        mid, block = m.group(1), m.group(2)
        al = re.search(r"aliases:\s*\[(.*?)\]", block)
        aliases = re.findall(r'"([^"]+)"', al.group(1)) if al else []
        if alias != mid and alias not in aliases:
            continue
        cm = re.search(r"^    cmd: \|\n((?:^      .*\n)+)", block, re.M)
        cmd = " ".join(l.strip() for l in cm.group(1).splitlines()) if cm else ""
        for _ in range(3):
            cmd = re.sub(r"\$\{([\w-]+)\}", lambda x: macros.get(x.group(1), x.group(0)), cmd)
        flags = {}
        for flag in ("-c", "-n", "-sm", "-m", *SERVER_FLAG_FOR.values()):
            fm = re.search(rf"(?:^|\s){re.escape(flag)}\s+(\S+)", cmd)
            if fm:
                flags[flag] = fm.group(1)
        if "-m" in flags:  # the model file's basename only: a full path can name a user or host
            flags["-m"] = Path(flags["-m"]).name
        nm = re.search(r'name:\s*"([^"]+)"', block)
        return {"model_id": mid, "name": nm.group(1) if nm else mid, "aliases": aliases, "cmd": cmd,
                "server_flags": flags, "source": rel(path),
                "note": "from the repo template; the live /etc/llama-swap/config.yaml may differ"
                if path == LLAMA_SWAP_TEMPLATE else "from the given llama-swap config"}
    return None


# ---- Serving config: what produced the tokens. Every field is {"value", "source"} or {"value": None, "reason"}.
_QUANT_RE = re.compile(r"(?:^|[-_.])((?:UD-)?(?:I?Q\d(?:_[A-Z0-9]+)*|BF16|F16|F32|MXFP4(?:_MOE)?))"
                       r"(?:-\d{5}-of-\d{5})?\.gguf$")
SERVING_FIELDS = ("model_file", "quant", "llama_cpp_build", "ctx_size", "chat_template_sha256",
                  "server_sampling_defaults")


def quant_from_filename(name: Optional[str]) -> Optional[str]:
    """The GGUF quant tag in a model filename ('Qwen3-8B-UD-Q4_K_XL.gguf' -> 'UD-Q4_K_XL'), or None."""
    m = _QUANT_RE.search(Path(name).name) if name else None
    return m.group(1) if m else None


def load_serving_config(path: Optional[str]) -> Optional[Dict[str, Any]]:
    """--serving-config: a static JSON file with what the server cannot report about itself, e.g.
    {"defaults": {"llama_cpp_build": "b6500", "gpu": "..."}, "models": {"coder": {"quant": "UD-Q4_K_XL",
    "model_file": "...gguf", "flags": "...", "llama_cpp_build": "..."}}}. Without "models", the whole object applies
    to every model. Keep hostnames and paths out of it: it is copied into run.json."""
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"--serving-config {Path(path).name}: {exc}") from None
    if not isinstance(data, dict):
        raise SystemExit("--serving-config: want a JSON object")
    return {"path": rel(Path(path)), "sha256": sha256_file(Path(path)), "data": data}


def static_serving_for(cfg: Optional[Dict[str, Any]], model: str) -> Optional[Dict[str, Any]]:
    if not cfg:
        return None
    data = cfg["data"]
    if "models" not in data:
        return dict(data)
    per = (data.get("models") or {}).get(model)
    out = dict(data.get("defaults") or {})
    if isinstance(per, dict):
        out.update(per)
    return out or None


def server_root(base_url: str) -> str:
    b = base_url.rstrip("/")
    return b[:-3] if b.endswith("/v1") else b


def probe_serving(fetch, base_url: str, model: str) -> Dict[str, Any]:
    """Opt-in (--probe-server): ask an OpenAI-compatible server about itself. ``fetch(url) -> dict`` raises on any
    failure; tests pass a stub. Tries {base}/models, then llama-swap's {root}/upstream/<model>/props, then a bare
    llama-server's {root}/props. Records endpoint paths and outcomes, never hosts. Note: on llama-swap, the upstream
    /props call loads the model if it is not loaded (the run loads it anyway)."""
    root = server_root(base_url)
    out: Dict[str, Any] = {"tried": [], "models_entry": None, "props": None}
    try:
        data = fetch(base_url.rstrip("/") + "/models")
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        entry = next((m for m in (data.get("data") or []) if isinstance(m, dict) and
                      (m.get("id") == model or model in (m.get("aliases") or []))), None)
        out["tried"].append({"path": "/v1/models", "ok": True, "found": entry is not None})
        if entry:
            out["models_entry"] = {k: entry.get(k) for k in ("id", "owned_by", "created", "meta") if k in entry}
    except Exception as exc:  # any failure: record it and move on
        out["tried"].append({"path": "/v1/models", "ok": False, "error": str(exc)[:120]})
    for path, scope in ((f"/upstream/{model}/props", "llama-swap upstream"), ("/props", "bare llama-server")):
        try:
            props = fetch(root + path)
        except Exception as exc:
            out["tried"].append({"path": path, "ok": False, "error": str(exc)[:120]})
            continue
        if not isinstance(props, dict):
            out["tried"].append({"path": path, "ok": False, "error": "not a JSON object"})
            continue
        out["tried"].append({"path": path, "ok": True})
        gen = props.get("default_generation_settings") or {}
        params = gen.get("params") if isinstance(gen.get("params"), dict) else gen
        tmpl = props.get("chat_template")
        out["props"] = {
            "scope": scope,
            "build_info": props.get("build_info"),
            "model_file": Path(props["model_path"]).name if props.get("model_path") else None,
            "n_ctx": gen.get("n_ctx") or props.get("n_ctx"),
            "total_slots": props.get("total_slots"),
            "chat_template_sha256": sha256_text(tmpl) if isinstance(tmpl, str) and tmpl else None,
            "sampling_defaults": {k: params[k] for k in (*SAMPLER_KEYS, "seed", "n_predict", "dry_base",
                                                          "dry_allowed_length", "repeat_last_n", "typical_p",
                                                          "mirostat") if k in params},
        }
        break
    return out


def verify_samplers(fetch, base_url: str, model: str, sent: Dict[str, Any]) -> Dict[str, Any]:
    """After generation (--probe-server): read llama-server's /slots, which shows the sampler params of each slot's
    last task, and compare them with what the run sent. This is the only end-to-end proof that the gateway forwarded
    the non-OpenAI keys (top_k, min_p, repeat_penalty, ...) rather than dropping them. Only sampler params are kept;
    a slot's prompt is never copied."""
    root = server_root(base_url)
    tried = []
    for path in (f"/upstream/{model}/slots", "/slots"):
        try:
            data = fetch(root + path)
        except Exception as exc:
            tried.append({"path": path, "ok": False, "error": str(exc)[:120]})
            continue
        slots = data if isinstance(data, list) else data.get("slots") if isinstance(data, dict) else None
        params = [sl.get("params") for sl in (slots or []) if isinstance(sl, dict) and isinstance(sl.get("params"), dict)]
        if not params:
            tried.append({"path": path, "ok": False, "error": "no slot params"})
            continue
        tried.append({"path": path, "ok": True})
        # the slot that ran our last request: the one whose seed and temperature match, else the first
        cand = [p for p in params if p.get("seed") == sent.get("seed")] or params
        obs = {k: cand[0][k] for k in (*SAMPLER_KEYS, "seed") if k in cand[0]}
        mismatch = {}
        for k, v in sent.items():
            if k not in obs:
                continue
            try:
                same = abs(float(obs[k]) - float(v)) < 1e-6
            except (TypeError, ValueError):
                same = obs[k] == v
            if not same:
                mismatch[k] = {"sent": v, "server_used": obs[k]}
        return {"tried": tried, "observed": obs, "mismatch": mismatch,
                "not_reported": sorted(k for k in sent if k not in obs),
                "ok": not mismatch, "note": "from the slot's last task; a concurrent user of the same model would "
                                            "show their params instead"}
    return {"tried": tried, "observed": None, "ok": None, "reason": "no /slots endpoint reachable"}


def _known(value: Any, source: str) -> Dict[str, Any]:
    return {"value": value, "source": source}


def _unknown(reason: str) -> Dict[str, Any]:
    return {"value": None, "reason": reason}


def resolve_serving(model: str, static: Optional[Dict[str, Any]], probe: Optional[Dict[str, Any]],
                    swap: Optional[Dict[str, Any]], sent: Dict[str, Any], probed: bool) -> Dict[str, Any]:
    """One record per serving fact, from the best source: the static --serving-config (the operator's word), then the
    live probe, then the repo's llama-swap template (which the live config may not match). Unknowns are explicit."""
    props = (probe or {}).get("props") or {}
    flags = (swap or {}).get("server_flags") or {}
    no_probe = "not probed (--probe-server off)" if not probed else "the probe did not report it"
    tmpl = "llama-swap template (the live config may differ)"
    st = static or {}
    f: Dict[str, Any] = {}

    def pick(name, *cands):
        for val, src in cands:
            if val not in (None, "", {}):
                f[name] = _known(val, src)
                return
        f[name] = _unknown(f"not in --serving-config; {no_probe}; not in the llama-swap template")

    pick("model_file", (st.get("model_file"), "serving-config"), (props.get("model_file"), "probe /props"),
         (flags.get("-m"), tmpl))
    quant_src = [(st.get("quant"), "serving-config")]
    for name, src in ((props.get("model_file"), "probe /props model_path"), (flags.get("-m"), tmpl)):
        quant_src.append((quant_from_filename(name), src + " (parsed from the file name)"))
    pick("quant", *quant_src)
    pick("llama_cpp_build", (st.get("llama_cpp_build"), "serving-config"), (props.get("build_info"), "probe /props"))
    pick("ctx_size", (st.get("ctx_size"), "serving-config"), (props.get("n_ctx"), "probe /props"),
         (flags.get("-c"), tmpl))
    pick("chat_template_sha256", (st.get("chat_template_sha256"), "serving-config"),
         (props.get("chat_template_sha256"), "probe /props"))
    server_defaults = props.get("sampling_defaults") or {
        k: flags[SERVER_FLAG_FOR[k]] for k in SAMPLER_KEYS if SERVER_FLAG_FOR[k] in flags}
    pick("server_sampling_defaults", (server_defaults if props.get("sampling_defaults") else None, "probe /props"),
         (server_defaults if not props.get("sampling_defaults") else None, tmpl + " (flags given; llama.cpp built-in "
                                                                          "defaults for the rest)"))
    # which server defaults the request overrode (the explicit sampling makes the server's value irrelevant)
    known = f["server_sampling_defaults"]["value"] or {}
    overridden = {}
    for k, v in sent.items():
        if k in known:
            try:
                same = float(known[k]) == float(v)
            except (TypeError, ValueError):
                same = str(known[k]) == str(v)
            if not same:
                overridden[k] = {"server": known[k], "sent": v}
    not_sent = [k for k in SAMPLER_KEYS if k not in sent]
    out = {"fields": f,
           "sampler_overrides": overridden,
           "samplers_left_to_server": not_sent,
           "note": ("every sampler is sent explicitly, so server defaults cannot apply to them" if not not_sent else
                    "legacy sampling: the server's defaults apply to " + ", ".join(not_sent)),
           "static": static,
           "probe": probe}
    if not probed:
        out["probe"] = None
    return out


# ------------------------------------------------------------------------------------------- backends
class CallError(Exception):
    def __init__(self, msg: str, retryable: bool):
        super().__init__(msg)
        self.retryable = retryable


class GatewayClient:
    """OpenAI-compatible /v1/chat/completions over urllib. Holds the key; never logs it."""

    def __init__(self, base_url: str, key: str, timeout: float):
        self.base = base_url.rstrip("/")
        self.url = self.base + "/chat/completions"
        self._key = key
        self.timeout = timeout

    def __repr__(self) -> str:  # never show the key
        return f"GatewayClient({self.url!r})"

    def get_json(self, url: str) -> Any:
        """GET a metadata endpoint on the same server (--probe-server): a JSON object or list. Raises on failure."""
        req = urllib.request.Request(url, method="GET", headers={"Authorization": f"Bearer {self._key}"})
        with urllib.request.urlopen(req, timeout=min(self.timeout, 30)) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        if not isinstance(data, (dict, list)):
            raise ValueError("not a JSON object or list")
        return data

    def chat(self, body: Dict[str, Any]) -> Dict[str, Any]:
        req = urllib.request.Request(self.url, data=json.dumps(body).encode("utf-8"), method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self._key}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise CallError(f"HTTP {exc.code}: {detail}", exc.code in (408, 409, 429) or exc.code >= 500) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise CallError(f"network error: {reason}", True) from None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise CallError(f"non-JSON reply: {raw[:200]}", True) from None
        if not isinstance(data, dict) or not data.get("choices"):
            raise CallError(f"reply has no choices: {raw[:200]}", True)
        return data


def frontier_env(api_host: str, base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Child env for the claude CLI, stripped exactly like the judge's frontier runner."""
    env = dict(os.environ if base is None else base)
    had_endpoint = any(env.get(k) for k in _ENDPOINT_VARS if k.startswith("ANTHROPIC_"))
    for k in _STRIP_VARS:
        env.pop(k, None)
    if had_endpoint:
        env.pop("ANTHROPIC_API_KEY", None)
    for k in [k for k, v in env.items() if api_host and api_host in v]:
        env.pop(k, None)
    return env


def frontier_argv(cmd: str, system_prompt: str, model: Optional[str], budget: str, extra: str) -> List[str]:
    """The judge's frontier invocation (judge/runner/run_judge.py frontier_argv): no tools, no MCP, no skills,
    no session persistence, plus --setting-sources project in an empty cwd so no user settings or hooks load."""
    argv = shlex.split(cmd) + [
        "-p",
        "--output-format", "json",
        "--system-prompt", system_prompt,
        "--tools", "",
        "--permission-prompts", "none",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--setting-sources", "project",
    ]
    if model:
        argv += ["--model", model]
    if budget and budget != "0":
        argv += ["--max-budget-usd", budget]
    argv += shlex.split(extra or "")
    return argv


class FrontierClient:
    def __init__(self, cmd: str, model: Optional[str], timeout: float, budget: str, extra: str, api_host: str):
        self.cmd, self.model, self.timeout, self.budget, self.extra = cmd, model, timeout, budget, extra
        self.api_host = api_host

    def chat(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        argv = frontier_argv(self.cmd, messages[0]["content"], self.model, self.budget, self.extra)
        with tempfile.TemporaryDirectory(prefix="evals-frontier-") as cwd:
            try:
                proc = subprocess.run(argv, input=messages[1]["content"], capture_output=True, text=True,
                                      timeout=self.timeout, env=frontier_env(self.api_host), cwd=cwd)
            except FileNotFoundError:
                raise CallError(f"frontier command not found: {argv[0]}", False) from None
            except subprocess.TimeoutExpired:
                raise CallError(f"frontier command timed out after {self.timeout:.0f}s", True) from None
        try:
            env = json.loads(proc.stdout.strip())
        except json.JSONDecodeError:
            raise CallError(f"frontier exited {proc.returncode} without a JSON envelope: "
                            f"{proc.stderr.strip()[:200]}", proc.returncode != 0) from None
        if not isinstance(env, dict):
            raise CallError("frontier envelope is not an object", False)
        return env


def frontier_price(model: Optional[str]) -> Tuple[float, float, str]:
    m = (model or "").lower()
    for k, v in FRONTIER_PRICES.items():
        if k in m:
            return v[0], v[1], k
    return FRONTIER_PRICES["opus"][0], FRONTIER_PRICES["opus"][1], "opus (assumed CLI default)"


def estimate_frontier_cost(items: List[Dict[str, Any]], model: Optional[str]) -> Dict[str, Any]:
    pin, pout, tier = frontier_price(model)
    tin = sum(len(m["content"]) for it in items for m in build_messages(it)) / 3.5 + FRONTIER_OVERHEAD_IN * len(items)
    tout = sum(FRONTIER_EST_OUT[it["type"]] for it in items)
    usd = tin / 1e6 * pin + tout / 1e6 * pout
    return {"items": len(items), "input_tokens": int(tin), "output_tokens": int(tout), "price_tier": tier,
            "usd_per_mtok": [pin, pout], "estimated_usd": round(usd, 2)}


# ------------------------------------------------------------------------------------------- one run
OUTPUT_FILES = ("responses.jsonl", "scores.jsonl", "grades.jsonl")


def outputs_sha256(d: Path) -> Dict[str, Optional[str]]:
    """sha256 of a run dir's output files (None for one that does not exist)."""
    return {n: (sha256_file(d / n) if (d / n).is_file() else None) for n in OUTPUT_FILES}


def protocol_fields(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Per-item fields every score record carries, so reports can stratify or exclude without the responses:
    finish_reason and truncated (the original reply's), followup_used, and the seed sent."""
    out: Dict[str, Any] = {"finish_reason": rec.get("finish_reason"), "truncated": rec.get("finish_reason") == "length",
                           "followup_used": bool(rec.get("followup_used") or rec.get("final_answer_prompt"))}
    if rec.get("seed") is not None:
        out["seed"] = rec["seed"]
    return out


class Run:
    def __init__(self, args, model: str, thinking: str, items: List[Dict[str, Any]], datasets, site: Dict[str, str],
                 run_name: str, client, grader_cfg: Dict[str, Any], seed: Optional[int] = None):
        self.args, self.model, self.thinking, self.items = args, model, thinking, items
        self.seed = args.seed if seed is None else seed
        self.frontier = model == FRONTIER_PREFIX or model.startswith(FRONTIER_PREFIX + ":")
        self.frontier_model = model.split(":", 1)[1] if self.frontier and ":" in model else args.frontier_model
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", model)
        suffix = "" if self.frontier or thinking == "off" else ("-think" if thinking == "on" else "-thinkdefault")
        # --seeds gives every seed its own dir (report.py labels runs that differ only by seed); --seed keeps the
        # old unsuffixed name so earlier runs still resume
        if getattr(args, "seeds_given", False) and not self.frontier:
            suffix += f"-seed{self.seed}"
        self.dir = Path(args.results) / f"{run_name}-{slug}{suffix}"
        self.client = client
        self.sampling = sampling_for(args, thinking)
        self.temperature, self.top_p = self.sampling["temperature"], self.sampling.get("top_p")
        self.lock = threading.Lock()
        self.cost_usd = 0.0
        self.stop = threading.Event()
        self.config = self._config(datasets, site, grader_cfg)

    def _prompt_provenance(self) -> Dict[str, Any]:
        """sha256 of every prompt text the run sends, per suite: what was actually sent, not what was configured."""
        per_suite: Dict[str, set] = {}
        overrides: Dict[str, int] = {}
        for it in self.items:
            per_suite.setdefault(it["suite"], set()).add(sha256_text(build_messages(it)[0]["content"]))
            if it.get("system"):
                overrides[it["suite"]] = overrides.get(it["suite"], 0) + 1
        return {
            "system_prompt_sha256": {k: sorted(v) if len(v) > 1 else next(iter(v)) for k, v in sorted(per_suite.items())},
            "items_with_own_system_prompt": overrides,
            "default_system_sha256": {t: sha256_text(DEFAULT_SYSTEM[t]) for t in TYPES},
            "final_answer_prompt_sha256": sha256_text(self.FINAL_ANSWER_PROMPT),
            "user_template": "prompt + blank line + lettered choices (run.py user_content)",
        }

    def _item_sets(self) -> Dict[str, Any]:
        """The frozen item set per suite: count and sha256 of the sorted selected ids (independent of file order)."""
        ids: Dict[str, List[str]] = {}
        for it in self.items:
            ids.setdefault(it["suite"], []).append(it["id"])
        return {s: {"n": len(v), "ids_sha256": sha256_text("\n".join(sorted(v)))} for s, v in sorted(ids.items())}

    def _budget(self) -> Dict[str, Any]:
        a = self.args
        pol = _policy(a)
        per_suite: Dict[str, Any] = {}
        for it in self.items:
            ans = answer_budget_for(it, a)
            tot = max_tokens_for(it, a, self.thinking)
            ps = per_suite.setdefault(it["suite"], {"answer": set(), "max_tokens": set(), "item_caps": 0})
            ps["answer"].add(ans)
            ps["max_tokens"].add(tot)
            if (it.get("meta") or {}).get("max_tokens"):
                ps["item_caps"] += 1
        flat = lambda v: sorted(v)[0] if len(v) == 1 else sorted(v)  # noqa: E731
        return {"policy": pol, "policy_sha256": sha256_text(json.dumps(pol, sort_keys=True)),
                "flat_override": a.max_tokens or None,
                "think_budget": a.think_budget if self.thinking != "off" else 0,
                "per_suite": {s: {"answer": flat(v["answer"]), "max_tokens": flat(v["max_tokens"]),
                                  "items_with_own_cap": v["item_caps"]} for s, v in sorted(per_suite.items())}}

    def _config(self, datasets, site, grader_cfg) -> Dict[str, Any]:
        a = self.args
        legacy = getattr(a, "legacy_sampling", False)
        if legacy:
            sampling: Dict[str, Any] = {"temperature": self.temperature, "seed": self.seed}
            if self.top_p is not None:
                sampling["top_p"] = self.top_p
            if a.presence_penalty is not None:
                sampling["presence_penalty"] = a.presence_penalty
        else:
            sampling = {**self.sampling, "seed": self.seed, "profile": sampling_profile_name(self.thinking),
                        "overrides": sampler_overrides(a)}
        if self.frontier:
            sampling = {"note": "claude CLI defaults (no sampling control)"}
        caps = {t: max_tokens_for({"type": t}, a, self.thinking) for t in TYPES}
        alias_entry = None if self.frontier else llama_swap_entry(self.model, Path(a.llama_swap_config))
        if self.frontier:
            serving: Any = {"note": "frontier API via the claude CLI; serving config not observable"}
        else:
            probe = None
            if getattr(a, "probe_server", False) and not a.rescore and self.client is not None and hasattr(self.client, "get_json"):
                probe = probe_serving(self.client.get_json, self.client.base, self.model)
            serving = resolve_serving(self.model, static_serving_for(getattr(a, "serving_cfg", None), self.model),
                                      probe, alias_entry, self.sampling, bool(getattr(a, "probe_server", False)))
            if getattr(a, "serving_cfg", None):
                serving["static_file"] = {k: a.serving_cfg[k] for k in ("path", "sha256")}
        req = None if self.frontier else self.request_body(
            {"type": "mcq", "suite": "", "id": ""}, [])
        if req:
            for k in ("messages", "max_tokens"):
                req.pop(k, None)
        return {
            "schema": SCHEMA,
            "model": self.model,
            "backend": "claude-cli" if self.frontier else "gateway",
            "frontier_model": self.frontier_model if self.frontier else None,
            "gateway": None if self.frontier else (a.base_url or "https://${SPARK_API_HOST}/v1"),
            "thinking": "n/a" if self.frontier else self.thinking,
            "sampling": sampling,
            "max_tokens": None if self.frontier else caps,
            "think_budget": a.think_budget if self.thinking != "off" and not self.frontier else 0,
            "concurrency": a.concurrency,
            "prompt_cache": None if self.frontier else {
                "sent": {"cache_prompt": False} if getattr(a, "no_prompt_cache", False) else None,
                "note": "--no-prompt-cache sends cache_prompt=false; whether the gateway forwards it is unverified"
                if getattr(a, "no_prompt_cache", False) else "server default (not sent)"},
            "timeout_s": a.timeout,
            "retries": a.retries,
            "limit": a.limit,
            "only_suite": a.only_suite,
            "scorer_overrides": a.overrides,
            "datasets": datasets,
            "items_selected": len(self.items),
            "llama_swap": alias_entry,
            "grader": grader_cfg,
            "harness": {"run.py": sha256_file(Path(__file__))[:16], "scorers.py": sha256_file(EVALS_DIR / "scorers.py")[:16],
                        "python": platform.python_version()},
            "git": git_info(),
            # --- protocol provenance (2026-10): everything needed to reproduce the run
            "request_params": req,
            "seed": None if self.frontier else self.seed,
            "budget": None if self.frontier else self._budget(),
            "followup": {"enabled": bool(a.final_answer) and not self.frontier, "max_tokens": 32, "thinking": "off",
                         "types": ["mcq", "classify"],
                         "note": "items that used it carry followup_used=true in responses and scores"},
            "prompts": self._prompt_provenance(),
            "item_sets": self._item_sets(),
            "harness_sha256": {n: sha256_file(EVALS_DIR / n) for n in ("run.py", "scorers.py", "report.py")},
            "scorer_version": (_known(getattr(S, "SCORER_VERSION"), "scorers.SCORER_VERSION")
                               if getattr(S, "SCORER_VERSION", None) else
                               _known(sha256_file(EVALS_DIR / "scorers.py"), "sha256 of scorers.py (no SCORER_VERSION)")),
            "scorer_frozen": getattr(S, "SCORER_FROZEN", None),
            "serving": serving,
            "protocol": getattr(a, "protocol_record", None),
        }

    # -- files
    def _append(self, name: str, rec: Dict[str, Any]) -> None:
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with self.lock:
            with (self.dir / name).open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())

    def prepare(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        rj = self.dir / "run.json"
        if rj.exists() and self.args.rescore:
            # re-scoring only: keep the generation provenance, record the new scoring setup
            old = json.loads(rj.read_text(encoding="utf-8"))
            prot = old.get("protocol") or {}
            changed = self._harness_changes(old)
            if prot.get("mode") == "confirmatory" and changed and not self.args.rescore_exploratory:
                raise SystemExit(f"{self.dir.name}: a confirmatory run's {', '.join(changed)} changed since it was "
                                 "generated; re-scoring would replace its confirmatory scores. Pass "
                                 "--rescore-exploratory to rescore anyway (the run becomes rescored-exploratory and "
                                 "the confirmatory scores are kept in scores.confirmatory.jsonl).")
            if prot.get("mode") == "confirmatory" and (self.dir / "scores.jsonl").exists() \
                    and not (self.dir / "scores.confirmatory.jsonl").exists():
                (self.dir / "scores.confirmatory.jsonl").write_bytes((self.dir / "scores.jsonl").read_bytes())
            # any rescore is exploratory: the scores no longer come from the pre-registered pipeline run
            old["protocol"] = {"mode": "rescored-exploratory",
                               "original": prot.get("original", prot) if prot.get("mode") == "rescored-exploratory"
                               else prot,
                               "checks_failed": prot.get("checks_failed", []), "deviations": prot.get("deviations")}
            old.setdefault("rescored", []).append({"at": utcnow(), "harness": self.config["harness"],
                                                   "git": self.config["git"],
                                                   "scorer_version": self.config.get("scorer_version"),
                                                   "changed": changed})
            old["grader"], old["scorer_overrides"] = self.config["grader"], self.config["scorer_overrides"]
            self.config = old
            self.write_run_json()
            return
        if self.args.rescore:
            raise SystemExit(f"{self.dir.name}: nothing to re-score (no run.json)")
        present = [n for n in ("responses.jsonl", "scores.jsonl", "grades.jsonl") if (self.dir / n).exists()]
        if present and not rj.exists():
            raise SystemExit(f"{self.dir.name}: {', '.join(present)} exist but run.json does not; outputs of unknown "
                             "origin are never resumed. Use a new --run-name (or a new --results root).")
        if rj.exists():
            old = json.loads(rj.read_text(encoding="utf-8"))
            keys = ("model", "thinking", "sampling", "max_tokens", "think_budget")
            if "prompt_cache" in old:   # recorded since 2026-10 (--no-prompt-cache)
                keys += ("prompt_cache",)
            # scorer overrides only change scoring, so they may differ (scores.jsonl is rewritten at the end)
            diff = [k for k in keys if old.get(k) != self.config.get(k)]
            # settings recorded since 2026-10: compared only when the old run recorded them
            if "budget" in old and (old["budget"] or {}).get("policy_sha256") != (self.config["budget"] or {}).get(
                    "policy_sha256"):
                diff.append("budget policy")
            if "followup" in old and (old["followup"] or {}).get("enabled") != self.config["followup"]["enabled"]:
                diff.append("final-answer follow-up")
            old_ds = {d["path"]: d["sha256"] for d in old.get("datasets", [])}
            for d in self.config["datasets"]:
                if d["path"] in old_ds and old_ds[d["path"]] != d["sha256"]:
                    diff.append(f"dataset {d['path']} checksum")
            if diff and not self.args.force_resume:
                raise SystemExit(f"{self.dir.name}: existing run has different {', '.join(diff)}; "
                                 "use a new --run-name (or --force-resume)")
            # protocol integrity: never --force-able. A run's outputs belong to one protocol mode, and a confirmatory
            # run to one harness commit and scorer version (else peeked or mixed outputs could pass as confirmatory).
            old_mode = (old.get("protocol") or {}).get("mode", "exploratory")
            new_mode = (self.config.get("protocol") or {}).get("mode", "exploratory")
            if old_mode != new_mode:
                raise SystemExit(f"{self.dir.name}: existing run is {old_mode}, this one {new_mode}; a run cannot "
                                 "change protocol mode on resume. Use a new --run-name.")
            if new_mode == "confirmatory":
                # resume only the outputs this run recorded when it last stopped (finish writes the hashes)
                rec_out = (old.get("protocol") or {}).get("outputs_sha256") or old.get("outputs_sha256")
                if not rec_out:
                    raise SystemExit(f"{self.dir.name}: a confirmatory run with no recorded output hashes (it did not "
                                     "stop cleanly) cannot be resumed; rerun under a new --results root and log it.")
                if outputs_sha256(self.dir) != {n: rec_out.get(n) for n in OUTPUT_FILES}:
                    raise SystemExit(f"{self.dir.name}: the output files differ from the hashes this confirmatory run "
                                     "recorded; they were changed outside the runner. Not resumed.")
                if (old.get("protocol") or {}).get("invalid"):
                    raise SystemExit(f"{self.dir.name}: this confirmatory run was stopped as invalid "
                                     f"({old['protocol']['invalid']}); rerun under a new --results root.")
            changed = self._harness_changes(old)
            if new_mode == "confirmatory" and changed:
                raise SystemExit(f"{self.dir.name}: a confirmatory run cannot resume across a change of "
                                 f"{', '.join(changed)}. Start a new --run-name and log the deviation.")
            self.config["started"] = old.get("started", utcnow())
            self.config["resumed"] = old.get("resumed", []) + [utcnow()]
            if old.get("git", {}).get("sha") != self.config["git"]["sha"]:
                self.config["git_history"] = old.get("git_history", [old.get("git")]) + [self.config["git"]]
        else:
            self.config["started"] = utcnow()
        self.config["ended"] = None
        self.write_run_json()
        self._ledger("start")

    def _ledger(self, event: str, **extra) -> None:
        """Append one line to the attempt ledger (plans/attempts-C1.jsonl) for a confirmatory run: every attempt,
        complete or not, is on record, so a report can use only the first complete attempt of each run id. Commit
        the ledger after each confirmatory invocation (an uncommitted ledger fails git_dirty on the next one)."""
        prot = self.config.get("protocol") or {}
        if prot.get("mode") != "confirmatory" or self.args.rescore:
            return
        rec = {"event": event, "run_id": self.dir.name, "results_root": rel(Path(self.args.results)),
               "model": self.model, "thinking": self.thinking, "seed": self.seed, "at": utcnow(),
               "started": self.config.get("started"), "git_sha": (self.config.get("git") or {}).get("sha"),
               "frozen_sha": prot.get("frozen_sha"), **extra}
        ATTEMPTS_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with ATTEMPTS_LEDGER.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _harness_changes(self, old: Dict[str, Any]) -> List[str]:
        """What changed between a stored run and this invocation: harness SHA, scorer version."""
        out = []
        if (old.get("git") or {}).get("sha") != (self.config.get("git") or {}).get("sha"):
            out.append("harness SHA")
        ov, nv = (old.get("scorer_version") or {}).get("value"), (self.config.get("scorer_version") or {}).get("value")
        if ov != nv:
            out.append("scorer version")
        return out

    def write_run_json(self) -> None:
        tmp = self.dir / "run.json.tmp"
        tmp.write_text(json.dumps(self.config, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.dir / "run.json")

    def done_ids(self) -> set:
        resp = R.latest_by_id(R.read_jsonl(self.dir / "responses.jsonl"))
        return {i for i, r in resp.items() if not r.get("error")}

    # -- generation
    def request_body(self, item: Dict[str, Any], messages) -> Dict[str, Any]:
        a = self.args
        body: Dict[str, Any] = {"model": self.model, "messages": messages, "stream": False,
                                "max_tokens": max_tokens_for(item, a, self.thinking),
                                **self.sampling, "seed": self.seed}
        if self.thinking in ("on", "off"):
            body["chat_template_kwargs"] = {"enable_thinking": self.thinking == "on"}
        if getattr(a, "no_prompt_cache", False):
            body["cache_prompt"] = False   # llama-server: no KV reuse across requests (A/A determinism)
        return body

    def call(self, item: Dict[str, Any]) -> Dict[str, Any]:
        messages = build_messages(item)
        rec: Dict[str, Any] = {"id": item["id"], "suite": item["suite"], "type": item["type"],
                               "scorer": item["scorer"], "model": self.model, "started": utcnow()}
        if not self.frontier:
            rec["seed"] = self.seed
        body = None if self.frontier else self.request_body(item, messages)
        rec["request"] = {"messages": messages, **({k: v for k, v in body.items() if k not in ("messages",)} if body else {})}
        attempts, last_err = 0, None
        t0 = time.monotonic()
        for attempt in range(self.args.retries + 1):
            if self.stop.is_set():
                last_err = "stopped"
                break
            attempts += 1
            t_try = time.monotonic()
            try:
                data = self.client.chat(messages) if self.frontier else self.client.chat(body)
                rec["latency_s"] = round(time.monotonic() - t_try, 3)
                self._fill(rec, data)
                last_err = rec.get("error")
                if last_err and rec.pop("_retryable", False) and attempt < self.args.retries:
                    rec.pop("error", None)
                    time.sleep(self.args.backoff * (2 ** attempt))
                    continue
                break
            except CallError as exc:
                last_err = str(exc)
                if not exc.retryable or attempt >= self.args.retries:
                    break
                time.sleep(self.args.backoff * (2 ** attempt))
        rec["attempts"] = attempts
        rec["wall_s"] = round(time.monotonic() - t0, 3)
        rec["ended"] = utcnow()
        if last_err:
            rec["error"] = last_err[:500]
        rec.pop("_retryable", None)
        return rec

    def _fill(self, rec: Dict[str, Any], data: Dict[str, Any]) -> None:
        if self.frontier:
            cost = float(data.get("total_cost_usd") or 0.0)
            rec["cost_usd"] = cost
            with self.lock:
                self.cost_usd += cost
            if data.get("is_error") or data.get("subtype") not in (None, "success"):
                rec["error"] = f"frontier error: {str(data.get('result') or data.get('subtype'))[:300]}"
                rec["_retryable"] = False
            usage = data.get("usage") or {}
            mu = data.get("modelUsage") or {}
            served = max(mu, key=lambda k: (mu[k] or {}).get("outputTokens", 0)) if isinstance(mu, dict) and mu else None
            rec.update({
                "content": data.get("result") if isinstance(data.get("result"), str) else "",
                "reasoning_content": None,
                "finish_reason": "stop" if not rec.get("error") else data.get("subtype"),
                "usage": {"prompt_tokens": int(usage.get("input_tokens") or 0)
                          + int(usage.get("cache_read_input_tokens") or 0)
                          + int(usage.get("cache_creation_input_tokens") or 0),
                          "completion_tokens": int(usage.get("output_tokens") or 0)},
                "served_model": served,
                "raw": data,
            })
            return
        ch = data["choices"][0]
        msg = ch.get("message") or {}
        rec.update({
            "content": msg.get("content") or "",
            "reasoning_content": msg.get("reasoning_content"),
            "finish_reason": ch.get("finish_reason"),
            "usage": data.get("usage") or {},
            "timings": data.get("timings"),
            "served_model": data.get("model"),
            "system_fingerprint": data.get("system_fingerprint"),
            "raw": data,
        })

    FINAL_ANSWER_REASONING_CHARS = 6000
    FINAL_ANSWER_PROMPT = ("Stop here. Reply with only your final answer as a single line of the form "
                           "'Answer: X'. No explanation.")

    def final_answer(self, item: Dict[str, Any], rec: Dict[str, Any], scorer_table) -> None:
        """One short follow-up for an mcq/classify reply that was cut off or had no parsable answer (the model
        ran out of tokens while reasoning in the open). The original reply is kept in ``content_before_final``;
        the follow-up's line is appended to ``content``, and the item is flagged ``final_answer_prompt`` so
        reports can show how many scores depended on it. The follow-up always has thinking off. Local models only;
        off with --no-final-answer."""
        if self.frontier or not self.args.final_answer or item["type"] not in ("mcq", "classify") or rec.get("error"):
            return
        sc = S.score_item(rec.get("content") or "", item, scorer_table, finish_reason=rec.get("finish_reason"))
        if rec.get("finish_reason") != "length" and sc.status not in ("unparsed", "unparsed_truncated"):
            return
        # A thinking reply cut off at max_tokens often has empty content and all its work in reasoning_content,
        # which the chat template drops from history; send the tail of the reasoning as the assistant turn instead.
        prior = rec.get("content") or ""
        if not prior.strip() and rec.get("reasoning_content"):
            prior = rec["reasoning_content"][-self.FINAL_ANSWER_REASONING_CHARS:]
            rec["final_answer_context"] = "reasoning"
        msgs = build_messages(item) + [{"role": "assistant", "content": prior},
                                       {"role": "user", "content": self.FINAL_ANSWER_PROMPT}]
        body = self.request_body(item, msgs)
        body["max_tokens"] = 32
        # Always thinking off: with thinking on, the model spends all 32 tokens reasoning and returns no answer.
        body["chat_template_kwargs"] = {"enable_thinking": False}
        try:
            data = self.client.chat(body)
            ans = ((data["choices"][0].get("message") or {}).get("content") or "").strip()
        except Exception as exc:  # the original reply stands; the item is scored as it was
            rec["final_answer_error"] = str(exc)[:200]
            return
        rec["content_before_final"] = rec.get("content") or ""
        rec["final_answer"] = ans
        rec["final_answer_prompt"] = True
        rec["followup_used"] = True
        rec["content"] = (rec.get("content") or "") + "\n" + ans

    def score(self, rec: Dict[str, Any], scorer_table) -> Dict[str, Any]:
        item = self.by_id[rec["id"]]
        sc = S.score_item(rec.get("content") or "", item, scorer_table, finish_reason=rec.get("finish_reason"))
        d = {"id": rec["id"], "suite": rec["suite"], "scorer": item["scorer"], **sc.to_dict()}
        label_source = (item.get("meta") or {}).get("label_source")
        if label_source:
            d["label_source"] = label_source
        if item["scorer"] == "llm_judge":
            d["model_graded"] = sc.status == "ok" and bool(sc.extra.get("model_graded"))
            d["reference_provenance"] = G.reference_provenance(rec["suite"])
            if sc.extra.get("count_as_fail"):
                d["count_as_fail"] = True  # a grader error is a FAIL in the strict accuracy, never a silent drop
        if rec.get("final_answer_prompt"):
            d["final_answer_prompt"] = True
        d.update(protocol_fields(rec))
        return d

    def generate(self) -> Dict[str, int]:
        self.by_id = {it["id"]: it for it in self.items}
        done = self.done_ids()
        todo = [it for it in self.items if it["id"] not in done]
        log(f"[{self.dir.name}] {len(self.items)} items, {len(done & set(self.by_id))} already done, {len(todo)} to run")
        stats = {"ok": 0, "error": 0}
        # non-llm_judge items are scored as they arrive; llm_judge ones in the grading phase
        table = dict(S.SCORERS)

        confirm = (self.config.get("protocol") or {}).get("mode") == "confirmatory" or \
            bool(getattr(self.args, "stop_rules", False))
        max_err = int(0.05 * len(self.items))  # more than 5% of the run's items failing = an invalid run
        ident: Dict[str, Any] = {}
        prior = R.latest_by_id(R.read_jsonl(self.dir / "responses.jsonl"))
        n_err = [sum(1 for r in prior.values() if r.get("error"))]
        for r in prior.values():
            if not r.get("error") and r.get("served_model"):
                ident.setdefault("v", (r.get("served_model"), r.get("system_fingerprint")))

        def stop_invalid(reason: str) -> None:
            with self.lock:
                if not getattr(self, "invalid", None):
                    self.invalid = reason
                    log(f"[{self.dir.name}] STOPPING: {reason} (PROTOCOL C1.12: the run is invalid)")
            self.stop.set()

        def work(it):
            if self.frontier and self.args.frontier_max_usd and self.cost_usd >= self.args.frontier_max_usd:
                self.stop.set()
            if self.stop.is_set():
                return {"id": it["id"], "error": "stopped", "_skip": True}
            rec = self.call(it)
            if confirm and rec.get("error") and rec["error"] != "stopped":
                with self.lock:
                    n_err[0] += 1
                    over = n_err[0] > max_err
                if over:
                    stop_invalid(f"{n_err[0]} of {len(self.items)} items failed (more than 5%)")
            if confirm and not rec.get("error"):
                now = (rec.get("served_model"), rec.get("system_fingerprint"))
                with self.lock:
                    first = ident.setdefault("v", now)
                if first != now:
                    stop_invalid(f"the serving identity changed mid-run: {first} -> {now}")
            self.final_answer(it, rec, table)
            self._append("responses.jsonl", rec)
            if not rec.get("error") and it["scorer"] != "llm_judge":
                self._append("scores.jsonl", self.score(rec, table))
            return rec

        with cf.ThreadPoolExecutor(max_workers=max(1, self.args.concurrency)) as ex:
            futs = {ex.submit(work, it): it for it in todo}
            try:
                for i, f in enumerate(cf.as_completed(futs), 1):
                    rec = f.result()
                    if rec.get("_skip"):
                        continue
                    key = "error" if rec.get("error") else "ok"
                    stats[key] += 1
                    toks = (rec.get("usage") or {}).get("completion_tokens", "?")
                    log(f"[{self.dir.name}] {i}/{len(todo)} {rec['id']}: "
                        f"{'ERROR ' + rec['error'][:120] if rec.get('error') else 'ok'} "
                        f"({rec.get('latency_s', '-')}s, {toks} tok, {rec.get('finish_reason')})")
            except KeyboardInterrupt:
                self.stop.set()
                for f in futs:
                    f.cancel()
                raise
        self._check_samplers()
        return stats

    def _check_samplers(self) -> None:
        """/slots check right after this run's generation (each seed is its own run), while the slot still holds this
        run's last request: a check made after every run would only see the last seed's sampler settings."""
        a = self.args
        sv = self.config.get("serving")
        if isinstance(sv, dict) and "fields" in sv and getattr(a, "probe_server", False) and not a.rescore \
                and hasattr(self.client, "get_json") and (self.dir / "responses.jsonl").exists():
            sv["sampler_check"] = verify_samplers(self.client.get_json, self.client.base, self.model,
                                                  {**self.sampling, "seed": self.seed})
            sv["sampler_check"]["checked_at"] = "end of this run's generation"
            if sv["sampler_check"].get("ok") is False:
                log(f"[{self.dir.name}] WARNING: the server used different samplers than sent: "
                    f"{sv['sampler_check']['mismatch']}")

    def rescore(self, judges: Optional[List[G.Judge]], grade_llm: bool, aggregate: str = "first") -> None:
        """Rewrite scores.jsonl from the latest responses. llm_judge items use the grade cache.
        judges: [(grader model, cached grader)], graded per grading.make_judge_scorer."""
        self.by_id = {it["id"]: it for it in self.items}
        resp = R.latest_by_id(R.read_jsonl(self.dir / "responses.jsonl"))
        table = dict(S.SCORERS)
        if grade_llm:
            table["llm_judge"] = G.make_judge_scorer(judges, aggregate, question_fn=user_content)
        old = R.latest_by_id(R.read_jsonl(self.dir / "scores.jsonl"))
        out = []
        for rid, rec in resp.items():
            if rec.get("error") or rid not in self.by_id:
                continue
            if self.by_id[rid]["scorer"] == "llm_judge" and not grade_llm:
                if rid in old:
                    out.append(old[rid])
                continue
            out.append(self.score(rec, table))
        tmp = self.dir / "scores.jsonl.tmp"
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out), encoding="utf-8")
        tmp.replace(self.dir / "scores.jsonl")

    def finish(self) -> Dict[str, Any]:
        self.config["ended"] = utcnow()
        summ = R.summarize(self.dir)
        resp = R.latest_by_id(R.read_jsonl(self.dir / "responses.jsonl"))
        fps = sorted({r.get("system_fingerprint") for r in resp.values() if r.get("system_fingerprint")})
        served = sorted({r.get("served_model") for r in resp.values() if r.get("served_model")})
        self.config["served"] = {"models": served, "system_fingerprints": fps}
        sv = self.config.get("serving")
        a = self.args
        if isinstance(sv, dict) and "fields" in sv and "sampler_check" not in sv and getattr(a, "probe_server", False) \
                and not a.rescore and hasattr(self.client, "get_json") and resp:
            self._check_samplers()  # a run interrupted before generation ended
        if isinstance(sv, dict) and "fields" in sv and sv["fields"].get("llama_cpp_build", {}).get("value") is None \
                and len(fps) == 1:
            sv["fields"]["llama_cpp_build"] = _known(fps[0], "response system_fingerprint")
        if getattr(self, "invalid", None):
            prot = self.config.get("protocol")
            if isinstance(prot, dict):
                prot["invalid"] = self.invalid
        # bind the record to its outputs: report.py's gate checks these hashes against the files
        outs = outputs_sha256(self.dir)
        self.config["outputs_sha256"] = outs
        prot = self.config.get("protocol")
        if isinstance(prot, dict) and prot.get("mode") == "confirmatory":
            prot["outputs_sha256"] = outs
        self.write_run_json()
        self._ledger("end", outputs_sha256=outs, invalid=getattr(self, "invalid", None))
        tot = {k: sum(s[k] for s in summ["suites"].values()) for k in
               ("n_items", "n_scored", "passed", "api_errors", "truncated", "unparsed", "item_or_grader_errors",
                "completion_tokens", "prompt_tokens")}
        tot["final_answer_prompts"] = sum(s.get("final_answer_prompts", 0) for s in summ["suites"].values())
        tot["cost_usd"] = round(sum(s["cost_usd"] for s in summ["suites"].values()), 4)
        out = {"schema": SCHEMA, "run_dir": self.dir.name, "model": self.model, "thinking": self.config["thinking"],
               "started": self.config["started"], "ended": self.config["ended"], "totals": tot,
               "suites": summ["suites"], "served": self.config["served"],
               "model_graded_suites": sorted(n for n, s in summ["suites"].items() if s["model_graded"])}
        (self.dir / "summary.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        (self.dir / "report.md").write_text(R.render_markdown([R.summarize(self.dir)], f"Eval run {self.dir.name}"),
                                            encoding="utf-8")
        return out


# ------------------------------------------------------------------------------------------- grading
def make_grader(client: Optional[GatewayClient], model: str, template: str, args) -> Optional[S.Grader]:
    """template: the grader prompt text (read once in main, so run.json records the sha of what is used)."""
    if client is None:
        return None

    def grader(item: Dict[str, Any], candidate: str, spec: Dict[str, str]) -> Tuple[str, Dict[str, Any]]:
        cand = candidate[: args.grader_max_chars]
        nonce = secrets.token_hex(8)
        while nonce in cand:  # the wrapper must be unforgeable from inside the candidate
            nonce = secrets.token_hex(8)
        ref = spec["reference"] or "(none given)"
        if spec["reference"] and spec.get("format") == "json":
            ref = "(JSON)\n" + ref
        vals = {"NONCE": nonce, "QUESTION": user_content(item),
                "RUBRIC": spec["rubric"] or "(none given: grade by the reference)", "REFERENCE": ref, "CANDIDATE": cand}
        prompt = re.sub(r"@@([A-Z]+)@@", lambda m: vals.get(m.group(1), m.group(0)), template)
        body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 512,
                "temperature": 0, "seed": args.seed, "stream": False,
                "chat_template_kwargs": {"enable_thinking": False}, "response_format": {"type": "json_object"}}
        # An invalid reply (not the grade JSON) is re-asked a fixed number of times with seeds seed+1, seed+2, ...
        # (deterministic variants); if every reply is invalid the last one is returned and scored as an error.
        rejected: List[Dict[str, Any]] = []
        for k in range(max(0, args.grader_parse_retries) + 1):
            body["seed"] = args.seed + k
            text, meta = _grader_call(client, body, args)
            if G.valid_grade(text) or k == args.grader_parse_retries:
                if rejected:
                    meta["rejected"] = rejected
                meta["parse_attempts"] = k + 1
                return text, meta
            rejected.append({"seed": body["seed"], "raw": text[:500]})
        raise AssertionError("unreachable")

    return grader


def _grader_call(client: GatewayClient, body: Dict[str, Any], args) -> Tuple[str, Dict[str, Any]]:
    """One grader request with the usual network retries."""
    last: Optional[Exception] = None
    for attempt in range(args.retries + 1):
        try:
            data = client.chat(body)
            text = (data["choices"][0].get("message") or {}).get("content") or ""
            return text, {"model": body["model"], "served_model": data.get("model"), "usage": data.get("usage"),
                          "seed": body["seed"], "system_fingerprint": data.get("system_fingerprint")}
        except CallError as exc:
            last = exc
            if not exc.retryable:
                break
            time.sleep(args.backoff * (2 ** attempt))
    raise RuntimeError(str(last))


JUDGE_IDENT: Dict[str, Any] = {}   # judge alias -> first (served_model, fingerprint) seen in this invocation
JUDGE_STOP: Dict[str, Any] = {}    # set when a judge identity changed: every later grade call fails


def cached_grader(run: Run, grader: Optional[S.Grader], grader_cfg: Dict[str, Any]) -> Optional[S.Grader]:
    """Wraps the grader with a per-run cache (grades.jsonl), keyed by item, candidate text and grader config."""
    if grader is None:
        return None
    path = run.dir / "grades.jsonl"
    cache = {r.get("cache_id") or r.get("key"): r for r in R.read_jsonl(path)}
    cfg = grader_cache_cfg(grader_cfg)
    cfg_key = sha256_text(json.dumps(cfg, sort_keys=True))
    # Grades made before the legacy prompt was renamed were cached under the old path. The prompt's sha256 is in
    # the key too, so this alias only ever matches the same prompt text with the same settings.
    alt_keys = [sha256_text(json.dumps({**cfg, "prompt": LEGACY_PROMPT_PATH}, sort_keys=True))]
    if grader_cfg.get("cache_legacy") and "served_identity" in cfg:  # opt-in: grades cached before identity keyed
        bare = grader_cache_cfg(grader_cfg, identity=False)
        alt_keys += [sha256_text(json.dumps(bare, sort_keys=True)),
                     sha256_text(json.dumps({**bare, "prompt": LEGACY_PROMPT_PATH}, sort_keys=True))]

    def g(item, candidate, spec):
        cid = sha256_text(json.dumps([item["id"], candidate, spec, cfg_key]))
        alts = [sha256_text(json.dumps([item["id"], candidate, spec, k])) for k in alt_keys]
        for k in (cid, *alts):
            if k in cache:
                return cache[k]["raw"], cache[k]["meta"]
        if JUDGE_STOP.get("reason"):
            raise RuntimeError(f"grading stopped: {JUDGE_STOP['reason']}")
        raw, meta = grader(item, candidate, spec)
        # the judge's serving identity must not change during grading (confirmatory runs: stop as invalid)
        if (run.config.get("protocol") or {}).get("mode") == "confirmatory" and isinstance(meta, dict):
            now = (meta.get("served_model"), meta.get("system_fingerprint"))
            first = JUDGE_IDENT.setdefault(grader_cfg.get("model"), now)
            if first != now:
                JUDGE_STOP["reason"] = f"the judge's serving identity changed mid-grading: {first} -> {now}"
                log(f"STOPPING grading: {JUDGE_STOP['reason']} (PROTOCOL C1.12: the run is invalid)")
                run.invalid = JUDGE_STOP["reason"]
                raise RuntimeError(JUDGE_STOP["reason"])
        rec = {"cache_id": cid, "id": item["id"], "judge": grader_cfg.get("model"), "raw": raw, "meta": meta,
               "at": utcnow()}
        run._append("grades.jsonl", rec)
        cache[cid] = rec
        return raw, meta

    return g


# The run.json grader keys that change a grade. Only these key the cache, so a rescore with the legacy prompt
# (evals/prompts/llm_judge.v1.md) and --grader-parse-retries 0 reuses grades made before the keys were added.
LEGACY_PROMPT_PATH = "evals/prompts/llm_judge.md"  # where judge-v1 (now llm_judge.v1.md) lived
GRADER_CACHE_KEYS = ("model", "thinking", "temperature", "prompt", "prompt_sha256", "max_candidate_chars")


def grader_cache_cfg(cfg: Dict[str, Any], identity: bool = True) -> Dict[str, Any]:
    out = {k: cfg.get(k) for k in GRADER_CACHE_KEYS}
    if cfg.get("parse_retries"):
        out["parse_retries"] = cfg["parse_retries"]
    if identity and cfg.get("served_identity"):
        out["served_identity"] = cfg["served_identity"]
    return out


def judge_cfg(grader_cfg: Dict[str, Any], model: str) -> Dict[str, Any]:
    """The per-judge config: the shared grader config with this judge as ``model`` (and its served identity)."""
    out = {**grader_cfg, "model": model}
    if isinstance(grader_cfg.get("served_identity"), dict):
        out["served_identity"] = grader_cfg["served_identity"].get(model)
    return out


def grader_identity(model: str, args) -> Optional[Dict[str, Any]]:
    """What actually serves a grader alias, from the serving provenance (static --serving-config first, then the
    llama-swap config): model file, quant and llama.cpp build, the known ones only. None when nothing is known, so
    an alias with no serving record keys the grade cache as before. A changed weight file or quant behind the same
    alias then misses the cache instead of reusing grades another model made."""
    st = static_serving_for(getattr(args, "serving_cfg", None), model) or {}
    entry = llama_swap_entry(model, Path(args.llama_swap_config))
    probe = (getattr(args, "grader_probe", None) or {}).get(model)
    f = resolve_serving(model, st, None, entry, {}, False)["fields"]
    out = {k: f[k]["value"] for k in ("model_file", "quant", "llama_cpp_build") if f[k]["value"] is not None}
    props = (probe or {}).get("props") or {}
    if props.get("model_file"):  # the live server's own answer (--probe-server) decides what is served
        if out.get("model_file") and out["model_file"] != props["model_file"]:
            out["conflict"] = (f"{model}: --serving-config/llama-swap says {out['model_file']!r}, the live server "
                               f"(/props) serves {props['model_file']!r}")
        out["model_file"] = props["model_file"]
        if props.get("build_info"):
            out["llama_cpp_build"] = props["build_info"]
        q = quant_from_filename(props["model_file"])
        if q:
            out["quant"] = q
        out["source"] = "probe /props"
    return out or None


# ------------------------------------------------------------------------------------------- main
def parse_args(argv: Optional[List[str]] = None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--suite", action="append", required=True,
                    help="suite JSONL file, a directory of them, or a suite name under evals/data/ (repeatable)")
    ap.add_argument("--only-suite", action="append", help="keep only these suite names (repeatable)")
    ap.add_argument("--model", action="append", required=True,
                    help="gateway alias (coder, coder-fast, big, vision, hermes), or 'claude' / 'claude:<model>' "
                         "for the frontier baseline; repeatable or comma-separated")
    ap.add_argument("--thinking", default="off",
                    help="off | on | default (send nothing: Qwen thinks) | a comma list such as off,on")
    ap.add_argument("--scorer-override", action="append", default=[], metavar="SUITE=SCORER",
                    help="score a suite with another scorer, e.g. cse-frr=refusal (keyword false-refusal check, "
                         "no grader model needed); repeatable; recorded in run.json")
    ap.add_argument("--limit", type=int, help="first N items per suite (smoke runs)")
    ap.add_argument("--temperature", type=float,
                    help=f"default {THINK_OFF_TEMPERATURE} with thinking off, {THINK_TEMPERATURE} with thinking on/default "
                         "(Qwen's recommendation: greedy decoding makes thinking loop)")
    ap.add_argument("--seed", type=int, default=1234, help="one seed, no dir suffix (default 1234)")
    ap.add_argument("--seeds", help="comma list of seeds, e.g. 1,2,3: one run dir per seed (<dir>-seed<N>), "
                                    "each response records its seed")
    ap.add_argument("--top-p", type=float, help=f"override the profile's top_p (thinking off 1.0, on {THINK_TOP_P})")
    ap.add_argument("--presence-penalty", type=float, help="override the profile's presence_penalty (0)")
    ap.add_argument("--sampler", action="append", default=[], metavar="KEY=VALUE",
                    help=f"override one sampler of the profile; keys: {', '.join(SAMPLER_KEYS)} (repeatable)")
    ap.add_argument("--legacy-sampling", action="store_true",
                    help="send only temperature/top_p (+presence_penalty if given) as before 2026-10, so server "
                         "defaults apply; for resuming or reproducing old runs only")
    ap.add_argument("--max-tokens", type=int, help="flat output cap for every item (smoke runs; replaces the policy)")
    ap.add_argument("--budget-policy", help="JSON token-budget policy (answer_tokens per type, suite_answer_tokens, "
                                            "think_budget); default: BUDGET_POLICY in run.py")
    ap.add_argument("--think-budget", type=int, default=None,
                    help=f"extra max_tokens when thinking is on or default (default: the policy's, "
                         f"{DEFAULT_THINK_BUDGET})")
    ap.add_argument("--concurrency", type=int, default=1, help="parallel requests (default 1: 2 shared GPUs)")
    ap.add_argument("--timeout", type=float, default=900.0, help="seconds per request")
    ap.add_argument("--retries", type=int, default=2, help="retries on network errors, 408/409/429 and 5xx")
    ap.add_argument("--backoff", type=float, default=5.0, help="first retry delay in seconds (doubles)")
    ap.add_argument("--run-name", help="results dir prefix (default: UTC timestamp); reuse it to resume")
    ap.add_argument("--results", default=str(RESULTS_DIR))
    ap.add_argument("--force-resume", action="store_true", help="resume even if the settings changed")
    ap.add_argument("--rescore", action="store_true", help="re-score existing responses; no model calls")
    ap.add_argument("--key-file", default=None, help=f"gateway key file (default {DEFAULT_KEY_FILE})")
    ap.add_argument("--site-env", default=str(REPO_DIR / "site.env"))
    ap.add_argument("--base-url", help="OpenAI-compatible base URL (default https://$SPARK_API_HOST/v1)")
    ap.add_argument("--llama-swap-config", default=str(LLAMA_SWAP_TEMPLATE),
                    help="llama-swap config to record the model's cmd from (default: the repo template)")
    g = ap.add_argument_group("llm_judge grading")
    g.add_argument("--grader-model", default=None,
                   help="gateway alias(es) that grade llm_judge items; required when llm_judge items are graded "
                        "(no default: pick a judge that is not a contestant and not of a contestant's model family). "
                        "A comma list grades with every judge and stores each verdict (see --judge-aggregate)")
    g.add_argument("--grader-cache-legacy", action="store_true",
                   help="also reuse cached grades made before the grader's served identity (model file, quant, "
                        "build) keyed the cache; recorded in run.json")
    g.add_argument("--judge-aggregate", choices=G.AGGREGATES, default="first",
                   help="with several judges: 'first' (the first decides; the rest are stored) or 'majority'")
    g.add_argument("--allow-self-grade", action="store_true",
                   help="let a grader also be a contestant (refused by default; recorded in run.json)")
    g.add_argument("--grader-parse-retries", type=int, default=2,
                   help="re-ask the grader this many times (seed+1, seed+2, ...) when its reply is not valid grade "
                        "JSON; still invalid = a grader error, which counts as FAIL in the strict accuracy")
    g.add_argument("--grader-prompt", default=str(GRADER_PROMPT))
    g.add_argument("--no-grade", action="store_true", help="leave llm_judge items ungraded")
    g.add_argument("--final-answer", action="store_true",
                   help="exploratory only: after a cut-off or unparsable mcq/classify reply, ask once for the answer "
                        "line (thinking off, 32 tokens); such items are marked followup_used")
    g.add_argument("--no-final-answer", action="store_true",
                   help="the default since 2026-10; kept so old commands still work")
    g.add_argument("--grader-max-chars", type=int, default=12000, help="candidate text sent to the grader")
    pr = ap.add_argument_group("protocol and provenance (evals/PROTOCOL.md)")
    pr.add_argument("--protocol", choices=("exploratory", "confirmatory"), default="exploratory",
                    help="confirmatory refuses to start unless the pre-registered conditions hold")
    pr.add_argument("--freeze", action="append", default=[], help="repeatable. JSON manifest of the frozen item sets: {\"files\": {path: sha256}, "
                                     "\"suites\": {suite: {\"n\": n, \"ids_sha256\": sha}}}, or the held-out "
                                     "manifest (datasets/heldout/*.json, suites with \"entries\"); checked before running")
    pr.add_argument("--select-frozen", action="store_true",
                    help="run only the items the --freeze manifest lists by id (e.g. the sealed held-out split)")
    pr.add_argument("--deviation", action="append", default=[], metavar="CHECK_ID=REASON",
                    help="accept one failed confirmatory check, with the reason; one per failed check, recorded in "
                         "run.json (exploratory runs record free text)")
    pr.add_argument("--frozen-ref", help="the git tag or SHA the protocol froze; confirmatory needs HEAD to be it")
    pr.add_argument("--protocol-plan", help=f"the comparisons file (contestants, primaries); default for "
                                            f"confirmatory: {rel(PLAN_C1)}")
    pr.add_argument("--rescore-exploratory", action="store_true",
                    help="allow --rescore of a confirmatory run whose harness or scorer changed; the run becomes "
                         "rescored-exploratory and its confirmatory scores are kept in scores.confirmatory.jsonl")
    pr.add_argument("--intervention", action="append", default=[], metavar="NAME",
                    help=f"apply and check a pinned intervention from {rel(INTERVENTIONS)} (e.g. "
                         "D6-defensive-system-prompt, D8-untuned-flash); recorded in run.json")
    pr.add_argument("--serving-config", help="static JSON with build/quant/flags per model (copied into run.json)")
    pr.add_argument("--probe-server", action="store_true",
                    help="ask the server for /v1/models and llama-swap/llama.cpp /props and record them")
    ap.add_argument("--no-prompt-cache", action="store_true",
                    help="send cache_prompt=false (llama-server: no KV-cache reuse between requests), for "
                         "determinism checks; recorded in run.json. Off by default")
    pr.add_argument("--stop-rules", action="store_true",
                    help="enforce the confirmatory stopping rules (C1.12) in an exploratory run too: stop as invalid "
                         "(exit 4) past 5%% failed items or on a serving-identity change mid-run (used by gate.py)")
    f = ap.add_argument_group("frontier baseline (off unless --model claude)")
    f.add_argument("--yes-frontier", action="store_true", help="confirm the estimated frontier cost")
    f.add_argument("--frontier-cmd", default="claude")
    f.add_argument("--frontier-model", help="--model for the claude CLI (default: its own default)")
    f.add_argument("--frontier-call-usd", default="0.50", help="--max-budget-usd per call")
    f.add_argument("--frontier-max-usd", type=float, default=10.0, help="stop the frontier run past this total")
    f.add_argument("--frontier-extra-args", default="")
    a = ap.parse_args(argv)
    a.models = [m.strip() for spec in a.model for m in spec.split(",") if m.strip()]
    a.thinking_modes = [t.strip() for t in a.thinking.split(",") if t.strip()]
    a.grader_models = [m.strip() for m in (a.grader_model or "").split(",") if m.strip()]
    if (a.grader_model is not None and not a.grader_models) or len(set(a.grader_models)) != len(a.grader_models):
        ap.error("--grader-model: give one or more distinct aliases")
    if a.grader_parse_retries < 0:
        ap.error("--grader-parse-retries >= 0")
    bad = [t for t in a.thinking_modes if t not in ("on", "off", "default")]
    if bad or not a.thinking_modes:
        ap.error(f"--thinking: unknown mode(s) {bad}")
    a.overrides = {}
    for spec in a.scorer_override:
        suite, sep, scorer = spec.partition("=")
        if not sep or not suite.strip() or scorer.strip() not in S.SCORERS:
            ap.error(f"--scorer-override {spec!r}: want SUITE=SCORER with one of {sorted(S.SCORERS)}")
        a.overrides[suite.strip()] = scorer.strip()
    if a.concurrency < 1 or a.retries < 0 or (a.limit is not None and a.limit < 1):
        ap.error("--concurrency >= 1, --retries >= 0, --limit >= 1")
    if a.final_answer and a.no_final_answer:
        ap.error("--final-answer and --no-final-answer conflict")
    a.seeds_given = a.seeds is not None
    try:
        a.seed_list = [int(x) for x in a.seeds.split(",") if x.strip()] if a.seeds_given else [a.seed]
    except ValueError:
        ap.error(f"--seeds {a.seeds!r}: want a comma list of integers")
    if not a.seed_list or len(set(a.seed_list)) != len(a.seed_list):
        ap.error("--seeds: want distinct integers")
    a.seed = a.seed_list[0]  # the grader and anything else that wants one seed
    a.sampler_kv = {}
    for spec in a.sampler:
        k, sep, v = spec.partition("=")
        k = k.strip().replace("-", "_")
        if not sep or k not in SAMPLER_KEYS:
            ap.error(f"--sampler {spec!r}: want KEY=VALUE with KEY one of {', '.join(SAMPLER_KEYS)}")
        try:
            a.sampler_kv[k] = int(v) if k == "top_k" else float(v)
        except ValueError:
            ap.error(f"--sampler {spec!r}: not a number")
    if a.legacy_sampling and a.sampler_kv:
        ap.error("--sampler needs the explicit sampling profiles (drop --legacy-sampling)")
    a.budget_policy = load_budget_policy(a.budget_policy)
    a.think_budget_given = a.think_budget is not None
    if a.think_budget_given:  # part of the recorded policy, so its sha (and resume) sees the change
        a.budget_policy["think_budget"] = a.think_budget
        a.budget_policy["name"] += f"+think_budget={a.think_budget}"
    a.think_budget = a.budget_policy["think_budget"]
    if a.protocol_plan is None and a.protocol == "confirmatory" and PLAN_C1.is_file():
        a.protocol_plan = str(PLAN_C1)
    a.plan = load_plan(a.protocol_plan)
    a.serving_cfg = load_serving_config(a.serving_config)
    return a


def _audit():
    """evals/datasets/audit.py (the held-out key rules), or None when it cannot be imported."""
    try:
        sys.path.insert(0, str(EVALS_DIR / "datasets"))
        import audit  # noqa: E402
        return audit
    except Exception:  # noqa: BLE001 - any import failure: the caller reports it
        return None


def _load_json(path) -> Dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return data


def _listed_ids(want: Dict[str, Any]) -> Optional[Dict[str, Optional[str]]]:
    """id -> held-out content key (None when the manifest lists bare ids)."""
    if isinstance(want.get("ids"), list):
        return {str(i): None for i in want["ids"]}
    if isinstance(want.get("entries"), list):
        out = {}
        for e in want["entries"]:
            parts = str(e).split("\t")
            out[parts[0]] = (parts[1] if len(parts) > 1 else "") or None
        return out
    return None


def _content_keys(want: Dict[str, Any]) -> Dict[str, str]:
    """id -> content binding (heldout-v1.1: a third tab field "id\tkey\tcontent_key", or "content_keys": {id: ck})."""
    out = {str(k): str(v) for k, v in (want.get("content_keys") or {}).items()} \
        if isinstance(want.get("content_keys"), dict) else {}
    for e in want.get("entries") or []:
        parts = str(e).split("\t")
        if len(parts) > 2 and parts[2]:
            out[parts[0]] = parts[2]
    return out


def content_key(audit, item: Dict[str, Any]) -> str:
    """The content binding of a held-out item: audit.heldout_content_key when the data area provides it, else
    '<heldout key>|sha256(normalized prompt + answer)'. Unlike heldout_key (the CVE id for CVE items), it changes
    whenever the prompt or the answer changes."""
    fn = getattr(audit, "heldout_content_key", None)
    if callable(fn):
        return fn(item)
    ans = item.get("answer")
    body = audit.norm_text(str(item.get("prompt", ""))) + "\n" + json.dumps(ans, sort_keys=True, ensure_ascii=False)
    return f"{audit.heldout_key(item)}|{hashlib.sha256(body.encode('utf-8')).hexdigest()}"


def frozen_ids(paths) -> Dict[str, set]:
    """suite -> the frozen ids listed by the freeze manifest(s) ("ids": [...] or held-out "entries")."""
    out: Dict[str, set] = {}
    for path in ([paths] if isinstance(paths, (str, Path)) else paths):
        for suite, want in (_load_json(path).get("suites") or {}).items():
            listed = _listed_ids(want) if isinstance(want, dict) else None
            if listed is not None:  # several manifests (sealed split + the C1 pool): an item must be in every list
                out[suite] = set(listed) if suite not in out else out[suite] & set(listed)
    return out


def check_freeze(paths, datasets, items) -> List[Tuple[str, str]]:
    """(check_id, problem) for the selected data against the freeze manifest(s); empty = matches.
    Held-out entries ("id<TAB>key") are verified by content: the item's key is recomputed with audit.heldout_key
    (sha256 of the normalized content, or the CVE id), so an item edited under the same id fails."""
    paths = [paths] if isinstance(paths, (str, Path)) else list(paths)
    probs: List[Tuple[str, str]] = []
    files: Dict[str, str] = {}
    frozen: Dict[str, Dict[str, Any]] = {}
    lists: Dict[str, List[Dict[str, Optional[str]]]] = {}
    cbind: Dict[str, Dict[str, str]] = {}
    for path in paths:
        try:
            man = _load_json(path)
        except (OSError, ValueError) as exc:
            probs.append(("freeze_unreadable", f"freeze manifest {Path(path).name} unreadable: {exc}"))
            continue
        files.update(man.get("files") or {})
        for suite, want in (man.get("suites") or {}).items():
            if isinstance(want, dict):
                frozen.setdefault(suite, {}).update(want)
                listed = _listed_ids(want)
                if listed is not None:
                    lists.setdefault(suite, []).append(listed)
                cbind.setdefault(suite, {}).update(_content_keys(want))
    have = {d["path"]: d["sha256"] for d in datasets}
    for fpath, sha in files.items():
        got = have.get(fpath) or next((v for k, v in have.items() if Path(k).name == Path(fpath).name), None)
        if got is None:
            continue  # a pinned file this invocation does not select (a subset of the suites) is fine
        elif got != sha:
            probs.append(("freeze_files", f"frozen file {fpath}: sha256 differs"))
    by_suite: Dict[str, List[Dict[str, Any]]] = {}
    for it in items:
        by_suite.setdefault(it["suite"], []).append(it)
    audit = None
    for suite, its in sorted(by_suite.items()):  # a run may cover a subset of the frozen suites, never other items
        want = frozen.get(suite)
        if want is None:
            continue
        got_ids = [it["id"] for it in its]
        n = want.get("n", want.get("n_sealed")) if len(lists.get(suite, [])) <= 1 else None
        if n is not None and n != len(got_ids):
            probs.append(("freeze_ids", f"frozen suite {suite}: {len(got_ids)} items selected, {n} frozen"))
        if want.get("ids_sha256") and want["ids_sha256"] != sha256_text("\n".join(sorted(got_ids))):
            probs.append(("freeze_ids", f"frozen suite {suite}: item ids differ"))
        if suite not in lists:
            continue
        want_ids = set.intersection(*(set(l) for l in lists[suite]))
        if want_ids != set(got_ids):
            probs.append(("freeze_ids", f"frozen suite {suite}: item ids differ from the frozen list"))
        listed = {}
        for l in lists[suite]:
            listed.update({k: v for k, v in l.items() if v})
        keyed = [it for it in its if listed.get(it["id"])]
        if keyed:
            audit = audit or _audit()
            if audit is None:
                probs.append(("freeze_content", "cannot verify held-out content keys: evals/datasets/audit.py "
                                                "is not importable"))
                continue
            bad = [it["id"] for it in keyed if audit.heldout_key(it) != listed[it["id"]]]
            cb = cbind.get(suite, {})
            bad += [it["id"] for it in keyed if it["id"] not in bad and it["id"] in cb
                    and content_key(audit, it) != cb[it["id"]]]
            if bad:
                probs.append(("freeze_content", f"frozen suite {suite}: {len(bad)} item(s) whose content no longer "
                                                f"matches the sealed key, e.g. {', '.join(bad[:3])}"))
            # a CVE item's sealed key is its CVE id, which survives any edit: it needs a content binding (v1.1)
            unbound = [it["id"] for it in keyed if it["id"] not in cb and audit.cve_of(it)]
            if unbound:
                probs.append(("freeze_content", f"frozen suite {suite}: {len(unbound)} CVE item(s) sealed by CVE id "
                                                f"only, with no content binding (use heldout-v1.1), e.g. "
                                                f"{', '.join(unbound[:3])}"))
    if not frozen and not files:
        probs.append(("freeze_unreadable", "freeze manifest(s) hold neither files nor suites"))
    extra = sorted(set(by_suite) - set(frozen)) if frozen else []
    if extra:
        probs.append(("freeze_ids", f"suites not in the freeze manifest: {', '.join(extra)}"))
    return probs


def load_plan(path: Optional[str]) -> Optional[Dict[str, Any]]:
    """The comparisons file a run belongs to (contestants and primaries), with its sha256."""
    if not path:
        return None
    p = Path(path)
    try:
        data = _load_json(p)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"--protocol-plan {p.name}: {exc}") from None
    return {"path": rel(p), "sha256": sha256_file(p), "contestants": list(data.get("contestants") or []),
            "id": data.get("protocol"),
            "suites": sorted({c.get("suite") for f in data.get("families") or [] for c in f.get("comparisons") or []
                              if c.get("suite")}),
            "run_interventions": data.get("run_interventions"),
            "scorer_overrides": data.get("scorer_overrides") or {}}


def _families_table() -> Dict[str, Any]:
    try:
        return _load_json(MODEL_FAMILIES)
    except (OSError, ValueError):
        return {}


def model_families(model: str, table: Optional[Dict[str, Any]] = None) -> Optional[set]:
    """The family tags of a model alias from model_families.json, or None when unknown."""
    t = _families_table() if table is None else table
    ent = (t.get("models") or {}).get(model)
    if isinstance(ent, dict) and ent.get("families"):
        return set(ent["families"])
    for pat in t.get("patterns") or []:
        try:
            if re.search(pat["regex"], model, re.I):
                return set(pat["families"])
        except (re.error, KeyError, TypeError):
            continue
    return None


def _served_names(model: str, args=None) -> List[str]:
    """The served model file name behind an alias (static --serving-config, else the llama-swap config), so a family
    can be resolved from what is actually served, not only from the alias."""
    if args is None:
        return []
    ident = grader_identity(model, args) or {}
    return [ident["model_file"]] if ident.get("model_file") else []


def family_pairs(graders: List[str], contestants: List[str], args=None) -> List[Dict[str, Any]]:
    """(grader, contestant) pairs that are not independent: same alias, shared family, or unknown family (fail
    closed). grading.family_overlap (the judge area's) when present, else model_families.json read here. With args,
    each side is also resolved through its served model file, and the union of the findings is returned (an alias
    renamed to hide its lineage is still caught by its weights file)."""
    fn = getattr(G, "family_overlap", None)
    if callable(fn):
        if args is None:
            return list(fn(graders, contestants))
        idents = {m: grader_identity(m, args) for m in list(graders) + list(contestants)}
        idents = {m: v for m, v in idents.items() if v}
        conflicts = [(m, v["conflict"]) for m, v in idents.items() if v.get("conflict") and m in graders]
        if conflicts:  # the static record and the live server disagree about what is served: fail closed
            return [{"grader": m, "candidate": c, "reason": "identity_conflict", "conflict": [msg]}
                    for m, msg in conflicts for c in contestants]
        try:  # the judge area's resolver: served weights decide the family; alias vs file disagreement fails closed
            return list(fn(graders, contestants, identities=idents))
        except TypeError:  # an older grading.py without identities
            return list(fn(graders, contestants))
    table = _families_table()
    out = []
    for g in graders:
        fg = model_families(g, table)
        for c in contestants:
            fc = model_families(c, table)
            if g.lower() == c.lower():
                out.append({"grader": g, "candidate": c, "reason": "self"})
            elif fg is None or fc is None:
                out.append({"grader": g, "candidate": c, "reason": "unknown"})
            elif fg & fc:
                out.append({"grader": g, "candidate": c, "reason": "family", "shared": sorted(fg & fc)})
    return out


def family_overlap(graders: List[str], contestants: List[str], args=None) -> List[str]:
    """Graders that are not independent of some contestant (see family_pairs)."""
    return sorted({p["grader"] for p in family_pairs(graders, contestants, args)})


# Every confirmatory check has a stable id; a failed check is accepted only with "--deviation <id>=<reason>".
CHECK_IDS = (
    "git_sha_unknown", "git_dirty", "git_untracked", "frozen_ref", "limit", "max_tokens", "final_answer",
    "legacy_sampling", "force_resume", "sampler_override", "scorer_override", "think_budget", "budget_policy",
    "allow_self_grade", "grader_contestant", "grader_family", "thinking_greedy", "seeds", "freeze_missing",
    "freeze_unreadable", "freeze_files", "freeze_ids", "freeze_content", "files_unpinned", "plan_missing",
    "plan_unfrozen", "serving_unrecorded", "sealed_only", "freeze_untracked", "freeze_unstripped", "intervention",
    "harness_files", "bytecode", "freeze_conflict", "stage2_missing", "judge_pin", "serving_pin")
# Integrity checks define what "confirmatory" means; no --deviation can accept them (PROTOCOL C1.12).
NON_WAIVABLE = frozenset({
    # integrity: what "confirmatory" means
    "git_sha_unknown", "git_dirty", "git_untracked", "frozen_ref", "harness_files", "bytecode", "freeze_missing",
    "freeze_unreadable", "freeze_files", "freeze_ids", "freeze_content", "freeze_untracked", "freeze_unstripped",
    "freeze_conflict", "files_unpinned", "plan_missing", "plan_unfrozen", "sealed_only", "stage2_missing",
    "serving_pin",
    # estimand: a run that fails one of these measures something other than what was registered
    "scorer_override", "budget_policy", "think_budget", "max_tokens", "sampler_override", "legacy_sampling",
    "thinking_greedy", "seeds", "final_answer", "intervention", "allow_self_grade", "grader_contestant",
    "grader_family", "judge_pin"})
# Waivable (each by its own --deviation): limit (it fails freeze_ids anyway), force_resume, serving_unrecorded.
# A stage-2 commit (PROTOCOL C1.3, the prospective data manifest) may add files under this path and nothing else.
STAGE2_PREFIX = "evals/plans/stage2/"
FREEZE_PATHS = ("evals/plans/freeze-C1.json",)   # added once, after the harness commit, by make_c1.py freeze
# The analysis-defining files: their sha256 are pinned in the freeze manifest (harness_sha256) and checked here.
HARNESS_FILES = ("evals/run.py", "evals/scorers.py", "evals/report.py", "evals/stats.py", "evals/grading.py",
                 "evals/datasets/audit.py", "evals/prompts/llm_judge.md", "evals/prompts/model_families.json",
                 "evals/plans/c1-interventions.json", "evals/plans/comparisons-C1.json")
HELDOUT_MANIFEST_V11 = EVALS_DIR / "datasets" / "heldout" / "heldout-v1.1.json"


def git_changed(base: str, head: str) -> Optional[List[str]]:
    """Paths that differ between two commits, or None when git cannot tell."""
    st = git_diff_status(base, head)
    return None if st is None else [p for _, p in st]


def git_diff_status(base: str, head: str) -> Optional[List[Tuple[str, str]]]:
    """[(status letter, path)] between two commits (A added, M modified, D deleted, ...), or None."""
    try:
        out = subprocess.run(["git", "diff", "--name-status", "--no-renames", base, head], cwd=REPO_DIR,
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return [(l.split("\t", 1)[0][:1], l.split("\t", 1)[1]) for l in out.stdout.splitlines() if "\t" in l]


def git_touch_count(base: str, head: str, path: str) -> Optional[int]:
    """How many commits in base..head touch a path."""
    try:
        out = subprocess.run(["git", "rev-list", "--count", f"{base}..{head}", "--", path], cwd=REPO_DIR,
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return int(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip().isdigit() else None


def git_deleted_lines(base: str, head: str, path: str) -> Optional[int]:
    """Lines removed from a path by any commit in base..head (an append-only file must have none)."""
    try:
        out = subprocess.run(["git", "log", "--numstat", "--format=", f"{base}..{head}", "--", path], cwd=REPO_DIR,
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return sum(int(l.split("\t")[1]) for l in out.stdout.splitlines() if l.split("\t")[1].isdigit())


def post_freeze_problems(frozen_sha: str, head_sha: str) -> Tuple[List[str], List[str]]:
    """What may change between the harness commit (the manifest's harness_ref) and HEAD: only ADDING the C1 freeze
    manifest (once), ADDING files under the stage-2 prefix (each once), and APPENDING to the attempt ledger. Returns
    (problems, stage-2 paths)."""
    st = git_diff_status(frozen_sha, head_sha)
    if st is None:
        return [f"git cannot compare the frozen commit {frozen_sha[:12]} with HEAD"], []
    probs, stage2 = [], []
    for code, path in st:
        if path == rel(ATTEMPTS_LEDGER):
            if git_deleted_lines(frozen_sha, head_sha, path):
                probs.append(f"{path}: lines were removed (the attempt ledger is append-only)")
            continue
        if code != "A" or not (path in FREEZE_PATHS or path.startswith(STAGE2_PREFIX)):
            probs.append(f"{path} ({'added' if code == 'A' else 'changed'} after the harness commit)")
            continue
        if (git_touch_count(frozen_sha, head_sha, path) or 0) > 1:
            probs.append(f"{path} was rewritten after it was first committed")
        if path.startswith(STAGE2_PREFIX):
            stage2.append(path)
    return probs, stage2


def manifest_problem(path: str, frozen_sha: Optional[str], head_sha: Optional[str]) -> Optional[str]:
    """Why a freeze manifest is not frozen: it must be inside the repo, committed and byte-identical at HEAD, and
    either already part of the harness commit (the sealed manifest) or one of the post-freeze paths (the C1 freeze
    manifest, stage-2 manifests), whose history post_freeze_problems checks. None = fine."""
    p = Path(path).resolve()
    try:
        relp = str(p.relative_to(REPO_DIR))
    except ValueError:
        return f"{Path(path).name} is outside the repo (not committed)"
    if not head_sha:
        return f"{relp}: no HEAD to check it against"
    try:
        blob = subprocess.run(["git", "show", f"{head_sha}:{relp}"], cwd=REPO_DIR, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return f"{relp}: git unavailable"
    if blob.returncode != 0:
        return f"{relp} is not committed"
    if blob.stdout != p.read_bytes():
        return f"{relp} differs from its committed version"
    if relp in FREEZE_PATHS or relp.startswith(STAGE2_PREFIX):
        return None
    if not frozen_sha:
        return f"{relp}: no frozen commit to check it against"
    old = subprocess.run(["git", "show", f"{frozen_sha}:{relp}"], cwd=REPO_DIR, capture_output=True, timeout=10)
    if old.returncode != 0 or old.stdout != p.read_bytes():
        return f"{relp} is not part of the frozen harness commit {frozen_sha[:12]}"
    return None


def in_repo(path) -> bool:
    try:
        Path(path).resolve().relative_to(REPO_DIR)
        return True
    except ValueError:
        return False


def harness_sha256() -> Dict[str, str]:
    """sha256 of the files that define the analysis (pinned in the freeze manifest, checked at run time)."""
    return {f: sha256_file(REPO_DIR / f) for f in HARNESS_FILES if (REPO_DIR / f).is_file()}


def pycache_dirs() -> List[str]:
    return sorted(rel(p) for p in EVALS_DIR.rglob("__pycache__"))


def _sealed_manifests(a) -> List[str]:
    sealed = {HELDOUT_MANIFEST.resolve(), HELDOUT_MANIFEST_V11.resolve()}
    return [f for f in a.freeze or [] if Path(f).resolve() in sealed]


def _freeze_value(a, key: str) -> Any:
    for path in a.freeze or []:
        try:
            v = _load_json(path).get(key)
        except (OSError, ValueError):
            continue
        if v is not None:
            return v
    return None


def protocol_checks(a, datasets, items, git: Dict[str, Any], plan: Optional[Dict[str, Any]] = None,
                    n_judge: int = 0) -> List[Dict[str, str]]:
    """Pre-registered conditions for a confirmatory run (PROTOCOL.md C1.12 and the README). Returns the failed
    ones as {"id", "msg"}. Exploratory runs are not checked, only labelled."""
    f: List[Dict[str, str]] = []

    def fail(cid: str, msg: str) -> None:
        f.append({"id": cid, "msg": msg})

    if git.get("sha") in (None, "", "unknown"):
        fail("git_sha_unknown", "harness git SHA unknown")
    if git.get("dirty"):
        fail("git_dirty", "harness working tree is dirty (commit the harness first)")
    if git.get("dirty"):
        f[-1]["msg"] += ": " + ", ".join(git.get("dirty_files") or [])[:300]
    if git.get("untracked"):
        fail("git_untracked", "untracked files in the repo (they could change the harness without a commit): "
             + ", ".join(git.get("untracked_files") or [])[:300])
    # The freeze manifest's harness_ref is the frozen harness commit: a full 40-hex SHA (a tag or branch would be
    # resolved at run time and could move). --frozen-ref, when given, must name the same commit.
    man_ref, flag_ref = _freeze_value(a, "harness_ref"), a.frozen_ref
    frozen_sha = None
    if not man_ref:
        fail("frozen_ref", "the freeze manifest has no harness_ref (the frozen harness commit SHA)")
    elif not re.fullmatch(r"[0-9a-f]{40}", str(man_ref)):
        fail("frozen_ref", f"harness_ref {man_ref!r} is not a full commit SHA (tags and branches can move)")
    else:
        frozen_sha = git_rev(man_ref)
        if frozen_sha != man_ref:
            fail("frozen_ref", f"harness_ref {man_ref[:12]} is not a commit of this repository")
            frozen_sha = None
    if flag_ref and frozen_sha and git_rev(flag_ref) != frozen_sha:
        fail("frozen_ref", f"--frozen-ref {flag_ref} is not the manifest's harness_ref {man_ref[:12]}")
    a.frozen_sha = frozen_sha
    if frozen_sha and frozen_sha != git.get("sha"):
        probs, stage2 = post_freeze_problems(frozen_sha, git.get("sha") or "HEAD")
        if probs:
            fail("frozen_ref", "HEAD differs from the frozen harness commit beyond the allowed freeze, stage-2 and "
                               "ledger additions: " + "; ".join(probs[:5]))
        a.stage2 = stage2 or None
    pinned = _freeze_value(a, "harness_sha256")
    if not isinstance(pinned, dict) or not pinned:
        fail("harness_files", "the freeze manifest pins no harness_sha256 (the analysis-defining files)")
    else:
        now = harness_sha256()
        bad = sorted(k for k, v in pinned.items() if now.get(k) != v)
        missing = sorted(set(HARNESS_FILES) - set(pinned))
        if bad or missing:
            fail("harness_files", "running harness files differ from the frozen pins: "
                 + ", ".join(bad + [f"{m} (not pinned)" for m in missing])[:300])
    pyc = pycache_dirs()
    if pyc:
        fail("bytecode", f"__pycache__ under evals/ ({', '.join(pyc[:3])}); remove it (find evals -name __pycache__ "
                         "-exec rm -rf {} +) so no compiled file can stand in for the frozen source")
    if a.limit is not None:
        fail("limit", "--limit selects a subset")
    if a.max_tokens:
        fail("max_tokens", "--max-tokens replaces the declared budget policy")
    if a.final_answer:
        fail("final_answer", "--final-answer is exploratory only")
    if a.legacy_sampling:
        fail("legacy_sampling", "--legacy-sampling lets server sampler defaults apply")
    if a.force_resume:
        fail("force_resume", "--force-resume can mix settings within a run")
    ov = sampler_overrides(a)
    if ov:
        fail("sampler_override", f"sampler overrides {sorted(ov)} replace the declared profile")
    cli_ov = getattr(a, "cli_scorer_overrides", a.overrides)
    if cli_ov:
        fail("scorer_override", f"--scorer-override for {sorted(cli_ov)} (only the plan's scorer_overrides apply)")
    if a.think_budget_given:
        fail("think_budget", "--think-budget replaces the policy's thinking budget")
    frozen_pol = _freeze_value(a, "budget_policy_sha256") or sha256_text(json.dumps(BUDGET_POLICY, sort_keys=True))
    if sha256_text(json.dumps(a.budget_policy, sort_keys=True)) != frozen_pol:
        fail("budget_policy", "the token-budget policy is not the frozen one")
    if a.allow_self_grade:
        fail("allow_self_grade", "--allow-self-grade")
    if n_judge and not a.no_grade and a.grader_models:
        contestants = sorted(set(a.models) | set((plan or {}).get("contestants") or []))
        same = G.self_grade_overlap(a.grader_models, contestants)
        if same:
            fail("grader_contestant", f"grader(s) {', '.join(same)} are contestants in the protocol")
        pairs = family_pairs(a.grader_models, contestants, a)
        if pairs:
            why = "; ".join(sorted({f"{p['grader']} vs {p['candidate']}: {p['reason']}"
                                    + (f" ({', '.join(map(str, p.get('conflict') or p.get('shared') or p.get('unknown') or []))})"
                                       if p.get('conflict') or p.get('shared') or p.get('unknown') else "")
                                    for p in pairs}))
            fail("grader_family", "the judge is not independent of the contestants: " + why[:400])
    if any(t in ("on", "default") and sampling_for(a, t)["temperature"] == 0 for t in a.thinking_modes):
        fail("thinking_greedy", "thinking on with temperature 0 (greedy reasoning loops)")
    if any(sampling_for(a, t)["temperature"] > 0 for t in a.thinking_modes) and len(a.seed_list) < 3:
        fail("seeds", "sampled decoding (temperature > 0) needs at least 3 seeds (--seeds)")
    if not a.freeze:
        fail("freeze_missing", "no --freeze manifest of the item sets")
    else:
        for cid, msg in check_freeze(a.freeze, datasets, items):
            fail(cid, msg)
        for path in a.freeze:
            why = manifest_problem(path, frozen_sha, git.get("sha"))
            if why:
                fail("freeze_untracked", f"freeze manifest not frozen: {why}")
        _sealed_check(a, items, fail)
        _unstripped_check(items, fail)
        pinned = {}
        for path in a.freeze:
            try:
                pinned.update(_load_json(path).get("files") or {})
            except (OSError, ValueError):
                pass
        names = {Path(k).name for k in pinned}
        loose = [d["path"] for d in datasets if d["path"] not in pinned and Path(d["path"]).name not in names]
        if loose:
            fail("files_unpinned", f"data file(s) not pinned by sha256 in a freeze manifest: {', '.join(loose)}")
    if plan is None:
        fail("plan_missing", "no --protocol-plan comparisons file")
    else:
        want = _freeze_value(a, "comparisons_sha256")
        if not want or want != plan["sha256"]:
            fail("plan_unfrozen", "the comparisons file's sha256 is not the one frozen in the freeze manifest")
    if not a.probe_server:
        fail("serving_unrecorded", "--probe-server is required (it records the live serving config and proves, "
                                   "through /slots, that the samplers were applied)")
    for msg in getattr(a, "intervention_problems", []) or []:
        fail("intervention", msg)
    if a.freeze:
        for msg in manifest_conflicts(a.freeze):
            fail("freeze_conflict", msg)
        if plan and any(su.endswith("-prospective") for su in plan.get("suites") or []) \
                and not any(_is_stage2(p) for p in a.freeze):
            fail("stage2_missing", "the plan has prospective comparisons, so every C1 run happens after stage 2 "
                                   "(PROTOCOL C1.3): pass the committed stage-2 manifest with --freeze")
    # each run name of the plan has a declared intervention set; a comparison side cannot be an unmarked rename
    if plan is not None and plan.get("run_interventions") is not None:
        want = plan["run_interventions"].get(a.run_name or "")
        if want is None:
            fail("intervention", f"run name {a.run_name!r} is not one the plan declares "
                                 f"({', '.join(sorted(plan['run_interventions']))})")
        elif sorted(want) != sorted(a.intervention or []):
            fail("intervention", f"run name {a.run_name!r} needs --intervention {sorted(want)} "
                                 f"(got {sorted(a.intervention or [])})")
    _judge_pin_check(a, n_judge, fail)
    sv_pin = _freeze_value(a, "serving_config_sha256")
    if not a.serving_config:
        fail("serving_pin", "no --serving-config (the static serving record must be in the repo and pinned)")
    else:
        sp = Path(a.serving_config).resolve()
        if not in_repo(sp):
            fail("serving_pin", f"--serving-config {sp.name} is outside the repo (keep it in the repo, e.g. "
                                "evals/plans/serving-C1.json, committed and pinned)")
        if not sv_pin or sv_pin != sha256_file(sp):
            fail("serving_pin", "--serving-config does not match the freeze manifest's serving_config_sha256")
    return f


def _judge_pin_check(a, n_judge: int, fail) -> None:
    """judge_pin: the judge alias, prompt sha256, candidate cap and served weights are pinned in the freeze."""
    if not n_judge or a.no_grade:
        return
    pin = _freeze_value(a, "judge")
    if not isinstance(pin, dict):
        fail("judge_pin", "the freeze manifest pins no judge (alias, prompt_sha256, max_candidate_chars, model_file)")
        return
    if a.grader_models != [pin.get("alias")]:
        fail("judge_pin", f"--grader-model {','.join(a.grader_models)} is not the pinned judge {pin.get('alias')}")
    try:
        psha = sha256_file(Path(a.grader_prompt))
    except OSError:
        psha = None
    if psha != pin.get("prompt_sha256"):
        fail("judge_pin", f"the grader prompt {Path(a.grader_prompt).name} is not the pinned one")
    if a.grader_max_chars != pin.get("max_candidate_chars"):
        fail("judge_pin", f"--grader-max-chars {a.grader_max_chars} is not the pinned {pin.get('max_candidate_chars')}")
    ident = grader_identity(pin.get("alias") or "", a) or {}
    if pin.get("model_file") and ident.get("model_file") != pin.get("model_file"):
        fail("judge_pin", f"the judge serves {ident.get('model_file')!r}, not the pinned {pin.get('model_file')!r}")
    if ident.get("conflict"):
        fail("judge_pin", f"judge identity conflict: {ident['conflict']}")


def apply_interventions(a, items: List[Dict[str, Any]]) -> None:
    """--intervention NAME: a pinned change from plans/c1-interventions.json. system_prompt: every selected item gets
    the pinned system prompt (a run option, so the data files and their pins are unchanged); its text must hash to the
    pinned sha256. server_cmd: the llama-swap entry that serves the alias (--llama-swap-config, a copy of the live
    file) must hash to the pinned cmd sha256."""
    table = _load_json(INTERVENTIONS).get("interventions") or {}
    a.interventions_record, a.intervention_problems = [], []
    for name in a.intervention:
        iv = table.get(name)
        if not isinstance(iv, dict):
            raise ValueError(f"unknown intervention {name!r} (one of {', '.join(sorted(table))})")
        rec = {"name": name, "kind": iv.get("kind")}
        if iv.get("kind") == "system_prompt":
            text = iv.get("text") or ""
            rec["sha256"] = sha256_text(text)
            if rec["sha256"] != iv.get("sha256"):
                a.intervention_problems.append(f"{name}: the prompt text does not hash to its pinned sha256")
            for it in items:
                it["system"] = text
        elif iv.get("kind") == "server_cmd":
            entry = llama_swap_entry(iv.get("llama_swap_alias", ""), Path(a.llama_swap_config))
            rec["cmd_sha256"] = sha256_text(entry["cmd"]) if entry else None
            rec["llama_swap_config"] = rel(Path(a.llama_swap_config))
            if rec["cmd_sha256"] != iv.get("cmd_sha256"):
                a.intervention_problems.append(
                    f"{name}: the llama-swap cmd for {iv.get('llama_swap_alias')} in {Path(a.llama_swap_config).name} "
                    "is not the pinned one (pass a copy of the live config with --llama-swap-config)")
        else:
            raise ValueError(f"intervention {name!r}: unknown kind {iv.get('kind')!r}")
        a.interventions_record.append(rec)


STAGE2_KEYS = {"stage", "protocol", "note", "suites", "files", "window", "calibration_verdict"}
STAGE2_CALIBRATION_KEYS = {"path", "sha256"}   # report.py reads plan-independent {"calibration_verdict": {path, sha256}}
SCALAR_PINS = ("comparisons_sha256", "harness_ref", "budget_policy_sha256", "harness_sha256", "code_sha256", "judge",
               "serving_config_sha256", "analysis")


def _is_stage2(path) -> bool:
    try:
        return str(Path(path).resolve().relative_to(REPO_DIR)).startswith(STAGE2_PREFIX)
    except ValueError:
        return False


def manifest_conflicts(paths: List[str]) -> List[str]:
    """freeze_conflict: a stage-2 manifest may only add prospective suites (nvdp- ids), their files, a window and a
    calibration-verdict pin of fixed schema; it cannot carry comparisons, harness, budget, judge or analysis pins.
    Two manifests may not disagree on a pin, a file hash or (outside the sealed manifest) a suite's id list."""
    probs: List[str] = []
    seen: Dict[str, Any] = {}
    files: Dict[str, str] = {}
    suites: Dict[str, Any] = {}
    sealed = {str(HELDOUT_MANIFEST.resolve()), str(HELDOUT_MANIFEST_V11.resolve())}
    for path in paths:
        try:
            man = _load_json(path)
        except (OSError, ValueError):
            continue
        name = Path(path).name
        if _is_stage2(path) or man.get("stage") == 2:
            extra = sorted(set(man) - STAGE2_KEYS)
            if extra:
                probs.append(f"stage-2 manifest {name} carries {', '.join(extra)} (only prospective suites, their "
                             "files, a window and a calibration pin are allowed)")
            for suite, want in (man.get("suites") or {}).items():
                listed = (_listed_ids(want) or {}) if isinstance(want, dict) else {}
                if not suite.endswith("-prospective") or any(not str(i).startswith("nvdp-") for i in listed):
                    probs.append(f"stage-2 manifest {name} pins suite {suite} (only *-prospective suites with nvdp- ids)")
            for fpath in man.get("files") or {}:
                if not any(t in Path(fpath).name.casefold() for t in ("-prospective", "calibration")):
                    probs.append(f"stage-2 manifest {name} pins {fpath} (only prospective data or calibration files)")
            cal = man.get("calibration_verdict")
            if cal is not None and (not isinstance(cal, dict) or set(cal) != STAGE2_CALIBRATION_KEYS):
                probs.append(f"stage-2 manifest {name}: calibration_verdict must be exactly "
                             f"{sorted(STAGE2_CALIBRATION_KEYS)}")
        for k in SCALAR_PINS:
            if k in man:
                if k in seen and seen[k][1] != man[k]:
                    probs.append(f"{k} differs between {seen[k][0]} and {name}")
                seen.setdefault(k, (name, man[k]))
        for fpath, sha in (man.get("files") or {}).items():
            if fpath in files and files[fpath] != sha:
                probs.append(f"file pin {fpath} differs between manifests")
            files.setdefault(fpath, sha)
        if str(Path(path).resolve()) in sealed:
            continue
        for suite, want in (man.get("suites") or {}).items():
            listed = sorted((_listed_ids(want) or {}).keys()) if isinstance(want, dict) else None
            if suite in suites and listed is not None and suites[suite] is not None and suites[suite] != listed:
                probs.append(f"suite {suite} lists different ids in two manifests")
            suites.setdefault(suite, listed)
    return probs


def verify_interventions_live(a, client) -> None:
    """D8 (server_cmd): what the live server can tell about itself (llama.cpp /props: the model file) must match the
    pinned cmd. The other flags (MTP draft head, CPU expert layers) are not reported by /props, so beyond the model
    file the check rests on the --llama-swap-config copy the operator passes: honour-system, disclosed in C1.5."""
    table = _load_json(INTERVENTIONS).get("interventions") or {}
    for rec in getattr(a, "interventions_record", None) or []:
        iv = table.get(rec["name"]) or {}
        if iv.get("kind") != "server_cmd":
            continue
        m = re.search(r"(?:^|\s)-m\s+(\S+)", iv.get("cmd") or "")
        want = Path(m.group(1)).name if m else None
        props = (probe_serving(client.get_json, client.base, iv.get("llama_swap_alias", "")) or {}).get("props") or {}
        got = props.get("model_file")
        rec["live_model_file"] = got
        rec["live_check"] = "model file via /props; other flags honour-system"
        if got is None:
            rec["live_check"] = "not available (/props unreachable); honour-system"
        elif want and got != want:
            a.intervention_problems.append(f"{rec['name']}: the live server serves {got}, the pinned cmd {want}")


def _sealed_check(a, items, fail) -> None:
    """sealed_only: every selected item is a sealed held-out item (or a prospective item pinned by id), and none is
    a near-copy of a development item (meta.rigor.dev_neardup_exposed)."""
    sealed = _sealed_manifests(a)
    if not sealed:
        fail("sealed_only", f"the sealed manifest ({rel(HELDOUT_MANIFEST)} or its v1.1) is not among --freeze")
        return
    ids: Dict[str, set] = {}
    for path in sealed:
        for suite, want in (_load_json(path).get("suites") or {}).items():
            if isinstance(want, dict):
                ids.setdefault(suite, set()).update(_listed_ids(want) or {})
    pros: Dict[str, set] = {}
    windows: Dict[str, Any] = {}
    for path in a.freeze or []:
        if not _is_stage2(path):
            continue
        man = _load_json(path)
        for suite, want in (man.get("suites") or {}).items():
            if isinstance(want, dict) and suite.endswith("-prospective"):
                pros.setdefault(suite, set()).update(_listed_ids(want) or {})
                windows[suite] = man.get("window")
    out, exposed = [], []
    for it in items:
        if it["suite"].endswith("-prospective"):
            # prospective items: pinned by a stage-2 manifest, in the nvdp- namespace, inside the declared window
            why = None
            if it["id"] not in pros.get(it["suite"], ()):
                why = "not listed by a stage-2 manifest"
            elif not str(it["id"]).startswith("nvdp-"):
                why = "outside the nvdp- id namespace"
            else:
                w = windows.get(it["suite"])
                meta = it.get("meta") or {}
                day = str(meta.get("first_public_date") or meta.get("published") or "")[:10]
                if not (isinstance(w, list) and len(w) == 2 and day and w[0] <= day <= w[1]):
                    why = f"first-public date {day or '?'} outside the declared window {w}"
            if why:
                out.append(f"{it['id']} ({why})")
            elif ((it.get("meta") or {}).get("rigor") or {}).get("dev_neardup_exposed"):
                exposed.append(it["id"])
            continue
        if it["id"] not in ids.get(it["suite"], ()):
            out.append(it["id"])
        elif ((it.get("meta") or {}).get("rigor") or {}).get("dev_neardup_exposed"):
            exposed.append(it["id"])
    if out:
        fail("sealed_only", f"{len(out)} selected item(s) are not sealed, e.g. {', '.join(out[:3])}")
    if exposed:
        fail("sealed_only", f"{len(exposed)} sealed item(s) are near-copies of development items "
                            f"(dev_neardup_exposed), e.g. {', '.join(exposed[:3])}")


def _unstripped_check(items, fail) -> None:
    """freeze_unstripped: an nvd-* item whose text still carries a strippable key leak comes from an unstripped file
    (items the data rules could not strip are flagged "unstrippable" and stay, for the no_leak exclusion)."""
    nvd = [it for it in items if it["suite"].startswith("nvd-")]
    if not nvd:
        return
    audit = _audit()
    if audit is None or not hasattr(audit, "leak_flags"):
        fail("freeze_unstripped", "cannot check nvd-* items for unstripped leaks: audit.py is not importable")
        return
    bad = []
    for it in nvd:
        rig = (it.get("meta") or {}).get("rigor") or {}
        if "unstrippable" in (rig.get("leak_stripped") or []) or "unstrippable" in (rig.get("leak") or []):
            continue
        kinds = audit.STRIPPABLE.get(audit.task_kind(it), ())
        if [x for x in audit.leak_flags(it) if x in kinds]:
            bad.append(it["id"])
    if bad:
        fail("freeze_unstripped", f"{len(bad)} nvd-* item(s) still state their answer (an unstripped file), "
                                  f"e.g. {', '.join(bad[:3])}")


def parse_deviations(specs: List[str]) -> Tuple[Dict[str, str], List[str]]:
    """'check_id=reason' -> ({id: reason}, problems)."""
    out: Dict[str, str] = {}
    probs = []
    for spec in specs:
        cid, sep, text = spec.partition("=")
        cid, text = cid.strip(), text.strip()
        if not sep or not text:
            probs.append(f"--deviation {spec!r}: want CHECK_ID=reason")
        elif cid not in CHECK_IDS:
            probs.append(f"--deviation {spec!r}: unknown check id (one of {', '.join(CHECK_IDS)})")
        elif cid in NON_WAIVABLE:
            probs.append(f"--deviation {spec!r}: {cid} is an integrity or estimand check and cannot be accepted "
                         "as a deviation")
        else:
            out[cid] = text
    return out, probs


def sealed_overlap(items: List[Dict[str, Any]], path: Optional[Path] = None) -> int:
    """How many selected items are sealed held-out items (by id, or by content key across suites)."""
    path = HELDOUT_MANIFEST if path is None else path
    try:
        man = _load_json(path)
    except (OSError, ValueError):
        return 0
    ids, keys = set(), set()
    for suite, want in (man.get("suites") or {}).items():
        for i, key in ((_listed_ids(want) or {}) if isinstance(want, dict) else {}).items():
            ids.add((suite, i))
            if key:
                keys.add(key)
    audit = _audit() if keys else None
    n = 0
    for it in items:
        if (it["suite"], it["id"]) in ids:
            n += 1
        elif audit is not None:
            try:
                if audit.heldout_key(it) in keys:
                    n += 1
            except Exception:  # noqa: BLE001 - an item audit cannot key is simply not matched
                pass
    return n


def main(argv: Optional[List[str]] = None) -> int:
    a = parse_args(argv)
    site = load_site_env(Path(a.site_env))
    try:
        items, datasets = load_datasets(resolve_suite_paths(a.suite), a.limit, a.only_suite)
    except DatasetError as exc:
        log(f"dataset error: {exc}")
        return 2
    if a.select_frozen:
        if not a.freeze:
            log("--select-frozen needs --freeze")
            return 2
        try:
            keep = frozen_ids(a.freeze)
        except (OSError, ValueError) as exc:
            log(f"--freeze: {exc}")
            return 2
        items = [it for it in items if it["id"] in keep.get(it["suite"], ())]
        if not items:
            log("--select-frozen: no selected item is in the freeze manifest")
            return 2
    # The plan's scorer_overrides (frozen with it, e.g. cse-frr -> refusal for D6) apply to every run of the plan;
    # only --scorer-override flags that differ from them count as an override.
    plan_ov = {k: v for k, v in ((a.plan or {}).get("scorer_overrides") or {}).items() if v in S.SCORERS}
    a.cli_scorer_overrides = {k: v for k, v in a.overrides.items() if plan_ov.get(k) != v}
    present = {it["suite"] for it in items}
    a.overrides = {**{k: v for k, v in plan_ov.items() if k in present}, **a.overrides}
    if a.overrides:
        unknown = sorted(set(a.cli_scorer_overrides) - present)
        if unknown:
            log(f"--scorer-override: no selected items in suite(s) {', '.join(unknown)}")
            return 2
        for it in items:
            if it["suite"] in a.overrides:
                it["scorer_original"] = it["scorer"]
                it["scorer"] = a.overrides[it["suite"]]
    n_judge = sum(1 for it in items if it["scorer"] == "llm_judge")
    if a.intervention:
        try:
            apply_interventions(a, items)
        except (OSError, ValueError, KeyError) as exc:
            log(f"--intervention: {exc}")
            return 2
        if a.intervention_problems and a.protocol != "confirmatory":
            log("intervention check failed:\n  - " + "\n  - ".join(a.intervention_problems))
            return 2
    is_frontier = lambda m: m == FRONTIER_PREFIX or m.startswith(FRONTIER_PREFIX + ":")  # noqa: E731
    local_models = [m for m in a.models if not is_frontier(m)]
    frontier_models = [m for m in a.models if is_frontier(m)]

    # frontier cost gate
    for fm in frontier_models:
        est = estimate_frontier_cost(items, fm.split(":", 1)[1] if ":" in fm else a.frontier_model)
        log(f"frontier baseline {fm}: {est['items']} items, ~{est['input_tokens']} input + ~{est['output_tokens']} "
            f"output tokens at ${est['usd_per_mtok'][0]}/${est['usd_per_mtok'][1]} per MTok ({est['price_tier']}) "
            f"= about ${est['estimated_usd']:.2f} (rough; hard cap ${a.frontier_max_usd:.2f} total, "
            f"${a.frontier_call_usd} per call)")
        if not a.yes_frontier and not a.rescore:
            log("not running the frontier baseline without --yes-frontier")
            return 3

    api_host = setting("SPARK_API_HOST", site)
    # EVAL_BASE_URL (environment or site.env) routes around the edge, e.g. through an SSH tunnel to the
    # backend's LiteLLM (evals/tools/gateway-tunnel.sh); --base-url still wins.
    a.base_url = a.base_url or setting("EVAL_BASE_URL", site) or None
    need_gateway = (local_models and not a.rescore) or (n_judge and not a.no_grade)
    client = None
    if need_gateway:
        if not api_host and not a.base_url:
            log("SPARK_API_HOST is not set (site.env or environment)")
            return 2
        key = read_key(a.key_file or setting("SPARK_EVAL_KEY_FILE", site, DEFAULT_KEY_FILE))
        client = GatewayClient(a.base_url or f"https://{api_host}/v1", key, a.timeout)
        del key
    JUDGE_IDENT.clear()
    JUDGE_STOP.clear()
    # the judge's served weights decide its family: ask the live server (--probe-server) before the checks
    a.grader_probe = {}
    if a.probe_server and client is not None and n_judge and not a.no_grade and not a.rescore:
        for gm in a.grader_models:
            a.grader_probe[gm] = probe_serving(client.get_json, client.base, gm)
    if a.intervention and a.probe_server and client is not None and not a.rescore:
        verify_interventions_live(a, client)
    if not a.rescore:
        git = git_info()
        devs, dev_probs = parse_deviations(a.deviation) if a.protocol == "confirmatory" else ({}, [])
        fails = protocol_checks(a, datasets, items, git, a.plan, n_judge) if a.protocol == "confirmatory" else []
        unexcused = [f for f in fails if f["id"] not in devs or f["id"] in NON_WAIVABLE]
        stale = sorted(set(devs) - {f["id"] for f in fails})
        if dev_probs or unexcused or stale:
            msg = ["confirmatory run refused:"] + [f"  - {p}" for p in dev_probs]
            msg += [f"  - [{f['id']}] {f['msg']}" for f in unexcused]
            msg += [f"  - --deviation {d}=...: that check did not fail" for d in stale]
            log("\n".join(msg) + "\nFix them, run with --protocol exploratory, or accept each failed check with "
                "its own --deviation CHECK_ID=reason (integrity and estimand checks cannot be accepted: "
                + ", ".join(sorted(NON_WAIVABLE)) + ").")
            return 2
        freezes = [{"path": rel(Path(f)), "sha256": sha256_file(Path(f))} for f in a.freeze if Path(f).is_file()]
        # "freeze": the manifest that pins the comparisons file (what report.py's gate reads); "freezes": all of them
        main_fz = next((fz for fz, f in zip(freezes, [f for f in a.freeze if Path(f).is_file()])
                        if _load_json(f).get("comparisons_sha256")), freezes[0] if freezes else None)
        a.protocol_record = {"mode": a.protocol, "checks_failed": fails,
                             "deviations": ([{"id": k, "text": v} for k, v in devs.items()]
                                            if a.protocol == "confirmatory" else list(a.deviation)),
                             "freeze": main_fz, "freezes": freezes or None,
                             "frozen_ref": a.frozen_ref, "harness_ref": _freeze_value(a, "harness_ref"),
                             "frozen_sha": getattr(a, "frozen_sha", None),
                             "ledger": rel(ATTEMPTS_LEDGER) if a.protocol == "confirmatory" else None,
                             "stage2": getattr(a, "stage2", None), "plan": a.plan,
                             "interventions": getattr(a, "interventions_record", None) or None,
                             "seeds": a.seed_list, "doc": "evals/PROTOCOL.md"}
        if getattr(a, "stop_rules", False):
            a.protocol_record["stop_rules"] = True
        if fails:
            log(f"protocol: {len(fails)} failed check(s) accepted as declared deviations")
        if a.protocol == "exploratory":
            n_sealed = sealed_overlap(items)
            a.protocol_record["sealed_items_read"] = n_sealed
            if n_sealed:
                bar = "!" * 100
                log(f"{bar}\nWARNING: this exploratory run reads {n_sealed} SEALED held-out item(s) "
                    f"({rel(HELDOUT_MANIFEST)}).\nSealed items are for the pre-registered confirmatory run only. "
                    f"Looking at these outputs spends the held-out split; it is recorded in run.json.\n{bar}")

    try:
        grader_template = Path(a.grader_prompt).read_text(encoding="utf-8")
    except OSError as exc:
        log(f"cannot read the grader prompt: {exc.strerror}")
        return 2
    if n_judge and not a.no_grade and not a.grader_models:
        contestants = sorted(set(a.models) | set((a.plan or {}).get("contestants") or []))
        fams = sorted({f for m in contestants for f in (model_families(m) or {"unknown"})})
        log(f"{n_judge} llm_judge item(s) need a grader: pass --grader-model (there is no default), or --no-grade. "
            f"Choose a judge that is not a contestant ({', '.join(contestants)}) and not of a contestant's model "
            f"family ({', '.join(fams)}; see {rel(MODEL_FAMILIES)}): a model judging itself or its relatives "
            "favours their answers.")
        return 2
    contestants = sorted(set(a.models) | set((a.plan or {}).get("contestants") or []))
    self_grade = G.self_grade_overlap(a.grader_models, contestants) if n_judge and not a.no_grade else []
    fam_overlap = family_overlap(a.grader_models, contestants, a) if n_judge and not a.no_grade else []
    if fam_overlap and not self_grade:
        msg = (f"grader(s) {', '.join(fam_overlap)} share a model family with a contestant ({', '.join(contestants)}), "
               f"or their family is unknown ({rel(MODEL_FAMILIES)}); their grades may favour that family.")
        if not a.allow_self_grade:
            log(msg + " Pick an independent --grader-model, or pass --allow-self-grade (recorded in run.json; a "
                      "failed check in confirmatory runs).")
            return 2
        log("warning: " + msg + " Allowed by --allow-self-grade; recorded in run.json.")
    if self_grade and not a.allow_self_grade:
        log(f"grader(s) {', '.join(self_grade)} are also contestants: a model must not grade its own answers. "
            "Pick another --grader-model, or pass --allow-self-grade (recorded in run.json).")
        return 2
    if self_grade:
        log(f"warning: self-grading allowed for {', '.join(self_grade)}; recorded in run.json")
    grader_cfg: Dict[str, Any] = {"model": a.grader_models[0] if a.grader_models and not a.no_grade else None,
                                  "thinking": "off",
                                  "temperature": 0, "prompt": rel(Path(a.grader_prompt)),
                                  "prompt_sha256": sha256_text(grader_template),
                                  "max_candidate_chars": a.grader_max_chars}
    if not a.no_grade and a.grader_models:
        suites = sorted({it["suite"] for it in items if it["scorer"] == "llm_judge"})
        grader_cfg.update({
            "models": a.grader_models, "aggregate": a.judge_aggregate, "parse_retries": a.grader_parse_retries,
            "seed": a.seed, "injection_precheck": G.PRECHECK_VERSION, "self_grade": self_grade,
            "allow_self_grade": a.allow_self_grade, "family_overlap": family_pairs(a.grader_models, contestants, a),
            "contestants": contestants,
            "served_identity": {m: grader_identity(m, a) for m in a.grader_models},
            "cache_legacy": a.grader_cache_legacy,
            "reference_provenance": {su: G.reference_provenance(su) for su in suites}})
    for gm in a.grader_models if n_judge and not a.no_grade else []:
        if gm in SWAPPING_ALIASES:
            log(f"note: {n_judge} llm_judge item(s) per run will be graded by '{gm}', which loads on both "
                "GPUs and evicts coder/coder-fast for its TTL. Use --no-grade (or a judge off this host) to avoid that.")

    run_name = a.run_name or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    runs: List[Run] = []
    for m in a.models:
        modes = ["n/a"] if is_frontier(m) else a.thinking_modes
        for t in modes:
            if is_frontier(m):
                fclient = FrontierClient(a.frontier_cmd, m.split(":", 1)[1] if ":" in m else a.frontier_model,
                                         a.timeout, a.frontier_call_usd, a.frontier_extra_args, api_host)
                runs.append(Run(a, m, "off", items, datasets, site, run_name, fclient, grader_cfg))
            else:
                if m in SWAPPING_ALIASES and not a.rescore:
                    log(f"note: '{m}' loads on both GPUs and evicts coder/coder-fast while it runs")
                if a.seeds_given and len(a.seed_list) > 1 and sampling_for(a, t)["temperature"] == 0:
                    log(f"note: {m} thinking {t} is greedy; extra seeds only measure server nondeterminism")
                for seed in a.seed_list:
                    runs.append(Run(a, m, t, items, datasets, site, run_name, client, grader_cfg, seed=seed))

    interrupted = False
    try:
        for run in runs:
            run.prepare()
            if a.rescore:
                continue
            st = run.generate()
            log(f"[{run.dir.name}] generation done: {st['ok']} ok, {st['error']} errors")
        # grading phase: once every model has generated
        graders = [] if a.no_grade else [(m, make_grader(client, m, grader_template, a)) for m in a.grader_models]
        for run in runs:
            judges = [(m, cached_grader(run, gr, judge_cfg(grader_cfg, m))) for m, gr in graders]
            run.rescore(judges, grade_llm=n_judge > 0, aggregate=a.judge_aggregate)
    except KeyboardInterrupt:
        interrupted = True
        log("interrupted; writing partial summaries (re-run with the same --run-name to resume)")
    for run in runs:
        if not (run.dir / "run.json").exists():
            continue
        out = run.finish()
        t = out["totals"]
        log(f"[{run.dir.name}] {t['passed']}/{t['n_scored']} passed, {t['api_errors']} api errors, "
            f"{t['truncated']} truncated, {t['unparsed']} unparsed, {t['item_or_grader_errors']} item/grader errors"
            + (f", ${t['cost_usd']:.2f}" if t["cost_usd"] else ""))
        print(str(run.dir))
    if len(runs) > 1:
        log("compare: python3 evals/report.py " + " ".join(rel(r.dir) for r in runs))
    if any(getattr(r, "invalid", None) for r in runs):
        log("protocol: at least one run was stopped as invalid (see protocol.invalid in its run.json)")
        return 4
    return 130 if interrupted else 0


if __name__ == "__main__":
    sys.exit(main())
