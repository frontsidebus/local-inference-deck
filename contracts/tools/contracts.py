#!/usr/bin/env python3
"""Generate and check the contract artifacts in contracts/ (see contracts/README.md).

    python3 contracts/tools/contracts.py aliases [--write]   # contracts/aliases.json from contracts/gateway.yaml
    python3 contracts/tools/contracts.py vectors [--write]   # contracts/serving-identity/vectors.json
    python3 contracts/tools/contracts.py vendor  [--write]   # the vendored copies other components test against

Without --write each command only checks: exit 0 when the file on disk is what it would write, 1 when it differs
(it names the files). Offline, no site values, stdlib plus PyYAML (aliases only).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[2]
CONTRACTS = REPO / "contracts"
GATEWAY = CONTRACTS / "gateway.yaml"
ALIASES = CONTRACTS / "aliases.json"
SERVING = CONTRACTS / "serving-identity"
VECTORS = SERVING / "vectors.json"
FIXTURE_TEMPLATE = SERVING / "llama-swap.contract.yaml"

# (source owned by the producing component, vendored copy a consumer tests against). Paths are repo-relative.
# A copy must be byte-identical to its source; refresh with `vendor --write` in the same commit as the source
# change (contracts/README.md). After the split, `deck check --integration` compares across checkouts instead.
VENDORED = (
    # C3 gateway aliases: sec-evals and agent-judge consume them
    ("contracts/aliases.json", "evals/tests/fixtures/contracts/aliases.json"),
    ("contracts/aliases.json", "judge/tests/fixtures/contracts/aliases.json"),
    # C4 serving identity: sec-evals consumes the vectors, the fixture template and the agent's JSON schema
    ("contracts/serving-identity/vectors.json", "evals/tests/fixtures/contracts/serving-vectors.json"),
    ("contracts/serving-identity/llama-swap.contract.yaml", "evals/tests/fixtures/llama-swap.contract.yaml"),
    ("contracts/serving-identity/serving.schema.json", "evals/tests/fixtures/contracts/serving.schema.json"),
    # C7 backend observability and C8 Hermes profile: agent-judge consumes them
    ("contracts/observability.yaml", "judge/tests/fixtures/contracts/observability.yaml"),
    ("contracts/hermes-profile.yaml", "judge/tests/fixtures/contracts/hermes-profile.yaml"),
    # C5 eval liveness and C6 gate CLI: sec-evals owns them, the deck consumes them
    ("evals/contracts/eval-liveness.json", "contracts/vendored/sec-evals/eval-liveness.json"),
    ("evals/contracts/gate-cli.json", "contracts/vendored/sec-evals/gate-cli.json"),
    # C9 frontier env strip list and C10 infra-repo requirements: agent-judge owns them
    ("judge/contracts/frontier-env.json", "evals/tests/fixtures/contracts/frontier-env.json"),
    ("judge/contracts/infra-repo.json", "contracts/vendored/agent-judge/infra-repo.json"),
)


def dump_json(data) -> str:
    return json.dumps(data, indent=2, sort_keys=False) + "\n"


# ------------------------------------------------------------------------------------------------ C3 aliases
def load_gateway(path: Path = GATEWAY) -> dict:
    import yaml  # PyYAML (python3-yaml)
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def gen_aliases(gw: dict) -> str:
    """aliases.json: the alias table of gateway.yaml as JSON, so consumers need no YAML parser."""
    return dump_json({
        "contract": "gateway",
        "contract_version": gw["contract_version"],
        "generated_from": "contracts/gateway.yaml (python3 contracts/tools/contracts.py aliases --write)",
        "frontier_safety_net": gw["routes"]["frontier_safety_net"],
        "lab_prefix": gw["routes"]["lab_prefix"],
        "aliases": {name: dict(a) for name, a in gw["aliases"].items()},
    })


# ------------------------------------------------------------------------------------------------ C4 vectors
LOOKUPS = ("coder", "qwen3.8-27b", "fast", "fast-2", "example-moe", "example-lab", "no-such-alias")


def gen_vectors(text: str) -> str:
    """vectors.json from the fixture template, with the deck's reader (walter/models/lib/swapcmd.py)."""
    sys.path.insert(0, str(REPO / "walter" / "models" / "lib"))
    import swapcmd
    ents = swapcmd.entries(text)
    cmds = cmd_texts(text, swapcmd)

    def lookup(alias):
        for mid, e in ents.items():
            if alias == mid or alias in e["aliases"]:
                return mid
        return None
    return dump_json({
        "contract": "serving-identity",
        "contract_version": "1.0",
        "generated_from": "contracts/serving-identity/llama-swap.contract.yaml "
                          "(python3 contracts/tools/contracts.py vectors --write)",
        "flags": list(swapcmd.FLAGS),
        "hash": "sha256 of the cmd text, UTF-8: the cmd block's lines stripped and joined with one space, then "
                "${macro} references expanded (3 passes); unknown ${...} stay as written",
        "entries": {mid: {"name": e["name"], "aliases": e["aliases"], "cmd": cmds[mid],
                          "cmd_sha256": e["cmd_sha256"], "server_flags": e["server_flags"]}
                    for mid, e in ents.items()},
        "lookups": {a: lookup(a) for a in LOOKUPS},
        "coder": {"alias": "coder", "model_id": "qwen3.8-27b",
                  "image_pinned": "llama.cpp:server-cuda@sha256:", "no_unexpanded": "${image}",
                  "server_flags": {"--temp": "0.6"}},
    })


def cmd_texts(text: str, swapcmd) -> dict:
    """The expanded cmd text per model (swapcmd only exposes its hash): the same parse, kept here for the vectors."""
    import re
    macros = {}
    msec = re.search(r"^macros:\n(.*?)^\S", text, re.S | re.M)
    if msec:
        for k, v in re.findall(r'^\s+"([\w-]+)":\s*"(.*)"\s*$', msec.group(1), re.M):
            macros[k] = v
    out = {}
    mods = re.search(r"^models:\n(.*?)(?=^\S|\Z)", text, re.S | re.M)
    for m in re.finditer(r'^  "([^"]+)":\n((?:^(?:    .*|\s*)\n)+)', mods.group(1), re.M):
        cm = re.search(r"^    cmd: \|\n((?:^      .*\n)+)", m.group(2), re.M)
        cmd = " ".join(ln.strip() for ln in cm.group(1).splitlines()) if cm else ""
        for _ in range(3):
            cmd = re.sub(r"\$\{([\w-]+)\}", lambda x: macros.get(x.group(1), x.group(0)), cmd)
        assert swapcmd.sha256_text(cmd) == swapcmd.entries(text)[m.group(1)]["cmd_sha256"], m.group(1)
        out[m.group(1)] = cmd
    return out


# ------------------------------------------------------------------------------------------------ vendoring
def vendor_drift(repo: Path = REPO):
    """[(source, copy, why)] for every vendored copy that is missing or differs from its source."""
    out = []
    for src, dst in VENDORED:
        s, d = repo / src, repo / dst
        if not s.is_file():
            out.append((src, dst, "source missing"))
        elif not d.is_file():
            out.append((src, dst, "copy missing"))
        elif s.read_bytes() != d.read_bytes():
            out.append((src, dst, "copy differs"))
    return out


def _check_or_write(path: Path, want: str, write: bool) -> int:
    have = path.read_text(encoding="utf-8") if path.is_file() else None
    if have == want:
        print(f"ok        {path.relative_to(REPO)}")
        return 0
    if write:
        path.write_text(want, encoding="utf-8")
        print(f"written   {path.relative_to(REPO)}")
        return 0
    print(f"STALE     {path.relative_to(REPO)} (re-run with --write)")
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=("aliases", "vectors", "vendor"))
    ap.add_argument("--write", action="store_true", help="write the file(s) instead of only checking")
    a = ap.parse_args(argv)
    if a.what == "aliases":
        return _check_or_write(ALIASES, gen_aliases(load_gateway()), a.write)
    if a.what == "vectors":
        return _check_or_write(VECTORS, gen_vectors(FIXTURE_TEMPLATE.read_text(encoding="utf-8")), a.write)
    rc = 0
    for src, dst, why in vendor_drift():
        if a.write and why != "source missing":
            (REPO / dst).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO / src, REPO / dst)
            print(f"copied    {src} -> {dst}")
        else:
            print(f"{why.upper():<9} {src} -> {dst}")
            rc = 1
    if rc == 0 and not a.write:
        print(f"ok        {len(VENDORED)} vendored copies match their sources")
    return rc


if __name__ == "__main__":
    sys.exit(main())
