"""swapcmd.py and ``spark-model-agent serving``: the live config's cmd hashes, as the eval gate records them.

swapcmd.entries must hash a cmd exactly as evals/run.py's llama_swap_entry does, or the gate's serving identity
would change with the reader instead of the config. The contract between the two readers is C4
(contracts/serving-identity/vectors.json): the deck's provider test (contracts/tests/test_c4_serving_identity.py)
and the sec-evals consumer test (evals/tests/test_contract_serving_identity.py) each reproduce the vectors, so
neither side imports the other. The direct cross-check below (swapcmd vs run.py over the real rendered template and
lab entries) is kept only while evals/ is in this checkout, behind the vectors: it skips once evals/run.py is gone.
No root, no network.
Run: python3 -B -m pytest -q walter/tests/test_spark_model_serving.py
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML (python3-yaml) is needed for the registry")
HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "walter" / "models" / "lib"))
import registry as R  # noqa: E402
import swapcmd        # noqa: E402

VECTORS = json.loads((REPO / "contracts" / "serving-identity" / "vectors.json").read_text())
RUN_PY = REPO / "evals" / "run.py"
if RUN_PY.is_file():          # monorepo only (C4: the vectors are the contract; see the module docstring)
    sys.path.insert(0, str(RUN_PY.parent))
    import run as RUN     # noqa: E402
else:
    RUN = None
needs_run_py = pytest.mark.skipif(RUN is None, reason="evals/run.py is not in this checkout (C4 vectors cover it)")

AGENT = REPO / "walter" / "models" / "spark-model-agent"
TEMPLATE = REPO / "walter" / "llama-swap" / "config.yaml.tmpl"


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    d = tmp_path_factory.mktemp("serving")
    out = d / "config.yaml"
    r = subprocess.run(["bash", str(REPO / "scripts/render.sh"), "-e", str(REPO / "site.env.example"),
                        str(TEMPLATE), str(out)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    reg = yaml.safe_load((REPO / "walter/models/registry.example.yaml").read_text())
    for m in reg["models"]:
        m["enabled"] = True
    with_lab = d / "with-lab.yaml"
    with_lab.write_text(R.render(out.read_text(), R.validate(reg)))
    return {"template": TEMPLATE, "base": out, "lab": with_lab}


@pytest.mark.parametrize("which", ["template", "base", "lab"])
def test_entries_parse_every_model(rendered, which):
    got = swapcmd.entries(rendered[which].read_text())
    assert got and list(swapcmd.FLAGS) == VECTORS["flags"]
    assert all(e["cmd_sha256"] != swapcmd.sha256_text("") for e in got.values())   # every model has a cmd
    if which == "lab":
        assert any(mid.startswith("lab-") for mid in got)


@needs_run_py
@pytest.mark.parametrize("which", ["template", "base", "lab"])
def test_entries_hash_like_run_py(rendered, which):
    path = rendered[which]
    got = swapcmd.entries(path.read_text())
    assert got and set(swapcmd.FLAGS) == {"-c", "-n", "-sm", "-m", *RUN.SERVER_FLAG_FOR.values()}
    if which == "lab":
        assert any(mid.startswith("lab-") for mid in got)
    for mid, e in got.items():
        want = RUN.llama_swap_entry(mid, path)
        assert want is not None, mid
        assert e["cmd_sha256"] == RUN.sha256_text(want["cmd"]), mid
        assert e["server_flags"] == want["server_flags"] and e["aliases"] == want["aliases"], mid
        assert e["name"] == want["name"], mid
        for al in e["aliases"]:
            assert RUN.llama_swap_entry(al, path)["model_id"] == mid


def agent(tmp_path, *args):
    env = {"PATH": "/usr/bin:/bin", "SPARK_MODEL_ENV_FILE": str(tmp_path / "none.env"),
           "SPARK_MODEL_CONFIG_DIR": str(tmp_path), "SPARK_MODEL_MODELS_DIR": str(tmp_path / "models"),
           "SPARK_MODEL_LOCK": str(tmp_path / "lock")}
    return subprocess.run([sys.executable, "-B", str(AGENT), *args], capture_output=True, text=True, timeout=60,
                          env=env)


def test_agent_serving_reports_hashes_not_cmds(rendered, tmp_path):
    text = rendered["base"].read_text()
    (tmp_path / "config.yaml").write_text(text)
    r = agent(tmp_path, "serving", "--json")
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["ok"] and d["verb"] == "serving" and d["config_sha256"] == swapcmd.sha256_text(text)
    assert d["models"] == swapcmd.entries(text)
    assert "coder" in {a for e in d["models"].values() for a in e["aliases"]}
    body = json.dumps(d["models"])
    assert "/models/gguf" not in body and "docker" not in body and "${" not in body


def test_agent_serving_without_a_config(tmp_path):
    r = agent(tmp_path, "serving", "--json")
    assert r.returncode == 1
    assert json.loads(r.stdout)["ok"] is False
    (tmp_path / "config.yaml").write_text("models: {}\n")
    r = agent(tmp_path, "serving", "--json")
    assert r.returncode == 1 and "no models parsed" in json.loads(r.stdout)["error"]
