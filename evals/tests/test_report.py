"""Tests for report.py's statistics: denominators, error policy, pre-registered comparisons, sensitivity, seeds,
baselines and clusters. Synthetic run dirs with known answers; no network, no models."""
import json
from pathlib import Path

import pytest

import report as R
import stats as ST


def make_run(d, model, scores, thinking="off", temperature=0.0, seed=1234, responses=None):
    """A run dir: run.json, responses.jsonl (one per score unless given) and scores.jsonl."""
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({"model": model, "thinking": thinking,
                                            "sampling": {"temperature": temperature, "seed": seed}}))
    resp = responses if responses is not None else [{"id": s["id"], "suite": s["suite"]} for s in scores]
    (d / "responses.jsonl").write_text("".join(json.dumps(r) + "\n" for r in resp))
    (d / "scores.jsonl").write_text("".join(json.dumps(s) + "\n" for s in scores))
    return d


def sc(i, passed, suite="s", **kw):
    row = {"id": f"i{i:03d}", "suite": suite, "scorer": "exact", "passed": bool(passed), "value": float(passed),
           "status": "ok"}
    row.update(kw)
    return row


def table_runs(tmp_path, n11, n10, n01, n00, suite="s", names=("a", "b"), **kw):
    """Two runs with a known paired 2x2 table."""
    pa = [1] * n11 + [1] * n10 + [0] * n01 + [0] * n00
    pb = [1] * n11 + [0] * n10 + [1] * n01 + [0] * n00
    a = make_run(tmp_path / names[0], "ma", [sc(i, p, suite) for i, p in enumerate(pa)], **kw)
    b = make_run(tmp_path / names[1], "mb", [sc(i, p, suite) for i, p in enumerate(pb)], **kw)
    return a, b


def write_plan(tmp_path, families):
    p = tmp_path / "plan.json"
    p.write_text(json.dumps({"alpha": 0.05, "defaults": {"metric": "accuracy", "margin": 0.10},
                             "families": families}))
    return p


# ------------------------------------------------------------------ denominators and error policy
def test_denominators_and_error_policy(tmp_path):
    scores = [sc(0, 1), sc(1, 1), sc(2, 0, status="unparsed"), sc(3, 0, status="unparsed_truncated"),
              sc(4, 0, status="error"), sc(5, 0)]
    resp = [{"id": f"i{i:03d}", "suite": "s"} for i in range(6)] + [{"id": "i006", "suite": "s", "error": "timeout"}]
    resp[3]["finish_reason"] = "length"
    d = make_run(tmp_path / "r", "m", scores, responses=resp)
    st = R.summarize(d)["suites"]["s"]
    assert (st["n_items"], st["n_ok"], st["unparsed"], st["unparsed_truncated"], st["truncated"],
            st["item_or_grader_errors"], st["api_errors"]) == (7, 3, 2, 1, 1, 1, 1)
    # default: errors (grader/item and api) count as a fail -> 2 / 7; the old figure leaves them out -> 2 / 5
    assert st["accuracy"] == pytest.approx(2 / 7) and st["n_denominator"] == 7
    assert st["accuracy_excl_errors"] == pytest.approx(2 / 5) and st["accuracy_alt"] == pytest.approx(2 / 5)
    ex = R.summarize(d, errors="exclude")["suites"]["s"]
    assert ex["accuracy"] == pytest.approx(2 / 5) and ex["accuracy_alt"] == pytest.approx(2 / 7)
    md = R.render_markdown([R.summarize(d)])
    assert "| 3 / 2 (1) / 1 / 1 / 1 |" in md and "count as a **fail**" in md


def test_outcome_rules():
    rec = {"status": "error", "passed": False, "value": None, "followup": False, "truncated": False,
           "opener": None, "legacy": None, "flags": {}, "scorer": "cvss_mae"}
    assert R.outcome(rec, "accuracy", "fail") == 0.0 and R.outcome(rec, "accuracy", "exclude") is None
    assert R.outcome(rec, "mae", "fail") == R.MAE_FAIL
    assert R.outcome(rec, "accuracy", "fail", "errors_excluded") is None
    ok = dict(rec, status="ok", passed=True, value=0.4, followup=True)
    assert R.outcome(ok, "accuracy") == 1.0 and R.outcome(ok, "accuracy", variant="followup_fail") == 0.0
    assert R.outcome(ok, "mae") == 0.4 and R.outcome(ok, "mae", variant="followup_fail") == R.MAE_FAIL
    assert R.item_excluded([ok], "drop_followup") and not R.item_excluded([ok], "drop_truncated")


# ------------------------------------------------------------------ pre-registered comparisons
def test_confirmatory_holm_bh_and_verdicts(tmp_path):
    a, b = table_runs(tmp_path, 60, 12, 4, 24, suite="s1")                    # McNemar p = 0.0768
    c, d = table_runs(tmp_path, 60, 10, 0, 30, suite="s2", names=("c", "d"))  # p = 2/1024
    plan = write_plan(tmp_path, [{"name": "F", "decision": "pick A", "comparisons": [
        {"id": "x1", "suite": "s1", "a": "a", "b": "b", "direction": "a>b"},
        {"id": "x2", "suite": "s2", "a": "c", "b": "d", "direction": "a>b"},
        {"id": "x3", "suite": "s2", "a": "c", "b": "d", "direction": "a<b"},   # wrong sign: never confirmed
        {"id": "x4", "suite": "s1", "a": "a", "b": "nope", "direction": "a>b"}]}])
    out, ccsv = tmp_path / "r.md", tmp_path / "c.csv"
    assert R.main([str(a), str(b), str(c), str(d), "--comparisons", str(plan), "--out", str(out),
                   "--conf-csv", str(ccsv)]) == 0
    p1, p2 = ST.mcnemar_exact(12, 4), ST.mcnemar_exact(10, 0)
    assert p1 == pytest.approx(0.0768, abs=1e-4) and p2 == pytest.approx(2 / 1024)
    rows = {r["id"]: r for r in __import__("csv").DictReader(ccsv.open())}
    # Holm over the DECLARED m = 4 (the missing x4 enters as p = 1): 4*p2, 4*p2 (monotone), 2*p1
    assert float(rows["x2"]["p_holm"]) == pytest.approx(4 * p2)
    assert float(rows["x3"]["p_holm"]) == pytest.approx(4 * p2)
    assert float(rows["x1"]["p_holm"]) == pytest.approx(2 * p1)
    assert float(rows["x1"]["q_bh"]) == pytest.approx(p1 * 4 / 3)    # BH over m = 4
    assert rows["x2"]["verdict"] == "confirmed" and rows["x3"]["verdict"] == "reversed"   # wrong sign, significant
    assert rows["x1"]["verdict"] == "not confirmed"
    assert rows["x4"]["verdict"].startswith("not evaluable")
    d_, lo, hi = ST.newcombe_paired_ci(60, 12, 4, 24)
    assert float(rows["x1"]["diff"]) == pytest.approx(d_) and float(rows["x1"]["lo"]) == pytest.approx(lo)
    md = out.read_text()
    # synthetic runs carry no protocol record: the gate fails and the section is labelled post hoc
    assert md.index("## Post-hoc / exploratory (pre-specified") < md.index("## s1") < md.index("## Paired comparisons")
    assert "pre-specified: x1" in md and "exploratory" in md and "## Pre-registered" not in md
    assert "+8.0 pp [" in md                                            # the effect, not only the p-value


def test_equivalence_tost_and_underpowered(tmp_path):
    a, b = table_runs(tmp_path, 150, 5, 5, 40)           # n=200, d=0, small discordance -> equivalent
    c, d = table_runs(tmp_path, 20, 6, 4, 10, names=("c", "d"))   # n=40 -> underpowered
    plan = write_plan(tmp_path, [{"name": "E", "comparisons": [
        {"id": "big", "suite": "s", "a": "a", "b": "b", "direction": "equivalent", "margin": 0.10},
        {"id": "small", "suite": "s", "a": "c", "b": "d", "direction": "equivalent", "n": 40,
         "assumed": {"p_b": 0.6, "psi": 0.25, "delta": 0.0}},
        {"id": "small2", "suite": "s", "a": "c", "b": "d", "direction": "equivalent"}]}])
    out = tmp_path / "r.md"
    assert R.main([str(a), str(b), str(c), str(d), "--comparisons", str(plan), "--out", str(out)]) == 0
    md = out.read_text()
    big = ST.tost_paired_exact(150, 5, 5, 40, 0.10)["p"]
    assert big < 0.01
    line = next(l for l in md.splitlines() if l.startswith("| big |"))
    assert "equivalence shown" in line
    small = next(l for l in md.splitlines() if l.startswith("| small |"))
    assert "equivalence not shown" in small and "underpowered" in small and \
        "not detectable at this n: a-priori power" in small
    # r4 S4: without a-priori assumptions the label is never derived from the observed data
    small2 = next(l for l in md.splitlines() if l.startswith("| small2 |"))
    assert "not detectable" not in small2 and "a-priori: none declared" in small2


def test_seed_group_and_single_seed_flag(tmp_path):
    # three sampled seeds of A; B greedy. Item i passes in A for seeds where (i + seed) % 3 != 0.
    ids = range(30)
    a_dirs = []
    for s in (1, 2, 3):
        a_dirs.append(make_run(tmp_path / f"a{s}", "m", [sc(i, (i + s) % 3 != 0) for i in ids], thinking="on",
                               temperature=0.6, seed=s))
    b = make_run(tmp_path / "b", "m", [sc(i, i % 2 == 0) for i in ids])
    lone = make_run(tmp_path / "lone", "m2", [sc(i, 1) for i in ids], thinking="on", temperature=0.6)
    plan = write_plan(tmp_path, [{"name": "S", "comparisons": [
        {"id": "grp", "suite": "s", "a": ["a1", "a2", "a3"], "b": "b", "direction": "a>b"},
        {"id": "one", "suite": "s", "a": "lone", "b": "b", "direction": "a>b"}]}])
    out = tmp_path / "r.md"
    assert R.main([str(x) for x in a_dirs + [b, lone]] + ["--comparisons", str(plan), "--out", str(out)]) == 0
    md = out.read_text()
    assert "## Seed groups" in md and "m (think on) [3 seeds]" in md
    # every item passes in exactly 2 of 3 seeds: the per-item mean is 2/3, B is 50%
    grp = next(l for l in md.splitlines() if l.startswith("| grp |"))
    assert "66.7%" in grp and "50.0%" in grp and "+16.7 pp" in grp and "single seed" not in grp
    one = next(l for l in md.splitlines() if l.startswith("| one |"))
    assert "not confirmatory" in one
    assert "m2 (think on) (single seed — not confirmatory)" in md
    groups = R.seed_groups(R.unique_labels([R.summarize(x) for x in a_dirs + [b]]))
    assert len(groups) == 1 and groups[0]["seeds"] == [1, 2, 3]


def test_seed_group_needs_distinct_seeds(tmp_path):
    x = make_run(tmp_path / "x", "m", [sc(0, 1)])
    y = make_run(tmp_path / "y", "m", [sc(0, 0)])      # same config, same seed: a re-run, not a seed group
    assert R.seed_groups(R.unique_labels([R.summarize(x), R.summarize(y)])) == []


# ------------------------------------------------------------------ sensitivity, flags, legacy, clusters
def test_sensitivity_variants_and_item_flags(tmp_path):
    n = 40
    a_scores, b_scores, items = [], [], []
    for i in range(n):
        extra = {"legacy_passed": True} if i < 10 else {}
        a_scores.append(sc(i, i % 4 != 0, final_answer_prompt=(i < 6), extra=extra))
        b_scores.append(sc(i, i % 2 == 0))
        items.append({"id": f"i{i:03d}", "suite": "s",
                      "meta": {"has_signal": i >= 20, "cluster_id": f"t{i // 4}", "post_cutoff": i % 2 == 0}})
    resp = [{"id": f"i{i:03d}", "suite": "s", "finish_reason": "length" if i in (30, 31) else "stop"}
            for i in range(n)]
    a = make_run(tmp_path / "a", "ma", a_scores, responses=resp)
    b = make_run(tmp_path / "b", "mb", b_scores)
    ip = tmp_path / "items.jsonl"
    ip.write_text("".join(json.dumps(x) + "\n" for x in items))
    plan = write_plan(tmp_path, [{"name": "F", "comparisons": [{"id": "c1", "suite": "s", "a": "a", "b": "b",
                                                                 "direction": "a>b"}]}])
    out = tmp_path / "r.md"
    assert R.main([str(a), str(b), "--items", str(ip), "--comparisons", str(plan), "--out", str(out),
                   "--cluster-bootstrap"]) == 0
    md = out.read_text()
    for lab in ("follow-up → fail", "excl. follow-up items", "truncated → fail", "excl. truncated items",
                "excl. no-signal items", "post-cutoff only (both models)", "legacy scorer"):
        assert lab in md, lab
    assert "(10 clusters)" in md and "; cluster [" in md
    recs = [R.item_records(d, R.load_items([ip])) for d in (a, b)]
    run_a, run_b = [R._row_records(r, "s", None) for r in recs]
    hs = R.compare([run_a], [run_b], variant="has_signal")
    assert hs["n"] == 20
    dfu = R.compare([run_a], [run_b], variant="drop_followup")
    assert dfu["n"] == n - 6
    lg = R.compare([run_a], [run_b], variant="legacy")
    # items with a legacy score use it; the rest keep their only score: A passes all of i000-i009 under legacy
    prim = R.compare([run_a], [run_b])
    assert lg["n"] == n and lg["a_passed"] == prim["a_passed"] + sum(1 for i in range(10) if i % 4 == 0)
    # per-run sensitivity table under the suite
    assert "Sensitivity of each run's accuracy" in md


def test_legacy_scores_file(tmp_path):
    a = make_run(tmp_path / "a", "m", [sc(0, 1), sc(1, 1)])
    (a / "scores.legacy.jsonl").write_text(json.dumps(sc(0, 0)) + "\n" + json.dumps(sc(1, 0)) + "\n")
    recs = R.item_records(a, None, "scores.legacy.jsonl")
    assert R.outcome(recs["i000"], "accuracy", variant="legacy") == 0.0
    assert R.outcome(recs["i000"], "accuracy") == 1.0


def test_followup_detected_from_responses(tmp_path):
    a = make_run(tmp_path / "a", "m", [sc(0, 1)], responses=[{"id": "i000", "suite": "s",
                                                              "final_answer_prompt": True}])
    assert R.item_records(a)["i000"]["followup"] is True
    st = R.summarize(a)["suites"]["s"]
    assert st["final_answer_prompts"] == 1


def test_mae_comparison_uses_bootstrap_and_permutation(tmp_path):
    a_sc = [{"id": f"i{i}", "suite": "v", "scorer": "cvss_mae", "value": 0.5, "passed": True, "status": "ok"}
            for i in range(30)]
    b_sc = [{"id": f"i{i}", "suite": "v", "scorer": "cvss_mae", "value": 1.5 if i % 3 else 0.5,
             "passed": not i % 3, "status": "ok"} for i in range(30)]
    a = make_run(tmp_path / "a", "ma", a_sc)
    b = make_run(tmp_path / "b", "mb", b_sc)
    plan = write_plan(tmp_path, [{"name": "M", "comparisons": [
        {"id": "m1", "suite": "v", "metric": "mae", "a": "a", "b": "b", "direction": "a<b"},
        {"id": "m2", "suite": "v", "metric": "mae", "a": "a", "b": "b", "direction": "equivalent",
         "margin": 0.5}]}])
    ccsv = tmp_path / "c.csv"
    assert R.main([str(a), str(b), "--comparisons", str(plan), "--conf-csv", str(ccsv),
                   "--out", str(tmp_path / "r.md")]) == 0
    rows = {r["id"]: r for r in __import__("csv").DictReader(ccsv.open())}
    # 20 of 30 items differ by -1.0: mean diff -2/3; exact-ish permutation p is tiny
    assert float(rows["m1"]["diff"]) == pytest.approx(-2 / 3)
    assert float(rows["m1"]["p"]) < 0.001 and rows["m1"]["verdict"] == "confirmed"
    assert rows["m2"]["verdict"] == "different"         # the 90% interval lies wholly outside +/- 0.5
    md = (tmp_path / "r.md").read_text()
    assert "MAE -0.67 [" in md        # the exploratory pairs table shows the MAE difference with its CI


# ------------------------------------------------------------------ baselines
def test_baselines_mcq_cwe_cvss():
    mcq = [{"id": f"m{i}", "scorer": "mcq_letter", "answer": "C" if i < 6 else "A",
            "choices": ["A) x", "B) y", "C) z", "D) w"]} for i in range(10)]
    b = {x["name"]: x for x in R.baselines(mcq)}
    assert b["chance (uniform over the options)"]["accuracy"] == pytest.approx(0.25)
    top = b["always 'C' (most common gold letter)"]
    assert top["accuracy"] == pytest.approx(0.6) and top["oracle"]
    cwe = [{"id": f"c{i}", "scorer": "cwe_match", "answer": "CWE-269" if i < 7 else "CWE-79"} for i in range(10)]
    b = {x["name"]: x for x in R.baselines(cwe)}
    assert b["always CWE-79"]["accuracy"] == pytest.approx(0.3) and not b["always CWE-79"]["oracle"]
    assert b["always CWE-269 (most frequent gold label)"]["accuracy"] == pytest.approx(0.7)
    cv = [{"id": "v1", "scorer": "cvss_mae", "answer": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"},  # 9.8
          {"id": "v2", "scorer": "cvss_mae", "answer": 5.5}, {"id": "v3", "scorer": "cvss_mae", "answer": "7.0"}]
    b = {x["name"]: x for x in R.baselines(cv, cvss_constant=7.5)}
    c = b["constant 7.5"]
    assert c["mae"] == pytest.approx((2.3 + 2.0 + 0.5) / 3) and c["accuracy"] == pytest.approx(1 / 3)
    assert b["constant 7 (median gold score)"]["mae"] == pytest.approx((2.8 + 1.5 + 0) / 3)
    assert R.baselines([{"id": "q", "scorer": "llm_judge", "answer": "x"}]) == []


def test_baselines_in_report_from_dataset_paths(tmp_path):
    data = tmp_path / "repo" / "evals" / "data"
    data.mkdir(parents=True)
    items = [{"id": f"i{i:03d}", "suite": "s", "scorer": "mcq_letter", "answer": "B",
              "choices": ["A) a", "B) b", "C) c"]} for i in range(4)]
    (data / "s.jsonl").write_text("".join(json.dumps(x) + "\n" for x in items))
    a = make_run(tmp_path / "a", "m", [sc(i, 1) for i in range(4)])
    run = json.loads((a / "run.json").read_text())
    run["datasets"] = [{"path": "evals/data/s.jsonl"}]
    (a / "run.json").write_text(json.dumps(run))
    out = tmp_path / "r.md"
    assert R.main([str(a), "--data-root", str(tmp_path / "repo"), "--out", str(out)]) == 0
    md = out.read_text()
    assert "_baseline: chance (uniform over the options)_ | 4 | - | 33.3%" in md
    assert "_baseline: always 'B' (most common gold letter) (oracle" in md
    assert R.main([str(a), "--data-root", str(tmp_path / "repo"), "--out", str(out), "--no-baselines"]) == 0
    assert "_baseline" not in out.read_text()


# ------------------------------------------------------------------ plan file
def test_plan_validation_and_yaml(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"families": [{"comparisons": [{"id": "x", "suite": "s", "a": "a", "b": "b",
                                                                "direction": "bigger"}]}]}))
    with pytest.raises(ValueError):
        R.load_comparisons(bad)
    yaml = pytest.importorskip("yaml")
    y = tmp_path / "p.yaml"
    y.write_text(yaml.safe_dump({"families": [{"name": "F", "comparisons": [
        {"id": "x", "suite": "s", "a": "a", "b": "b", "direction": "a>b"}]}]}))
    assert R.load_comparisons(y)["families"][0]["comparisons"][0]["id"] == "x"



def test_render_markdown_backward_compatible(tmp_path):
    a = make_run(tmp_path / "a", "m", [sc(0, 1), sc(1, 0)])
    md = R.render_markdown([R.summarize(a)], "t")
    assert md.startswith("# t") and "## s" in md and "Pre-registered" not in md


# ------------------------------------------------------------------ round 2: real rigor metadata, primary specs,
# robustness, the confirmatory gate, new metrics, IUT, aggregates
FIXTURE = R.EVALS_DIR / "tests" / "fixtures" / "rigor_items.jsonl"


def test_rigor_flags_from_real_annotated_items(tmp_path):
    """20 verbatim meta blocks from the DATA round-2 build (prompts removed, e-mails replaced)."""
    items = R.load_items([FIXTURE])
    assert len(items) == 20
    scores = [{"id": i, "suite": it["suite"], "scorer": it["scorer"], "passed": True, "value": 1.0, "status": "ok"}
              for i, it in items.items()]
    d = make_run(tmp_path / "r", "coder", scores)
    recs = R.item_records(d, items)
    fl = lambda i, k: recs[i]["flags"][k]  # noqa: E731
    # cwe_name_in_text: True / False / None (unknown) taken as is
    assert fl("nvd-cwe-0001", "name_in_text") is True and fl("nvd-cwe-0005", "name_in_text") is False
    assert fl("nvd-cwe-0010", "name_in_text") is None
    # has_signal false items; a stripped key leak is no longer a leak, a flagged one on a public suite is
    assert fl("nvd-cwe-0607", "has_signal") is False
    # this run did not read the annotated (stripped) file, so the stripped leaks were in its prompts: present
    assert fl("nvd-cvss-0170", "leak") is True and fl("nvd-cvss-0205", "leak") is True
    assert fl("ctibench-rcm-0835", "leak") is True
    d2 = make_run(tmp_path / "r2", "coder", scores)
    rj = json.loads((d2 / "run.json").read_text())
    rj["datasets"] = [{"path": "x", "sha256": R.sha256_file(FIXTURE)}]
    (d2 / "run.json").write_text(json.dumps(rj))
    recs2 = R.item_records(d2, items)
    assert recs2["nvd-cvss-0170"]["flags"]["leak"] is False and recs2["nvd-cvss-0205"]["flags"]["leak"] is False
    assert recs2["ctibench-rcm-0835"]["flags"]["leak"] is True
    recs = recs2
    assert sum(1 for r in recs.values() if r["flags"]["leak"]) == 1
    fl = lambda i, k: recs[i]["flags"][k]  # noqa: E731
    # near-duplicate dev exposure, CSE tiers and exclusion, label role, per-model post-cutoff, template clusters
    assert fl("nvd-cwe-0022", "dev_neardup_exposed") is True
    assert fl("cse-frr-0035", "frr_exclude") is True and recs["cse-frr-0035"]["cats"]["frr_tier"] == "covert-harmful"
    assert recs["nvd-cwe-0011"]["cats"]["label_role"] == "adp" and recs["nvd-cwe-0220"]["cats"]["label_role"] == "nvd"
    assert recs["nvd-cwe-0001"]["cats"]["label_role"] == "cna"
    assert fl("nvd-cwe-0001", "post_cutoff") is True            # the run's model is "coder"
    assert recs["nvd-cwe-0001"]["cluster"] == items["nvd-cwe-0001"]["meta"]["rigor"]["template_cluster"]
    flash = make_run(tmp_path / "f", "local/qwen3.8-flash-next", scores)
    assert R.item_records(flash, items)["nvd-cwe-0001"]["flags"]["post_cutoff"] is None   # unknown for that model
    # the variants see them
    rr = list(recs.values())
    for op in ("has_signal", "no_leak", "no_name_in_text", "no_dev_neardup", "no_frr_exclude", "post_cutoff"):
        assert R.variant_applies(rr, op), op
    a = R._row_records(recs, "nvd-cwe", None)
    assert R.compare([a], [a], variant=("has_signal",))["n"] == len(a) - 2
    assert R.compare([a], [a], variant=("no_name_in_text",))["n"] == len(a) - 7   # 7 name-in-text items
    assert R.compare([a], [a], variant=("no_dev_neardup",))["n"] == len(a) - 3


def _pair(tmp_path, wins_a, wins_b, ties_pass, ties_fail, *, names=("a", "b"), suite="s", extra_a=None, resp_a=None,
          **kw):
    pa = [1] * wins_a + [0] * wins_b + [1] * ties_pass + [0] * ties_fail
    pb = [0] * wins_a + [1] * wins_b + [1] * ties_pass + [0] * ties_fail
    sa = [sc(i, p, suite, **((extra_a or {}).get(i, {}))) for i, p in enumerate(pa)]
    a = make_run(tmp_path / names[0], "ma", sa, responses=resp_a, **kw)
    b = make_run(tmp_path / names[1], "mb", [sc(i, p, suite) for i, p in enumerate(pb)], **kw)
    return a, b


def test_declared_primary_exclusions_and_robustness_downgrade(tmp_path):
    # A wins 14 items, B 2; A's wins on 10 items happened on truncated replies (finish_reason length)
    resp = [{"id": f"i{i:03d}", "suite": "s", "finish_reason": "length" if i < 10 else "stop"} for i in range(60)]
    a, b = _pair(tmp_path, 14, 2, 30, 14, resp_a=resp)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"alpha": 0.05, "defaults": {"sensitivity": ["drop_truncated"]}, "families": [
        {"name": "F", "comparisons": [{"id": "c", "suite": "s", "a": "a", "b": "b", "direction": "a>b"}]},
        {"name": "G", "comparisons": [{"id": "d", "suite": "s", "a": "a", "b": "b", "direction": "a>b",
                                       "primary": {"exclude": ["drop_truncated"]}, "sensitivity": []}]}]}))
    ccsv = tmp_path / "c.csv"
    out = tmp_path / "r.md"
    assert R.main([str(a), str(b), "--comparisons", str(plan), "--conf-csv", str(ccsv), "--out", str(out)]) == 0
    rows = {r["id"]: r for r in __import__("csv").DictReader(ccsv.open())}
    assert float(rows["c"]["p"]) == pytest.approx(ST.mcnemar_exact(14, 2))
    assert rows["c"]["verdict"] == "confirmed (not robust to excl. truncated items)" and rows["c"]["robust"] == "drop_truncated"
    # the declared primary of d drops the truncated items: n 50, 4 vs 2 -> not confirmed
    assert rows["d"]["n"] == "50" and rows["d"]["primary"] == "excl. truncated items"
    assert rows["d"]["verdict"] == "not confirmed"
    md = out.read_text()
    assert "excl. truncated items°" in md and "| no: excl. truncated items |" in md


def _outs(d):
    return {n: (R.sha256_file(d / n) if (d / n).is_file() else None) for n in R.OUTPUT_FILES}


def _manifest_dict(plan, **extra):
    """A tag-time freeze manifest that pins the plan, the analysis knobs and the running code."""
    code = {k: R.sha256_file(R.EVALS_DIR / rel) for k, rel in R.CODE_FILES.items()}
    m = {"suites": {}, "comparisons_sha256": R.sha256_file(plan),
         "analysis": {"min_n": R.MIN_N, "n_boot": ST.DEFAULT_BOOT, "n_perm": ST.DEFAULT_PERM,
                      "seed": ST.DEFAULT_SEED, "alpha": 0.05},
         "code_sha256": code}
    m.update(extra)
    return m


def _item_sets(d):
    by = {}
    for l in (d / "scores.jsonl").read_text().splitlines():
        if l.strip():
            x = json.loads(l)
            by.setdefault(x["suite"], []).append(x["id"])
    return {su: {"n": len(ids), "ids_sha256": R._ids_sha(ids)} for su, ids in by.items()}


def _protocol_run(d, comparisons_sha, manifest, **over):
    run = {"model": d.name, "thinking": "off", "sampling": {"temperature": 0.0, "seed": 1234},
           "protocol": {"mode": "confirmatory", "checks_failed": [], "deviations": [], "frozen_ref": "eval-protocol-x",
                        "freeze": {"path": str(manifest), "sha256": R.sha256_file(manifest)}, "seeds": [1234]},
           "budget": {"policy_sha256": "b" * 64}, "serving": {"sampler_check": {"ok": True}},
           "scorer_version": {"value": "s2", "source": "x"}, "git": {"sha": "f" * 40, "dirty": False},
           "harness_sha256": {"run.py": "1" * 64, "scorers.py": "2" * 64, "report.py": "3" * 64},
           "ended": "2026-10-11T00:00:00Z", "item_sets": _item_sets(d),
           "outputs_sha256": _outs(d)}
    run["protocol"]["outputs_sha256"] = _outs(d)
    run["started"] = "2026-10-11T00:00:00Z"
    for k, v in over.items():
        run[k] = v
    (d / "run.json").write_text(json.dumps(run))
    led = Path(manifest).parent / "evals" / "plans" / "attempts-C1.jsonl"
    led.parent.mkdir(parents=True, exist_ok=True)
    lines = [x for x in R.read_jsonl(led) if x.get("run_id") != d.name]
    lines += [{"event": "start", "run_id": d.name, "started": run["started"]},
              {"event": "end", "run_id": d.name, "started": run["started"], "outputs_sha256": _outs(d)}]
    led.write_text("".join(json.dumps(x) + "\n" for x in lines))


def _gate_runs(dirs):
    return [dict(json.loads((d / "run.json").read_text()), _dir=d.name, _path=str(d)) for d in dirs]


def test_confirmatory_gate(tmp_path):
    a, b = _pair(tmp_path, 12, 1, 40, 7)
    plan = write_plan(tmp_path, [{"name": "F", "comparisons": [{"id": "c", "suite": "s", "a": "a", "b": "b",
                                                                 "direction": "a>b"}]}])
    man = tmp_path / "manifest.json"
    man.write_text(json.dumps(_manifest_dict(plan)))
    for d in (a, b):
        _protocol_run(d, None, man)
    out = tmp_path / "r.md"
    assert R.main([str(a), str(b), "--comparisons", str(plan), "--data-root", str(tmp_path), "--out", str(out)]) == 0
    md = out.read_text()
    assert "## Pre-registered comparisons (primary analysis)" in md and "confirmatory: c" in md
    runs = _gate_runs((a, b))
    assert R.confirmatory_gate(runs, plan) == []
    # each failure mode
    bad = json.loads(json.dumps(runs))
    bad[0]["protocol"]["mode"] = "rescored-exploratory"
    assert any("protocol.mode" in g for g in R.confirmatory_gate(bad, plan))
    bad = json.loads(json.dumps(runs))
    bad[0]["protocol"]["checks_failed"] = ["serving_unrecorded: no probe", "dirty_tree: untracked files"]
    bad[0]["protocol"]["deviations"] = ["serving_unrecorded=approved by owner"]
    g = R.confirmatory_gate(bad, plan)
    assert any("dirty_tree" in x for x in g) and not any("serving_unrecorded" in x for x in g)
    # r4 loophole D4: an estimand-changing check accepted as a deviation still makes the family post-hoc
    bad[0]["protocol"]["checks_failed"] = [{"id": "scorer_override", "msg": "x"}]
    bad[0]["protocol"]["deviations"] = [{"id": "scorer_override", "text": "x"}]
    assert any("estimand-changing" in x or "never waivable" in x for x in R.confirmatory_gate(bad, plan))
    bad = json.loads(json.dumps(runs))
    bad[1]["budget"]["policy_sha256"] = "c" * 64
    assert any("policy_sha256" in x for x in R.confirmatory_gate(bad, plan))
    bad = json.loads(json.dumps(runs))
    bad[1]["serving"]["sampler_check"]["ok"] = None
    assert any("sampler_check" in x for x in R.confirmatory_gate(bad, plan))
    bad = json.loads(json.dumps(runs))
    bad[1]["scorer_version"] = {"value": "s3"}
    assert any("scorer_version" in x for x in R.confirmatory_gate(bad, plan))
    other = tmp_path / "other.json"
    other.write_text(plan.read_text() + " ")
    assert any("comparisons file sha256" in x for x in R.confirmatory_gate(runs, other))
    man.write_text(json.dumps(_manifest_dict(plan, x=1)))
    assert any("manifest changed" in x for x in R.confirmatory_gate(runs, plan))
    assert R.main([str(a), str(b), "--comparisons", str(plan), "--data-root", str(tmp_path), "--out", str(out)]) == 0
    assert "## Post-hoc / exploratory (pre-specified comparisons, not confirmatory)" in out.read_text()


def test_gate_r3_hardening(tmp_path):
    """Inverted red-team loopholes N6-N9 (round 3): each now fails the gate."""
    a, b = _pair(tmp_path, 12, 1, 40, 7)
    plan = write_plan(tmp_path, [{"name": "F", "comparisons": [{"id": "c", "suite": "s", "a": "a", "b": "b",
                                                                 "direction": "a>b"}]}])
    ann = tmp_path / "ann.jsonl"
    ann.write_text(json.dumps({"id": "i000", "suite": "s", "meta": {"rigor": {"has_signal": True}}}) + "\n")
    man = tmp_path / "manifest.json"
    man.write_text(json.dumps(_manifest_dict(plan, annotations={"ann.jsonl": R.sha256_file(ann)})))
    for d in (a, b):
        _protocol_run(d, None, man)
    runs = _gate_runs((a, b))
    assert R.confirmatory_gate(runs, plan, tmp_path, [ann]) == []
    # N6: scores rewritten after the run (a rescore relabelled by hand): outputs no longer match run.json
    with (a / "scores.jsonl").open("a") as fh:
        fh.write(json.dumps(sc(99, 1)) + "\n")
    assert any("scores.jsonl does not match" in x for x in R.confirmatory_gate(_gate_runs((a, b)), plan, tmp_path))
    nohash = json.loads(json.dumps(runs))
    nohash[0]["protocol"].pop("outputs_sha256")         # the top-level copy is never a fallback
    assert any("protocol.outputs_sha256 missing" in x for x in R.confirmatory_gate(nohash, plan, tmp_path))
    _protocol_run(a, None, man)
    runs = _gate_runs((a, b))
    # a grades.jsonl that appeared after the run, and a hand-restored rescored run
    (b / "grades.jsonl").write_text("{}\n")
    assert any("grades.jsonl does not match" in x for x in R.confirmatory_gate(_gate_runs((a, b)), plan, tmp_path))
    (b / "grades.jsonl").unlink()
    bad = json.loads(json.dumps(runs))
    bad[0]["protocol"]["original"] = {"mode": "confirmatory"}
    assert any("rescored" in x for x in R.confirmatory_gate(bad, plan, tmp_path))
    (a / "scores.confirmatory.jsonl").write_text("")
    assert any("rescored" in x for x in R.confirmatory_gate(_gate_runs((a, b)), plan, tmp_path))
    (a / "scores.confirmatory.jsonl").unlink()
    nop = json.loads(json.dumps(runs))
    nop[1]["protocol"].pop("outputs_sha256")
    assert any("protocol.outputs_sha256" in x for x in R.confirmatory_gate(nop, plan, tmp_path))
    # N7: runs at different harness commits / frozen refs / harness hashes
    for key, val in (("git", {"sha": "e" * 40}), ("harness_sha256", {"run.py": "9" * 64})):
        bad = json.loads(json.dumps(runs))
        bad[1][key] = val
        assert any(k in x for x in R.confirmatory_gate(bad, plan, tmp_path)
                   for k in ("git.sha", "harness_sha256")), key
    bad = json.loads(json.dumps(runs))
    bad[1]["protocol"]["frozen_ref"] = "HEAD"
    assert any("frozen ref" in x for x in R.confirmatory_gate(bad, plan, tmp_path))
    # N8: integrity checks are never waivable, even with a deviation each
    for cid in ("git_dirty", "git_untracked", "frozen_ref", "freeze_ids", "freeze_content", "freeze_unstripped",
                "files_unpinned", "plan_unfrozen", "sealed_only", "items_hash"):
        bad = json.loads(json.dumps(runs))
        bad[0]["protocol"]["checks_failed"] = [{"id": cid, "msg": "x"}]
        bad[0]["protocol"]["deviations"] = [{"id": cid, "text": "waived"}]
        assert any("never waivable" in x for x in R.confirmatory_gate(bad, plan, tmp_path)), cid
    # N9: an --items annotation file that no manifest pins
    ann2 = tmp_path / "ann2.jsonl"
    ann2.write_text(ann.read_text().replace("true", "false"))
    assert any("not pinned" in x for x in R.confirmatory_gate(runs, plan, tmp_path, [ann2]))
    # an accepted (waivable) deviation keeps the gate but shows in the title
    for d in (a, b):
        rj = json.loads((d / "run.json").read_text())
        rj["protocol"]["checks_failed"] = [{"id": "serving_unrecorded", "msg": "x"}]
        rj["protocol"]["deviations"] = [{"id": "serving_unrecorded", "text": "probe offline"}]
        (d / "run.json").write_text(json.dumps(rj))
    out = tmp_path / "r.md"
    assert R.main([str(a), str(b), "--comparisons", str(plan), "--items", str(ann), "--data-root", str(tmp_path),
                   "--out", str(out)]) == 0
    md = out.read_text()
    assert "## Pre-registered (with deviations: serving_unrecorded)" in md and \
        "a: serving_unrecorded = probe offline" in md


def test_gate_fails_on_not_evaluable_and_judge(tmp_path):
    a, b = _pair(tmp_path, 3, 1, 10, 6)          # n = 20 < 30: not evaluable
    plan = write_plan(tmp_path, [{"name": "F", "comparisons": [{"id": "c", "suite": "s", "a": "a", "b": "b",
                                                                 "direction": "a>b"}]}])
    man = tmp_path / "manifest.json"
    man.write_text(json.dumps(_manifest_dict(plan)))
    for d in (a, b):
        _protocol_run(d, None, man)
    out = tmp_path / "r.md"
    assert R.main([str(a), str(b), "--comparisons", str(plan), "--data-root", str(tmp_path), "--out", str(out)]) == 0
    md = out.read_text()
    assert "registered comparison c is not evaluable" in md and "## Post-hoc" in md


def test_refusal_tiers_metric_excludes_truncated(tmp_path):
    tiers = ["full", "full", "partial", "refuse", "full", "unparsed_truncated"]
    scores = [{"id": f"r{i}", "suite": "frr", "scorer": "refusal", "passed": True, "value": 1.0,
               "status": "unparsed_truncated" if t == "unparsed_truncated" else "ok",
               "extra": {"refusal_tier": t, "opener_refusal": t != "full"}} for i, t in enumerate(tiers)]
    resp = [{"id": f"r{i}", "suite": "frr", "finish_reason": "length" if t == "unparsed_truncated" else "stop"}
            for i, t in enumerate(tiers)]
    d = make_run(tmp_path / "a", "m", scores, responses=resp)
    recs = R.item_records(d)
    vals = [R.outcome(r, "refusal_full") for r in recs.values()]
    assert vals.count(None) == 1 and sum(v for v in vals if v) == 3
    st = R.summarize(d)["suites"]["frr"]
    assert st["tiers"] == {"full": 3, "partial": 1, "refuse": 1}
    assert "Refusal tiers (truncated replies excluded)" in R.render_markdown([R.summarize(d)])
    assert "60.0% / 20.0% / 20.0% (n 5)" in R.render_markdown([R.summarize(d)])


def test_mae_imputation_rules(tmp_path):
    items = {f"v{i}": {"id": f"v{i}", "suite": "v", "scorer": "cvss_mae", "answer": g}
             for i, g in enumerate([9.8, 5.0, 7.0])}
    scores = [{"id": "v0", "suite": "v", "scorer": "cvss_mae", "value": 0.8, "passed": True, "status": "ok"},
              {"id": "v1", "suite": "v", "scorer": "cvss_mae", "value": None, "passed": False, "status": "unparsed",
               "extra": {"legacy_value": 10}},
              {"id": "v2", "suite": "v", "scorer": "cvss_mae", "value": None, "passed": False,
               "status": "unparsed_truncated"}]
    d = make_run(tmp_path / "a", "m", scores)
    recs = R.item_records(d, items)
    o = lambda i, imp: R.outcome(recs[i], "mae", impute=imp, constant=7.5)  # noqa: E731
    assert o("v0", "none") == 0.8 and o("v1", "none") is None
    assert o("v1", "max") == 10.0 and o("v1", "baseline") == pytest.approx(2.5) and o("v2", "baseline") == pytest.approx(0.5)
    assert R.summarize(d, impute="baseline", items=items)["suites"]["v"]["mae"] == pytest.approx((0.8 + 2.5 + 0.5) / 3)
    s = R.summarize(d, impute="none", items=items)["suites"]["v"]
    assert s["mae"] == pytest.approx(0.8) and s["n_mae_unimputed"] == 2
    assert R.summarize(d, items=items)["suites"]["v"]["mae"] == pytest.approx((0.8 + 20) / 3)   # legacy 'max'


def test_either_label_and_ceiling(tmp_path):
    items = {
        "c1": {"id": "c1", "suite": "w", "scorer": "cwe_match", "answer": "CWE-79",
               "meta": {"labels": {"cwe_nvd": ["CWE-79"], "cwe_cna": ["CWE-80"]}}},
        "c2": {"id": "c2", "suite": "w", "scorer": "cwe_match", "answer": "CWE-89",
               "meta": {"labels": {"cwe_nvd": ["CWE-89"], "cwe_cna": ["CWE-89"]}}},
        "c3": {"id": "c3", "suite": "w", "scorer": "cwe_match", "answer": "CWE-20",
               "meta": {"labels": {"cwe_nvd": [], "cwe_cna": ["CWE-20"]}}},
    }
    scores = [{"id": "c1", "suite": "w", "scorer": "cwe_match", "passed": False, "value": 0.0, "status": "ok",
               "parsed": "CWE-80"},
              {"id": "c2", "suite": "w", "scorer": "cwe_match", "passed": True, "value": 1.0, "status": "ok",
               "parsed": "CWE-89"},
              {"id": "c3", "suite": "w", "scorer": "cwe_match", "passed": True, "value": 1.0, "status": "ok",
               "parsed": "CWE-20"}]
    d = make_run(tmp_path / "a", "m", scores)
    recs = R.item_records(d, items)
    # any label counts; a single-label item reduces to the strict metric
    assert [R.outcome(recs[i], "accuracy_either") for i in ("c1", "c2", "c3")] == [1.0, 1.0, 1.0]
    c = R.label_ceiling(items.values())["nvd_vs_cna"]
    assert c["cwe_n"] == 2 and c["cwe_exact"] == pytest.approx(0.5)
    cv = [{"id": "x", "meta": {"labels": {"cvss_nvd": ["CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"],
                                         "cvss_cna": ["CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N"]}}},
          {"id": "y", "meta": {"labels": {"cvss_nvd": ["CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N"],
                                         "cvss_cna": ["CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N"]}}}]
    c = R.label_ceiling(cv)["nvd_vs_cna"]
    assert c["cvss_n"] == 2 and c["cvss_mean_abs"] == pytest.approx((9.8 - 7.5) / 2)
    assert c["cvss_within1"] == pytest.approx(0.5) and c["cvss_exact_vector"] == pytest.approx(0.5)
    rec = {"scorer": "cvss_mae", "status": "ok", "predicted": 7.0, "followup": False, "truncated": False,
           "labels": cv[0]["meta"]["labels"]}
    assert R.outcome(rec, "mae_either") == pytest.approx(0.5) and R.outcome(rec, "accuracy_either") == 1.0


def test_mcq_balanced_longest_and_key_longest_stratum(tmp_path):
    def item(i, ans, longest):
        ch = [f"{l}) x" for l in "ABCD"]
        ch["ABCD".index(longest)] = f"{longest}) a much longer option text"
        return {"id": f"m{i}", "suite": "q", "scorer": "mcq_letter", "answer": ans, "choices": ch}
    its = [item(0, "A", "A"), item(1, "A", "B"), item(2, "A", "A"), item(3, "B", "B")]
    b = {x["name"]: x for x in R.baselines(its)}
    assert b["longest option (ties share credit)"]["accuracy"] == pytest.approx(0.75)
    assert b["always 'A' (most common gold letter)"]["balanced"] == pytest.approx(0.5)
    items = {x["id"]: x for x in its}
    d = make_run(tmp_path / "a", "m", [dict(sc(i, p, "q"), id=f"m{i}", scorer="mcq_letter")
                                       for i, p in enumerate([1, 0, 1, 0])])
    summ = [R.summarize(d)]
    recs = [R.item_records(d, items)]
    assert R.mcq_balanced(summ, recs, "q", "fail")[0][1] == pytest.approx((2 / 3 + 0) / 2)
    st = {x["flag"]: x for x in R.strata_rows(summ, recs, "q", "fail")}
    row = st["key_longest"]["rows"][0]
    assert (row["t"], row["nt"], row["f"], row["nf"]) == (pytest.approx(2 / 3), 3, 0.0, 1)


def test_iut_family(tmp_path):
    a, b = _pair(tmp_path, 15, 2, 30, 13, names=("a", "b"))
    c = make_run(tmp_path / "c", "mc", [sc(i, 1) for i in range(60)])     # A loses to c
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"alpha": 0.05, "families": [{"name": "Best", "test": "iut", "comparisons": [
        {"id": "ab", "suite": "s", "a": "a", "b": "b", "direction": "a>b"},
        {"id": "ac", "suite": "s", "a": "a", "b": "c", "direction": "a>b"}]}]}))
    ccsv, out = tmp_path / "c.csv", tmp_path / "r.md"
    assert R.main([str(a), str(b), str(c), "--comparisons", str(plan), "--conf-csv", str(ccsv), "--out", str(out)]) == 0
    rows = {r["id"]: r for r in __import__("csv").DictReader(ccsv.open())}
    p = ST.mcnemar_exact(15, 2)
    assert float(rows["ab"]["p_holm"]) == pytest.approx(p)          # no adjustment inside an IUT family
    assert rows["ab"]["verdict"] == "confirmed (IUT member)" and rows["ac"]["verdict"].startswith("reversed")
    assert "family: not confirmed (1 of 2 members fail)" in out.read_text()
    with pytest.raises(ValueError):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"families": [{"name": "x", "test": "iut", "comparisons": [
            {"id": "e", "suite": "s", "a": "a", "b": "b", "direction": "equivalent", "margin": 0.1}]}]}))
        R.load_comparisons(bad)


def test_aggregate_refuses_unequal_suite_sets(tmp_path):
    a = make_run(tmp_path / "a", "m1", [sc(0, 1, "s1"), sc(1, 0, "s2")])
    b = make_run(tmp_path / "b", "m2", [sc(0, 1, "s1")])
    assert R.main([str(a), str(b), "--aggregate", "--out", str(tmp_path / "r.md")]) == 2
    with pytest.raises(ValueError):
        R.macro_aggregate([R.summarize(a), R.summarize(b)], [R.item_records(a), R.item_records(b)])
    c = make_run(tmp_path / "c", "m3", [sc(0, 0, "s1"), sc(1, 1, "s2"), sc(2, 1, "s2")])
    agg = R.macro_aggregate([R.summarize(a), R.summarize(c)], [R.item_records(a), R.item_records(c)])
    # s2 uses only the shared item i001
    assert agg["runs"][1]["suites"]["s2"] == (1.0, 1) and agg["runs"][1]["macro"] == pytest.approx(0.5)


def test_sensitivity_columns_only_when_they_change_something(tmp_path):
    # follow-up items exist but every one already failed in both runs: follow-up -> fail changes nothing
    a, b = _pair(tmp_path, 10, 1, 20, 9, extra_a={i: {"final_answer_prompt": True} for i in range(31, 40)})
    plan = write_plan(tmp_path, [{"name": "F", "comparisons": [{"id": "c", "suite": "s", "a": "a", "b": "b",
                                                                 "direction": "a>b"}]}])
    out = tmp_path / "r.md"
    assert R.main([str(a), str(b), "--comparisons", str(plan), "--out", str(out)]) == 0
    md = out.read_text()
    conf_part = md.split("## s")[0]
    assert "follow-up → fail" not in conf_part and "excl. follow-up items" in conf_part


# ------------------------------------------------------------------ round 3: inverted red-team probes
def _cwe_rec(parsed, gold, nvd=(), cna=(), adp=(), passed=None):
    return {"status": "ok", "scorer": "cwe_match", "parsed": parsed, "passed": parsed == gold if passed is None
            else passed, "gold": gold, "followup": False, "truncated": False,
            "labels": {"cwe_nvd": list(nvd), "cwe_cna": list(cna), "cwe_adp": list(adp)}}


def test_either_matches_any_label_and_reduces_to_strict():
    """Inverted either_probe / either_sim (r3 N1): a CNA-only item is evaluable; an ADP label counts."""
    r = _cwe_rec("CWE-89", "CWE-89", cna=["CWE-89"])
    assert R.outcome(r, "accuracy") == R.outcome(r, "accuracy_either") == 1.0
    r2 = _cwe_rec("CWE-89", "CWE-20", cna=["CWE-20"], adp=["CWE-89"])
    assert R.outcome(r2, "accuracy") == 0.0 and R.outcome(r2, "accuracy_either") == 1.0
    r3 = dict(_cwe_rec("CWE-89", "CWE-20"), labels={"weaknesses": [{"role": "adp", "cwes": ["CWE-89"]}]})
    assert R.outcome(r3, "accuracy_either") == 1.0                       # roles from labels.weaknesses
    r4 = _cwe_rec("CWE-79", "CWE-20", cna=["CWE-20"])
    assert R.outcome(r4, "accuracy_either") == R.outcome(r4, "accuracy") == 0.0
    # CVSS: labels.metrics vectors of any role
    rec = {"scorer": "cvss_mae", "status": "ok", "predicted": 9.8, "followup": False, "truncated": False,
           "gold": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N",
           "labels": {"metrics": [{"role": "adp", "version": "3.1",
                                   "vector": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}]}}
    assert R.outcome(rec, "mae_either") == 0.0 and R.outcome(rec, "accuracy_either") == 1.0


def test_either_comparison_on_cna_only_pool_is_evaluable(tmp_path):
    """The C1 D4.sealed shape: nvd-cwe items with only CNA labels. Before r3 this evaluated on n = 0."""
    items = {f"w{i}": {"id": f"w{i}", "suite": "nvd-cwe", "scorer": "cwe_match", "answer": "CWE-79",
                       "meta": {"labels": {"cwe_nvd": [], "cwe_cna": ["CWE-79"], "cwe_adp": []}}} for i in range(40)}
    mk = lambda i, ok: {"id": f"w{i}", "suite": "nvd-cwe", "scorer": "cwe_match", "passed": ok,  # noqa: E731
                        "value": float(ok), "status": "ok", "parsed": "CWE-79" if ok else "CWE-20"}
    a = make_run(tmp_path / "a", "ma", [mk(i, i % 4 != 0) for i in range(40)])
    b = make_run(tmp_path / "b", "mb", [mk(i, i % 4 != 1) for i in range(40)])
    ip = tmp_path / "items.jsonl"
    ip.write_text("".join(json.dumps(x) + "\n" for x in items.values()))
    plan = write_plan(tmp_path, [{"name": "D4", "comparisons": [
        {"id": "e", "suite": "nvd-cwe", "metric": "accuracy_either", "a": "a", "b": "b", "direction": "a>b"}]}])
    ccsv = tmp_path / "c.csv"
    assert R.main([str(a), str(b), "--items", str(ip), "--comparisons", str(plan), "--conf-csv", str(ccsv),
                   "--out", str(tmp_path / "r.md")]) == 0
    row = next(__import__("csv").DictReader(ccsv.open()))
    assert row["n"] == "40" and not row["verdict"].startswith("not evaluable")


def test_zero_n_primary_is_not_evaluable_and_counts_as_p1(tmp_path):
    a, b = _pair(tmp_path, 12, 1, 40, 7)
    plan = write_plan(tmp_path, [{"name": "F", "comparisons": [
        {"id": "ok", "suite": "s", "a": "a", "b": "b", "direction": "a>b"},
        {"id": "empty", "suite": "no-such-suite", "a": "a", "b": "b", "direction": "a>b"}]}])
    ccsv = tmp_path / "c.csv"
    assert R.main([str(a), str(b), "--comparisons", str(plan), "--conf-csv", str(ccsv),
                   "--out", str(tmp_path / "r.md")]) == 0
    rows = {r["id"]: r for r in __import__("csv").DictReader(ccsv.open())}
    assert rows["empty"]["verdict"].startswith("not evaluable") and "n = 0" in rows["empty"]["verdict"]
    assert float(rows["ok"]["p_holm"]) == pytest.approx(min(1.0, 2 * ST.mcnemar_exact(12, 1)))   # m = 2, not 1


def test_empty_post_cutoff_stratum_is_not_a_flip(tmp_path):
    """Inverted postcutoff_probe (r3 protocol N5): a public suite with no post-cutoff item."""
    n = 200
    pa = [1] * 120 + [1] * 40 + [0] * 5 + [0] * 35
    pb = [1] * 120 + [0] * 40 + [1] * 5 + [0] * 35
    a = make_run(tmp_path / "c1-a", "ma", [sc(i, p, "ctibench-mcq") for i, p in enumerate(pa)])
    b = make_run(tmp_path / "c1-b", "mb", [sc(i, p, "ctibench-mcq") for i, p in enumerate(pb)])
    items = tmp_path / "items.jsonl"
    items.write_text("".join(json.dumps({"id": f"i{i:03d}", "suite": "ctibench-mcq", "meta": {"rigor": {
        "post_cutoff": {"ma": False, "mb": False}, "has_signal": True}}}) + "\n" for i in range(n)))
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"alpha": 0.05, "defaults": {"metric": "accuracy", "primary": {"errors": "fail"}},
                                "families": [{"name": "F", "test": "holm", "comparisons": [
                                    {"id": "F.1", "suite": "ctibench-mcq", "direction": "a>b", "a": "c1-a",
                                     "b": "c1-b", "primary": {"exclude": []},
                                     "sensitivity": ["post_cutoff", "drop_truncated"]}]}]}))
    ccsv, out = tmp_path / "c.csv", tmp_path / "r.md"
    assert R.main([str(a), str(b), "--comparisons", str(plan), "--items", str(items), "--conf-csv", str(ccsv),
                   "--out", str(out)]) == 0
    row = next(__import__("csv").DictReader(ccsv.open()))
    assert row["verdict"] == "confirmed" and "post" not in row["robust"]
    md = out.read_text()
    assert "| F.1 | post-cutoff only (both models) | n/a (n = 0) |" in md


def test_tiny_sensitivity_is_not_evaluable_not_a_flip(tmp_path):
    # 10 of A's wins are on follow-up items; dropping follow-ups leaves n = 25 < 30
    extra = {i: {"final_answer_prompt": True} for i in range(35)}
    a, b = _pair(tmp_path, 14, 2, 30, 14, extra_a=extra)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"alpha": 0.05, "defaults": {"sensitivity": ["drop_followup"]}, "families": [
        {"name": "F", "comparisons": [{"id": "c", "suite": "s", "a": "a", "b": "b", "direction": "a>b"}]}]}))
    ccsv, out = tmp_path / "c.csv", tmp_path / "r.md"
    assert R.main([str(a), str(b), "--comparisons", str(plan), "--conf-csv", str(ccsv), "--out", str(out)]) == 0
    row = next(__import__("csv").DictReader(ccsv.open()))
    assert row["verdict"] == "confirmed" and row["robust"] == "yes"
    assert "not evaluable (n = 25 < 30)" in out.read_text()


def test_label_role_from_item_provenance_not_score_record(tmp_path):
    """Inverted adp_role_bug (r3 N2): run.py writes meta.label_source ('cna') into scores; the item says ADP."""
    it = {"id": "x", "suite": "nvd-cwe", "meta": {"label_source": "cna",
                                                 "rigor": {"label_source": "adp-secondary:CISA-ADP"}}}
    assert R._label_role({"label_source": "cna"}, it) == "adp"
    assert R._label_role({"label_source": "cna"}, {"meta": {}}) == "cna"


def test_ceiling_per_source_pair_and_cluster_noise():
    v = lambda c, i, a: f"CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:{c}/I:{i}/A:{a}"  # noqa: E731
    its = [{"id": "1", "scorer": "cvss_mae", "meta": {"labels": {"cvss_nvd": [v("H", "H", "H")],
                                                                 "cvss_cna": [v("H", "N", "N")],
                                                                 "cvss_adp": [v("H", "H", "H")]}}},
           {"id": "2", "scorer": "cvss_mae", "meta": {"labels": {"cvss_nvd": [v("H", "N", "N")],
                                                                 "cvss_adp": [v("H", "N", "N")]}}}]
    c = R.label_ceiling(its)
    assert c["nvd_vs_cna"]["cvss_n"] == 1 and c["nvd_vs_cna"]["cvss_exact_vector"] == 0.0
    assert c["nvd_vs_adp"]["cvss_n"] == 2 and c["nvd_vs_adp"]["cvss_exact_vector"] == 1.0
    assert c["nvd_vs_adp"]["cvss_mean_abs_ci"] is None          # n < 10: no CI ("n too small")
    cl = [{"id": str(i), "scorer": "cwe_match", "answer": a, "meta": {"rigor": {"near_dup_cluster": k}}}
          for i, (a, k) in enumerate([("CWE-269", "t1"), ("CWE-269", "t1"), ("CWE-284", "t1"), ("CWE-79", "t2")])]
    nz = R.cluster_label_noise(cl)
    assert nz["clusters"] == 1 and nz["cwe_pairs"] == 3 and nz["cwe_disagree"] == pytest.approx(2 / 3)
    assert nz["too_small"] and nz["cwe_disagree_cluster"] == pytest.approx(2 / 3)
    # r4 F5: per-cluster weighting differs from per-pair when clusters differ in size
    big = [{"id": f"b{i}", "scorer": "cwe_match", "answer": "CWE-79", "meta": {"rigor": {"near_dup_cluster": "big"}}}
           for i in range(10)]
    small = [{"id": f"s{i}", "scorer": "cwe_match", "answer": a, "meta": {"rigor": {"near_dup_cluster": "small"}}}
             for i, a in enumerate(["CWE-79", "CWE-89"])]
    nz2 = R.cluster_label_noise(big + small)
    assert nz2["cwe_disagree"] == pytest.approx(1 / 46) and nz2["cwe_disagree_cluster"] == pytest.approx(0.5)
    # r4 F12: one row per CVE when suites share CVEs
    dup = [dict(its[1], id="x1", meta=dict(its[1]["meta"], cve="CVE-1")),
           dict(its[1], id="x2", meta=dict(its[1]["meta"], cve="CVE-1"))]
    assert R.label_ceiling(dup)["nvd_vs_adp"]["cvss_n"] == 1
    lines = R._render_extras({"ceiling": c})
    assert any("n too small for a CI" in l for l in lines) and any("ADP labels not used" in l for l in lines)


def test_flag_coverage_unknown_refuses_unless_allowed(tmp_path):
    items = [{"id": f"i{i:03d}", "suite": "s", "meta": {"rigor": {"cwe_name_in_text": (None if i < 5 else i % 2 == 0)}}}
             for i in range(60)]
    ip = tmp_path / "items.jsonl"
    ip.write_text("".join(json.dumps(x) + "\n" for x in items))
    a, b = _pair(tmp_path, 15, 2, 30, 13)
    plan = tmp_path / "plan.json"
    base = {"id": "c", "suite": "s", "a": "a", "b": "b", "direction": "a>b"}
    plan.write_text(json.dumps({"alpha": 0.05, "families": [{"name": "F", "comparisons": [
        dict(base, id="strict", primary={"exclude": ["no_name_in_text"]}),
        dict(base, id="allowed", primary={"exclude": ["no_name_in_text"], "allow_unknown": ["no_name_in_text"]})]}]}))
    ccsv, out = tmp_path / "c.csv", tmp_path / "r.md"
    assert R.main([str(a), str(b), "--items", str(ip), "--comparisons", str(plan), "--conf-csv", str(ccsv),
                   "--out", str(out)]) == 0
    rows = {r["id"]: r for r in __import__("csv").DictReader(ccsv.open())}
    assert rows["strict"]["verdict"].startswith("not evaluable (exclusion flag not known")
    assert not rows["allowed"]["verdict"].startswith("not evaluable")
    md = out.read_text()
    assert "| strict | name_in_text 55/60 |" in md and "(allowed unknown)" in md


def test_impute_and_tier_ops_and_prefix(tmp_path):
    rec = {"scorer": "cvss_mae", "status": "unparsed", "value": None, "expected": 9.0, "gold": None,
           "followup": False, "truncated": False, "legacy": None}
    assert R.outcome(rec, "mae", impute="baseline", constant=7.5) == pytest.approx(1.5)
    assert R.outcome(rec, "mae", variant="impute_none", impute="baseline") is None
    assert R.outcome(rec, "mae", variant="impute_max", impute="baseline") == 10.0
    t = {"status": "unparsed_truncated", "truncated": True, "tier": "unparsed_truncated", "tier_prefix": "partial",
         "followup": False}
    assert R.outcome(t, "refusal_partial") == 1.0                       # tiered from the visible prefix
    assert R.outcome(t, "refusal_full", variant="trunc_best") == 1.0
    assert R.outcome(t, "refusal_refuse", variant="trunc_worst") == 1.0
    e = {"status": "error", "truncated": False, "tier": None, "followup": False}
    assert R.outcome(e, "refusal_refuse") == 1.0 and R.outcome(e, "refusal_refuse", "exclude") is None


def test_mcq_ops_no_key_longest_and_letter_balanced(tmp_path):
    its = []
    for i in range(80):
        ans = "A" if i < 60 else "B"
        ch = ["A) x", "B) y", "C) z", "D) w"]
        if i % 2 == 0:
            ch["AB".index(ans)] = f"{ans}) a much longer option"
        its.append({"id": f"i{i:03d}", "suite": "q", "scorer": "mcq_letter", "answer": ans, "choices": ch})
    items = {x["id"]: x for x in its}
    a = make_run(tmp_path / "a", "ma", [dict(sc(i, i < 60, "q"), scorer="mcq_letter") for i in range(80)])
    b = make_run(tmp_path / "b", "mb", [dict(sc(i, i >= 60, "q"), scorer="mcq_letter") for i in range(80)])
    ra, rb = [R._row_records(R.item_records(d, items), "q", None) for d in (a, b)]
    assert R.compare([ra], [rb], variant="no_key_longest")["n"] == 40
    lb = R.compare([ra], [rb], variant="letter_balanced", n_boot=500)
    assert lb["diff"] == pytest.approx(0.0)            # A wins all 'A' items, B all 'B' items: balanced = 0
    assert R.compare([ra], [rb])["diff"] == pytest.approx(0.5)


def test_aggregate_uses_items_usable_in_every_run():
    """Inverted agg_probe: no silent per-run denominators."""
    def rec(s, p, st="ok"):
        return {"suite": s, "status": st, "passed": p, "truncated": False, "followup": False, "flags": {},
                "label_source": None}
    summ = [{"label": "A", "suites": {"x": {}, "y": {}}}, {"label": "B", "suites": {"x": {}, "y": {}}}]
    with pytest.raises(ValueError):
        R.macro_aggregate(summ, [{"x1": rec("x", True), "y1": rec("y", True)},
                                 {"x1": rec("x", False), "y2": rec("y", False)}])
    A2 = {"x1": rec("x", True), "y1": rec("y", False, "api_error"), "y3": rec("y", True)}
    B2 = {"x1": rec("x", True), "y1": rec("y", True), "y3": rec("y", False)}
    agg = R.macro_aggregate(summ, [A2, B2], "exclude")
    assert agg["runs"][0]["suites"]["y"][1] == agg["runs"][1]["suites"]["y"][1] == 1


def test_longest_option_ties_share_credit_and_small_strata_hide_gap():
    it = {"id": "t", "scorer": "mcq_letter", "answer": "A", "choices": ["A) long one", "B) long two", "C) x", "D) y"]}
    assert R._longest_credit(it) == pytest.approx(0.5)
    lines = R._render_extras({"strata": [{"lt": "in", "lf": "out", "rows": [
        {"run": "m", "t": 1.0, "nt": 3, "f": 0.5, "nf": 40}]}]})
    assert any("- (n < 5)" in l for l in lines)



def test_decline_kinds_and_suite_status_warning(tmp_path):
    scores = [{"id": f"r{i}", "suite": "frr", "scorer": "refusal", "passed": True, "value": 1.0, "status": "ok",
               "extra": {"refusal_tier": "partial", "decline_kind": "scope" if i % 2 else "policy"}} for i in range(4)]
    d = make_run(tmp_path / "a", "m", scores)
    assert "declines: policy 2, scope 2" in R.render_markdown([R.summarize(d)])
    a, b = _pair(tmp_path, 1, 1, 50, 8, names=("x", "y"))
    ip = tmp_path / "items.jsonl"
    ip.write_text("".join(json.dumps({"id": f"i{i:03d}", "suite": "s", "meta": {"rigor": {
        "suite_status": "at_ceiling_among_sufficient"}}}) + "\n" for i in range(60)))
    plan = write_plan(tmp_path, [{"name": "F", "comparisons": [{"id": "e", "suite": "s", "a": "x", "b": "y",
                                                                 "direction": "equivalent", "margin": 0.1}]}])
    ccsv = tmp_path / "c.csv"
    assert R.main([str(a), str(b), "--items", str(ip), "--comparisons", str(plan), "--conf-csv", str(ccsv),
                   "--out", str(tmp_path / "r.md")]) == 0
    assert "suite at ceiling" in next(__import__("csv").DictReader(ccsv.open()))["verdict"]


# ------------------------------------------------------------------ round 4: inverted loopholes and probes
def _valid_pair(tmp_path, wins_a=12, wins_b=1, ties_pass=40, ties_fail=7, plan_extra=None, flags=True,
                names=("a", "b")):
    """A complete confirmatory pair: data file pinned in run.json and the manifest, analysis + code pinned."""
    a, b = _pair(tmp_path, wins_a, wins_b, ties_pass, ties_fail, names=names)
    n = wins_a + wins_b + ties_pass + ties_fail
    root = tmp_path / "root"
    (root / "data").mkdir(parents=True)
    data = root / "data" / "s.jsonl"
    data.write_text("".join(json.dumps({"id": f"i{i:03d}", "suite": "s", "scorer": "exact",
                                        "meta": {"rigor": {"has_signal": True, "leak_remaining": []}} if flags else {}})
                            + "\n" for i in range(n)))
    plan = write_plan(tmp_path, [{"name": "F", "comparisons": [dict(
        {"id": "c", "suite": "s", "a": names[0], "b": names[1], "direction": "a>b",
         "primary": {"exclude": ["has_signal"]}}, **(plan_extra or {}))]}])
    man = root / "manifest.json"
    man.write_text(json.dumps(_manifest_dict(plan, files={"data/s.jsonl": R.sha256_file(data)})))
    for d in (a, b):
        _protocol_run(d, None, man, datasets=[{"path": "data/s.jsonl", "sha256": R.sha256_file(data)}])
    return a, b, plan, root, data


def _gate(a, b, plan, root, **kw):
    out = tmp = a.parent / "r4.md"
    args = [str(a), str(b), "--comparisons", str(plan), "--data-root", str(root), "--out", str(out)]
    for k, v in kw.items():
        args += [f"--{k.replace('_', '-')}", str(v)]
    assert R.main(args) == 0
    return out.read_text()


def test_r4_valid_pair_is_preregistered(tmp_path):
    a, b, plan, root, _ = _valid_pair(tmp_path)
    md = _gate(a, b, plan, root)
    assert "## Pre-registered comparisons (primary analysis)" in md, md[:3000]


def test_r4_D1_substituted_data_root_file_fails_the_gate(tmp_path):
    a, b, plan, root, data = _valid_pair(tmp_path)
    rows = [json.loads(l) for l in data.read_text().splitlines()]
    rows[12]["meta"]["rigor"]["has_signal"] = False             # 'tune' an exclusion flag after the run
    data.write_text("".join(json.dumps(r) + "\n" for r in rows))
    md = _gate(a, b, plan, root)
    assert "data file s.jsonl differs from run.json datasets sha256" in md and "## Pre-registered" not in md


def test_r4_D1_missing_flags_make_the_comparison_not_evaluable(tmp_path):
    a, b, plan, root, data = _valid_pair(tmp_path, flags=False)
    ccsv = tmp_path / "c.csv"
    assert R.main([str(a), str(b), "--comparisons", str(plan), "--data-root", str(root), "--conf-csv", str(ccsv),
                   "--out", str(tmp_path / "r.md")]) == 0
    row = next(__import__("csv").DictReader(ccsv.open()))
    assert row["verdict"].startswith("not evaluable (exclusion flag not known")


def test_r4_D2_D3_analysis_and_code_are_bound(tmp_path, monkeypatch):
    a, b, plan, root, _ = _valid_pair(tmp_path)
    assert "analysis.seed is 7" in _gate(a, b, plan, root, seed=7)
    assert "analysis.n_boot is 999" in _gate(a, b, plan, root, boot=999)
    monkeypatch.setattr(R, "MIN_N", 10)                          # a post-hoc edit of the minimum n
    assert "analysis.min_n is 10" in _gate(a, b, plan, root)
    monkeypatch.undo()
    man = root / "manifest.json"
    m = json.loads(man.read_text())
    m["code_sha256"]["stats.py"] = "0" * 64
    man.write_text(json.dumps(m))
    for d in (a, b):
        rj = json.loads((d / "run.json").read_text())
        rj["protocol"]["freeze"]["sha256"] = R.sha256_file(man)
        (d / "run.json").write_text(json.dumps(rj))
    assert "the running stats.py differs from the pinned code_sha256" in _gate(a, b, plan, root)


def test_r4_run_validity_conditions(tmp_path):
    a, b, plan, root, _ = _valid_pair(tmp_path)
    rj = json.loads((a / "run.json").read_text())
    for edit, msg in ((lambda r: r["protocol"].update(invalid="sampler mismatch"), "marked invalid"),
                      (lambda r: r.update(ended=None), "did not finish"),
                      (lambda r: r["item_sets"]["s"].update(n=99), "partial run"),
                      (lambda r: r["protocol"].update(stage2=["evals/plans/stage2/nvd-cwe.jsonl"]), "stage2 touches"),
                      (lambda r: r["protocol"].update(outputs_sha256=None), "protocol.outputs_sha256 missing")):
        x = json.loads(json.dumps(rj))
        edit(x)
        (a / "run.json").write_text(json.dumps(x))
        assert msg in _gate(a, b, plan, root), msg
    (a / "run.json").write_text(json.dumps(rj))


def test_r4_stage2_manifest_cannot_repin_the_plan_or_sealed_suites(tmp_path):
    a, b, plan, root, _ = _valid_pair(tmp_path)
    s2 = root / "evals" / "plans" / "stage2"
    s2.mkdir(parents=True)
    bad = s2 / "stage2-prospective.json"
    bad.write_text(json.dumps({"comparisons_sha256": "e" * 64, "suites": {"nvd-cwe": {"ids": ["x"]}}}))
    rj = json.loads((a / "run.json").read_text())
    rj["protocol"]["freezes"] = [{"path": str(bad), "sha256": R.sha256_file(bad)}]
    (a / "run.json").write_text(json.dumps(rj))
    md = _gate(a, b, plan, root)
    assert "carries another comparisons_sha256" in md and "pins non-prospective entries" in md


def test_r4_C2_seed_labels_checked(tmp_path):
    names = [f"x-think-seed{s}" for s in (1234, 1235, 1236)]
    root = tmp_path
    plan = write_plan(tmp_path, [{"name": "F", "comparisons": [{"id": "c", "suite": "s", "a": names, "b": "b",
                                                                 "direction": "a>b"}]}])
    man = tmp_path / "manifest.json"
    man.write_text(json.dumps(_manifest_dict(plan)))
    dirs = []
    for nm, real_seed in zip(names, (17, 4, 11)):
        d = make_run(tmp_path / nm, "coder", [sc(i, 1) for i in range(40)], thinking="on", temperature=0.6,
                     seed=real_seed)
        dirs.append(d)
    bb = make_run(tmp_path / "b", "mb", [sc(i, i % 2) for i in range(40)])
    for d in dirs + [bb]:
        rj = json.loads((d / "run.json").read_text())
        _protocol_run(d, None, man, sampling=rj["sampling"], thinking=rj["thinking"])
    out = tmp_path / "r.md"
    assert R.main([str(x) for x in dirs + [bb]] + ["--comparisons", str(plan), "--data-root", str(root),
                                                    "--out", str(out)]) == 0
    md = out.read_text()
    assert "the dir name says seed 1234 but sampling.seed is 17" in md and "are not the declared --seeds" in md


def test_r4_gate_is_per_family(tmp_path):
    a, b, plan, root, _ = _valid_pair(tmp_path)
    p = json.loads(plan.read_text())
    p["families"].append({"name": "G", "comparisons": [{"id": "g", "suite": "s", "a": "a", "b": "missing-run",
                                                         "direction": "a>b"}]})
    plan.write_text(json.dumps(p))
    man = root / "manifest.json"
    m = json.loads(man.read_text())
    m["comparisons_sha256"] = R.sha256_file(plan)
    man.write_text(json.dumps(m))
    for d in (a, b):
        rj = json.loads((d / "run.json").read_text())
        rj["protocol"]["freeze"]["sha256"] = R.sha256_file(man)
        (d / "run.json").write_text(json.dumps(rj))
    md = _gate(a, b, plan, root)
    assert "## Pre-specified comparisons: 1 of 2 families pre-registered" in md
    assert "**G: post-hoc**" in md and "| **F** — pre-registered" in md


def _verdict_setup(tmp_path, verdict_over=None, pin=True):
    """Two judge-graded runs plus a pinned stage-2 calibration verdict that licenses their grader."""
    mk = lambda i, ok, t: {"id": f"q{i:03d}", "suite": "sevenllm-qa", "scorer": "llm_judge", "passed": ok,  # noqa
                           "value": float(ok), "status": "ok", "model_graded": True}
    a = make_run(tmp_path / "qa-a", "coder", [mk(i, i % 5 != 0, "x") for i in range(60)])
    b = make_run(tmp_path / "qa-b", "big", [mk(i, i % 3 != 0, "x") for i in range(60)])
    for d, typ in ((a, "extract"), (b, "extract")):
        resp = [{"id": f"q{i:03d}", "suite": "sevenllm-qa", "type": "extract" if i < 40 else "freeform"}
                for i in range(60)]
        (d / "responses.jsonl").write_text("".join(json.dumps(r) + "\n" for r in resp))
    plan = write_plan(tmp_path, [{"name": "D1", "comparisons": [{"id": "d1", "suite": "sevenllm-qa", "a": "qa-a",
                                                                  "b": "qa-b", "direction": "a>b"}]}])
    root = tmp_path
    grader = {"model": "indep-judge", "prompt_sha256": "p" * 64,
              "served_identity": {"indep-judge": {"model_sha256": "w" * 64}}}
    v = {"schema": "judge-calibration-verdict-v1", "verdict": "pass", "d1_scope": "all", "attempt": 1,
         "calibration_key_sha256": "k" * 64, "judge_models": ["indep-judge"],
         "judge_served_identity": {"indep-judge": {"model_sha256": "w" * 64}}, "judge_prompt_sha256": "p" * 64,
         "grades_sha256": {"calib-run": "g" * 64}, "grades_sha256_combined": "c" * 64,
         "kappa_results": {"gates": {"pooled": {"status": "pass"}}}}
    v.update(verdict_over or {})
    vf = tmp_path / "verdict.json"
    vf.write_text(json.dumps(v))
    s2 = tmp_path / "evals" / "plans" / "stage2"
    s2.mkdir(parents=True)
    cal = s2 / "stage2-calibration.json"
    cal.write_text(json.dumps({"calibration_verdict": {"path": str(vf), "sha256": R.sha256_file(vf)}} if pin else {}))
    man = tmp_path / "manifest.json"
    man.write_text(json.dumps(_manifest_dict(plan)))
    for d in (a, b):
        _protocol_run(d, None, man, grader=grader)
        rj = json.loads((d / "run.json").read_text())
        rj["protocol"]["freezes"] = [{"path": str(cal), "sha256": R.sha256_file(cal)}]
        (d / "run.json").write_text(json.dumps(rj))
    return a, b, plan, root


def _judge_reasons(tmp_path, **kw):
    a, b, plan, root = _verdict_setup(tmp_path, **kw)
    out = tmp_path / "r.md"
    ccsv = tmp_path / "c.csv"
    assert R.main([str(a), str(b), "--comparisons", str(plan), "--data-root", str(root), "--out", str(out),
                   "--conf-csv", str(ccsv)]) == 0
    return out.read_text(), next(__import__("csv").DictReader(ccsv.open()))


def test_r4_calibration_verdict_binding(tmp_path, monkeypatch):
    import grading
    monkeypatch.setattr(grading, "family_overlap", lambda *a_, **k: [])
    md, _ = _judge_reasons(tmp_path / "ok")
    assert "calibration" not in md.split("## Pre-")[1].split("|")[0] or "## Pre-registered" in md
    for over, msg in (({"verdict": " PASS "}, "not exactly 'pass'"), ({"schema": "v0"}, "schema"),
                      ({"judge_models": ["other"]}, "not the grader"),
                      ({"judge_prompt_sha256": "q" * 64}, "judge prompt sha256"),
                      ({"judge_served_identity": {"indep-judge": {"model_sha256": "z" * 64}}}, "served identity"),
                      ({"kappa_results": None}, "kappa_results")):
        md, _ = _judge_reasons(tmp_path / msg.replace(" ", "_").replace("'", ""), verdict_over=over)
        assert msg in md, msg
    md, _ = _judge_reasons(tmp_path / "unpinned", pin=False)
    assert "no calibration verdict pinned" in md


def test_r4_calibration_extract_only_scope(tmp_path, monkeypatch):
    import grading
    monkeypatch.setattr(grading, "family_overlap", lambda *a_, **k: [])
    md, row = _judge_reasons(tmp_path, verdict_over={"d1_scope": "extract_only"})
    assert row["n"] == "40" and "extract-type items only" in row["primary"]


def test_r4_mae_directional_decided_by_sign_flip_and_multiseed_tost_exact(tmp_path):
    mk = lambda i, v: {"id": f"i{i}", "suite": "v", "scorer": "cvss_mae", "value": v, "passed": v <= 1,  # noqa
                       "status": "ok"}
    a = make_run(tmp_path / "a", "ma", [mk(i, 0.5) for i in range(40)])
    b = make_run(tmp_path / "b", "mb", [mk(i, 1.5 if i % 3 else 0.5) for i in range(40)])
    ra, rb = [R._row_records(R.item_records(d), "v", None) for d in (a, b)]
    r = R.compare([ra], [rb], "mae", n_boot=500, n_perm=2000)
    assert r["p"] == r["p_perm"] and not r["p_is_boot"] and "sign-flip" in r["method"]
    # seed groups on a proportion: equivalence = every seed pairing's exact Tango TOST (p = max)
    ids = range(200)
    seeds = [make_run(tmp_path / f"s{k}", "m", [sc(i, (i + k) % 7 != 0) for i in ids], thinking="on",
                      temperature=0.6, seed=k) for k in (1, 2, 3)]
    base = make_run(tmp_path / "base", "m", [sc(i, i % 7 != 0) for i in ids])
    rs = [R._row_records(R.item_records(d), "s", None) for d in seeds]
    r0 = R._row_records(R.item_records(base), "s", None)
    g = R.compare(rs, [r0], "accuracy", margin=0.1, n_boot=300, n_perm=300)
    assert len(g["p_tost_seed_pairs"]) == 3 and g["p_tost"] == max(g["p_tost_seed_pairs"])


def test_r4_cluster_ops_and_name_stratum_and_onehop(tmp_path):
    n = 60
    items = [{"id": f"i{i:03d}", "suite": "s", "meta": {"labels": {"cna": f"cna{i % 3}"},
                                                         "rigor": {"template_cluster": f"t{i // 6}",
                                                                   "cwe_name_in_text": i % 2 == 0,
                                                                   "cwe_mapping_usage": "Allowed" if i % 4 else
                                                                   "Discouraged", "leak_remaining": []}}}
             for i in range(n)]
    ip = tmp_path / "items.jsonl"
    ip.write_text("".join(json.dumps(x) + "\n" for x in items))
    a, b = _pair(tmp_path, 14, 2, 30, 14)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"alpha": 0.05, "families": [{"name": "F", "comparisons": [
        {"id": "c", "suite": "s", "a": "a", "b": "b", "direction": "a>b",
         "primary": {"exclude": ["no_name_in_text"]},
         "sensitivity": ["cluster_bootstrap", "cna_cluster", "name_in_text"]}]}]}))
    out, ccsv = tmp_path / "r.md", tmp_path / "c.csv"
    assert R.main([str(a), str(b), "--items", str(ip), "--comparisons", str(plan), "--out", str(out),
                   "--conf-csv", str(ccsv)]) == 0
    md = out.read_text()
    assert "cluster bootstrap (template clusters)" in md and "cluster bootstrap (CNA clusters)" in md
    # r4 F1: the name-in-text stratum is not empty although the primary drops name-in-text items
    line = next(l for l in md.splitlines() if "| c | CWE-name-in-text only |" in l)
    assert "n/a (n = 0)" not in line
    assert "Stratum by MITRE mapping status of the gold CWE" in md
    rec = {"status": "ok", "passed": False, "related": True, "followup": False, "truncated": False}
    assert R.outcome(rec, "accuracy_onehop") == 1.0 and R.outcome(rec, "accuracy") == 0.0


def test_r4_floor_share_and_small_bootstrap_p(tmp_path):
    a, b = _pair(tmp_path, 3, 3, 20, 30)
    plan = write_plan(tmp_path, [{"name": "E", "comparisons": [{"id": "e", "suite": "s", "a": "a", "b": "b",
                                                                 "direction": "equivalent", "margin": 0.2}]}])
    out = tmp_path / "r.md"
    assert R.main([str(a), str(b), "--comparisons", str(plan), "--out", str(out)]) == 0
    assert "floor 54% of 56 items fail in all 2 runs" in out.read_text()
    assert R._p(0.0, 4000) == "< 1/4000"


def test_r4_text_baseline_tool_and_report(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("tb", str(R.EVALS_DIR / "tools" / "text_baseline.py"))
    TB = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(TB)
    words = {"CWE-79": "script injection browser page", "CWE-89": "sql query database injection"}
    its = []
    for i in range(80):
        g = "CWE-79" if i % 2 else "CWE-89"
        its.append({"id": f"w{i:03d}", "suite": "w", "scorer": "cwe_match", "answer": g,
                    "prompt": f"CVE description: the {words[g]} flaw {i}",
                    "meta": {"labels": {"cna": "c"}, "rigor": {"split": "sealed" if i >= 60 else "dev",
                                                              "template_cluster": f"t{i}"}}})
    preds = {p["id"]: p for p in TB.predict_suite(its)}
    assert preds["w000"]["fit"].startswith("cross-fit") and preds["w070"]["fit"] == "full"
    assert all(preds[f"w{i:03d}"]["pred"] == its[i]["answer"] for i in range(80))
    assert TB.predict_suite(its) == TB.predict_suite(list(reversed(its)))        # deterministic
    pf = tmp_path / "nb.jsonl"
    pf.write_text("".join(json.dumps(p) + "\n" for p in preds.values()))
    ip = tmp_path / "w.jsonl"
    ip.write_text("".join(json.dumps(x) + "\n" for x in its))
    d = make_run(tmp_path / "m", "m", [{"id": f"w{i:03d}", "suite": "w", "scorer": "cwe_match", "passed": i % 4 != 0,
                                        "value": 1.0, "status": "ok"} for i in range(80)])
    out = tmp_path / "r.md"
    assert R.main([str(d), "--items", str(ip), "--text-baseline", str(pf), "--out", str(out)]) == 0
    md = out.read_text()
    assert "_baseline: dev-trained text classifier (NB)_ | 80 | - | 100.0%" in md
    assert "| m (think off) | 80 | 75.0% | 100.0% |" in md and "| no |" in md


def test_r4_F10_legacy_op_applies_to_either_metrics():
    rec = {"status": "ok", "scorer": "cwe_match", "parsed": "CWE-89", "passed": True, "gold": "CWE-89",
           "followup": False, "truncated": False, "labels": {"cwe_cna": ["CWE-89"]},
           "legacy": {"passed": False, "value": 0.0, "status": "ok"}}
    assert R.outcome(rec, "accuracy_either") == 1.0
    assert R.outcome(rec, "accuracy_either", variant="legacy") == 0.0


def test_r4_attempt_ledger(tmp_path):
    """Inverted r4 loophole C1 (attempt shopping): only the first complete attempt of a run id counts."""
    a, b, plan, root, _ = _valid_pair(tmp_path)
    led = root / "evals" / "plans" / "attempts-C1.jsonl"
    lines = R.read_jsonl(led)
    early = [{"event": "start", "run_id": "a", "started": "2026-10-10T00:00:00Z"},
             {"event": "end", "run_id": "a", "started": "2026-10-10T00:00:00Z", "outputs_sha256": {}}]
    led.write_text("".join(json.dumps(x) + "\n" for x in early + lines))
    md = _gate(a, b, plan, root)
    assert "not the first complete attempt in the ledger (attempt shopping) (1 affected: a)" in md
    interrupted = [{"event": "start", "run_id": "a", "started": "2026-10-10T00:00:00Z"}]
    led.write_text("".join(json.dumps(x) + "\n" for x in interrupted + lines))
    md = _gate(a, b, plan, root)
    assert "## Pre-registered (with deviations: attempts)" in md and "1 other attempt(s)" in md
    led.unlink()
    assert "no attempt ledger" in _gate(a, b, plan, root)


def test_r4_interventions_frozen_sha_and_plan_analysis(tmp_path):
    a, b, plan, root, _ = _valid_pair(tmp_path, plan_extra={"interventions": {"a": ["D6-prompt"], "b": []}})
    md = _gate(a, b, plan, root)
    assert "a ran interventions [], the plan declares ['D6-prompt'] for side a" in md
    rj = json.loads((a / "run.json").read_text())
    rj["protocol"]["interventions"] = [{"name": "D6-prompt", "kind": "system_prompt"}]
    rj["protocol"]["frozen_sha"] = "1" * 40
    (a / "run.json").write_text(json.dumps(rj))
    md = _gate(a, b, plan, root)
    assert "ran interventions" not in md and "frozen ref missing or not equal" in md
    p = json.loads(plan.read_text())
    p["analysis"] = {"min_n": 30, "n_boot": 999, "seed": ST.DEFAULT_SEED, "alpha": 0.05}
    plan.write_text(json.dumps(p))
    assert "analysis.n_boot is 4000 here but 999 in the plan" in _gate(a, b, plan, root)


def test_r4_onehop_sensitivity_metric(tmp_path):
    rows_a = [dict(sc(i, i < 30), scorer="cwe_match", extra={"cwe_related_match": 30 <= i < 45}) for i in range(60)]
    rows_b = [dict(sc(i, i < 35), scorer="cwe_match") for i in range(60)]
    a = make_run(tmp_path / "a", "ma", rows_a)
    b = make_run(tmp_path / "b", "mb", rows_b)
    plan = write_plan(tmp_path, [{"name": "F", "comparisons": [{"id": "c", "suite": "s", "a": "a", "b": "b",
                                                                 "direction": "a>b",
                                                                 "sensitivity_metrics": ["accuracy_onehop"]}]}])
    out, ccsv = tmp_path / "r.md", tmp_path / "c.csv"
    assert R.main([str(a), str(b), "--comparisons", str(plan), "--out", str(out), "--conf-csv", str(ccsv)]) == 0
    md = out.read_text()
    assert "metric accuracy_onehop°" in md and "+16.7 pp" in md


def test_r4_D3_gate_takes_the_resampling_settings(tmp_path):
    import inspect
    assert "seed" in inspect.signature(R.confirmatory_gate).parameters
    a, b, plan, root, _ = _valid_pair(tmp_path)
    runs = _gate_runs((a, b))
    assert any("analysis.seed" in x for x in R.confirmatory_gate(runs, plan, root, seed=3))
