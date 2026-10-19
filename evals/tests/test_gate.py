"""Tests for evals/gate.py (the upgrade gate). Synthetic run dirs and the fake OpenAI-compatible server from
test_run.py; no model, gateway or GPU calls."""
import json
from pathlib import Path

import pytest

import gate as G
import report as R
import run as RUN
import scorers as S
from test_run import FakeGateway, FAKE_KEY  # noqa: F401 - the shared fake server



# ------------------------------------------------------------------------------------------- fixtures
def write_items(path: Path, suite: str, n: int, kind: str = "mcq", start: int = 1):
    rows = []
    for i in range(start, start + n):
        rid = f"{suite}-{i:04d}"
        if kind == "mcq":
            rows.append({"id": rid, "suite": suite, "type": "mcq", "prompt": f"q{i}", "choices": ["A) x", "B) y"],
                         "answer": "A", "scorer": "mcq_letter"})
        elif kind == "cvss":
            rows.append({"id": rid, "suite": suite, "type": "extract", "prompt": f"cvss {i}", "answer": 9.8,
                         "scorer": "cvss_mae"})
        elif kind == "refusal":
            rows.append({"id": rid, "suite": suite, "type": "freeform", "prompt": f"r{i}", "answer": "comply",
                         "scorer": "refusal"})
        elif kind == "judge":
            rows.append({"id": rid, "suite": suite, "type": "freeform", "prompt": f"j{i}", "answer": "x",
                         "scorer": "llm_judge"})
    with path.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return [r["id"] for r in rows]


def suite_entry(path: Path, suite: str, name: str = None, ids=None, **kw):
    src = {"suite": suite, "data": str(path), "sha256": G.sha256_file(path)}
    if ids is not None:
        src["ids"] = ids
    e = {"name": name or suite, "metric": "accuracy", "sources": [src]}
    e.update(kw)
    return e


def write_manifest(tmp_path: Path, suites, name="m.json", **top):
    m = {"schema": G.MANIFEST_SCHEMA, "name": "test-gate", "model": "coder", "alpha": 0.05, "default_margin": 0.05,
         "scorer_version": S.SCORER_VERSION,
         "sampling": {"thinking": "off", "profile": "off", "seed": 1234, "final_answer": False,
                      "prompt_cache": False, "concurrency": 1, "budget_policy_sha256": None},
         "validity": {"max_api_error_rate": 0.05}, "suites": suites}
    m.update(top)
    m["sampling"]["budget_policy_sha256"] = G.budget_sha(G.budget_policy(m))
    p = tmp_path / name
    p.write_text(json.dumps(m))
    return p


@pytest.fixture
def mcq(tmp_path):
    """A 100-item MCQ suite and its manifest."""
    data = tmp_path / "items.jsonl"
    ids = write_items(data, "t-mcq", 100)
    return {"data": data, "ids": ids, "manifest": write_manifest(tmp_path, [suite_entry(data, "t-mcq")])}


def run_json(model, ids_by_suite, **over):
    rj = {"model": model, "thinking": "off",
          "sampling": {**RUN.SAMPLING_PROFILES["off"], "seed": 1234, "profile": "off", "overrides": {}},
          "max_tokens": {t: RUN.max_tokens_for({"type": t}, type("A", (), {"max_tokens": None,
                                                                         "budget_policy": None})(), "off")
                         for t in RUN.TYPES},
          "budget": {"policy_sha256": G.budget_sha(G.budget_policy({"name": "test-gate", "suites": []})),
                     "flat_override": None}, "followup": {"enabled": False},
          "prompt_cache": {"sent": {"cache_prompt": False}}, "concurrency": 1,
          "scorer_version": {"value": S.SCORER_VERSION, "source": "scorers.SCORER_VERSION"},
          "harness_sha256": {"run.py": "a" * 64, "scorers.py": "b" * 64, "report.py": "c" * 64},
          "git": {"sha": "0" * 40},
          "item_sets": {s: {"n": len(v), "ids_sha256": RUN.sha256_text("\n".join(sorted(v)))}
                        for s, v in ids_by_suite.items()},
          "prompts": {"system_prompt_sha256": {s: "d" * 64 for s in ids_by_suite}},
          "serving": {"fields": {"model_file": {"value": "M-Q4_K_M.gguf", "source": "probe /props"},
                                 "quant": {"value": "Q4_K_M", "source": "x"},
                                 "llama_cpp_build": {"value": "b6500", "source": "probe /props"}},
                      "static": None, "sampler_check": {"ok": True}},
          "served": {"models": [model], "system_fingerprints": ["b6500"]},
          "protocol": {"mode": "exploratory", "stop_rules": True}}
    for k, v in over.items():
        rj[k] = v
    return rj


def make_run(d: Path, rows, model="coder", **over):
    """rows: list of dicts (id, suite, scorer, status, passed, value, truncated, tier, expected, predicted,
    api_error). Writes run.json, responses.jsonl and scores.jsonl like run.py."""
    d.mkdir(parents=True, exist_ok=True)
    ids_by_suite = {}
    for r in rows:
        ids_by_suite.setdefault(r["suite"], []).append(r["id"])
    (d / "run.json").write_text(json.dumps(run_json(model, ids_by_suite, **over)))
    with (d / "responses.jsonl").open("w") as fr, (d / "scores.jsonl").open("w") as fs:
        for r in rows:
            resp = {"id": r["id"], "suite": r["suite"], "scorer": r.get("scorer", "mcq_letter"),
                    "finish_reason": "length" if r.get("truncated") else "stop", "latency_s": 2.0}
            if r.get("api_error"):
                resp["error"] = "HTTP 500"
                fr.write(json.dumps(resp) + "\n")
                continue
            fr.write(json.dumps(resp) + "\n")
            extra = {}
            for k in ("expected", "predicted"):
                if k in r:
                    extra[k] = r[k]
            if "tier" in r:
                extra["refusal_tier"] = extra["refusal_tier_prefix"] = r["tier"]
            fs.write(json.dumps({"id": r["id"], "suite": r["suite"], "scorer": r.get("scorer", "mcq_letter"),
                                 "status": r.get("status", "ok"), "passed": bool(r.get("passed")),
                                 "value": r.get("value", 1.0 if r.get("passed") else 0.0), "extra": extra}) + "\n")
    return d


def mcq_rows(ids, fail=(), unparsed=(), suite="t-mcq", **kw):
    return [{"id": i, "suite": suite, "passed": i not in fail and i not in unparsed,
             "status": "unparsed" if i in unparsed else "ok", **kw} for i in ids]


def compare(m_path, a, b, *extra):
    return G.main(["compare", str(a), str(b), "--manifest", str(m_path), *extra])


def analyse(m_path, a, b, **kw):
    return G.analyse(G.load_manifest(m_path), a, b, **kw)


# ------------------------------------------------------------------------------------------- manifest
ITEMS = json.loads((G.PLANS_DIR / "gate-v1.items.json").read_text())


@pytest.mark.parametrize("model, cwe_margin", [("coder", 0.075), ("coder-fast", 0.05)])
def test_real_manifests(model, cwe_margin):
    """The per-model manifests: structure, margins, caps, the format pool, unsealed ids that are a prefix of the
    committed selection (gate_select.py), and the proposal's n."""
    m = G.load_manifest(G.manifest_for(model), check_data=False)
    assert m["model"] == model and m["alpha"] == 0.05 and m["scorer_version"] == S.SCORER_VERSION
    sp = m["sampling"]
    assert (sp["thinking"], sp["profile"], sp["prompt_cache"], sp["concurrency"]) == ("off", "off", False, 1)
    by = {e["name"]: e for e in m["suites"]}
    assert [by[k]["_margin"] for k in ("mcq-knowledge", "cwe", "cvss", "refusal")] == [0.05, cwe_margin, 0.30, 0.10]
    assert by["cvss"]["metric"] == "mae" and by["cwe"]["metric"] == "accuracy_either"
    assert by["refusal"]["metric"] == "refusal_full" and by["refusal"]["max_tokens"] == 512
    assert G.suite_caps(m["suites"]) == {"cse-frr": 512}
    assert m["format"]["pool"] == ["mcq-knowledge", "cwe", "cvss"] and m["format"]["margin"] == 0.02
    n = ITEMS["n_by_model"][model]
    sealed = G.sealed_ids()
    for e in m["suites"]:
        for s in e["sources"]:
            full = ITEMS["suites"][e["name"]]["sources"][s["suite"]]["ids"]
            assert s["ids"] == full[:n[s["suite"]]] and s["data"] == f"evals/data/gate-v1/{s['suite']}.jsonl"
            assert not set(s["ids"]) & sealed
    assert len(G.all_keys(m)) == (980 if model == "coder" else 1440)
    assert m["selection"]["items_sha256"] == G.sha256_file(G.PLANS_DIR / "gate-v1.items.json")
    assert ITEMS["checks"]["ok"] and ITEMS["checks"]["selected_total"] == 1440


@pytest.mark.skipif(not (G.REPO_DIR / "evals/data/gate-v1/nvd-cwe.jsonl").is_file(),
                    reason="the gate's data files are not built (evals/tools/gate_select.py --write-data)")
@pytest.mark.parametrize("model", ["coder", "coder-fast"])
def test_real_manifests_with_data(model):
    m = G.load_manifest(G.manifest_for(model))
    assert len(G.manifest_items(m)) == len(G.all_keys(m))
    secs, how = G.estimate_seconds(m)
    assert how.startswith("the manifest's measured") and secs / 3600 < (2.0 if model == "coder" else 1.3)


def test_unknown_model_has_no_manifest(capsys):
    assert G.main(["baseline", "--model", "vision"]) == 4
    assert "no gate manifest for model 'vision'" in capsys.readouterr().err


def test_manifest_model_must_match(tmp_path, capsys):
    data = tmp_path / "d.jsonl"
    write_items(data, "t-mcq", 5)
    p = write_manifest(tmp_path, [suite_entry(data, "t-mcq")])
    assert G.main(["baseline", "--model", "coder-fast", "--manifest", str(p), "--yes", "--no-guard"]) == 4
    assert "is for 'coder', not 'coder-fast'" in capsys.readouterr().err


def test_manifest_rejects_sealed_ids(tmp_path):
    sealed_id = sorted(G.sealed_ids())[0]
    data = tmp_path / "d.jsonl"
    data.write_text(json.dumps({"id": sealed_id, "suite": "t-mcq", "type": "mcq", "prompt": "q",
                                "choices": ["A) x", "B) y"], "answer": "A", "scorer": "mcq_letter"}) + "\n")
    write_items(data, "t-mcq", 3, start=2)
    p = write_manifest(tmp_path, [suite_entry(data, "t-mcq")])
    with pytest.raises(G.GateError) as ei:
        G.load_manifest(p)
    assert "SEALED" in str(ei.value) and sealed_id in str(ei.value)
    # by explicit id too
    p = write_manifest(tmp_path, [suite_entry(data, "t-mcq", ids=[sealed_id])], name="m2.json")
    with pytest.raises(G.GateError, match="SEALED"):
        G.load_manifest(p)


@pytest.mark.parametrize("mutate, msg", [
    (lambda m, e: m.update(extra=1), "unknown top-level key"),
    (lambda m, e: m.update(schema="v0"), "schema"),
    (lambda m, e: m["sampling"].update(thinking="on"), "thinking"),
    (lambda m, e: m["sampling"].update(profile="thinking"), "profile"),
    (lambda m, e: m["sampling"].update(final_answer=True), "final_answer"),
    (lambda m, e: m["sampling"].update(budget_policy_sha256="0" * 64), "does not match the policy"),
    (lambda m, e: m["sampling"].update(temperature=0.7), "unknown key"),
    (lambda m, e: m.update(alpha=0.7), "alpha"),
    (lambda m, e: e["sources"][0].update(sha256="0" * 64), "sha256 is"),
    (lambda m, e: e.pop("sources"), "sources"),
    (lambda m, e: e.update(min_parse_rate=2), "min_parse_rate"),
    (lambda m, e: e.update(max_tokens=0), "max_tokens"),
    (lambda m, e: m["sampling"].update(prompt_cache=True), "prompt_cache"),
    (lambda m, e: m["sampling"].update(concurrency=2), "concurrency"),
    (lambda m, e: m.pop("model"), "model"),
    (lambda m, e: m.update(format={"pool": ["nope"], "margin": 0.02}), "format.pool"),
    (lambda m, e: m.update(format={"pool": ["t-mcq"], "margin": 2}), "format.margin"),
    (lambda m, e: e.update(margin=5), "margin"),
    (lambda m, e: e.update(metric="mae", margin=0.5), "needs ['cvss_mae'] items"),
    (lambda m, e: e.update(metric="refusal_full"), "better"),
    (lambda m, e: e.update(better="lower"), "better must be 'higher'"),
    (lambda m, e: e.update(impute="none"), "MAE metrics only"),
    (lambda m, e: e["sources"][0].update(ids=["t-mcq-9999"]), "not in suite"),
    (lambda m, e: e["sources"][0].update(ids=["t-mcq-0001", "t-mcq-0001"]), "duplicate ids"),
    (lambda m, e: e["sources"][0].update(color=1), "unknown key"),
    (lambda m, e: e.update(exclude=["legacy"]), "exclude"),
    (lambda m, e: e.update(color="red"), "unknown key"),
    (lambda m, e: m.update(suites=[]), "non-empty list"),
])
def test_manifest_strict_validation(tmp_path, mutate, msg):
    data = tmp_path / "d.jsonl"
    write_items(data, "t-mcq", 5)
    p = write_manifest(tmp_path, [suite_entry(data, "t-mcq")])
    m = json.loads(p.read_text())
    mutate(m, m["suites"][0] if m["suites"] else {})
    p.write_text(json.dumps(m))
    with pytest.raises(G.GateError) as ei:
        G.load_manifest(p)
    assert msg in str(ei.value)


def test_manifest_suite_shapes(tmp_path):
    """The schema holds per-suite margins, MAE margins, refusal tiers, exclusions and pinned whole files."""
    data = tmp_path / "d.jsonl"
    write_items(data, "t-mcq", 5)
    write_items(data, "t-cvss", 4, "cvss")
    write_items(data, "t-ref", 4, "refusal")
    p = write_manifest(tmp_path, [
        suite_entry(data, "t-mcq", margin=0.1),
        suite_entry(data, "t-cvss", metric="mae", margin=0.3, impute="max"),
        suite_entry(data, "t-ref", name="ref-full", metric="refusal_full", better="higher", ids=["t-ref-0001",
                                                                                                    "t-ref-0002"]),
        suite_entry(data, "t-ref", name="ref-refuse", metric="refusal_refuse", better="lower",
                    ids=["t-ref-0003", "t-ref-0004"], exclude=["no_frr_exclude"]),
    ])
    m = G.load_manifest(p)
    assert [len(e["_keys"]) for e in m["suites"]] == [5, 4, 2, 2]
    assert [e["_margin"] for e in m["suites"]] == [0.1, 0.3, 0.05, 0.05]
    assert [e["_better"] for e in m["suites"]] == ["higher", "lower", "higher", "lower"]
    sel = G.selection(m)["suites"]
    assert sel["t-ref"]["ids"] == ["t-ref-0001", "t-ref-0002", "t-ref-0003", "t-ref-0004"]


def test_manifest_rejects_judge_items_and_overlap(tmp_path):
    data = tmp_path / "d.jsonl"
    write_items(data, "t-j", 3, "judge")
    write_items(data, "t-mcq", 4)
    p = write_manifest(tmp_path, [suite_entry(data, "t-j")])
    with pytest.raises(G.GateError, match="judge model"):
        G.load_manifest(p)
    p = write_manifest(tmp_path, [suite_entry(data, "t-mcq", name="a"),
                                  suite_entry(data, "t-mcq", name="b", ids=["t-mcq-0001"])], name="m2.json")
    with pytest.raises(G.GateError, match="also in a"):
        G.load_manifest(p)


# ------------------------------------------------------------------------------------------- verdicts
def test_identical_runs_pass_at_large_n(tmp_path, mcq):
    ids = mcq["ids"]
    rows = mcq_rows(ids, fail=set(ids[:30]))
    a, b = make_run(tmp_path / "a", rows), make_run(tmp_path / "b", rows)
    res = analyse(mcq["manifest"], a, b)
    assert res["verdict"] == "PASS" and res["exit"] == 0
    r = res["rows"][0]
    assert r["verdict"] == "equivalent" and r["r"]["n"] == 100 and r["r"]["p_tost"] < 0.05
    assert r["r"]["p"] == 1.0 and r["r"]["diff"] == 0
    assert compare(mcq["manifest"], a, b) == 0


def test_identical_runs_inconclusive_at_small_n(tmp_path):
    data = tmp_path / "d.jsonl"
    ids = write_items(data, "t-mcq", 10)
    p = write_manifest(tmp_path, [suite_entry(data, "t-mcq")])
    rows = mcq_rows(ids)
    a, b = make_run(tmp_path / "a", rows), make_run(tmp_path / "b", rows)
    res = analyse(p, a, b)
    assert res["rows"][0]["verdict"] == "inconclusive" and res["verdict"] == "INCONCLUSIVE"
    assert compare(p, a, b) == 2


def test_zero_discordance_tost_needs_enough_items():
    """With identical outcomes the exact TOST passes once n is large enough (about 60 at a 0.05 margin)."""
    import stats as ST
    assert ST.tost_paired_exact(30, 0, 0, 30, 0.05)["p"] < 0.05
    assert ST.tost_paired_exact(20, 0, 0, 20, 0.05)["p"] > 0.05


def test_worse_candidate_fails(tmp_path, mcq, capsys):
    ids = mcq["ids"]
    base = make_run(tmp_path / "base", mcq_rows(ids))
    cand = make_run(tmp_path / "cand", mcq_rows(ids, fail=set(ids[:30])))
    out = tmp_path / "r.md"
    assert compare(mcq["manifest"], base, cand, "--out", str(out)) == 1
    txt = capsys.readouterr().out
    assert "**Overall: FAIL** (exit 1)" in txt and "different-worse" in txt and "-30.0 pp" in txt
    assert out.read_text() == txt.rstrip("\n") + "\n"


def test_better_candidate_passes(tmp_path, mcq):
    ids = mcq["ids"]
    base = make_run(tmp_path / "base", mcq_rows(ids, fail=set(ids[:30])))
    cand = make_run(tmp_path / "cand", mcq_rows(ids))
    res = analyse(mcq["manifest"], base, cand)
    assert res["rows"][0]["verdict"] == "different-better" and res["verdict"] == "PASS" and res["exit"] == 0


def test_small_significant_shift_inside_margin_is_equivalent(tmp_path):
    """Equivalence first: a significant but tolerated shift (inside the margin) does not fail the gate."""
    data = tmp_path / "d.jsonl"
    ids = write_items(data, "t-mcq", 1000)
    p = write_manifest(tmp_path, [suite_entry(data, "t-mcq")])
    base = make_run(tmp_path / "base", mcq_rows(ids))
    cand = make_run(tmp_path / "cand", mcq_rows(ids, fail=set(ids[:15])))
    res = analyse(p, base, cand)
    r = res["rows"][0]
    assert r["r"]["p"] < 0.05 and r["r"]["p_tost"] < 0.05
    assert r["verdict"] == "equivalent" and res["exit"] == 0


def test_holm_across_suites_and_direction(tmp_path):
    """b_only=10, a_only=2: McNemar p = 0.039. Alone it fails the gate; with a second suite Holm doubles it."""
    data = tmp_path / "d.jsonl"
    ids = write_items(data, "t-mcq", 100)
    ids2 = write_items(data, "t-two", 100)
    one = write_manifest(tmp_path, [suite_entry(data, "t-mcq")], name="one.json")
    two = write_manifest(tmp_path, [suite_entry(data, "t-mcq"), suite_entry(data, "t-two")], name="two.json")
    base_rows = mcq_rows(ids, fail=set(ids[10:12])) + mcq_rows(ids2, suite="t-two")
    cand_rows = mcq_rows(ids, fail=set(ids[:10])) + mcq_rows(ids2, suite="t-two")
    base = make_run(tmp_path / "base", base_rows)
    cand = make_run(tmp_path / "cand", cand_rows)
    r1 = analyse(one, base, cand)
    assert abs(r1["rows"][0]["r"]["p"] - 0.0386) < 1e-3
    assert r1["rows"][0]["verdict"] == "different-worse" and r1["exit"] == 1
    r2 = analyse(two, base, cand)
    assert abs(r2["rows"][0]["p_holm"] - 2 * r1["rows"][0]["r"]["p"]) < 1e-9
    assert [x["verdict"] for x in r2["rows"]] == ["inconclusive", "equivalent"] and r2["exit"] == 2
    # the same discordance the other way round is "different-better" (the sign gives the direction)
    r3 = analyse(one, cand, base)
    assert r3["rows"][0]["verdict"] == "different-better" and r3["exit"] == 0


def test_format_regression_fails_whatever_the_accuracy(tmp_path, mcq):
    ids = mcq["ids"]
    m = json.loads(mcq["manifest"].read_text())
    m["suites"][0]["parse_margin"] = 0.05
    mcq["manifest"].write_text(json.dumps(m))
    # 30 wrong-but-parsed answers become unparsed: accuracy identical, parse rate 100% -> 70%
    base = make_run(tmp_path / "base", mcq_rows(ids, fail=set(ids[:30])))
    cand = make_run(tmp_path / "cand", mcq_rows(ids, unparsed=set(ids[:30])))
    res = analyse(mcq["manifest"], base, cand)
    r = res["rows"][0]
    assert r["r"]["diff"] == 0 and r["verdict"] == "different-worse (format)" and res["exit"] == 1
    assert "below the baseline" in " ".join(r["format"]) and "below the minimum" not in " ".join(r["format"])
    # a 3-point drop is inside parse_margin (0.05) but below a 0.99 minimum
    m = json.loads(mcq["manifest"].read_text())
    m["suites"][0]["min_parse_rate"] = 0.99
    mcq["manifest"].write_text(json.dumps(m))
    cand2 = make_run(tmp_path / "cand2", mcq_rows(ids, fail=set(ids[3:30]), unparsed=set(ids[:3])))
    res = analyse(mcq["manifest"], base, cand2)
    assert res["rows"][0]["verdict"] == "different-worse (format)"
    assert "below the minimum" in res["rows"][0]["format"][0]


def cvss_rows(ids, err, unparsed=()):
    return [{"id": i, "suite": "t-cvss", "scorer": "cvss_mae", "status": "unparsed" if i in unparsed else "ok",
             "value": None if i in unparsed else err(k), "passed": err(k) <= 1.0, "expected": 9.8,
             **({} if i in unparsed else {"predicted": 9.8 - err(k)})} for k, i in enumerate(ids)]


def test_mae_equivalent_worse_better(tmp_path):
    data = tmp_path / "d.jsonl"
    ids = write_items(data, "t-cvss", 80, "cvss")
    p = write_manifest(tmp_path, [suite_entry(data, "t-cvss", metric="mae", margin=0.5)])
    errs = lambda k: (k % 5) * 0.4  # noqa: E731
    base = make_run(tmp_path / "base", cvss_rows(ids, errs))
    same = make_run(tmp_path / "same", cvss_rows(ids, errs))
    worse = make_run(tmp_path / "worse", cvss_rows(ids, lambda k: errs(k) + 1.0))
    better = make_run(tmp_path / "better", cvss_rows(ids, lambda k: max(0.0, errs(k) - 0.8)))
    r = analyse(p, base, same)
    assert r["rows"][0]["verdict"] == "equivalent" and r["exit"] == 0
    r = analyse(p, base, worse)
    assert r["rows"][0]["r"]["diff"] == pytest.approx(1.0)
    assert r["rows"][0]["verdict"] == "different-worse" and r["exit"] == 1     # lower MAE is better
    r = analyse(p, base, better)
    assert r["rows"][0]["verdict"] == "different-better" and r["exit"] == 0
    assert "bootstrap" in r["rows"][0]["r"]["test"]
    # unparsed CVSS items are imputed with the 7.5 constant's error (|9.8 - 7.5| = 2.3), never dropped
    unp = make_run(tmp_path / "unp", cvss_rows(ids, errs, unparsed=set(ids[:2])))
    r = analyse(p, base, unp)
    assert r["rows"][0]["r"]["n"] == 80


def test_refusal_tier_lower_is_better(tmp_path):
    data = tmp_path / "d.jsonl"
    ids = write_items(data, "t-ref", 100, "refusal")
    p = write_manifest(tmp_path, [suite_entry(data, "t-ref", metric="refusal_refuse", better="lower", margin=0.1)])
    rows = lambda refuse: [{"id": i, "suite": "t-ref", "scorer": "refusal", "tier": "refuse" if i in refuse  # noqa
                            else "full", "passed": i not in refuse} for i in ids]
    base = make_run(tmp_path / "base", rows(set(ids[:40])))
    cand = make_run(tmp_path / "cand", rows(set(ids[:5])))
    r = analyse(p, base, cand)
    assert r["rows"][0]["r"]["diff"] == pytest.approx(-0.35)
    assert r["rows"][0]["verdict"] == "different-better" and r["exit"] == 0
    r = analyse(p, cand, base)
    assert r["rows"][0]["verdict"] == "different-worse" and r["exit"] == 1


# ------------------------------------------------------------------------------------------- validity
@pytest.mark.parametrize("kind", ["stopped", "identity", "missing", "api", "samplers"])
def test_invalid_runs_exit_3(tmp_path, mcq, kind):
    ids = mcq["ids"]
    base = make_run(tmp_path / "base", mcq_rows(ids))
    rows = mcq_rows(ids)
    over = {}
    if kind == "stopped":
        over["protocol"] = {"mode": "exploratory", "invalid": "6 of 100 items failed (more than 5%)"}
    elif kind == "identity":
        over["served"] = {"models": ["coder"], "system_fingerprints": ["b6500", "b6501"]}
    elif kind == "missing":
        rows = rows[:90]
    elif kind == "api":
        rows = [dict(r, api_error=True) if k < 6 else r for k, r in enumerate(rows)]
    elif kind == "samplers":
        over["serving"] = {"fields": {}, "sampler_check": {"ok": False, "mismatch": {"top_k": {}}}}
    cand = make_run(tmp_path / "cand", rows, **over)
    if kind == "missing":   # run.json still lists the full item set
        rj = json.loads((cand / "run.json").read_text())
        rj["item_sets"] = json.loads((base / "run.json").read_text())["item_sets"]
        (cand / "run.json").write_text(json.dumps(rj))
    res = analyse(mcq["manifest"], base, cand)
    assert res["verdict"] == "INVALID" and res["exit"] == 3 and res["invalid"]
    assert compare(mcq["manifest"], base, cand) == 3


# ------------------------------------------------------------------------------------------- compatibility
@pytest.mark.parametrize("field, value, msg", [
    ("scorer_version", {"value": "2026.09-old"}, "scorer_version"),
    ("sampling", {**RUN.SAMPLING_PROFILES["off"], "temperature": 0.7, "seed": 1234, "profile": "off",
                  "overrides": {"temperature": 0.7}}, "sampling profile differs"),
    ("budget", {"policy_sha256": "e" * 64, "flat_override": None}, "budget_policy_sha256"),
    ("thinking", "on", "thinking"),
    ("prompts", {"system_prompt_sha256": {"t-mcq": "f" * 64}}, "system prompts"),
])
def test_incompatible_baseline_refused(tmp_path, mcq, capsys, field, value, msg):
    ids = mcq["ids"]
    base = make_run(tmp_path / "base", mcq_rows(ids))
    cand = make_run(tmp_path / "cand", mcq_rows(ids), **{field: value})
    with pytest.raises(G.GateError) as ei:
        analyse(mcq["manifest"], base, cand)
    assert msg in str(ei.value)
    assert compare(mcq["manifest"], base, cand) == 4
    assert "gate refused" in capsys.readouterr().err


def test_item_set_must_match_manifest(tmp_path, mcq):
    ids = mcq["ids"]
    base = make_run(tmp_path / "base", mcq_rows(ids[:50]))
    cand = make_run(tmp_path / "cand", mcq_rows(ids[:50]))
    with pytest.raises(G.GateError, match="item set is not the manifest's"):
        analyse(mcq["manifest"], base, cand)


def test_manifest_sha_mismatch_refused(tmp_path, mcq):
    ids = mcq["ids"]
    m = G.load_manifest(mcq["manifest"])
    for name in ("base", "cand"):
        gd = tmp_path / name
        make_run(gd / "gate-coder", mcq_rows(ids))
        G.write_record(gd, {"schema": G.RECORD_SCHEMA, "kind": "baseline", "run_dir": "gate-coder",
                            "manifest": {"sha256": m["_sha256"] if name == "cand" else "9" * 64}})
    with pytest.raises(G.GateError, match="baseline: made with manifest sha256"):
        G.analyse(m, tmp_path / "base", tmp_path / "cand")
    G.write_record(tmp_path / "base", {"schema": G.RECORD_SCHEMA, "kind": "baseline", "run_dir": "gate-coder",
                                       "manifest": {"sha256": m["_sha256"]}})
    assert G.analyse(m, tmp_path / "base", tmp_path / "cand")["exit"] == 0


def test_stale_harness_refused_unless_allowed(tmp_path, mcq, capsys):
    ids = mcq["ids"]
    base = make_run(tmp_path / "base", mcq_rows(ids))
    cand = make_run(tmp_path / "cand", mcq_rows(ids),
                    harness_sha256={"run.py": "1" * 64, "scorers.py": "b" * 64, "report.py": "c" * 64})
    with pytest.raises(G.GateError, match="allow-stale-harness"):
        analyse(mcq["manifest"], base, cand)
    res = analyse(mcq["manifest"], base, cand, allow_stale_harness=True)
    assert res["stale_harness"]["candidate"]["run.py"] == "1" * 64 and res["exit"] == 0
    assert "STALE HARNESS" in res["report"]
    assert compare(mcq["manifest"], base, cand, "--allow-stale-harness") == 0
    assert "STALE HARNESS" in capsys.readouterr().out


def test_identity_diff_in_report(tmp_path, mcq):
    ids = mcq["ids"]
    base = make_run(tmp_path / "base", mcq_rows(ids))
    sv = run_json("coder", {})["serving"]
    sv = json.loads(json.dumps(sv))
    sv["fields"]["quant"] = {"value": "Q8_0", "source": "x"}
    sv["static"] = {"model_sha256": "ab" * 32, "image_digest": "sha256:" + "cd" * 32, "flags": "-c 65536"}
    cand = make_run(tmp_path / "cand", mcq_rows(ids), serving=sv)
    res = analyse(mcq["manifest"], base, cand)
    assert res["identity"]["candidate"]["quant"] == "Q8_0" and res["identity"]["baseline"]["quant"] == "Q4_K_M"
    line = next(x for x in res["report"].splitlines() if x.startswith("| quant |"))
    assert "Q4_K_M" in line and "Q8_0" in line and "**yes**" in line
    assert "| image_digest |" in res["report"] and "| model_sha256 |" in res["report"]


# ------------------------------------------------------------------------------------------- CLI end to end
@pytest.fixture
def gw():
    g = FakeGateway()
    yield g
    g.close()


@pytest.fixture
def gate_env(tmp_path, monkeypatch):
    key = tmp_path / "k.key"
    key.write_text(FAKE_KEY + "\n")
    site = tmp_path / "site.env"
    site.write_text("SPARK_API_HOST=api.example.com\n")
    calls = []

    def runner(argv):
        calls.append(list(argv))
        return RUN.main(argv + ["--backoff", "0"])
    monkeypatch.setattr(G, "RUNNER", runner)
    monkeypatch.setenv("EVAL_STATE_DIR", str(tmp_path / "state"))
    live = {"calls": [], "seq": []}     # spark-model-agent serving replies, one per snapshot; none = unreachable

    def live_fetch(target):
        live["calls"].append(target)
        if not live["seq"]:
            raise G.GateError("spark-model-agent serving: unreachable in tests")
        return live["seq"].pop(0) if len(live["seq"]) > 1 else live["seq"][0]
    monkeypatch.setattr(G, "LIVE_FETCH", live_fetch)
    # the sample MCQ suite, min parse rate 0, so the fake server's "no idea" replies are not a format failure
    data = Path(G.EVALS_DIR / "samples" / "mcq.jsonl")
    man = write_manifest(tmp_path, [suite_entry(data, "sample-mcq", min_parse_rate=0.0, parse_margin=0.05)])
    return {"calls": calls, "manifest": man, "root": tmp_path / "gate", "live": live,
            "args": ["--manifest", str(man), "--results-root", str(tmp_path / "gate"), "--key-file", str(key),
                     "--site-env", str(site), "--no-guard"]}


def test_cli_needs_yes_and_makes_no_calls(gw, gate_env, capsys):
    rc = G.main(["baseline", "--model", "coder", "--base-url", gw.url, *gate_env["args"]])
    assert rc == 4 and not gw.requests and not gate_env["calls"]
    err = capsys.readouterr().err
    assert "model coder, 10 items" in err and "estimated" in err and "--yes" in err


def test_cli_check_without_baseline_refused(gw, gate_env):
    assert G.main(["check", "--model", "coder", "--yes", "--base-url", gw.url, *gate_env["args"]]) == 4
    assert not gw.requests


def test_cli_stop_file_refuses(gw, gate_env, tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "STOP").write_text("guard\n")
    assert G.main(["baseline", "--model", "coder", "--yes", "--base-url", gw.url, *gate_env["args"]]) == 4
    assert not gw.requests


def test_cli_baseline_then_check(gw, gate_env, capsys):
    base_args = ["--model", "coder", "--yes", "--base-url", gw.url, *gate_env["args"]]
    assert G.main(["baseline", *base_args]) == 0
    argv = gate_env["calls"][0]
    for flag in ("--probe-server", "--stop-rules", "--select-frozen", "--no-final-answer", "--no-prompt-cache",
                 "--budget-policy"):
        assert flag in argv
    assert argv[argv.index("--concurrency") + 1] == "1"
    assert all(r["cache_prompt"] is False for r in gw.requests)
    assert "--grader-model" not in argv and argv[argv.index("--thinking") + 1] == "off"
    assert argv[argv.index("--run-name") + 1] == "gate"
    bdir = next((gate_env["root"] / "coder").glob("baseline-*"))
    rec = json.loads((bdir / "gate.json").read_text())
    assert rec["status"] == "complete" and rec["manifest"]["sha256"] == G.load_manifest(gate_env["manifest"])["_sha256"]
    assert rec["serving_identity"]["model"] == "coder" and rec["serving_identity"]["system_fingerprints"] == ["b0-test"]
    n_base = len(gw.requests)
    assert n_base == 10 and all(r["temperature"] == 0 and r["top_k"] == 0 for r in gw.requests)
    capsys.readouterr()
    # no A/A calibration for this model under this manifest: check refuses before any call
    assert G.main(["check", *base_args]) == 4
    assert "no A/A calibration" in capsys.readouterr().err and len(gw.requests) == n_base
    base_args.append("--accept-aa")
    # same server, same replies: identical runs; at n = 10 that is INCONCLUSIVE
    assert G.main(["check", *base_args]) == 2
    out = capsys.readouterr()
    assert "A/A ACCEPTED by --accept-aa" in out.out
    cdir = next((gate_env["root"] / "coder").glob("check-*"))
    assert (cdir / "gate-report.md").read_text() in out.out + "\n"
    assert "**Overall: INCONCLUSIVE (hold: the owner decides)** (exit 2)" in out.out and "| model | coder | coder |" in out.out
    crec = json.loads((cdir / "gate.json").read_text())
    assert crec["verdict"] == "INCONCLUSIVE" and crec["baseline"] == str(bdir) and crec["exit"] == 2
    assert crec["aa"] == {"from": None, "pass": False, "accepted": True}
    assert FAKE_KEY not in out.out + out.err
    # a candidate that stops answering in the expected format fails the gate
    gw.answer = lambda body: "I cannot say."
    assert G.main(["check", *base_args]) == 1
    assert "different-worse (format)" in capsys.readouterr().out
    # offline compare of the two dirs agrees with check, and makes no calls
    n = len(gw.requests)
    assert G.main(["compare", str(bdir), str(cdir), "--manifest", str(gate_env["manifest"])]) == 2
    assert len(gw.requests) == n
    # another manifest: the stored baseline is not compatible
    m2 = json.loads(gate_env["manifest"].read_text())
    m2["note"] = "changed"
    other = gate_env["manifest"].with_name("other.json")
    other.write_text(json.dumps(m2))
    args2 = [x if x != str(gate_env["manifest"]) else str(other) for x in base_args]
    assert G.main(["check", *args2]) == 4
    assert "another manifest" in capsys.readouterr().err


def test_cli_stopping_rule_gives_invalid(gw, gate_env):
    """run.py's --stop-rules hook: more than 5% failed items stops the run as invalid; the gate exits 3."""
    gw.plan = {i: 400 for i in range(20)}
    rc = G.main(["baseline", "--model", "coder", "--yes", "--base-url", gw.url, *gate_env["args"]])
    assert rc == 3
    bdir = next((gate_env["root"] / "coder").glob("baseline-*"))
    rec = json.loads((bdir / "gate.json").read_text())
    assert rec["status"] == "invalid" and rec["run_exits"] == [4]
    rj = json.loads((bdir / "gate-coder" / "run.json").read_text())
    assert rj["protocol"]["stop_rules"] is True and "more than 5%" in rj["protocol"]["invalid"]
    assert len(gw.requests) < 10     # it stopped early
    # an invalid baseline is never used as the reference
    gw.plan = {}
    assert G.main(["check", "--model", "coder", "--yes", "--base-url", gw.url, *gate_env["args"]]) == 4


def test_run_py_stop_rules_off_by_default(gw, tmp_path):
    """Without --stop-rules an exploratory run keeps going past failures, as before."""
    key = tmp_path / "k.key"
    key.write_text(FAKE_KEY)
    site = tmp_path / "site.env"
    site.write_text("SPARK_API_HOST=api.example.com\n")
    gw.plan = {0: 400, 1: 400}
    args = ["--key-file", str(key), "--site-env", str(site), "--results", str(tmp_path / "r"), "--base-url", gw.url,
            "--backoff", "0", "--run-name", "t", "--suite", str(G.EVALS_DIR / "samples" / "mcq.jsonl"),
            "--model", "coder"]
    assert RUN.main(args) == 0 and len(gw.requests) == 10
    rj = json.loads((tmp_path / "r" / "t-coder" / "run.json").read_text())
    assert "invalid" not in rj["protocol"] and "stop_rules" not in rj["protocol"]


def test_frontier_model_refused(gate_env):
    assert G.main(["baseline", "--model", "claude:opus", "--yes", *gate_env["args"]]) == 4


def test_usage_error_is_a_refusal(capsys):
    with pytest.raises(SystemExit) as ei:
        G.main(["check"])
    assert ei.value.code == 4


# ------------------------------------------------------------------------------------------- per-suite caps, cache, A/A
def test_suite_cap_applies_to_that_suite_only(gw, gate_env, tmp_path):
    data = tmp_path / "caps.jsonl"
    write_items(data, "t-mcq", 3)
    write_items(data, "t-ref", 3, "refusal")
    man = write_manifest(tmp_path, [suite_entry(data, "t-mcq"),
                                    suite_entry(data, "t-ref", metric="refusal_full", better="higher", margin=0.1,
                                                max_tokens=512)], name="caps.json")
    args = [x if x != str(gate_env["manifest"]) else str(man) for x in gate_env["args"]]
    assert G.main(["baseline", "--model", "coder", "--yes", "--base-url", gw.url, *args]) == 0
    caps = {r["messages"][-1]["content"][:2]: r["max_tokens"] for r in gw.requests}
    assert caps == {"q1": RUN.MAX_TOKENS["mcq"], "q2": RUN.MAX_TOKENS["mcq"], "q3": RUN.MAX_TOKENS["mcq"],
                    "r1": 512, "r2": 512, "r3": 512}
    bdir = next((gate_env["root"] / "coder").glob("baseline-*"))
    rec = json.loads((bdir / "gate.json").read_text())
    assert rec["budget"]["suite_answer_tokens"] == {"t-ref": 512}
    assert rec["budget"]["per_suite"]["t-ref"]["answer"] == 512
    assert rec["budget"]["per_suite"]["t-mcq"]["answer"] == RUN.MAX_TOKENS["mcq"]
    rj = json.loads((bdir / "gate-coder" / "run.json").read_text())
    assert rj["budget"]["policy"]["suite_answer_tokens"] == {"t-ref": 512}
    assert rj["budget"]["policy_sha256"] == G.load_manifest(man)["sampling"]["budget_policy_sha256"]


def test_manifest_rejects_conflicting_caps(tmp_path):
    data = tmp_path / "d.jsonl"
    write_items(data, "t-mcq", 4)
    p = write_manifest(tmp_path, [suite_entry(data, "t-mcq", name="a", ids=["t-mcq-0001"], max_tokens=256),
                                  suite_entry(data, "t-mcq", name="b", ids=["t-mcq-0002"])])
    with pytest.raises(G.GateError, match="another max_tokens"):
        G.load_manifest(p)


def test_run_py_no_prompt_cache(gw, tmp_path):
    key = tmp_path / "k.key"
    key.write_text(FAKE_KEY)
    site = tmp_path / "site.env"
    site.write_text("SPARK_API_HOST=api.example.com\n")
    base = ["--key-file", str(key), "--site-env", str(site), "--results", str(tmp_path / "r"), "--base-url", gw.url,
            "--backoff", "0", "--suite", str(G.EVALS_DIR / "samples" / "mcq.jsonl"), "--model", "coder",
            "--limit", "2"]
    assert RUN.main(base + ["--run-name", "on"]) == 0
    assert all("cache_prompt" not in r for r in gw.requests)
    rj = json.loads((tmp_path / "r" / "on-coder" / "run.json").read_text())
    assert rj["prompt_cache"]["sent"] is None
    n = len(gw.requests)
    assert RUN.main(base + ["--run-name", "off", "--no-prompt-cache"]) == 0
    assert all(r["cache_prompt"] is False for r in gw.requests[n:])
    rj = json.loads((tmp_path / "r" / "off-coder" / "run.json").read_text())
    assert rj["prompt_cache"]["sent"] == {"cache_prompt": False} and rj["request_params"]["cache_prompt"] is False
    # a resume cannot switch the cache setting
    with pytest.raises(SystemExit, match="prompt_cache"):
        RUN.main(base + ["--run-name", "off"])


def test_pooled_format_row_and_tripwire(tmp_path):
    data = tmp_path / "d.jsonl"
    a_ids = write_items(data, "t-a", 100)
    b_ids = write_items(data, "t-b", 100)
    p = write_manifest(tmp_path, [suite_entry(data, "t-a"), suite_entry(data, "t-b")],
                       format={"pool": ["t-a", "t-b"], "margin": 0.02})
    rows = mcq_rows(a_ids, suite="t-a", fail=set(a_ids[:20])) + mcq_rows(b_ids, suite="t-b", fail=set(b_ids[:20]))
    base = make_run(tmp_path / "base", rows)
    same = make_run(tmp_path / "same", rows)
    res = analyse(p, base, same)
    assert [r["name"] for r in res["rows"]] == ["t-a", "t-b", "format"]
    fmt = res["rows"][-1]
    assert fmt["r"]["n"] == 200 and fmt["verdict"] == "equivalent" and res["exit"] == 0
    # 8 wrong-but-parsed t-a answers become unparsed: accuracy unchanged, the t-a tripwire fires
    rows2 = mcq_rows(a_ids, suite="t-a", fail=set(a_ids[8:20]), unparsed=set(a_ids[:8])) + \
        mcq_rows(b_ids, suite="t-b", fail=set(b_ids[:20]))
    cand = make_run(tmp_path / "cand", rows2)
    res = analyse(p, base, cand)
    by = {r["name"]: r for r in res["rows"]}
    assert by["t-a"]["r"]["diff"] == 0 and by["t-a"]["tripwire"]["lost"] == 8
    assert by["t-a"]["verdict"] == "different-worse (format)" and by["t-b"]["verdict"] == "equivalent"
    assert by["format"]["r"]["diff"] == pytest.approx(-0.04) and res["exit"] == 1


def test_calibrate_aa_gates_check(gw, gate_env, tmp_path, capsys):
    """calibrate = the baseline config twice; a passing A/A (80 identical items) unlocks check; a failing one
    blocks it until --accept-aa."""
    data = tmp_path / "aa.jsonl"
    write_items(data, "t-mcq", 80)
    man = write_manifest(tmp_path, [suite_entry(data, "t-mcq")], name="aa.json")
    args = ["--model", "coder", "--yes", "--base-url", gw.url,
            *[x if x != str(gate_env["manifest"]) else str(man) for x in gate_env["args"]]]
    assert G.main(["calibrate", *args]) == 0
    assert len(gw.requests) == 160
    assert "# A/A calibration: coder" in capsys.readouterr().out
    bdir = next((gate_env["root"] / "coder").glob("baseline-*"))
    aa = json.loads((bdir / "gate.json").read_text())["aa"]
    assert aa["pass"] and aa["verdict"] == "PASS" and aa["diagnostics"]["t-mcq"]["psi"] == 0
    assert (Path(aa["partner"]) / "gate-report.md").is_file()
    assert G.main(["check", *args]) == 0                  # A/A passed: no --accept-aa needed
    # a failing A/A (the server changes its answers between the two runs) blocks check; "better" fails too
    for first, second, code in (("Answer: no idea", "Answer: A", 2), ("Answer: A", "Answer: no idea", 1)):
        calls = {"n": 0}

        def flaky(body, first=first, second=second, calls=calls):
            calls["n"] += 1
            return second if calls["n"] > 80 else first
        gw.answer = flaky
        assert G.main(["calibrate", *args]) == code
    capsys.readouterr()
    assert G.main(["check", *args]) == 4
    assert "did not pass" in capsys.readouterr().err
    gw.answer = lambda body: "Answer: A"
    assert G.main(["check", *args, "--accept-aa"]) in (0, 1, 2)


# ------------------------------------------------------------------------------------------- gateway route
@pytest.fixture
def tunnel_env(gate_env, tmp_path, monkeypatch, gw):
    """site.env with a backend, and a fake tools/gateway-tunnel.sh (subprocess.run) that 'starts' the fake server."""
    (tmp_path / "site.env").write_text("SPARK_API_HOST=api.example.com\nBACKEND_LAN_IP=192.0.2.10\n"
                                       "BACKEND_WG_IP=10.99.0.2\n")
    monkeypatch.delenv("EVAL_BASE_URL", raising=False)
    monkeypatch.delenv("EVAL_GPU_SSH", raising=False)
    tunnel = {"calls": [], "fail": False}

    class Done:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    real_run = G.subprocess.run   # the same module run.py uses (git status): pass everything else through

    def fake_run(argv, *a, **kw):
        if argv[:2] != ["bash", str(G.TUNNEL)]:
            return real_run(argv, *a, **kw)
        assert kw["env"]["EVAL_RUN"] == "gate"
        tunnel["calls"].append(argv[2])
        if argv[2] == "start" and tunnel["fail"]:
            return Done(1, err="tunnel did not come up")
        return Done(0, f"EVAL_BASE_URL={gw.url}\n" if argv[2] == "start" else "")
    monkeypatch.setattr(G.subprocess, "run", fake_run)
    gate_env["tunnel"] = tunnel
    return gate_env


def test_route_tunnel_by_default(gw, tunnel_env, capsys):
    assert G.main(["baseline", "--model", "coder", "--yes", *tunnel_env["args"]]) == 0
    argv = tunnel_env["calls"][0]
    assert argv[argv.index("--base-url") + 1] == gw.url and len(gw.requests) == 10
    assert argv[argv.index("--timeout") + 1] == str(G.REQUEST_TIMEOUT_S)
    assert tunnel_env["tunnel"]["calls"] == ["start", "stop"]
    bdir = next((tunnel_env["root"] / "coder").glob("baseline-*"))
    assert json.loads((bdir / "gate.json").read_text())["routes"] == ["tunnel"]
    assert "SSH tunnel" in capsys.readouterr().err


def test_route_calibrate_starts_a_tunnel_per_run(gw, tunnel_env):
    G.main(["calibrate", "--model", "coder", "--yes", *tunnel_env["args"]])
    assert tunnel_env["tunnel"]["calls"] == ["start", "stop", "start", "stop"]
    assert len(tunnel_env["calls"]) == 2
    assert all(c[c.index("--base-url") + 1] == gw.url for c in tunnel_env["calls"])


def test_route_tunnel_failure_refuses_before_any_dir(gw, tunnel_env, capsys):
    tunnel_env["tunnel"]["fail"] = True
    assert G.main(["baseline", "--model", "coder", "--yes", *tunnel_env["args"]]) == 4
    assert not gw.requests and not tunnel_env["calls"] and not (tunnel_env["root"] / "coder").exists()
    assert "--via-edge" in capsys.readouterr().err


@pytest.mark.parametrize("extra", [["--via-edge"], ["--base-url", "BASE"]])
def test_route_no_tunnel_when_told(gw, tunnel_env, extra):
    extra = [gw.url if x == "BASE" else x for x in extra]
    G.main(["baseline", "--model", "coder", "--yes", "--request-timeout", "120", *extra, *tunnel_env["args"]])
    assert tunnel_env["tunnel"]["calls"] == []
    argv = tunnel_env["calls"][0]
    assert argv[argv.index("--timeout") + 1] == "120.0"
    assert ("--base-url" in argv) == ("--via-edge" not in extra)


def test_route_env_base_url_skips_tunnel(gw, tunnel_env, monkeypatch):
    monkeypatch.setenv("EVAL_BASE_URL", gw.url)
    assert G.main(["baseline", "--model", "coder", "--yes", *tunnel_env["args"]]) == 0
    assert tunnel_env["tunnel"]["calls"] == [] and "--base-url" not in tunnel_env["calls"][0]
    assert len(gw.requests) == 10


def test_route_no_backend_warns_and_uses_edge(gw, gate_env, monkeypatch, capsys):
    monkeypatch.delenv("EVAL_BASE_URL", raising=False)
    monkeypatch.delenv("EVAL_GPU_SSH", raising=False)
    real_run = G.subprocess.run
    monkeypatch.setattr(G.subprocess, "run", lambda argv, *a, **k: pytest.fail("no tunnel without a backend")
                        if argv[:2] == ["bash", str(G.TUNNEL)] else real_run(argv, *a, **k))
    route = G.start_route(G.parse_args(["baseline", "--model", "coder", *gate_env["args"]]))
    assert route == ("edge", None, None) and "public edge" in capsys.readouterr().err


# ------------------------------------------------------------------------------------------- live serving identity
def agent_reply(cmd_sha="c" * 64, flags=None):
    return {"ok": True, "verb": "serving", "config_sha256": "f" * 64,
            "models": {"other": {"aliases": ["x"], "cmd_sha256": "0" * 64, "server_flags": {}},
                       "qwen3.8-27b": {"aliases": ["coder"], "cmd_sha256": cmd_sha,
                                       "server_flags": flags or {"-c": "131072", "-sm": "none"}}}}


@pytest.fixture
def live_env(gate_env, tmp_path, monkeypatch):
    monkeypatch.delenv("EVAL_GPU_SSH", raising=False)
    (tmp_path / "site.env").write_text("SPARK_API_HOST=api.example.com\nBACKEND_LAN_IP=192.0.2.10\n"
                                       "BACKEND_SSH_USER=op\n")
    return gate_env


def _baseline(gw, env):
    assert G.main(["baseline", "--model", "coder", "--yes", "--base-url", gw.url, *env["args"]]) in (0, 3)
    bdir = sorted((env["root"] / "coder").glob("baseline-*"))[-1]
    return bdir, json.loads((bdir / G.RECORD).read_text())


def test_live_identity_recorded_before_and_after(gw, live_env):
    live_env["live"]["seq"] = [agent_reply()]
    bdir, rec = _baseline(gw, live_env)
    assert live_env["live"]["calls"] == ["op@192.0.2.10"] * 2
    assert [x["when"] for x in rec["llama_swap_live"]] == ["before", "after"]
    assert rec["llama_swap_live"][0]["model_id"] == "qwen3.8-27b" and rec["status"] == "complete"
    ident = rec["serving_identity"]
    assert ident["llama_swap_cmd_sha256"] == "c" * 64 and ident["llama_swap_source"] == G.SOURCE_LIVE
    assert ident["flags"] == {"-c": "131072", "-sm": "none"}


def test_live_cmd_change_during_run_is_invalid(gw, live_env):
    live_env["live"]["seq"] = [agent_reply("a" * 64), agent_reply("b" * 64)]
    bdir, rec = _baseline(gw, live_env)
    assert rec["status"] == "invalid" and any("changed during the run" in x for x in rec["invalid"])


def test_no_live_config_falls_back_to_the_template(gw, gate_env, capsys):
    bdir, rec = _baseline(gw, gate_env)              # site.env has no backend: no ssh at all
    assert gate_env["live"]["calls"] == [] and rec["status"] == "complete"
    assert "no backend" in rec["llama_swap_live"][0]["error"]
    assert rec["serving_identity"]["llama_swap_source"] == G.SOURCE_TEMPLATE
    assert rec["serving_identity"]["llama_swap_cmd_sha256"]
    assert "falls back to the repo template" in capsys.readouterr().err


def test_unreachable_agent_or_unknown_model_falls_back(gw, live_env):
    bdir, rec = _baseline(gw, live_env)              # LIVE_FETCH raises
    assert "unreachable" in rec["llama_swap_live"][0]["error"]
    live_env["live"]["seq"] = [{**agent_reply(), "models": {}}]
    bdir, rec = _baseline(gw, live_env)
    assert "not in the live llama-swap config" in rec["llama_swap_live"][0]["error"]
    assert rec["serving_identity"]["llama_swap_source"] == G.SOURCE_TEMPLATE


def test_report_notes_mixed_identity_sources(gw, live_env, gate_env, tmp_path, capsys):
    b1, _ = _baseline(gw, live_env)                  # template (agent unreachable)
    live_env["live"]["seq"] = [agent_reply()]
    b2, _ = _baseline(gw, live_env)                  # live
    res = G.analyse(G.load_manifest(gate_env["manifest"]), b1, b2)
    assert "not comparable" in res["report"] and "| llama_swap_source | repo template | live config | **yes** |" \
        in res["report"]
    assert res["identity"]["candidate"]["llama_swap_cmd_sha256"] == "c" * 64
