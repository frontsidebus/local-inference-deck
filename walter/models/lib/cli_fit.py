"""`spark-model search` and `spark-model fit`: run on the workstation, read the Hub, never download a model.

  spark-model search QUERY [--author ORG] [--limit N] [--json]
  spark-model fit REPO[@REV] [--file PATTERN] [--ctx N] [--mmproj [PATTERN]] [--kv-type T] [--json]

The dispatcher (clients/bin/spark-model) calls add_parsers(sub) and then args.func(args) (run_search / run_fit).
Allowed orgs come from LAB_HF_ORGS (environment, else the repo's site.env); search only shows them as a badge,
`pull` enforces them. An optional Hub token is read from --token-file (a path; never a value on the command line).
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from pathlib import Path

try:
    import fit
    import gguf
    import hf
except ImportError:  # pragma: no cover - package-style import
    from . import fit, gguf, hf  # type: ignore

REPO_ROOT = Path(__file__).resolve().parents[3]
SHARD_RE = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.I)
PREFERRED_QUANTS = ("Q4_K_M", "UD-Q4_K_XL", "Q4_K_XL", "Q4_K_S", "IQ4_XS", "IQ4_NL", "Q4_0")
Q4_BYTES_PER_PARAM = 4.85 / 8          # rough Q4_K_M average, for search badges only
# Hub licence ids treated as open (OSI or CC without NC/ND); anything else is flagged "custom"
OSI_LICENSES = {"apache-2.0", "mit", "bsd", "bsd-2-clause", "bsd-3-clause", "bsd-3-clause-clear", "gpl", "gpl-2.0",
                "gpl-3.0", "lgpl", "lgpl-2.1", "lgpl-3.0", "agpl-3.0", "mpl-2.0", "cc0-1.0", "cc-by-4.0",
                "cc-by-sa-4.0", "cc-by-3.0", "cc-by-sa-3.0", "unlicense", "artistic-2.0", "isc", "zlib", "ecl-2.0",
                "epl-2.0", "bsl-1.0", "osl-3.0", "afl-3.0", "wtfpl", "odc-by", "pddl", "postgresql", "ncsa"}


# --- parsers ----------------------------------------------------------------------------------------------------
def add_parsers(sub) -> None:
    s = sub.add_parser("search", help="search the Hub for GGUF repos (allow-list badge + rough fit badge)")
    s.add_argument("query", nargs="?", default="")
    s.add_argument("--author", "--org", dest="author", help="only this org/user")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--sort", default="downloads", choices=["downloads", "likes", "lastModified", "trendingScore"])
    s.add_argument("--json", action="store_true")
    s.add_argument("--token-file", help="file holding a read-only Hub token (path only)")
    s.set_defaults(func=run_search)

    f = sub.add_parser("fit", help="estimate VRAM/placement/speed from the GGUF headers (HTTP range reads only)")
    f.add_argument("repo", help="org/name[@revision]")
    f.add_argument("--file", help="glob for the quant to check (e.g. '*Q4_K_M*'); default: a Q4 quant")
    f.add_argument("--ctx", type=int, help="context length (default: min(trained, 131072))")
    f.add_argument("--kv-type", default="q8_0", choices=sorted(fit.KV_BYTES))
    f.add_argument("--mmproj", nargs="?", const="*", help="include a vision projector (optional glob)")
    f.add_argument("--placement", default="auto", choices=["auto", "gpu0", "gpu1", "both", "both+cpu-experts"])
    f.add_argument("--split", choices=["none", "layer", "tensor"])
    f.add_argument("--ram-budget", type=float, default=fit.RAM_BUDGET_GIB, help="GiB of RAM for CPU experts")
    f.add_argument("--json", action="store_true")
    f.add_argument("--token-file", help="file holding a read-only Hub token (path only)")
    f.set_defaults(func=run_fit)


def _hub(args) -> dict:
    return {"endpoint": getattr(args, "endpoint", None), "token_file": getattr(args, "token_file", None),
            "fetch": getattr(args, "fetch", None)}


def allowed_orgs(env=None, site_env: Path | None = None) -> set[str]:
    """LAB_HF_ORGS from the environment, else from site.env (only that key is read)."""
    env = os.environ if env is None else env
    raw = env.get("LAB_HF_ORGS")
    if raw is None:
        p = site_env or (REPO_ROOT / "site.env")
        try:
            for line in p.read_text().splitlines():
                m = re.match(r"^\s*LAB_HF_ORGS=(.*)$", line)
                if m:
                    raw = m.group(1).split("#", 1)[0].strip().strip("\"'")
        except OSError:
            raw = None
    return set((raw or "").split())


# --- search -----------------------------------------------------------------------------------------------------
def quick_badge(item: dict) -> str:
    """Rough fit from the search result alone (parameter count at ~Q4); `fit` gives the real answer."""
    g = item.get("gguf") or {}
    total = g.get("total")
    arch = g.get("architecture")
    if arch and arch not in fit.SUPPORTED_ARCHS:
        return f"arch {arch}: not in {fit.LLAMA_CPP_BUILD}"
    if not isinstance(total, (int, float)) or total < 5e8 or arch == "clip":
        return "?"           # the Hub's gguf summary came from a projector/MTP file, or is missing
    gib = total * Q4_BYTES_PER_PARAM / fit.GIB
    if gib <= fit.USABLE_GIB - 3:
        return f"~1 GPU at Q4 ({gib:.0f} GiB)"
    if gib <= 2 * fit.USABLE_GIB - 5:
        return f"~2 GPUs at Q4 ({gib:.0f} GiB)"
    return f"too big at Q4 ({gib:.0f} GiB; MoE may fit with CPU experts)"


def run_search(args) -> int:
    try:
        items = hf.search(args.query, author=args.author, limit=args.limit, sort=args.sort, **_hub(args))
    except hf.HFError as e:
        print(f"spark-model search: {e}", file=sys.stderr)
        return 2
    orgs = allowed_orgs(getattr(args, "env", None))
    rows = []
    for it in items:
        org = it["id"].split("/")[0]
        rows.append({**it, "allowed": org in orgs, "fit_badge": quick_badge(it)})
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("no GGUF repos found")
        return 0
    w = max(len(r["id"]) for r in rows)
    print(f"{'repo':{w}}  {'org':11}  {'arch':12}  {'licence':12}  {'gated':6}  {'downloads':>9}  fit (rough)")
    for r in rows:
        g = r.get("gguf") or {}
        print(f"{r['id']:{w}}  {'allowed' if r['allowed'] else 'NOT-ALLOWED':11}  {str(g.get('architecture') or '?'):12}"
              f"  {str(r.get('license') or '?')[:12]:12}  {_gated(r.get('gated')):6}  {r.get('downloads') or 0:>9}"
              f"  {r['fit_badge']}")
    if not orgs:
        print("(LAB_HF_ORGS is empty: every org shows NOT-ALLOWED; set it in site.env)")
    return 0


def _gated(g) -> str:
    return "no" if not g else str(g)


# --- file selection ---------------------------------------------------------------------------------------------
def group_quants(files: list[dict]) -> dict[str, list[dict]]:
    """GGUF model files (no mmproj) grouped by quant: shards of one quant share a key."""
    groups: dict[str, list[dict]] = {}
    for f in files:
        p = f["path"]
        if not p.lower().endswith(".gguf") or _is_mmproj(p) or _is_draft_head(p):
            continue
        key = SHARD_RE.sub(".gguf", p)
        groups.setdefault(key, []).append(f)
    for v in groups.values():
        v.sort(key=lambda f: f["path"])
    return groups


def _is_mmproj(path: str) -> bool:
    b = path.rsplit("/", 1)[-1].lower()
    return b.startswith("mmproj") or "/mmproj" in path.lower()


def _is_draft_head(path: str) -> bool:
    """MTP / draft heads shipped next to the model (e.g. MTP/mtp-*.gguf): not a quant of the model itself."""
    b = path.rsplit("/", 1)[-1].lower()
    return b.startswith(("mtp-", "draft-")) or path.lower().startswith("mtp/") or "/mtp/" in path.lower()


def _match(path: str, pattern: str) -> bool:
    p, b = path.lower(), path.rsplit("/", 1)[-1].lower()
    pat = pattern.lower()
    if not any(ch in pat for ch in "*?["):
        pat = f"*{pat}*"
    return fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(b, pat)


def pick_quant(groups: dict[str, list[dict]], pattern: str | None) -> tuple[str | None, list[str]]:
    """-> (chosen group key or None, candidate keys when ambiguous)."""
    keys = sorted(groups)
    if pattern:
        hits = [k for k in keys if any(_match(f["path"], pattern) for f in groups[k])]
        if len(hits) == 1:
            return hits[0], []
        exact = [k for k in hits if re.search(rf"(^|[-_./]){re.escape(pattern.strip('*'))}([-_.]|$)", k, re.I)]
        if len(exact) == 1:
            return exact[0], []
        return None, hits
    for q in PREFERRED_QUANTS:
        hits = [k for k in keys if re.search(rf"(^|[-_./]){re.escape(q)}([-_.]|$)", k, re.I)]
        if hits:
            return min(hits, key=lambda k: _size(groups[k])), []
    fitting = [k for k in keys if _size(groups[k]) <= 40 * fit.GIB]
    if fitting:
        return max(fitting, key=lambda k: _size(groups[k])), []
    return (min(keys, key=lambda k: _size(groups[k])), []) if keys else (None, [])


def _size(fs) -> int:
    return sum(f["size"] for f in fs)


# --- fit --------------------------------------------------------------------------------------------------------
def run_fit(args) -> int:
    try:
        report = fit_report(args)
    except (hf.HFError, gguf.GGUFError, ValueError) as e:
        print(f"spark-model fit: {e}", file=sys.stderr)
        return 2
    if report.get("error"):
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"spark-model fit: {report['error']}", file=sys.stderr)
            for c in report.get("candidates", []):
                print(f"  {c['quant']}  {c['gb']:.1f} GB  ({c['shards']} file(s))", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)
    return 0


def fit_report(args) -> dict:
    repo, _, rev = args.repo.partition("@")
    kw = _hub(args)
    sha = hf.resolve_revision(repo, rev or "main", **kw)
    files = hf.list_files(repo, sha, **kw)
    card = hf.model_card(repo, sha, **kw)
    groups = group_quants(files)
    if not groups:
        return {"error": f"{repo}@{sha[:12]} has no .gguf model files (GGUF only; safetensors are not supported)",
                "repo": repo, "revision": sha}
    key, cands = pick_quant(groups, args.file)
    quants = [{"quant": k, "gb": _size(groups[k]) / 1e9, "shards": len(groups[k])} for k in sorted(groups)]
    if key is None:
        cl = [q for q in quants if q["quant"] in cands] or quants
        why = f"--file {args.file!r} matches {len(cands)} quants" if cands else f"--file {args.file!r} matches nothing"
        return {"error": why + "; narrow it to one of:", "candidates": cl, "repo": repo, "revision": sha}
    shards = groups[key]
    headers = [gguf.parse_header(hf.range_reader(repo, sha, f["path"], **kw)) for f in shards]
    mmproj = None
    if args.mmproj:
        mm = [f for f in files if _is_mmproj(f["path"]) and f["path"].lower().endswith(".gguf")
              and (args.mmproj == "*" or _match(f["path"], args.mmproj))]
        if mm:
            mmproj = min(mm, key=lambda f: (0 if "f16" in f["path"].lower() else 1, f["size"]))
    main = next((h for h in headers if h.arch), headers[0])
    trained = main.kv.get(f"{main.arch}.context_length") if main.arch else None
    ctx = args.ctx or min(trained if isinstance(trained, int) and trained > 0 else 32768, 131072)
    est = fit.estimate(headers, ctx, kv_type=args.kv_type, mmproj_bytes=mmproj["size"] if mmproj else 0,
                       placement=args.placement, split=args.split, ram_budget_gb=args.ram_budget)
    sc = gguf.split_info(main)
    warns = list(est.warnings)
    if sc and sc[1] != len(shards):
        warns.append(f"split.count is {sc[1]} but the repo has {len(shards)} shard(s) for this quant")
    if any(f["sha256"] is None for f in shards):
        warns.append("a file has no LFS sha256 on the Hub: pull would refuse it")
    tmpl = main.kv.get("tokenizer.chat_template")
    lic = card.get("license")
    flag = _license_flag(lic, card.get("gated"))
    return {
        "repo": repo, "revision": sha, "requested_revision": rev or "main",
        "quant": key, "files": [{"path": f["path"], "bytes": f["size"], "sha256": f["sha256"]} for f in shards],
        "mmproj": None if not mmproj else {"path": mmproj["path"], "bytes": mmproj["size"]},
        "other_quants": [q for q in quants if q["quant"] != key],
        "arch": est.arch, "arch_supported": bool(est.arch in fit.SUPPORTED_ARCHS),
        "llama_cpp_build": fit.LLAMA_CPP_BUILD,
        "license": lic, "license_flag": flag, "gated": card.get("gated") or False,
        "base_model": card.get("base_model"),
        "chat_template": {"present": isinstance(tmpl, str), "bytes": len(tmpl.encode()) if isinstance(tmpl, str) else 0},
        "ctx": ctx, "trained_ctx": trained, "kv_type": args.kv_type,
        "fit": {**est.as_dict(), "warnings": warns},
        "registry_fit": {"vram_gb": est.vram_gb, "speed_class": est.speed_class},
    }


def _license_flag(lic, gated) -> str | None:
    """Same intent as the registry's license.flag (P1-SPEC): noncommercial | other | custom | gated | None."""
    s = (lic or "").strip().lower()
    if re.search(r"(^|[-_.])nc([-_.]|$)", s) or "noncommercial" in s or "non-commercial" in s:
        return "noncommercial"
    if not s or s in ("other", "unknown"):
        return "other"
    if s not in OSI_LICENSES:
        return "custom"
    return "gated" if gated else None


def print_report(r: dict) -> None:
    f = r["fit"]
    gb = lambda b: f"{b / 1e9:.1f} GB"  # noqa: E731
    shard_note = f" (+{len(r['files']) - 1} shard(s))" if len(r["files"]) > 1 else ""
    print(f"repo        {r['repo']} @ {r['revision'][:12]} ({r['requested_revision']})")
    print(f"quant       {r['quant']}{shard_note}  {gb(sum(x['bytes'] for x in r['files']))}")
    if r["mmproj"]:
        print(f"mmproj      {r['mmproj']['path']}  {gb(r['mmproj']['bytes'])}  (on GPU0 / the model's GPU)")
    print(f"arch        {r['arch']}  ({'supported' if r['arch_supported'] else 'NOT supported'} by llama.cpp "
          f"{r['llama_cpp_build']})")
    print(f"licence     {r['license'] or '?'}{'  [' + r['license_flag'] + ']' if r['license_flag'] else ''}"
          f"   gated: {_gated(r['gated'])}")
    t = r["chat_template"]
    print(f"template    {'embedded, ' + format(t['bytes'], ',') + ' bytes' if t['present'] else 'MISSING'}")
    print(f"context     {r['ctx']} (trained {r['trained_ctx']}), KV {r['kv_type']}")
    split = {"none": "-sm none", "layer": "-sm layer -ts 1,1", "tensor": "-sm tensor + ${tp}", None: "-"}[f["split"]]
    print(f"placement   {f['placement']}  ({split})" + ("" if f["fits"] else "  -> DOES NOT FIT"))
    v = f["vram_gb"]
    print(f"VRAM        GPU0 {v[0]:5.1f} GiB   GPU1 {v[1]:5.1f} GiB   (limit {fit.USABLE_GIB} each = 24 - "
          f"{fit.HEADROOM_GIB} headroom; calibration error about +-0.6)")
    if f["cpu_expert_layers"]:
        print(f"CPU experts {len(f['cpu_expert_layers'])} layers  -ot \"{f['cpu_override']}\"")
    print(f"host RAM    {f['ram_gb']:.1f} GiB of weights (input embeddings, CPU experts)")
    print(f"speed       ~{f['tok_s']:.0f} tok/s decode, class {f['speed_class']} (A >80, B 30-80, C 10-30, D <10)")
    for w in f["warnings"]:
        print(f"warning     {w}")
    if r["other_quants"]:
        others = ", ".join(f"{q['quant'].rsplit('/', 1)[-1]} {q['gb']:.0f}GB" for q in r["other_quants"][:12])
        print(f"other quants {others}{' ...' if len(r['other_quants']) > 12 else ''}  (--file PATTERN)")


def main(argv=None) -> int:  # standalone use: python3 cli_fit.py fit org/repo
    ap = argparse.ArgumentParser(prog="spark-model")
    add_parsers(ap.add_subparsers(dest="cmd", required=True))
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
