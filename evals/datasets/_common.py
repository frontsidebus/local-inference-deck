"""Shared helpers for the dataset converters in evals/datasets/<suite>/fetch.py.

Stdlib only. Each fetch.py:
  1. downloads its source files from the official location into a cache
     (evals/data/.cache/<suite>/), verifying a pinned sha256;
  2. converts them with a pure function (unit-tested on checked-in fixtures);
  3. writes evals/data/<suite>.jsonl plus a <suite>.provenance.json sidecar;
  4. with --sample N, also writes evals/data/<suite>.sample<N>.jsonl, a fixed-seed
     subset stratified on meta.category.

The task format is the one in the shared eval contract (EVALS-INTERFACE).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "evals" / "data"

TYPES = {"mcq", "extract", "classify", "freeform", "code"}
SCORERS = {
    "mcq_letter", "exact", "exact_set", "f1_tokens", "regex", "numeric_tol",
    "json_fields", "cwe_match", "cvss_mae", "llm_judge",
}
USER_AGENT = "local-inference-deck-evals/1.0 (+dataset fetch; stdlib urllib)"
DEFAULT_SEED = 20261005


class ChecksumError(RuntimeError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def http_get(url: str, timeout: int = 120, retries: int = 3, headers: dict | None = None) -> bytes:
    """GET a URL with a few retries. Raises the last error."""
    hdrs = {"User-Agent": USER_AGENT}
    hdrs.update(headers or {})
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    assert last is not None
    raise last


def download(url: str, dest: Path, sha256: str | None, offline: bool = False) -> Path:
    """Fetch url to dest (reusing a cached copy whose checksum matches).

    sha256=None means "no pin" and is only used by suites whose upstream is a live
    API (see nvd-recent); everything else pins the exact bytes.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and (sha256 is None or sha256_file(dest) == sha256):
        return dest
    if offline:
        raise FileNotFoundError(f"--offline and no valid cached copy of {url} at {dest}")
    data = http_get(url)
    got = sha256_bytes(data)
    if sha256 is not None and got != sha256:
        bad = dest.with_name(dest.name + ".bad")
        bad.write_bytes(data)
        raise ChecksumError(
            f"checksum mismatch for {url}\n  expected {sha256}\n  got      {got}\n"
            f"  (saved as {bad}; upstream changed or the download is corrupt)"
        )
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, dest)
    return dest


def validate(items: list[dict]) -> None:
    """Check items against the contract. Raises ValueError on the first problem."""
    seen: set[str] = set()
    for it in items:
        for key in ("id", "suite", "type", "prompt", "answer", "scorer", "meta"):
            if key not in it:
                raise ValueError(f"{it.get('id')}: missing field {key!r}")
        if it["id"] in seen:
            raise ValueError(f"duplicate id {it['id']}")
        seen.add(it["id"])
        if it["type"] not in TYPES:
            raise ValueError(f"{it['id']}: bad type {it['type']!r}")
        if it["scorer"] not in SCORERS:
            raise ValueError(f"{it['id']}: bad scorer {it['scorer']!r}")
        if not isinstance(it["prompt"], str) or not it["prompt"].strip():
            raise ValueError(f"{it['id']}: empty prompt")
        if it["type"] == "mcq":
            ch = it.get("choices")
            if not ch or not isinstance(ch, list):
                raise ValueError(f"{it['id']}: mcq without choices")
            letters = [c.split(")", 1)[0] for c in ch]
            ans = it["answer"] if isinstance(it["answer"], list) else [it["answer"]]
            if not ans or any(a not in letters for a in ans):
                raise ValueError(f"{it['id']}: answer {it['answer']!r} not in choices")
        for key in ("source", "license"):
            if key not in it["meta"]:
                raise ValueError(f"{it['id']}: meta.{key} missing")


def make_id(suite: str, n: int, total: int) -> str:
    return f"{suite}-{n:0{max(4, len(str(total)))}d}"


def letter_choices(options: Iterable[tuple[str, str]]) -> list[str]:
    return [f"{k}) {str(v).strip()}" for k, v in options]


def to_jsonl(items: list[dict]) -> bytes:
    return "".join(json.dumps(it, ensure_ascii=False) + "\n" for it in items).encode("utf-8")


def stratified_sample(items: list[dict], n: int, seed: int = DEFAULT_SEED,
                      key: Callable[[dict], str] | None = None) -> list[dict]:
    """Deterministic stratified subset of n items, returned in source order.

    Allocation is proportional to stratum size (largest remainder, ties broken by
    stratum name), then each stratum is sampled with random.Random(seed).
    """
    if n >= len(items):
        return list(items)
    key = key or (lambda it: str(it.get("meta", {}).get("category", "")))
    groups: dict[str, list[int]] = {}
    for idx, it in enumerate(items):
        groups.setdefault(key(it), []).append(idx)
    total = len(items)
    names = sorted(groups)
    quotas = {g: n * len(groups[g]) / total for g in names}
    alloc = {g: int(quotas[g]) for g in names}
    left = n - sum(alloc.values())
    for g in sorted(names, key=lambda g: (-(quotas[g] - alloc[g]), g))[:left]:
        alloc[g] += 1
    rng = random.Random(seed)
    chosen: list[int] = []
    for g in names:
        chosen.extend(rng.sample(groups[g], alloc[g]))
    return [items[i] for i in sorted(chosen)]


def write_outputs(suite: str, items: list[dict], args: argparse.Namespace,
                  sources: list[dict], license_: str, extra: dict | None = None) -> dict:
    """Validate, write <suite>.jsonl (+ sample) and the provenance sidecar."""
    validate(items)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    data = to_jsonl(items)
    (out_dir / f"{suite}.jsonl").write_bytes(data)
    prov = {
        "suite": suite,
        "items": len(items),
        "jsonl_sha256": sha256_bytes(data),
        "license": license_,
        "sources": sources,
        "fetched_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "converter": f"evals/datasets/{Path(sys.argv[0]).resolve().parent.name}/fetch.py",
    }
    if extra:
        prov.update(extra)
    if args.sample:
        sub = stratified_sample(items, args.sample, args.seed)
        sdata = to_jsonl(sub)
        (out_dir / f"{suite}.sample{args.sample}.jsonl").write_bytes(sdata)
        prov["sample"] = {"n": len(sub), "seed": args.seed, "jsonl_sha256": sha256_bytes(sdata),
                          "file": f"{suite}.sample{args.sample}.jsonl"}
    (out_dir / f"{suite}.provenance.json").write_text(json.dumps(prov, indent=2) + "\n")
    msg = f"{suite}: {len(items)} items  sha256={prov['jsonl_sha256']}"
    if args.sample:
        msg += f"  (+sample{args.sample}: {prov['sample']['n']} items)"
    print(msg)
    return prov


def base_parser(description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--sample", type=int, default=0, metavar="N",
                   help="also write a fixed-seed stratified subset of N items")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED, help="seed for --sample")
    p.add_argument("--out-dir", default=str(DATA_DIR), help="output dir (default evals/data)")
    p.add_argument("--cache-dir", default=str(DATA_DIR / ".cache"),
                   help="download cache (default evals/data/.cache)")
    p.add_argument("--offline", action="store_true", help="use cached downloads only")
    return p
