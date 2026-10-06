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
SWAPPING_ALIASES = {"big", "vision", "hermes"}   # these take both GPUs and evict the coding pair

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


def sampling_for(args, thinking: str) -> Tuple[float, Optional[float]]:
    """(temperature, top_p) for one run. Explicit --temperature/--top-p always win. Otherwise thinking off is greedy
    (repeatable), and thinking on/default uses Qwen's recommended 0.6 / 0.95: at temperature 0 the reasoning can
    loop until max_tokens (eval run (a): 9 of 50 coder mcq items)."""
    thinks = thinking in ("on", "default")
    temp = args.temperature if args.temperature is not None else (THINK_TEMPERATURE if thinks else THINK_OFF_TEMPERATURE)
    top_p = args.top_p if args.top_p is not None else (THINK_TOP_P if thinks else None)
    return temp, top_p


def max_tokens_for(item: Dict[str, Any], args, thinking: str) -> int:
    if args.max_tokens:
        return min(args.max_tokens, HARD_MAX_TOKENS)
    meta_cap = (item.get("meta") or {}).get("max_tokens")
    base = int(meta_cap) if isinstance(meta_cap, int) and meta_cap > 0 else MAX_TOKENS[item["type"]]
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
    return {"sha": g("rev-parse", "HEAD") or "unknown", "branch": g("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(status)}


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
        for flag in ("-c", "-n", "--temp", "--top-p", "--top-k", "--min-p", "--presence-penalty", "-sm"):
            fm = re.search(rf"(?:^|\s){re.escape(flag)}\s+(\S+)", cmd)
            if fm:
                flags[flag] = fm.group(1)
        nm = re.search(r'name:\s*"([^"]+)"', block)
        return {"model_id": mid, "name": nm.group(1) if nm else mid, "aliases": aliases, "cmd": cmd,
                "server_flags": flags, "source": rel(path),
                "note": "from the repo template; the live /etc/llama-swap/config.yaml may differ"
                if path == LLAMA_SWAP_TEMPLATE else "from the given llama-swap config"}
    return None


# ------------------------------------------------------------------------------------------- backends
class CallError(Exception):
    def __init__(self, msg: str, retryable: bool):
        super().__init__(msg)
        self.retryable = retryable


class GatewayClient:
    """OpenAI-compatible /v1/chat/completions over urllib. Holds the key; never logs it."""

    def __init__(self, base_url: str, key: str, timeout: float):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self._key = key
        self.timeout = timeout

    def __repr__(self) -> str:  # never show the key
        return f"GatewayClient({self.url!r})"

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
class Run:
    def __init__(self, args, model: str, thinking: str, items: List[Dict[str, Any]], datasets, site: Dict[str, str],
                 run_name: str, client, grader_cfg: Dict[str, Any]):
        self.args, self.model, self.thinking, self.items = args, model, thinking, items
        self.frontier = model == FRONTIER_PREFIX or model.startswith(FRONTIER_PREFIX + ":")
        self.frontier_model = model.split(":", 1)[1] if self.frontier and ":" in model else args.frontier_model
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", model)
        suffix = "" if self.frontier or thinking == "off" else ("-think" if thinking == "on" else "-thinkdefault")
        self.dir = Path(args.results) / f"{run_name}-{slug}{suffix}"
        self.client = client
        self.temperature, self.top_p = sampling_for(args, thinking)
        self.lock = threading.Lock()
        self.cost_usd = 0.0
        self.stop = threading.Event()
        self.config = self._config(datasets, site, grader_cfg)

    def _config(self, datasets, site, grader_cfg) -> Dict[str, Any]:
        a = self.args
        sampling: Dict[str, Any] = {"temperature": self.temperature, "seed": a.seed}
        if self.top_p is not None:
            sampling["top_p"] = self.top_p
        if a.presence_penalty is not None:
            sampling["presence_penalty"] = a.presence_penalty
        if self.frontier:
            sampling = {"note": "claude CLI defaults (no sampling control)"}
        caps = {t: max_tokens_for({"type": t}, a, self.thinking) for t in TYPES}
        alias_entry = None if self.frontier else llama_swap_entry(self.model, Path(a.llama_swap_config))
        return {
            "schema": SCHEMA,
            "model": self.model,
            "backend": "claude-cli" if self.frontier else "gateway",
            "frontier_model": self.frontier_model if self.frontier else None,
            "gateway": None if self.frontier else "https://${SPARK_API_HOST}/v1",
            "thinking": "n/a" if self.frontier else self.thinking,
            "sampling": sampling,
            "max_tokens": None if self.frontier else caps,
            "think_budget": a.think_budget if self.thinking != "off" and not self.frontier else 0,
            "concurrency": a.concurrency,
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
            old.setdefault("rescored", []).append({"at": utcnow(), "harness": self.config["harness"],
                                                   "git": self.config["git"]})
            old["grader"], old["scorer_overrides"] = self.config["grader"], self.config["scorer_overrides"]
            self.config = old
            self.write_run_json()
            return
        if self.args.rescore:
            raise SystemExit(f"{self.dir.name}: nothing to re-score (no run.json)")
        if rj.exists():
            old = json.loads(rj.read_text(encoding="utf-8"))
            keys = ("model", "thinking", "sampling", "max_tokens", "think_budget")
            # scorer overrides only change scoring, so they may differ (scores.jsonl is rewritten at the end)
            diff = [k for k in keys if old.get(k) != self.config.get(k)]
            old_ds = {d["path"]: d["sha256"] for d in old.get("datasets", [])}
            for d in self.config["datasets"]:
                if d["path"] in old_ds and old_ds[d["path"]] != d["sha256"]:
                    diff.append(f"dataset {d['path']} checksum")
            if diff and not self.args.force_resume:
                raise SystemExit(f"{self.dir.name}: existing run has different {', '.join(diff)}; "
                                 "use a new --run-name (or --force-resume)")
            self.config["started"] = old.get("started", utcnow())
            self.config["resumed"] = old.get("resumed", []) + [utcnow()]
            if old.get("git", {}).get("sha") != self.config["git"]["sha"]:
                self.config["git_history"] = old.get("git_history", [old.get("git")]) + [self.config["git"]]
        else:
            self.config["started"] = utcnow()
        self.config["ended"] = None
        self.write_run_json()

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
                                "temperature": self.temperature, "seed": a.seed}
        if self.top_p is not None:
            body["top_p"] = self.top_p
        if a.presence_penalty is not None:
            body["presence_penalty"] = a.presence_penalty
        if self.thinking in ("on", "off"):
            body["chat_template_kwargs"] = {"enable_thinking": self.thinking == "on"}
        return body

    def call(self, item: Dict[str, Any]) -> Dict[str, Any]:
        messages = build_messages(item)
        rec: Dict[str, Any] = {"id": item["id"], "suite": item["suite"], "type": item["type"],
                               "scorer": item["scorer"], "model": self.model, "started": utcnow()}
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
        if self.frontier or self.args.no_final_answer or item["type"] not in ("mcq", "classify") or rec.get("error"):
            return
        sc = S.score_item(rec.get("content") or "", item, scorer_table)
        if rec.get("finish_reason") != "length" and sc.status != "unparsed":
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
        rec["content"] = (rec.get("content") or "") + "\n" + ans

    def score(self, rec: Dict[str, Any], scorer_table) -> Dict[str, Any]:
        item = self.by_id[rec["id"]]
        sc = S.score_item(rec.get("content") or "", item, scorer_table)
        d = {"id": rec["id"], "suite": rec["suite"], "scorer": item["scorer"], **sc.to_dict()}
        label_source = (item.get("meta") or {}).get("label_source")
        if label_source:
            d["label_source"] = label_source
        if item["scorer"] == "llm_judge":
            d["model_graded"] = sc.status == "ok"
        if rec.get("final_answer_prompt"):
            d["final_answer_prompt"] = True
        return d

    def generate(self) -> Dict[str, int]:
        self.by_id = {it["id"]: it for it in self.items}
        done = self.done_ids()
        todo = [it for it in self.items if it["id"] not in done]
        log(f"[{self.dir.name}] {len(self.items)} items, {len(done & set(self.by_id))} already done, {len(todo)} to run")
        stats = {"ok": 0, "error": 0}
        # non-llm_judge items are scored as they arrive; llm_judge ones in the grading phase
        table = dict(S.SCORERS)

        def work(it):
            if self.frontier and self.args.frontier_max_usd and self.cost_usd >= self.args.frontier_max_usd:
                self.stop.set()
            rec = self.call(it)
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
        return stats

    def rescore(self, grader: Optional[S.Grader], grade_llm: bool) -> None:
        """Rewrite scores.jsonl from the latest responses. llm_judge items use the grade cache."""
        self.by_id = {it["id"]: it for it in self.items}
        resp = R.latest_by_id(R.read_jsonl(self.dir / "responses.jsonl"))
        table = dict(S.SCORERS)
        if grade_llm:
            table["llm_judge"] = S.make_llm_judge(grader)
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
        self.write_run_json()
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
        nonce = secrets.token_hex(8)
        cand = candidate[: args.grader_max_chars]
        ref = spec["reference"] or "(none given)"
        if spec["reference"] and spec.get("format") == "json":
            ref = "(JSON)\n" + ref
        vals = {"NONCE": nonce, "QUESTION": user_content(item),
                "RUBRIC": spec["rubric"] or "(none given: grade by the reference)", "REFERENCE": ref, "CANDIDATE": cand}
        prompt = re.sub(r"@@([A-Z]+)@@", lambda m: vals.get(m.group(1), m.group(0)), template)
        body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 512,
                "temperature": 0, "seed": args.seed, "stream": False,
                "chat_template_kwargs": {"enable_thinking": False}, "response_format": {"type": "json_object"}}
        last: Optional[Exception] = None
        for attempt in range(args.retries + 1):
            try:
                data = client.chat(body)
                text = (data["choices"][0].get("message") or {}).get("content") or ""
                return text, {"model": model, "served_model": data.get("model"), "usage": data.get("usage")}
            except CallError as exc:
                last = exc
                if not exc.retryable:
                    break
                time.sleep(args.backoff * (2 ** attempt))
        raise RuntimeError(str(last))

    return grader


def cached_grader(run: Run, grader: Optional[S.Grader], grader_cfg: Dict[str, Any]) -> Optional[S.Grader]:
    """Wraps the grader with a per-run cache (grades.jsonl), keyed by item, candidate text and grader config."""
    if grader is None:
        return None
    path = run.dir / "grades.jsonl"
    cache = {r.get("cache_id") or r.get("key"): r for r in R.read_jsonl(path)}
    cfg_key = sha256_text(json.dumps(grader_cfg, sort_keys=True))

    def g(item, candidate, spec):
        cid = sha256_text(json.dumps([item["id"], candidate, spec, cfg_key]))
        if cid in cache:
            return cache[cid]["raw"], cache[cid]["meta"]
        raw, meta = grader(item, candidate, spec)
        rec = {"cache_id": cid, "id": item["id"], "raw": raw, "meta": meta, "at": utcnow()}
        run._append("grades.jsonl", rec)
        cache[cid] = rec
        return raw, meta

    return g


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
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--top-p", type=float,
                    help=f"default: unset (server default) with thinking off, {THINK_TOP_P} with thinking on/default")
    ap.add_argument("--presence-penalty", type=float,
                    help="unset = the llama-swap server default (coder-fast has 1.5); 0 turns it off")
    ap.add_argument("--max-tokens", type=int, help="override the per-type output caps")
    ap.add_argument("--think-budget", type=int, default=DEFAULT_THINK_BUDGET,
                    help=f"extra max_tokens when thinking is on or default (default {DEFAULT_THINK_BUDGET})")
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
    g.add_argument("--grader-model", default="big", help="gateway alias that grades llm_judge items (default big)")
    g.add_argument("--grader-prompt", default=str(GRADER_PROMPT))
    g.add_argument("--no-grade", action="store_true", help="leave llm_judge items ungraded")
    g.add_argument("--no-final-answer", action="store_true",
                   help="do not ask for a final answer line after a cut-off or unparsable mcq/classify reply")
    g.add_argument("--grader-max-chars", type=int, default=12000, help="candidate text sent to the grader")
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
    return a


def main(argv: Optional[List[str]] = None) -> int:
    a = parse_args(argv)
    site = load_site_env(Path(a.site_env))
    try:
        items, datasets = load_datasets(resolve_suite_paths(a.suite), a.limit, a.only_suite)
    except DatasetError as exc:
        log(f"dataset error: {exc}")
        return 2
    if a.overrides:
        unknown = sorted(set(a.overrides) - {it["suite"] for it in items})
        if unknown:
            log(f"--scorer-override: no selected items in suite(s) {', '.join(unknown)}")
            return 2
        for it in items:
            if it["suite"] in a.overrides:
                it["scorer_original"] = it["scorer"]
                it["scorer"] = a.overrides[it["suite"]]
    is_frontier = lambda m: m == FRONTIER_PREFIX or m.startswith(FRONTIER_PREFIX + ":")  # noqa: E731
    local_models = [m for m in a.models if not is_frontier(m)]
    frontier_models = [m for m in a.models if is_frontier(m)]
    n_judge = sum(1 for it in items if it["scorer"] == "llm_judge")

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
    need_gateway = (local_models and not a.rescore) or (n_judge and not a.no_grade)
    client = None
    if need_gateway:
        if not api_host and not a.base_url:
            log("SPARK_API_HOST is not set (site.env or environment)")
            return 2
        key = read_key(a.key_file or setting("SPARK_EVAL_KEY_FILE", site, DEFAULT_KEY_FILE))
        client = GatewayClient(a.base_url or f"https://{api_host}/v1", key, a.timeout)
        del key

    try:
        grader_template = Path(a.grader_prompt).read_text(encoding="utf-8")
    except OSError as exc:
        log(f"cannot read the grader prompt: {exc.strerror}")
        return 2
    grader_cfg: Dict[str, Any] = {"model": None if a.no_grade else a.grader_model, "thinking": "off", "temperature": 0,
                                  "prompt": rel(Path(a.grader_prompt)), "prompt_sha256": sha256_text(grader_template),
                                  "max_candidate_chars": a.grader_max_chars}
    if n_judge and not a.no_grade and a.grader_model in SWAPPING_ALIASES:
        log(f"note: {n_judge} llm_judge item(s) per run will be graded by '{a.grader_model}', which loads on both "
            "GPUs and evicts coder/coder-fast for its TTL. Use --grader-model coder or --no-grade to avoid that.")

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
                runs.append(Run(a, m, t, items, datasets, site, run_name, client, grader_cfg))

    interrupted = False
    try:
        for run in runs:
            run.prepare()
            if a.rescore:
                continue
            st = run.generate()
            log(f"[{run.dir.name}] generation done: {st['ok']} ok, {st['error']} errors")
        # grading phase: once every model has generated
        grader = None if a.no_grade else make_grader(client, a.grader_model, grader_template, a)
        for run in runs:
            run.rescore(cached_grader(run, grader, grader_cfg), grade_llm=n_judge > 0)
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
    return 130 if interrupted else 0


if __name__ == "__main__":
    sys.exit(main())
