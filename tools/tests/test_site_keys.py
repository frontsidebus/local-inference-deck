"""C1 provider tests: contracts/site-keys.yaml against site.env.example, the templates, the secrets specs and the
scripts that read site keys; the schema; the generated sanitize policy. Offline, no site values. Run:
    python3 -m pytest tools/tests -q -p no:cacheprovider
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))
import site_keys as sk  # noqa: E402

# ${NAME}s in templates that are not site keys: render.sh leaves them alone (they are not in site.env.example) and
# a later stage fills them (compose, the shell, grafana, llama-swap, clients/install.sh's own list).
NON_SITE_VARS = {"DS_PROMETHEUS", "DUR", "HOME", "MODEL_ID", "PORT", "POSTGRES_PASSWORD",
                 "SPARK_KEY_DIR", "SPARK_KEY_DIR_TILDE", "V"}


@pytest.fixture(scope="module")
def reg():
    return sk.load_registry()


@pytest.fixture(scope="module")
def by_name(reg):
    return {k["name"]: k for k in reg["keys"]}


def tracked(*patterns):
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "--", *patterns],
                         capture_output=True, text=True, check=True).stdout.split()
    return [REPO / p for p in out]


def components_of(path):
    """The components whose deploy reads a template at PATH (repo-relative)."""
    rel = path.relative_to(REPO).as_posix()
    if rel.startswith("walter/digest/"):
        return {"digest", "walter"}          # walter/deploy.sh renders walter/digest too
    for c in ("walter", "covenant", "clients", "judge", "evals"):
        if rel.startswith(c + "/"):
            return {c}
    return set()


def assigned(entry, comp):
    return entry["owner"] == comp or comp in entry["consumers"]


# ------------------------------------------------------------------------------------------- format and schema
def test_registry_parses_and_validates(reg):
    assert reg["contract_version"] == 1
    assert sk.validate(reg) == []


def test_registry_is_yaml_too(reg):
    yaml = pytest.importorskip("yaml")
    assert yaml.safe_load((REPO / "contracts" / "site-keys.yaml").read_text()) == reg
    assert yaml.safe_load((REPO / "contracts" / "sanitize-policy.yaml").read_text())["rules"]


def test_schema_is_json_schema_shaped():
    schema = json.loads((REPO / "contracts" / "site-keys.schema.json").read_text())
    assert schema["type"] == "object" and "keys" in schema["required"]


def test_validator_rejects_bad_entries(reg):
    bad = json.loads(json.dumps(reg))
    bad["keys"][0]["sanitize"] = "nope"
    assert sk.validate(bad)
    bad = json.loads(json.dumps(reg))
    bad["keys"][0]["consumers"].append("nobody")
    assert any("unknown component" in e for e in sk.validate(bad))
    bad = json.loads(json.dumps(reg))
    bad["keys"][0]["extra"] = 1
    assert any("unexpected property" in e for e in sk.validate(bad))
    bad = json.loads(json.dumps(reg))
    bad["keys"].append(dict(bad["keys"][0]))
    assert any("duplicate key" in e for e in sk.validate(bad))
    oauth = next(k for k in bad["keys"] if k["name"] == "OAUTH2_PROXY_CLIENT_ID")
    oauth["sanitize"] = "check"      # a key with a digit is never read by check-sanitized.sh
    assert any("digits" in e for e in sk.validate(bad))


def test_parser_rejects_other_yaml(tmp_path):
    for text in ("keys:\n  - name: X\n", "keys:\n- {\"name\": \"X\"}\n", "a: [1]\n", "keys:\n\t- {}\n"):
        p = tmp_path / "r.yaml"
        p.write_text(text)
        with pytest.raises(sk.RegistryError):
            sk.load_registry(p)


# ------------------------------------------------------------------------------------------- site.env.example
def test_example_equals_registry(reg):
    example = sk.parse_example()
    assert [n for n, _, _ in example] == [k["name"] for k in reg["keys"]], \
        "site.env.example and contracts/site-keys.yaml list different keys (or a different order)"
    for (name, active, raw), k in zip(example, reg["keys"]):
        assert k["active"] == active, f"{name}: active flag"
        assert k["default"] == raw, f"{name}: default differs from site.env.example"


def test_judge_is_exempt_from_slicing(reg):
    assert sk.component(reg, "judge").get("slice") is False
    for c in reg["components"]:
        if c["name"] != "judge":
            assert c.get("slice", True) is True


def test_policy_is_up_to_date(reg):
    assert (REPO / "contracts" / "sanitize-policy.yaml").read_text() == sk.render_policy(reg), \
        "run: python3 tools/site_keys.py policy"


# ------------------------------------------------------------------------------------------- readers
def test_every_template_variable_is_registered_for_its_component(by_name):
    problems = []
    files = tracked("*.tmpl", "*.in")
    assert files
    for f in files:
        comps = components_of(f)
        text = f.read_text(encoding="utf-8", errors="replace")
        names = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", text)) | set(re.findall(r"@@if ([A-Z][A-Z0-9_]*)@@", text))
        for n in sorted(names):
            if n in by_name:
                for c in comps:
                    if not assigned(by_name[n], c):
                        problems.append(f"{f.relative_to(REPO)}: {n} is not assigned to {c}")
            elif n not in NON_SITE_VARS:
                problems.append(f"{f.relative_to(REPO)}: ${{{n}}} is not in contracts/site-keys.yaml "
                                "(add it to site.env.example and the registry, or to NON_SITE_VARS)")
    assert not problems, "\n".join(problems)


def code_refs(path, keys):
    """Registry keys referenced as $KEY / ${KEY...} on non-comment lines of a shell file."""
    refs = set()
    for line in path.read_text().splitlines():
        if line.lstrip().startswith("#"):
            continue
        refs |= {k for k in re.findall(r"\$\{?([A-Z][A-Z0-9_]*)", line) if k in keys}
    return refs


def test_secrets_specs_read_only_their_components_keys(by_name):
    specs = tracked("scripts/secrets.d/*.sh")
    assert {p.stem for p in specs} >= {"walter", "covenant"}
    for spec in specs:
        for k in code_refs(spec, by_name):
            assert assigned(by_name[k], spec.stem), f"{spec.name} reads {k}, which is not assigned to {spec.stem}"
    # the covenant spec reads SPARK_DIGEST_HOST from the env file it is given (a slice must carry it)
    assert "SPARK_DIGEST_HOST" in code_refs(REPO / "scripts/secrets.d/covenant.sh", by_name)


def array(text, name):
    m = re.search(rf"^{name}=\(([^)]*)\)", text, re.M)
    assert m, name
    return m.group(1).split()


def test_covenant_deploy_vars_are_in_the_covenant_slice(reg, by_name):
    text = (REPO / "covenant/deploy.sh").read_text()
    keys = set(sk.component_keys(reg, "covenant"))
    core, digest = array(text, "COVENANT_VARS"), array(text, "DIGEST_VARS")
    assert set(core) | set(digest) <= keys
    for k in core:
        assert by_name[k]["required"], f"{k}: covenant/deploy.sh refuses to run without it"
    for f in tracked("covenant/scripts/*.sh"):
        assert code_refs(f, by_name) <= keys, f.name
    assert code_refs(REPO / "covenant/deploy.sh", by_name) <= keys


def test_walter_deploy_vars_are_in_the_walter_slice(reg, by_name):
    text = (REPO / "walter/deploy.sh").read_text()
    keys = set(sk.component_keys(reg, "walter"))
    m = re.search(r"^for v in ((?:[A-Z_ ]|\\\n)+); do\n\s+\[\[ -n \$\{!v:-\} \]\] \|\| die", text, re.M)
    assert m
    required = m.group(1).replace("\\\n", " ").split()
    assert len(required) > 5 and set(required) <= keys
    for k in required:
        assert by_name[k]["required"], k
    assert code_refs(REPO / "walter/deploy.sh", by_name) <= keys


def test_clients_and_evals_readers(reg, by_name):
    clients = set(sk.component_keys(reg, "clients"))
    install = (REPO / "clients/install.sh").read_text()
    got = {k for k in re.findall(r"site_get ([A-Z][A-Z0-9_]*)", install) if k != "VAR"}
    assert got and got <= clients
    spark_model = (REPO / "clients/bin/spark-model").read_text()
    got = {k for k in re.findall(r'site_value\("([A-Z][A-Z0-9_]*)"\)', spark_model) if k in by_name}
    assert got and got <= clients
    # sec-evals is its own repo (plan step 3): its readers are checked in a checkout when one is present, and its
    # example (vendored, test_evals_example_matches_the_registry) always
    evals = set(sk.component_keys(reg, "evals"))
    se = sec_evals_dir()
    if se is not None:
        got = set()
        for f in sorted((se / "evals").glob("tools/*.sh")) + sorted((se / "evals").glob("plans/*.sh")):
            got |= {k for k in re.findall(r"site_value ([A-Z][A-Z0-9_]*)", f.read_text()) if k != "KEY"}  # usage
        for f in (se / "evals/gate.py", se / "evals/run.py"):
            got |= {k for k in re.findall(r'setting\("([A-Z][A-Z0-9_]*)"', f.read_text()) if k in by_name}
        assert got and got <= evals


EVALS_EXAMPLE = REPO / "contracts" / "vendored" / "sec-evals" / "evals.env.example"


def sec_evals_dir():
    import os
    d = Path(os.environ.get("SEC_EVALS_DIR") or Path.home() / "src" / "deck" / "sec-evals")
    return d if (d / "evals" / "run.py").is_file() else None


def test_evals_example_matches_the_registry(reg, by_name):
    """C1 coverage for sec-evals: its evals.env.example (vendored copy) holds exactly the registry keys with "evals"
    among their consumers, with the registry's example defaults; and it matches a sec-evals checkout when present."""
    rows = sk.parse_example(EVALS_EXAMPLE)
    active = {name: raw for name, act, raw in rows if act}
    assert len(active) == len([r for r in rows if r[1]]), "a key is listed twice"
    assert set(active) == {k["name"] for k in reg["keys"] if "evals" in k["consumers"] and k["active"]}
    for name, raw in active.items():
        assert by_name[name]["default"] == raw, name
    se = sec_evals_dir()
    if se is not None and (se / "evals.env.example").is_file():
        assert (se / "evals.env.example").read_bytes() == EVALS_EXAMPLE.read_bytes()


def test_judge_reads_what_sitemask_masks(by_name):
    # sitemask masks hosts, addresses, users, buckets, ids by key pattern; those keys list the judge
    pats = ("_EMAIL", "_IP", "_IPS", "_SUBNET", "_SSH_USER", "_BUCKET", "_PUBLIC_KEY", "_UUID", "_CLIENT_ID")
    for n, k in by_name.items():
        if n == "SPARK_DOMAIN" or (n.startswith("SPARK_") and n.endswith("_HOST")) or n.endswith(pats):
            assert assigned(k, "judge"), n


def test_component_keys_cli():
    r = subprocess.run([sys.executable, "-B", str(REPO / "tools/site_keys.py"), "keys", "covenant"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "SPARK_DIGEST_HOST" in r.stdout.split()
    r = subprocess.run([sys.executable, "-B", str(REPO / "tools/site_keys.py"), "policy", "--check"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
