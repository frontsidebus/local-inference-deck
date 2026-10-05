"""Tests for judge/runner/validate.py (finding normalization + evidence heuristic)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))
import validate as V  # noqa: E402

RID = "20261003T035210Z-fdc8ec-completion"


def item(**kw):
    base = {"id": "F1", "rubric": "R1", "severity": "high", "claim": "alias fixed",
            "evidence": "ssh -o BatchMode=yes edge-alias true -> Permission denied (publickey)",
            "verdict": "false", "recommendation": "set the alias user"}
    base.update(kw)
    return base


def run(items, **kw):
    return V.validate_finding({"items": items}, request_id=RID, judge="m", mode="local",
                              created="2026-10-03T04:00:00Z", **kw)


def test_valid_finding_passes_schema():
    f, errs, dropped = run([item()])
    assert errs == [] and dropped == []
    assert f["request"] == RID and f["mode"] == "local" and f["items"][0]["verdict"] == "false"
    assert V.schema_errors(f, V.load_schema()) == []


@pytest.mark.parametrize("raw,expected", [
    ("high", "high"), ("CRITICAL", "high"), ("Moderate", "medium"), ("warning", "medium"),
    ("info", "low"), ("nit", "low"), ("bogus", "medium"), (None, "medium")])
def test_severity_normalized(raw, expected):
    assert V.norm_severity(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("R1", "R1"), ("r3", "R3"), ("R-5", "R5"), ("R7 Knowledge integrity", "R7"), ("2", "R2"),
    ("R8", "R8"), ("R9", None), ("X1", None), ("", None)])
def test_rubric_normalized(raw, expected):
    assert V.norm_rubric(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    (True, "true"), (False, "false"), ("Yes", "true"), ("incorrect", "false"),
    ("partially", "partial"), ("whatever", "n/a")])
def test_verdict_normalized(raw, expected):
    assert V.norm_verdict(raw) == expected


def test_unknown_rubric_item_dropped():
    f, errs, dropped = run([item(rubric="R9"), item(id="F2")])
    assert not errs
    assert len(f["items"]) == 1 and "unknown rubric" in dropped[0]


@pytest.mark.parametrize("ev", ["", "   ", "\n\t", None])
def test_empty_evidence_dropped(ev):
    f, errs, dropped = run([item(evidence=ev)])
    assert f["items"] == [] and "empty evidence" in dropped[0]


@pytest.mark.parametrize("ev", [
    "I am confident this is wrong",
    "the agent did not do it properly",
    "trust me",
    "I find that the agent was wrong",
    "head of the plan says it is done",
])
def test_unreferenced_evidence_dropped(ev):
    f, errs, dropped = run([item(evidence=ev)])
    assert f["items"] == [] and "does not reference" in dropped[0]


@pytest.mark.parametrize("ev,why", [
    ("agent-diff.patch:12 + command_allowlist: python -c", "path"),
    ("~/.ssh/config unchanged since snapshot", "path"),
    ("/etc/nginx/sites-enabled/api changed at 2026-10-03T03:40:00Z", "path"),
    ("systemctl --user is-active foo => inactive", "command"),
    ("unit_state walter llama-swap -> failed", "command"),
    ('log line "request without max_tokens reached n_ctx"', "quote"),
    ("`n_decoded 131072, truncated true`", "quote"),
    ("find /etc -newermt 2026-10-03T03:20:00Z printed nothing", "path"),
    ("cat -A shows a tab in the config", "command"),
])
def test_referenced_evidence_kept(ev, why):
    assert V.evidence_reason(ev) == why
    f, errs, dropped = run([item(evidence=ev)])
    assert len(f["items"]) == 1 and not dropped


def test_bundle_substring_route():
    ev = "Preparing memory took eleven minutes per the agent log"
    assert V.evidence_reason(ev) is None
    assert V.evidence_reason(ev, bundle_text="xx Preparing memory took eleven minutes yy") == "bundle"


def test_ids_renumbered_when_missing_or_duplicate():
    f, _, _ = run([item(id=""), item(id="F1"), item(id="F1")])
    assert [i["id"] for i in f["items"]] == ["F1", "F2", "F3"]


def test_ids_kept_when_valid():
    f, _, _ = run([item(id="F3"), item(id="F7")])
    assert [i["id"] for i in f["items"]] == ["F3", "F7"]


def test_long_fields_capped():
    f, _, _ = run([item(claim="x" * 5000, evidence="hermes-log.txt " + "y" * 5000)])
    assert len(f["items"][0]["claim"]) <= V.CAPS["claim"]
    assert len(f["items"][0]["evidence"]) <= V.CAPS["evidence"]


def test_extract_json_from_fences_and_prose():
    obj = {"items": [item()]}
    assert V.extract_json("```json\n" + json.dumps(obj) + "\n```") == obj
    assert V.extract_json("Here is my review:\n" + json.dumps(obj) + "\nThanks") == obj
    with pytest.raises(ValueError):
        V.extract_json("no json here")
    with pytest.raises(ValueError):
        V.extract_json("")


@pytest.mark.parametrize("raw,msg", [
    ("not json", "not a JSON object"),
    (json.dumps({"verdict": "x"}), "missing 'items'"),
    (json.dumps({"items": "nope"}), "must be a list"),
    (json.dumps(42), "JSON object"),
])
def test_fatal_errors(raw, msg):
    f, errs, _ = V.validate_finding(raw, request_id=RID, judge="m", mode="local", created="2026-10-03T04:00:00Z")
    assert f is None and any(msg in e for e in errs)


def test_bad_envelope_fails_schema():
    f, errs, _ = V.validate_finding({"items": []}, request_id="not-an-id", judge="m", mode="cloud",
                                    created="2026-10-03T04:00:00Z")
    assert f is None and errs


def test_list_top_level_taken_as_items():
    f, errs, _ = V.validate_finding(json.dumps([item()]), request_id=RID, judge="m", mode="frontier",
                                    created="2026-10-03T04:00:00Z")
    assert not errs and len(f["items"]) == 1


def test_cli(tmp_path, capsys):
    p = tmp_path / "f.json"
    p.write_text(json.dumps({"request": RID, "judge": "m", "created": "2026-10-03T04:00:00Z", "mode": "local",
                             "items": [item(severity="critical"), item(id="F2", evidence="")]}))
    assert V.main([str(p)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["items"][0]["severity"] == "high" and len(out["items"]) == 1
    p.write_text("{}")
    assert V.main([str(p)]) == 1
