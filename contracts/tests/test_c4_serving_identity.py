"""C4 provider (deck): the serving-identity vectors are what the deck's reader produces, and the real template's
coder entry has the fields the contract promises.

The vectors (contracts/serving-identity/vectors.json) are generated from the fixture template next to them with
walter/models/lib/swapcmd.py. sec-evals reproduces them with run.llama_swap_entry over a vendored copy of the
fixture (evals/tests/test_contract_serving_identity.py), so the two readers agree without importing each other.
Run: python3 -m pytest contracts/tests -q -p no:cacheprovider
"""
import json
import subprocess
import sys

import pytest

from contract_helpers import CONTRACTS, EXAMPLE, REPO, TOOL, load_json, render

sys.path.insert(0, str(REPO / "walter" / "models" / "lib"))
import swapcmd  # noqa: E402

SERVING = CONTRACTS / "serving-identity"
VECTORS = load_json(SERVING / "vectors.json")
FIXTURE = (SERVING / "llama-swap.contract.yaml").read_text()
TEMPLATE = REPO / "walter" / "llama-swap" / "config.yaml.tmpl"
AGENT = REPO / "walter" / "models" / "spark-model-agent"


def test_vectors_are_versioned_and_generated_from_the_fixture():
    assert VECTORS["contract"] == "serving-identity" and VECTORS["contract_version"].startswith("1.")
    assert (SERVING / "vectors.json").read_text() == TOOL.gen_vectors(FIXTURE), \
        "stale: python3 contracts/tools/contracts.py vectors --write (and bump contract_version)"


def test_swapcmd_reproduces_the_vectors():
    assert list(swapcmd.FLAGS) == VECTORS["flags"]
    got = swapcmd.entries(FIXTURE)
    assert set(got) == set(VECTORS["entries"])
    for mid, want in VECTORS["entries"].items():
        e = got[mid]
        assert e["cmd_sha256"] == want["cmd_sha256"] == swapcmd.sha256_text(want["cmd"]), mid
        assert e["server_flags"] == want["server_flags"] and e["aliases"] == want["aliases"], mid
        assert e["name"] == want["name"], mid
    for alias, mid in VECTORS["lookups"].items():
        hit = [m for m, e in got.items() if alias == m or alias in e["aliases"]]
        assert hit == ([mid] if mid else []), alias


def _coder_checks(entries, cmds):
    c = VECTORS["coder"]
    mids = [m for m, e in entries.items() if c["alias"] in e["aliases"]]
    assert mids == [c["model_id"]]
    cmd = cmds[c["model_id"]]
    assert c["image_pinned"] in cmd and c["no_unexpanded"] not in cmd
    for flag, value in c["server_flags"].items():
        assert entries[c["model_id"]]["server_flags"][flag] == value


def test_fixture_and_real_template_carry_the_coder_fields():
    """The real-template assertion that sec-evals' test_run.py::test_llama_swap_entry_from_template made against
    walter/llama-swap/config.yaml.tmpl while evals/ was in this repo (plan step 3 moved it here): coder resolves to
    the contract's model id, its cmd has a digest-pinned image and no unexpanded ${image}, --temp is pinned, and an
    unknown alias resolves to nothing. sec-evals now checks its reader against the vendored fixture only."""
    for text in (FIXTURE, TEMPLATE.read_text()):
        entries = swapcmd.entries(text)
        _coder_checks(entries, TOOL.cmd_texts(text, swapcmd))
        assert not [m for m, e in entries.items() if "no-such-alias" == m or "no-such-alias" in e["aliases"]]


def test_rendered_template_keeps_the_coder_fields(tmp_path):
    text = render(TEMPLATE, tmp_path / "config.yaml", EXAMPLE)
    _coder_checks(swapcmd.entries(text), TOOL.cmd_texts(text, swapcmd))


def test_agent_serving_json_matches_the_schema_and_the_vectors(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    (tmp_path / "config.yaml").write_text(FIXTURE)
    env = {"PATH": "/usr/bin:/bin", "SPARK_MODEL_ENV_FILE": str(tmp_path / "none.env"),
           "SPARK_MODEL_CONFIG_DIR": str(tmp_path), "SPARK_MODEL_MODELS_DIR": str(tmp_path / "models"),
           "SPARK_MODEL_LOCK": str(tmp_path / "lock")}
    r = subprocess.run([sys.executable, "-B", str(AGENT), "serving", "--json"], capture_output=True, text=True,
                       timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    jsonschema.validate(d, load_json(SERVING / "serving.schema.json"))
    assert d["models"] == {mid: {k: e[k] for k in ("aliases", "name", "cmd_sha256", "server_flags")}
                           for mid, e in VECTORS["entries"].items()}
