"""C6 provider (sec-evals): the gate's exit codes and gate.json are what evals/contracts/gate-cli.json says.

Runs the gate end to end against the fake gateway of test_run.py (as test_gate.py does): no model, GPU or network.
The deck consumes a vendored copy (contracts/tests/test_c6_gate_cli.py checks bin/deck and the runbook).
"""
import json
import re
from pathlib import Path

import pytest

import gate as G
from test_gate import gate_env, gw, suite_entry, write_items, write_manifest  # noqa: F401 - shared fixtures

C6 = json.loads((Path(__file__).resolve().parents[1] / "contracts" / "gate-cli.json").read_text())
CODES = {int(k): v["name"] for k, v in C6["exit_codes"].items()}


def test_contract_header():
    assert C6["contract"] == "gate-cli" and C6["contract_version"].startswith("1.")
    assert C6["record_schema"]["properties"]["schema"]["const"] == G.RECORD_SCHEMA and C6["record"]["file"] == G.RECORD


def test_exit_codes_are_the_gates():
    assert (G.EXIT_PASS, G.EXIT_FAIL, G.EXIT_INCONCLUSIVE, G.EXIT_INVALID, G.EXIT_REFUSED) == (0, 1, 2, 3, 4)
    assert {v: k for k, v in G.OVERALL_EXIT.items()} == {k: v for k, v in CODES.items() if v != "REFUSED"}
    assert CODES[G.EXIT_REFUSED] == "REFUSED"
    assert set(C6["record_schema"]["properties"]["verdict"]["enum"]) == set(G.OVERALL_EXIT)
    doc = G.__doc__
    for code, name in CODES.items():
        assert re.search(rf"exit {code}\b", doc), code


def test_commands_are_the_gates(capsys):
    with pytest.raises(SystemExit):
        G.main(["--help"])
    out = capsys.readouterr().out
    for c in C6["commands"]:
        assert c in out, c


def _validate(rec):
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(rec, C6["record_schema"])


def test_records_match_the_schema(gw, gate_env, tmp_path, capsys):
    data = tmp_path / "aa.jsonl"
    write_items(data, "t-mcq", 80)
    man = write_manifest(tmp_path, [suite_entry(data, "t-mcq")], name="aa.json")
    args = ["--model", "coder", "--yes", "--base-url", gw.url,
            *[x if x != str(gate_env["manifest"]) else str(man) for x in gate_env["args"]]]
    assert G.main(["calibrate", *args]) == 0
    rc = G.main(["check", *args])
    assert rc in CODES
    recs = {p.parent.name.split("-")[0]: json.loads(p.read_text())
            for p in (gate_env["root"] / "coder").glob(f"*/{G.RECORD}")}
    assert set(recs) == {"baseline", "aa", "check"}
    for kind, rec in recs.items():
        _validate(rec)
        assert rec["kind"] == kind and rec["status"] == "complete"
    assert recs["check"]["exit"] == rc and CODES[rc] == recs["check"]["verdict"]
    # a refusal (no --yes) is exit 4 and writes no record
    n = len(recs)
    assert G.main(["check", *[x for x in args if x != "--yes"]]) == G.EXIT_REFUSED
    assert len(list((gate_env["root"] / "coder").glob(f"*/{G.RECORD}"))) == n
