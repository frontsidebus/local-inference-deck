"""C3 provider (deck): contracts/gateway.yaml agrees with everything in this repo that names a gateway alias, and
contracts/aliases.json is generated from it.

Consumers test the vendored aliases.json on their side: sec-evals (evals/tests/test_contract_aliases.py, run.py's
alias constants and the gate manifests) and agent-judge (judge/tests/test_contract_gateway.py, JUDGE_LOCAL_MODEL).
Run: python3 -m pytest contracts/tests -q -p no:cacheprovider
"""
import json
import re
import tomllib

import pytest

from contract_helpers import CONTRACTS, REPO, TOOL, example_value, load_json, load_yaml

yaml = pytest.importorskip("yaml")
GW = load_yaml(CONTRACTS / "gateway.yaml")
ALIASES = GW["aliases"]
LITELLM = load_yaml(REPO / "walter/gateway/litellm.yaml.tmpl")
LLAMA_SWAP = load_yaml(REPO / "walter/llama-swap/config.yaml.tmpl")
# Known differences, each with its reason. A fixed one must be removed from here (the test fails while it is listed
# but no longer differs), so the list only shrinks.
KNOWN_DRIFT = {
    ("opencode", "vision", "output"): "opencode asks for 32768 output tokens on vision; the gateway caps it at "
                                      "16384 (spark_hooks.py MODEL_MAX_OUTPUT), so a request is clamped, not refused",
}


def test_contract_header():
    assert GW["contract"] == "gateway" and re.fullmatch(r"\d+\.\d+", GW["contract_version"])
    for a in ALIASES.values():
        assert set(a) == {"max_output_tokens", "context_window", "vision", "evicts_coding_pair"}


def test_aliases_json_is_generated_from_gateway_yaml():
    assert (CONTRACTS / "aliases.json").read_text() == TOOL.gen_aliases(GW), \
        "stale: python3 contracts/tools/contracts.py aliases --write"


def test_litellm_routes_are_the_aliases():
    routes = {m["model_name"]: m for m in LITELLM["model_list"]}
    static = {n for n in routes if "*" not in n}
    assert static == set(ALIASES)
    assert {n for n in routes if "*" in n} == {GW["routes"]["local"], GW["routes"]["frontier_safety_net"]}
    assert routes["claude-*"]["litellm_params"]["model"] == "openai/coder"
    for name, a in ALIASES.items():
        r = routes[name]
        info = r["model_info"]
        assert r["litellm_params"]["model"] == f"openai/{name}", name
        assert info["max_tokens"] == info["max_output_tokens"] == a["max_output_tokens"], name
        assert info["max_input_tokens"] == a["context_window"], name
        assert info["supports_vision"] is a["vision"], name
    for r in routes.values():
        assert r["model_info"]["supported_endpoints"] == GW["api"]["endpoints"], r["model_name"]


def _served():
    """alias -> its llama-swap model entry (model id, entry)."""
    out = {}
    for mid, m in LLAMA_SWAP["models"].items():
        for al in m.get("aliases") or []:
            assert al not in out, f"{al} is served twice"
            out[al] = (mid, m)
    return out


def test_llama_swap_serves_each_alias_with_its_capabilities():
    served = _served()
    assert set(served) == set(ALIASES)
    for name, a in ALIASES.items():
        mid, m = served[name]
        cap = m["capabilities"]
        assert cap["context"] == a["context_window"], name
        assert ("image" in cap["in"]) is a["vision"], name
        assert not mid.startswith(GW["routes"]["lab_prefix"]), name
    lab = [mid for mid, m in LLAMA_SWAP["models"].items() if not m.get("aliases")]
    assert lab and all(mid.startswith(GW["routes"]["lab_prefix"]) for mid in lab)


def test_evicts_coding_pair_is_the_router_sets():
    """evicts_coding_pair: the alias's model is not in llama-swap's default "coding" set (read from the router, not
    from any model's cmd)."""
    matrix = LLAMA_SWAP["routing"]["router"]["settings"]["matrix"]
    coding = {matrix["vars"][v.strip()] for v in matrix["sets"]["coding"].split("&")}
    served = _served()
    for name, a in ALIASES.items():
        assert (served[name][0] not in coding) is a["evicts_coding_pair"], name


def _hook_constants(*names):
    """Literal constants of spark_hooks.py, read with ast (it imports LiteLLM, which the tests do not need)."""
    import ast
    tree = ast.parse((REPO / "walter/gateway/hooks/spark_hooks.py").read_text())
    found = {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body
             if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id in names}
    return [found[n] for n in names]


def test_gateway_output_caps_are_the_contracts():
    max_output, local_id_alias = _hook_constants("MODEL_MAX_OUTPUT", "LOCAL_ID_ALIAS")
    assert max_output == {n: a["max_output_tokens"] for n, a in ALIASES.items()}
    assert set(local_id_alias.values()) == set(ALIASES)
    assert local_id_alias == {mid: al for al, (mid, _) in _served().items()}


def _drift(*key):
    return key in KNOWN_DRIFT


def test_client_catalogs_name_only_aliases_with_their_limits():
    seen = set()
    codex = load_json(REPO / "clients/codex/spark-models.json")
    for m in codex["models"]:
        a = ALIASES[m["slug"]]
        assert m["context_window"] == a["context_window"], m["slug"]
    for f in ("spark.config.toml", "spark-fast.config.toml"):
        assert tomllib.loads((REPO / "clients/codex" / f).read_text())["model"] in ALIASES, f
    assert re.search(r'^model = "([^"]+)"', (REPO / "clients/codex/config.toml.tmpl").read_text(), re.M)[1] in ALIASES

    oc = json.loads((REPO / "clients/opencode/opencode.json.tmpl").read_text())
    assert oc["model"].split("/", 1)[1] in ALIASES
    for name, m in oc["provider"]["spark"]["models"].items():
        a = ALIASES[name]
        assert m["limit"]["context"] == a["context_window"], name
        if m["limit"]["output"] != a["max_output_tokens"]:
            assert _drift("opencode", name, "output"), (name, m["limit"]["output"])
            seen.add(("opencode", name, "output"))

    hermes = load_yaml(REPO / "clients/hermes/config.spark.yaml.tmpl")
    assert hermes["model"]["default"] in ALIASES
    assert hermes["model"]["max_tokens"] == ALIASES[hermes["model"]["default"]]["max_output_tokens"]
    spark = next(p for p in hermes["custom_providers"] if p["name"] == "spark")
    assert spark["model"] in ALIASES
    for name, m in spark["models"].items():
        assert m["context_length"] == ALIASES[name]["context_window"], name
    assert seen == set(KNOWN_DRIFT), "a KNOWN_DRIFT entry no longer differs: remove it"


def test_site_example_judge_model_is_an_alias():
    assert example_value("JUDGE_LOCAL_MODEL") in ALIASES
