"""C4 consumer (sec-evals): run.llama_swap_entry reproduces the deck's serving-identity vectors.

Reads only vendored copies (refresh: python3 contracts/tools/contracts.py vendor --write in the deck repo):
  fixtures/llama-swap.contract.yaml            <- contracts/serving-identity/llama-swap.contract.yaml
  fixtures/contracts/serving-vectors.json      <- contracts/serving-identity/vectors.json
  fixtures/contracts/serving.schema.json       <- contracts/serving-identity/serving.schema.json
run.py is read, never edited (it is a harness file). gate.live_snapshot reads `spark-model-agent serving --json`
output of the schema's shape. No network, no GPU.
"""
import json
from pathlib import Path

import pytest

import gate as G
import run as RUN

FIX = Path(__file__).resolve().parent / "fixtures"
TEMPLATE = FIX / "llama-swap.contract.yaml"
VECTORS = json.loads((FIX / "contracts" / "serving-vectors.json").read_text())
SCHEMA = json.loads((FIX / "contracts" / "serving.schema.json").read_text())
ACCEPTS = "1."          # the contract major version this consumer is written for


def test_contract_version_is_accepted():
    assert VECTORS["contract"] == "serving-identity" and VECTORS["contract_version"].startswith(ACCEPTS)
    assert SCHEMA["contract_version"].startswith(ACCEPTS)


def test_run_py_flag_list_is_the_contracts():
    assert ["-c", "-n", "-sm", "-m", *RUN.SERVER_FLAG_FOR.values()] == VECTORS["flags"]


@pytest.mark.parametrize("alias", sorted(VECTORS["lookups"]))
def test_llama_swap_entry_reproduces_the_vectors(alias):
    mid = VECTORS["lookups"][alias]
    e = RUN.llama_swap_entry(alias, TEMPLATE)
    if mid is None:
        assert e is None
        return
    want = VECTORS["entries"][mid]
    assert e["model_id"] == mid and e["name"] == want["name"] and e["aliases"] == want["aliases"]
    assert e["cmd"] == want["cmd"] and RUN.sha256_text(e["cmd"]) == want["cmd_sha256"]
    assert e["server_flags"] == want["server_flags"]


def test_coder_entry_fields():
    c = VECTORS["coder"]
    e = RUN.llama_swap_entry(c["alias"], TEMPLATE)
    assert e["model_id"] == c["model_id"] and c["image_pinned"] in e["cmd"] and c["no_unexpanded"] not in e["cmd"]
    for flag, value in c["server_flags"].items():
        assert e["server_flags"][flag] == value


def _agent_reply():
    """A `spark-model-agent serving --json` reply built from the vectors (what the deck's agent prints for them)."""
    return {"ok": True, "verb": "serving", "config": "/etc/llama-swap/config.yaml", "config_sha256": "0" * 64,
            "models": {mid: {k: e[k] for k in ("aliases", "name", "cmd_sha256", "server_flags")}
                       for mid, e in VECTORS["entries"].items()}}


def test_schema_describes_what_the_gate_reads():
    reply = _agent_reply()
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(reply, SCHEMA)


def test_gate_live_snapshot_reads_the_contract_shape(monkeypatch):
    import argparse
    monkeypatch.setattr(G, "LIVE_FETCH", lambda target: _agent_reply())
    monkeypatch.setenv("EVAL_GPU_SSH", "backend-alias")
    a = argparse.Namespace(model="fast-2", site_env="/nonexistent/site.env")
    snap = G.live_snapshot(a, "before")
    want = VECTORS["entries"]["example-moe"]
    assert snap["model_id"] == "example-moe" and snap["cmd_sha256"] == want["cmd_sha256"]
    assert snap["server_flags"] == want["server_flags"] and snap["config_sha256"] == "0" * 64
