"""C7 provider (deck): every llama-swap model container carries the label, name, loopback port and stop command
of contracts/observability.yaml, and the units it names are the ones the deploy scripts manage.

The consumer side (agent-judge) tests a vendored copy: judge/tests/test_contract_observability.py.
Run: python3 -m pytest contracts/tests -q -p no:cacheprovider
"""
import re
import sys

import pytest

from contract_helpers import CONTRACTS, EXAMPLE, REPO, load_yaml, render

OBS = load_yaml(CONTRACTS / "observability.yaml")
MC = OBS["model_containers"]
TEMPLATE = REPO / "walter/llama-swap/config.yaml.tmpl"


@pytest.fixture(scope="module")
def configs(tmp_path_factory):
    """The rendered template, and the same with every example lab model enabled (the registry render)."""
    yaml = pytest.importorskip("yaml")
    tmp = tmp_path_factory.mktemp("c7")
    base = render(TEMPLATE, tmp / "config.yaml", EXAMPLE)
    sys.path.insert(0, str(REPO / "walter/models/lib"))
    import registry as R
    reg = yaml.safe_load((REPO / "walter/models/registry.example.yaml").read_text())
    for m in reg["models"]:
        m["enabled"] = True
    return {"base": yaml.safe_load(base), "lab": yaml.safe_load(R.render(base, R.validate(reg)))}


def _expand(cfg, text):
    for _ in range(4):
        text = re.sub(r"\$\{([\w-]+)\}", lambda m: cfg["macros"].get(m.group(1), m.group(0)), text)
    return text


def test_contract_header():
    assert OBS["contract"] == "observability" and OBS["contract_version"].startswith("1.")
    assert MC["docker_ps_filter"] == f"label={MC['label']}"


@pytest.mark.parametrize("which", ["base", "lab"])
def test_every_model_container_is_observable(configs, which):
    cfg = configs[which]
    assert cfg["models"]
    for mid, m in cfg["models"].items():
        cmd = " ".join(_expand(cfg, m["cmd"]).split())
        assert f"--label {MC['label']}" in cmd, mid
        assert cmd.startswith("/usr/local/sbin/spark-docker-run.sh ${MODEL_ID} "), mid   # --name <model id>
        assert f"-p {MC['publish']}" in cmd, mid
        assert _expand(cfg, m["cmdStop"]) == MC["stop"], mid
        assert m["proxy"] == "http://127.0.0.1:${PORT}", mid


def test_spark_docker_run_names_the_container_after_its_first_argument():
    text = (REPO / "walter/llama-swap/spark-docker-run.sh").read_text()
    assert re.search(r'^name="\$1"; shift$', text, re.M) and 'exec docker run --name "$name" "$@"' in text


def test_maya_entrypoint_serves_slots():
    assert OBS["slots"]["path"] in (REPO / "walter/maya/lab_entry.py").read_text()


def test_units_are_deployed():
    walter = (REPO / "walter/deploy.sh").read_text()
    for u in OBS["units"]["walter"]:
        assert re.search(rf"(?<![\w-]){re.escape(u)}(\.service|\.timer)\b", walter), u
    covenant = (REPO / "covenant/deploy.sh").read_text()
    for u in OBS["units"]["covenant"]:
        assert re.search(rf"(?<![\w-]){re.escape(u)}\b", covenant), u
