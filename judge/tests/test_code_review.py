"""Rubric R8 (code correctness): prompt rendering, the runner's on/off decision and the validator's rule r8."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))
import run_judge as RJ  # noqa: E402
import validate as V  # noqa: E402

from test_runner import enqueue, env, finding, frontier, local, rid  # noqa: E402,F401

DIFF = """# local diff vs snapshot taken 2026-10-04T17:19:15Z (session 20261004_121914_512dcf)
# repo /srv/example/deck: changes since session-start HEAD 0123456789ab
diff --git a/app/main.py b/app/main.py
--- /dev/null
+++ b/app/main.py
@@ -0,0 +1,6 @@
+def start_run(watch):
+    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
+    worker.submit(pipeline.run_watch, watch)
+    return {"run_id": run_id}
+
+SECRET_HEADER = "x-token"
diff --git a/app/pipeline.py b/app/pipeline.py
--- a/app/pipeline.py
+++ b/app/pipeline.py
@@ -60,3 +60,4 @@ def run_watch(watch):
     dt = datetime.now(timezone.utc)
-    run_id = "stub"
+    run_id = _run_id(dt)
     _write_run(state_dir, watch, run_id, payload)
# content withheld (sensitive path in an infra bundle; name not shown): [sensitive path #1 withheld]
"""
README_DIFF = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,2 +1,3 @@
 # Title
+A new line of prose that says run_id = _run_id(dt).
"""
RID = "20261004T171714Z-560251-completion"
CLAIMS = "Wrote app/main.py and app/pipeline.py; the run id is consistent."


def bundle(diff=DIFF, log="2026-10-04T17:20:00Z tool write_file completed (0.1s, 40 chars)\n"):
    return (f"=== FILE: manifest.json ===\n{json.dumps({'data_class': 'infra'})}\n"
            f"=== FILE: hermes-log.txt ===\n{log}\n=== FILE: agent-diff.patch ===\n{diff}\n")


def r8(**kw):
    base = {"id": "F1", "rubric": "R8", "severity": "medium",
            "claim": "The run id returned to the client is not the id of the files the pipeline writes.",
            "evidence": ('app/main.py:2 `run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")` vs '
                         'app/pipeline.py:61 `run_id = _run_id(dt)`'),
            "verdict": "defect",
            "failure_scenario": "POST returns id A; the pipeline computes B a second later and writes B.json; "
                                "GET /runs/A then returns 404.",
            "recommendation": "Pass the run id from start_run into run_watch."}
    base.update(kw)
    return base


def judge(items, *, code_review=True, b=None, mode="frontier"):
    notes = []
    f, errs, dropped = V.validate_finding({"items": items}, request_id=RID, judge="m", mode=mode,
                                          created="2026-10-05T00:00:00Z", bundle_text=bundle() if b is None else b,
                                          request={"id": RID, "kind": "completion", "claims": CLAIMS},
                                          notes_out=notes, code_review=code_review)
    assert errs == []
    return f, dropped, notes


# ------------------------------------------------------------------ validator: acceptance
def test_r8_item_accepted_without_a_contradicted_claim():
    f, dropped, notes = judge([r8()])
    assert dropped == [] and len(f["items"]) == 1
    it = f["items"][0]
    assert (it["rubric"], it["verdict"], it["severity"]) == ("R8", "defect", "medium")
    assert it["failure_scenario"].startswith("POST returns id A")
    assert V.schema_errors(f, V.load_schema()) == []


def test_r8_verdict_false_or_partial_becomes_defect():
    f, _, notes = judge([r8(verdict="false"), r8(id="F2", verdict="partial")])
    assert [i["verdict"] for i in f["items"]] == ["defect", "defect"]
    assert any("verdict false->defect" in n for n in notes)


def test_r8_quote_across_lines_and_with_diff_markers_matches():
    ev = "app/main.py: `+    worker.submit(pipeline.run_watch, watch)\n+    return {\"run_id\": run_id}`"
    f, dropped, _ = judge([r8(evidence=ev)])
    assert dropped == [] and len(f["items"]) == 1


def test_r8_ellipsis_splits_a_quote_and_file_names_are_ignored():
    ev = "`app/pipeline.py:61` `dt = datetime.now(timezone.utc) ... run_id = _run_id(dt)`"
    f, dropped, _ = judge([r8(evidence=ev)])
    assert dropped == [] and len(f["items"]) == 1


def test_r8_context_quote_from_another_bundle_file_is_allowed():
    ev = ("`run_id = _run_id(dt)` and hermes-log.txt `tool write_file completed (0.1s, 40 chars)`")
    f, dropped, _ = judge([r8(evidence=ev)])
    assert dropped == [] and len(f["items"]) == 1


def test_r8_scenario_in_the_claim_counts():
    it = r8(claim="When a run starts, start_run returns an id that differs from the files written, so GET returns 404.")
    del it["failure_scenario"]
    f, dropped, _ = judge([it])
    assert dropped == [] and "failure_scenario" not in f["items"][0]


# ------------------------------------------------------------------ validator: drop rules
def test_r8_quote_not_in_the_diff_is_dropped():
    f, dropped, _ = judge([r8(evidence="app/main.py:2 `run_id = make_run_id(started_at)`")])
    assert f["items"] == [] and "not found in agent-diff.patch" in dropped[0]


def test_r8_one_misquoted_line_drops_the_whole_item():
    ev = "`run_id = _run_id(dt)` vs `run_id = make_run_id(started_at)`"
    f, dropped, _ = judge([r8(evidence=ev)])
    assert f["items"] == [] and "make_run_id" in dropped[0]


def test_r8_quote_only_in_prose_of_a_doc_section_still_counts_as_diff():
    # a line added to a README is diff text too; what the judge quotes must exist, it need not be code
    f, dropped, _ = judge([r8(evidence="README.md `A new line of prose that says run_id`")], b=bundle(README_DIFF))
    assert dropped == [] and len(f["items"]) == 1


def test_r8_quote_from_a_header_or_withheld_line_does_not_count():
    for ev in ("`# repo /srv/example/deck: changes since`", "`content withheld (sensitive path in an infra bundle`",
               "`@@ -60,3 +60,4 @@ def run_watch(watch):`"):
        f, dropped, _ = judge([r8(evidence=ev)])
        assert f["items"] == [], ev


def test_r8_without_a_code_quote_is_dropped():
    f, dropped, _ = judge([r8(evidence="app/main.py and app/pipeline.py compute the run id twice")])
    assert f["items"] == [] and "quotes no code line" in dropped[0]


def test_r8_without_a_failure_scenario_is_dropped():
    f, dropped, _ = judge([r8(claim="The run id is computed twice.", failure_scenario="", recommendation="Fix it.")])
    assert f["items"] == [] and "failure_scenario" in dropped[0]


@pytest.mark.parametrize("claim", ["This might return the wrong id.", "Naming of _run_id is unclear.",
                                   "Possibly a race on the run id.", "Style: use a constant."])
def test_r8_speculative_or_style_items_are_dropped(claim):
    f, dropped, _ = judge([r8(claim=claim)])
    assert f["items"] == [] and "speculative or style" in dropped[0]


def test_r8_verdict_true_is_dropped():
    f, dropped, _ = judge([r8(verdict="true")])
    assert f["items"] == [] and "defects only" in dropped[0]


def test_r8_dropped_when_code_review_is_off():
    f, dropped, _ = judge([r8()], code_review=False)
    assert f["items"] == [] and "not enabled" in dropped[0]


def test_r8_dropped_without_a_diff_in_the_bundle():
    b = "=== FILE: manifest.json ===\n{}\n=== FILE: hermes-log.txt ===\nrun_id = _run_id(dt)\n"
    f, dropped, _ = judge([r8()], b=b)
    assert f["items"] == [] and "no agent-diff.patch" in dropped[0]


def test_r8_at_most_three_items_most_severe_kept():
    items = [r8(id=f"F{n}", severity="low") for n in range(1, 4)]
    items.append(r8(id="F4", severity="high", claim="When a run starts the token is written to the run file "
                                                     "(secret exposure), so GET returns it."))
    f, dropped, _ = judge(items)
    assert [i["id"] for i in f["items"]] == ["F1", "F2", "F4"]
    assert len(dropped) == 1 and "more than 3 R8 items" in dropped[0]


def test_r8_non_r8_items_do_not_count_against_the_cap():
    items = [r8(id=f"F{n}") for n in range(1, 4)] + [
        {"id": "F4", "rubric": "R6", "severity": "low", "claim": "x", "verdict": "n/a", "recommendation": "y",
         "evidence": "hermes-log.txt `tool write_file completed (0.1s, 40 chars)`"}]
    f, dropped, _ = judge(items)
    assert len(f["items"]) == 4 and dropped == []


# ------------------------------------------------------------------ validator: severity
def test_r8_high_without_security_or_data_loss_becomes_medium():
    f, _, notes = judge([r8(severity="high")])
    assert f["items"][0]["severity"] == "medium" and any("high->medium" in n for n in notes)


def test_r8_high_kept_for_data_loss():
    f, _, _ = judge([r8(severity="high", failure_scenario="When two runs start in the same second the second "
                                                          "overwrites the first run's files: data loss.")])
    assert f["items"][0]["severity"] == "high"


def test_r8_missing_severity_defaults_to_medium_and_low_is_kept():
    it = r8()
    del it["severity"]
    f, _, _ = judge([it, r8(id="F2", severity="low")])
    assert [i["severity"] for i in f["items"]] == ["medium", "low"]


def test_r8_local_mode_is_still_capped():
    f, _, _ = V.validate_finding({"items": [r8(severity="high", failure_scenario="when x the secret is logged")]},
                                 request_id=RID, judge="m", mode="local", created="2026-10-05T00:00:00Z",
                                 bundle_text=bundle(), max_severity="low", code_review=True)[0:3]
    assert f["items"][0]["severity"] == "low"


def test_defect_verdict_outside_r8_becomes_na():
    it = {"id": "F1", "rubric": "R6", "severity": "low", "claim": "x", "verdict": "defect", "recommendation": "y",
          "evidence": "hermes-log.txt `tool write_file completed (0.1s, 40 chars)`"}
    f, _, notes = judge([it])
    assert f["items"][0]["verdict"] == "n/a" and any("defect->n/a" in n for n in notes)


def test_r1_items_keep_the_claim_rules():
    # an R1 `false` with no contradicting quote is still downgraded: R8 does not loosen the other rubrics
    it = {"id": "F1", "rubric": "R1", "severity": "high", "claim": "run id is consistent", "verdict": "false",
          "recommendation": "y", "evidence": "app/main.py `run_id = _run_id(dt)` does not show it"}
    f, _, _ = judge([it])
    assert (f["items"][0]["verdict"], f["items"][0]["severity"]) == ("n/a", "low")


# ------------------------------------------------------------------ prompt rendering
def test_render_prompt_on_has_r8_and_no_markers():
    p = RJ.render_prompt(True)
    assert "R8 Code correctness" in p and "## Code review (R8)" in p and "failure_scenario" in p
    assert "R1 to R8" in p and "R1 to R7" not in p and "<!--" not in p


def test_render_prompt_off_has_no_r8():
    p = RJ.render_prompt(False)
    assert "R8" not in p and "failure_scenario" not in p and "defect" not in p
    assert "R1 to R7" in p and "<!--" not in p


def test_render_prompt_off_is_the_old_prompt_layout():
    p = RJ.render_prompt(False)
    assert "is kept at least `medium`.\n\n## Check the report itself" in p
    assert "- R7 Knowledge integrity: did memory or skills get worse (stale facts, lost entries, contradictions)?\n\n" in p


@pytest.mark.parametrize("path,code", [
    ("app/main.py", True), ("walter/deploy.sh", True), ("covenant/nginx/sites-available/60-digest.tmpl", True),
    ("x/unit.service.tmpl", True), ("Dockerfile", True), ("compose.yaml.tmpl", True),
    ("README.md", False), ("walter/digest/README.md.tmpl", False), ("notes.txt", False),
    ("static/app.css", False), ("static/font.woff2", False), ("static/index.html", False)])
def test_is_code_path(path, code):
    assert RJ.is_code_path(path) is code


def _ev(review, request_id, diff):
    (review / "evidence" / request_id / "agent-diff.patch").write_text(diff)


@pytest.fixture
def cr_env(env, monkeypatch):
    for k in ("JUDGE_CODE_REVIEW", "JUDGE_LOCAL_CODE_REVIEW"):
        monkeypatch.delenv(k, raising=False)
    return env


def test_decision_on_for_infra_completion_with_code(cr_env):
    r = rid()
    req = enqueue(cr_env, r)
    _ev(cr_env, r, DIFF)
    on, why, has = RJ.code_review_decision(req, cr_env / "evidence" / r, "frontier", "infra")
    assert on and has and "2 code file(s)" in why


@pytest.mark.parametrize("diff,mode,cls,kind,want", [
    (README_DIFF, "frontier", "infra", "completion", "changes no code"),
    (DIFF, "frontier", "sensitive", "completion", "data_class=sensitive"),
    (DIFF, "frontier", "infra", "gate", "kind=gate"),
    (DIFF, "frontier-claims", "infra", "completion", "claims-only"),
    (DIFF, "local", "infra", "completion", "JUDGE_LOCAL_CODE_REVIEW=0"),
])
def test_decision_off(cr_env, diff, mode, cls, kind, want):
    r = rid(kind=kind)
    req = enqueue(cr_env, r)
    _ev(cr_env, r, diff)
    on, why, _ = RJ.code_review_decision(req, cr_env / "evidence" / r, mode, cls)
    assert not on and want in why


def test_decision_local_opt_in_and_global_off(cr_env, monkeypatch):
    r = rid()
    req = enqueue(cr_env, r)
    _ev(cr_env, r, DIFF)
    monkeypatch.setenv("JUDGE_LOCAL_CODE_REVIEW", "1")
    assert RJ.code_review_decision(req, cr_env / "evidence" / r, "local", "infra")[0] is True
    monkeypatch.setenv("JUDGE_CODE_REVIEW", "0")
    assert RJ.code_review_decision(req, cr_env / "evidence" / r, "frontier", "infra")[0] is False


def test_decision_no_diff_file(cr_env):
    r = rid()
    req = enqueue(cr_env, r)
    on, why, has = RJ.code_review_decision(req, cr_env / "evidence" / r, "frontier", "infra")
    assert not on and not has and "no agent-diff.patch" in why


# ------------------------------------------------------------------ end to end through the runner
def test_frontier_run_sends_r8_prompt_and_keeps_r8_item(cr_env, frontier, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "frontier")
    r = rid()
    enqueue(cr_env, r)
    _ev(cr_env, r, DIFF)
    frontier.replies(json.dumps({"items": [r8()]}))
    assert RJ.main([r]) == 0
    call = frontier.calls()[0]
    system = " ".join(call["argv"])
    assert "## Code review (R8)" in system
    f = finding(cr_env, r)
    assert [(i["rubric"], i["verdict"]) for i in f["items"]] == [("R8", "defect")]
    assert any(n.startswith("code review (R8): on") for n in f["notes"])
    md = (cr_env / "findings" / f"{r}.md").read_text()
    assert "**Failure scenario:** POST returns id A" in md


def test_local_run_default_off_drops_r8(cr_env, local, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "local")
    r = rid()
    enqueue(cr_env, r)
    _ev(cr_env, r, DIFF)
    local["replies"] = [json.dumps({"items": [r8()]})]
    assert RJ.main([r]) == 0
    system = local["requests"][0]["body"]["messages"][0]["content"]
    assert "R8" not in system
    f = finding(cr_env, r)
    assert f["items"] == []
    assert any("code review (R8): off (local judge, JUDGE_LOCAL_CODE_REVIEW=0" in n for n in f["notes"])
    assert any("R8 (code correctness) is not enabled" in n for n in f["notes"])


def test_local_run_opt_in_keeps_r8(cr_env, local, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "local")
    monkeypatch.setenv("JUDGE_LOCAL_CODE_REVIEW", "1")
    r = rid()
    enqueue(cr_env, r)
    _ev(cr_env, r, DIFF)
    local["replies"] = [json.dumps({"items": [r8()]})]
    assert RJ.main([r]) == 0
    assert "## Code review (R8)" in local["requests"][0]["body"]["messages"][0]["content"]
    assert [i["rubric"] for i in finding(cr_env, r)["items"]] == ["R8"]


def test_bundle_without_code_has_no_r8_note(cr_env, frontier, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "frontier")
    r = rid()
    enqueue(cr_env, r)
    _ev(cr_env, r, README_DIFF)
    frontier.replies(json.dumps({"items": []}))
    assert RJ.main([r]) == 0
    assert "## Code review (R8)" not in " ".join(frontier.calls()[0]["argv"])
    assert not any("code review" in n for n in finding(cr_env, r).get("notes", []))
