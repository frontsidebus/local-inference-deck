#!/usr/bin/env python3
"""tools/site_keys.py: the C1 site-key registry (contracts/site-keys.yaml) and what is derived from it.

Stdlib only. Never prints a site value: output names keys, counts and paths, nothing else.

    python3 tools/site_keys.py validate                 registry: schema + consistency checks
    python3 tools/site_keys.py policy [--check]         write contracts/sanitize-policy.yaml (--check: compare only)
    python3 tools/site_keys.py keys COMPONENT           names of the keys a component owns or consumes
    python3 tools/site_keys.py slice COMPONENT --site-env FILE [-o FILE]
    python3 tools/site_keys.py check --site-env FILE [--judge-env FILE] [--sec-evals DIR] [--lock FILE]

`bin/deck env slice` and `bin/deck check` resolve the site env (--site-env > $SITE_ENV > ~/.config/deck/site.env >
<repo>/site.env) and call `slice` / `check` with it.

Registry format: a constrained YAML subset (see the header of contracts/site-keys.yaml): comments, blank lines,
`name: <JSON scalar>`, `name:` opening a list, and `  - {one JSON object}` list items. JSON objects are YAML flow
mappings, so a YAML parser reads the same data; this module needs no PyYAML (the bash tools stay dependency-free).
"""
import argparse
import fnmatch
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
REGISTRY = REPO / "contracts" / "site-keys.yaml"
SCHEMA = REPO / "contracts" / "site-keys.schema.json"
POLICY = REPO / "contracts" / "sanitize-policy.yaml"
EXAMPLE = REPO / "site.env.example"
LOCK = REPO / "deck.lock"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
# a site.env assignment line, as render.sh and the deploy scripts see it (bash `.`)
ASSIGN_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=")
EXAMPLE_RE = re.compile(r"^(#\s*)?([A-Z][A-Z0-9_]*)=(.*)$")
# check-sanitized.sh reads only keys matching ^[A-Z_]+$ (no digits); other keys are never checked
SANITIZER_KEY_RE = re.compile(r"^[A-Z_]+$")


class RegistryError(Exception):
    pass


# ----------------------------------------------------------------------------------------------- registry
def load_registry(path=REGISTRY):
    """Parse the constrained YAML subset. Raises RegistryError naming the line on anything else."""
    data, current = {}, None
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "\t" in line:
            raise RegistryError(f"{path}:{n}: tab character")
        m = re.match(r"^([a-z_]+):(?: (.*))?$", line)
        if m:
            name, rest = m.group(1), m.group(2)
            if name in data:
                raise RegistryError(f"{path}:{n}: duplicate top-level name {name}")
            if rest is None or rest == "":
                data[name], current = [], name
            else:
                try:
                    data[name] = json.loads(rest)
                except ValueError as e:
                    raise RegistryError(f"{path}:{n}: {name}: not a JSON scalar ({e})") from None
                if isinstance(data[name], (dict, list)):
                    raise RegistryError(f"{path}:{n}: {name}: only scalars after `name: `")
                current = None
            continue
        m = re.match(r"^  - (\{.*\})$", line)
        if m and current is not None:
            try:
                item = json.loads(m.group(1))
            except ValueError as e:
                raise RegistryError(f"{path}:{n}: list item is not one JSON object ({e})") from None
            data[current].append(item)
            continue
        raise RegistryError(f"{path}:{n}: line shape not allowed (see the file header)")
    return data


def validate_schema(value, schema, where="$"):
    """A stdlib subset of JSON Schema: type, const, enum, pattern, required, properties, additionalProperties,
    items, minItems, uniqueItems, minimum. Returns a list of error strings."""
    errs = []
    types = {"object": dict, "array": list, "string": str, "boolean": bool, "integer": int, "number": (int, float)}
    t = schema.get("type")
    if t:
        ok = isinstance(value, types[t]) and not (t in ("integer", "number") and isinstance(value, bool))
        if not ok:
            return [f"{where}: expected {t}"]
    if "const" in schema and value != schema["const"]:
        errs.append(f"{where}: must be {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{where}: {value!r} not in {schema['enum']}")
    if "pattern" in schema and isinstance(value, str) and not re.search(schema["pattern"], value):
        errs.append(f"{where}: {value!r} does not match {schema['pattern']}")
    if "minimum" in schema and isinstance(value, (int, float)) and value < schema["minimum"]:
        errs.append(f"{where}: below {schema['minimum']}")
    if isinstance(value, dict):
        for k in schema.get("required", []):
            if k not in value:
                errs.append(f"{where}: missing {k}")
        props = schema.get("properties", {})
        for k, v in value.items():
            if k in props:
                errs += validate_schema(v, props[k], f"{where}.{k}")
            elif schema.get("additionalProperties") is False:
                errs.append(f"{where}: unexpected property {k}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errs.append(f"{where}: fewer than {schema['minItems']} items")
        if schema.get("uniqueItems"):
            seen = [json.dumps(x, sort_keys=True) for x in value]
            if len(seen) != len(set(seen)):
                errs.append(f"{where}: items are not unique")
        if "items" in schema:
            for i, v in enumerate(value):
                errs += validate_schema(v, schema["items"], f"{where}[{i}]")
    return errs


def validate(reg, schema=None):
    """Schema validation plus the cross-field rules a schema cannot say. Returns a list of errors."""
    if schema is None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    errs = validate_schema(reg, schema)
    if errs:
        return errs
    comps = [c["name"] for c in reg["components"]]
    classes = [c["name"] for c in reg["sanitize_classes"]]
    for label, names in (("component", comps), ("sanitize class", classes),
                         ("key", [k["name"] for k in reg["keys"]])):
        dup = sorted({x for x in names if names.count(x) > 1})
        if dup:
            errs.append(f"duplicate {label}(s): {' '.join(dup)}")
    if sorted(classes) != sorted(schema["properties"]["keys"]["items"]["properties"]["sanitize"]["enum"]):
        errs.append("sanitize_classes and the schema's sanitize enum differ")
    for k in reg["keys"]:
        n = k["name"]
        for c in [k["owner"], *k["consumers"]]:
            if c not in comps:
                errs.append(f"{n}: unknown component {c}")
        if k["required"] and not k["active"]:
            errs.append(f"{n}: required but commented out in site.env.example")
        if action_of(reg, k["sanitize"]) != "skip" and not SANITIZER_KEY_RE.match(n):
            errs.append(f"{n}: class {k['sanitize']} but check-sanitized.sh never reads keys with digits; use a skip class")
    return errs


def action_of(reg, cls):
    for c in reg["sanitize_classes"]:
        if c["name"] == cls:
            return c["action"]
    raise RegistryError(f"unknown sanitize class {cls}")


def component(reg, name):
    for c in reg["components"]:
        if c["name"] == name:
            return c
    return None


def component_keys(reg, name):
    """Registry keys (in registry order) that the component owns or consumes."""
    return [k["name"] for k in reg["keys"] if k["owner"] == name or name in k["consumers"]]


def classify(reg, key):
    """The sanitize class check-sanitized.sh applies to KEY: its registry entry, else the first fallback glob,
    else `check`."""
    for k in reg["keys"]:
        if k["name"] == key:
            return k["sanitize"]
    for r in reg["sanitize_fallback"]:
        if fnmatch.fnmatchcase(key, r["key"]):
            return r["sanitize"]
    return "check"


# ----------------------------------------------------------------------------------------------- policy
POLICY_HEADER = """\
# contracts/sanitize-policy.yaml: GENERATED by `python3 tools/site_keys.py policy` from the sanitize classes in
# contracts/site-keys.yaml. Do not edit by hand; tools/tests/test_site_keys.py fails when it is stale.
#
# Read by scripts/check-sanitized.sh line by line (no YAML parser). For each KEY=value in the site env and in
# judge.env, the first rule whose `key` (a shell glob) matches KEY gives the action:
#   check   a real value (one that differs from site.env.example) is forbidden in tracked files
#   split   the same, for each space-separated item of the value
#   skip    not checked
# A key that no rule matches gets `default`. Without this file check-sanitized.sh uses its built-in table, which
# this file reproduces exactly (scripts/tests/test_sanitize_policy.py).
"""


def render_policy(reg):
    lines = [POLICY_HEADER.rstrip("\n"), f"contract_version: {reg['contract_version']}", "default: check", "rules:"]
    rule = '  - {{"key": "{}", "action": "{}", "class": "{}"}}'
    for k in reg["keys"]:
        lines.append(rule.format(k["name"], action_of(reg, k["sanitize"]), k["sanitize"]))
    for r in reg["sanitize_fallback"]:
        lines.append(rule.format(r["key"], action_of(reg, r["sanitize"]), r["sanitize"]))
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------------------------- site.env.example
def parse_example(path=EXAMPLE):
    """[(name, active, raw default)] for every KEY= line, active or `# KEY=` commented. The raw default is the
    value exactly as written (quotes kept), without the trailing comment."""
    out = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        m = EXAMPLE_RE.match(line)
        if not m:
            continue
        rest = m.group(3)
        if rest.startswith('"'):
            j = rest.find('"', 1)
            if j < 0:
                raise RegistryError(f"{path}:{n}: unterminated quote")
            raw, tail = rest[:j + 1], rest[j + 1:]
        else:
            raw, tail = re.match(r"(\S*)(.*)$", rest).groups()
        if tail and not re.match(r"\s+#", tail):
            raise RegistryError(f"{path}:{n}: unexpected text after the value")
        out.append((m.group(2), not m.group(1), raw))
    return out


# ----------------------------------------------------------------------------------------------- site env
def assigned_keys(path):
    """Key names assigned in an env file (bash syntax), in first-assignment order."""
    seen = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        m = ASSIGN_RE.match(line)
        if m and m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def sourced_values(path, keys):
    """{key: value} for the keys that are set after `set -a; . FILE` in a clean bash (HOME and PATH only), i.e.
    what the deploy scripts and render.sh see. Values stay in this process; callers never print them."""
    if not keys:
        return {}
    script = ('set -a; . "$1" >/dev/null 2>&1; shift; '
              'for k in "$@"; do if [[ -v $k ]]; then printf "%s\\0%s\\0" "$k" "${!k}"; fi; done')
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/nonexistent")}
    r = subprocess.run(["bash", "--norc", "--noprofile", "-c", script, "site_keys", str(path), *keys],
                       env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    parts = r.stdout.decode("utf-8", "surrogateescape").split("\0")
    return dict(zip(parts[0:-1:2], parts[1:-1:2]))


def die(msg, rc=2):
    print(f"deck: {msg}", file=sys.stderr)
    sys.exit(rc)


def default_slice_path(name):
    return Path(os.environ.get("HOME", "/nonexistent")) / ".config" / "deck" / "slices" / f"{name}.env"


def cmd_slice(reg, name, site_env, out):
    comp = component(reg, name)
    if comp is None:
        die(f"unknown component '{name}' (one of: {' '.join(c['name'] for c in reg['components'])})")
    if comp.get("slice") is False:
        die(f"refusing to slice '{name}': {comp.get('slice_refusal', 'this component reads the full site env')}")
    site_env = Path(site_env)
    if not site_env.is_file():
        die(f"site env is not a readable file: {site_env}")
    if out is None:
        out = default_slice_path(name)
        out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    out = Path(out)
    if os.path.lexists(out):
        st = os.lstat(out)
        if not stat.S_ISREG(st.st_mode):
            die(f"refusing to overwrite {out}: not a regular file (a symlink, directory or device)")
        if os.path.samefile(out, site_env):
            die(f"refusing to overwrite the site env itself: {out}")
    if not out.parent.is_dir():
        die(f"output directory does not exist: {out.parent}")

    want = set(component_keys(reg, name))
    lines = Path(site_env).read_text(encoding="utf-8", errors="surrogateescape").splitlines()
    kept, picked = [], []
    for line in lines:
        m = ASSIGN_RE.match(line)
        if m and m.group(1) in want:
            kept.append(line)          # verbatim: line parsers (lib.sh site_value, spark-model) see the same text
            if m.group(1) not in picked:
                picked.append(m.group(1))
    known = {k["name"] for k in reg["keys"]}
    unknown = [k for k in assigned_keys(site_env) if k not in known]
    absent = [k for k in component_keys(reg, name) if k not in picked]

    header = [f"# site.env slice for '{name}': written by `bin/deck env slice {name}` from contracts/site-keys.yaml",
              f"# (contract_version {reg['contract_version']}). It holds only the keys the registry assigns to {name}.",
              "# Do not edit: change the full site.env, then slice again."]
    fd, tmp = tempfile.mkstemp(prefix=".slice-", dir=str(out.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write("\n".join(header + kept) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        # The slice must give every picked key the value the full file gives it (a value that refers to an
        # unsliced key, a multi-line value or code in the site env would not survive line copying).
        full, part = sourced_values(site_env, picked), sourced_values(tmp, picked)
        bad = [k for k in picked if full.get(k) != part.get(k) or ((k in full) != (k in part))]
        if bad:
            die(f"refusing to write the slice: these keys would not keep their value: {' '.join(bad)} "
                "(a value that refers to another key, spans lines or runs code?)", 1)
        os.replace(tmp, out)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
    print(f"deck env slice: {name}: {len(picked)} keys -> {out} (mode 600)")
    print(f"  keys: {' '.join(picked) if picked else '(none)'}")
    if absent:
        print(f"  not set in the site env ({len(absent)}): {' '.join(absent)}")
    if unknown:
        print(f"  left out, not in the registry ({len(unknown)}): {' '.join(unknown)}")
    req = [k["name"] for k in reg["keys"] if k["required"] and k["name"] in absent]
    if req:
        print(f"deck env slice: WARN required keys missing from the site env: {' '.join(req)}", file=sys.stderr)
    return 0


# ----------------------------------------------------------------------------------------------- check
def resolve_judge_env(explicit):
    """(path or None, origin). $JUDGE_ENV must exist when set; the default may be absent."""
    if explicit:
        return Path(explicit), "--judge-env"
    if os.environ.get("JUDGE_ENV"):
        return Path(os.environ["JUDGE_ENV"]), "$JUDGE_ENV"
    p = Path(os.environ.get("HOME", "/nonexistent")) / ".config" / "judge" / "judge.env"
    if p.is_symlink() and not p.exists():
        return p, "~/.config/judge/judge.env (dangling symlink)"
    return (p if p.is_file() else None), "~/.config/judge/judge.env"


def check_site_env(reg, site_env, out):
    fails = 0
    keys = assigned_keys(site_env)
    known = {k["name"]: k for k in reg["keys"]}
    unknown = [k for k in keys if k not in known]
    values = sourced_values(site_env, [k["name"] for k in reg["keys"] if k["required"]])
    missing = [k["name"] for k in reg["keys"] if k["required"] and not values.get(k["name"])]
    out(f"site env: {len(keys)} keys; registry: {len(known)} keys (contract_version {reg['contract_version']})")
    for k in unknown:
        out(f"  WARN unknown key (not in contracts/site-keys.yaml): {k}")
    for k in missing:
        out(f"  FAIL required key missing or empty: {k}")
        fails += 1
    return fails


def check_judge_env(reg, judge_env, origin, out):
    if judge_env is None:
        out("judge.env: none (ok)")
        return 0
    if not judge_env.is_file():
        out(f"judge.env: FAIL not a readable file ({origin})")
        return 1
    fails = 0
    known = {k["name"] for k in reg["keys"]}
    keys = []
    for line in judge_env.read_text(encoding="utf-8", errors="replace").splitlines():
        m = ASSIGN_RE.match(line)          # judge.env is parsed, never sourced: KEY=value lines
        if m and m.group(1) not in keys:
            keys.append(m.group(1))
    out(f"judge.env: {len(keys)} keys ({origin})")
    if "SITE_ENV" not in keys:
        out("  WARN no SITE_ENV= line (the judge then falls back to $SITE_ENV / ~/.config/deck/site.env)")
    for k in keys:
        if k == "SITE_ENV":
            continue
        if classify(reg, k) != "judge-knob":
            out(f"  FAIL site-identifying key in judge.env (move it to site.env): {k}")
            fails += 1
        elif k not in known:
            out(f"  WARN unknown judge knob (not in contracts/site-keys.yaml): {k}")
    return fails


def check_evals_link(site_env, sec_evals, out):
    """The sec-evals checkout's gitignored site.env symlink (`deck install evals`) resolves to the site env deck
    resolved: run.py's --site-env default and the plan scripts read it. No checkout: a note, not a failure."""
    if not sec_evals:
        out("sec-evals: no checkout given (skipped)")
        return 0
    se = Path(sec_evals)
    if not (se / "evals" / "run.py").is_file():
        out(f"sec-evals: no checkout at {se} (ok; `deck evals` and `deck install evals` need one)")
        return 0
    link = se / "site.env"
    if not link.is_symlink():
        what = "a regular file, not a symlink" if link.exists() else "missing"
        out(f"sec-evals: FAIL {link} is {what}; run: bin/deck install evals")
        return 1
    if not link.exists():
        out(f"sec-evals: FAIL {link} is a dangling symlink; run: bin/deck install evals --force")
        return 1
    if link.resolve() != Path(site_env).resolve():
        out(f"sec-evals: FAIL {link} points at a different file than the resolved site env; "
            "run: bin/deck install evals --force")
        return 1
    out(f"sec-evals: site.env link ok ({link})")
    return 0


def check_lock(sec_evals, out, lock=LOCK):
    """deck.lock: for sec-evals (mode minimum) the locked commit must be an ancestor of the checkout's HEAD. Only
    warns, and never moves HEAD: sec-evals is a working checkout (the attempts ledger is committed there).
    For agent-judge (mode pinned) the checkout's HEAD must be the locked commit, a failure otherwise: the live hooks
    and units run from that checkout ($AGENT_JUDGE_DIR, else the lock's path). No checkout: skipped."""
    if not Path(lock).is_file():
        out("deck.lock: none (skipped)")
        return 0
    try:
        data = load_registry(lock)
    except RegistryError as e:
        out(f"deck.lock: FAIL {e}")
        return 1
    fails = 0
    for c in data.get("components", []):
        name, mode, commit = c.get("name"), c.get("mode"), c.get("commit")
        if mode not in ("pinned", "minimum"):
            out(f"deck.lock: FAIL {name}: mode must be pinned or minimum")
            fails += 1
            continue
        if name not in ("sec-evals", "agent-judge"):
            out(f"deck.lock: {name}: not checked here")
            continue
        if not (isinstance(commit, str) and SHA_RE.match(commit)):
            out(f"deck.lock: {name}: no commit locked yet (skipped)")
            continue
        if name == "agent-judge":
            fails += check_pinned(name, commit, c.get("path") or "", out)
            continue
        se = Path(sec_evals) if sec_evals else None
        if se is None or not (se / "evals" / "run.py").is_file():
            out(f"deck.lock: {name}: no checkout (skipped)")
            continue
        r = subprocess.run(["git", "-C", str(se), "merge-base", "--is-ancestor", commit, "HEAD"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            out(f"deck.lock: {name}: checkout contains the locked commit ({mode})")
        elif r.returncode == 1:
            out(f"deck.lock: {name}: WARN the checkout's HEAD does not contain the locked commit {commit[:12]}"
                " (update the checkout; deck never moves its HEAD)")
        else:
            out(f"deck.lock: {name}: WARN the locked commit {commit[:12]} is not in the checkout (git fetch?)")
    return fails


def check_pinned(name, commit, path, out):
    """agent-judge (mode pinned): its checkout's HEAD must be the locked commit. Never moves HEAD."""
    aj = Path(os.environ.get("AGENT_JUDGE_DIR") or os.path.expanduser(path))
    if not (aj / "judge" / "contracts" / "infra-repo.json").is_file():
        out(f"deck.lock: {name}: no checkout (skipped)")
        return 0
    r = subprocess.run(["git", "-C", str(aj), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=60)
    head = r.stdout.strip()
    if r.returncode == 0 and head == commit:
        out(f"deck.lock: {name}: checkout is at the locked commit (pinned)")
        return 0
    out(f"deck.lock: {name}: FAIL the checkout's HEAD {head[:12] or '?'} is not the locked commit {commit[:12]}"
        " (check out the locked tag there, or update deck.lock; deck never moves its HEAD)")
    return 1


def cmd_check(reg, site_env, judge_env_flag, sec_evals=None, lock=LOCK):
    out = print
    fails = 0
    # Each check prints names and counts only and returns its failure count. Add new checks here.
    fails += check_site_env(reg, Path(site_env), out)
    jpath, origin = resolve_judge_env(judge_env_flag)
    fails += check_judge_env(reg, jpath, origin, out)
    fails += check_evals_link(site_env, sec_evals, out)
    fails += check_lock(sec_evals, out, lock)
    print(f"deck check: {'FAILED' if fails else 'OK'}{f' ({fails} failure(s))' if fails else ''}")
    return 1 if fails else 0


# ----------------------------------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(prog="site_keys.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate")
    p = sub.add_parser("policy")
    p.add_argument("--check", action="store_true", help="exit 1 when the committed policy is stale")
    p = sub.add_parser("keys")
    p.add_argument("component")
    p = sub.add_parser("slice")
    p.add_argument("component")
    p.add_argument("--site-env", required=True)
    p.add_argument("-o", "--output")
    p = sub.add_parser("check")
    p.add_argument("--site-env", required=True)
    p.add_argument("--judge-env")
    p.add_argument("--sec-evals", help="the sec-evals checkout (bin/deck passes $SEC_EVALS_DIR or ~/src/deck/sec-evals)")
    p.add_argument("--lock", default=str(LOCK), help="deck.lock (default: the repo's)")
    a = ap.parse_args(argv)
    try:
        reg = load_registry()
    except RegistryError as e:
        die(f"registry: {e}")
    errs = validate(reg)
    if errs:
        for e in errs:
            print(f"site-keys: {e}", file=sys.stderr)
        die(f"registry {REGISTRY} is invalid ({len(errs)} error(s))")
    if a.cmd == "validate":
        print(f"site-keys: OK ({len(reg['keys'])} keys, contract_version {reg['contract_version']})")
        return 0
    if a.cmd == "policy":
        text = render_policy(reg)
        if a.check:
            cur = POLICY.read_text(encoding="utf-8") if POLICY.exists() else ""
            if cur != text:
                print(f"site-keys: {POLICY} is stale; run: python3 tools/site_keys.py policy", file=sys.stderr)
                return 1
            print("site-keys: sanitize policy up to date")
            return 0
        POLICY.write_text(text, encoding="utf-8")
        print(f"site-keys: wrote {POLICY}")
        return 0
    if a.cmd == "keys":
        if component(reg, a.component) is None:
            die(f"unknown component '{a.component}'")
        print("\n".join(component_keys(reg, a.component)))
        return 0
    if a.cmd == "slice":
        return cmd_slice(reg, a.component, a.site_env, a.output)
    if a.cmd == "check":
        return cmd_check(reg, a.site_env, a.judge_env, a.sec_evals, a.lock)
    return 2


if __name__ == "__main__":
    sys.exit(main())
