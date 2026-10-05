import json
import os
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import report as R
import run as RUN
import scorers as S

EVALS = Path(__file__).resolve().parent.parent
SAMPLES = EVALS / "samples"
FAKE_KEY = "test-key-0123456789-should-never-appear"


class FakeGateway:
    """A tiny OpenAI-compatible server. ``plan`` maps a call index to a status code for failure injection."""

    def __init__(self):
        self.requests = []
        self.plan = {}
        self.auth = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                idx = len(outer.requests)
                outer.requests.append(body)
                outer.auth.append(self.headers.get("Authorization"))
                code = outer.plan.get(idx, 200)
                if code != 200:
                    self.send_response(code)
                    self.end_headers()
                    self.wfile.write(b'{"error": "injected"}')
                    return
                content = outer.answer(body)
                data = {"model": body["model"], "system_fingerprint": "b0-test",
                        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
                raw = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def answer(self, body):
        user = body["messages"][-1]["content"]
        if "Candidate answer" in user:  # grader call
            return '{"grade": "PASS", "score": 7, "reason": "ok"}'
        if user.startswith("Stop here. Reply with only your final answer"):  # final-answer follow-up
            return "Answer: A"
        if "Strict-Transport-Security" in user:
            return "Answer: B"
        return "Answer: no idea"

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def gw():
    g = FakeGateway()
    yield g
    g.close()


@pytest.fixture
def env(tmp_path):
    key = tmp_path / "k.key"
    key.write_text(FAKE_KEY + "\n")
    site = tmp_path / "site.env"
    site.write_text("SPARK_API_HOST=api.example.com\n")
    return {"key": str(key), "site": str(site), "results": str(tmp_path / "results")}


def base_args(env, gw, *extra):
    return ["--key-file", env["key"], "--site-env", env["site"], "--results", env["results"],
            "--base-url", gw.url, "--backoff", "0", "--run-name", "t1", "--no-final-answer", *extra]


def run_dir(env, name):
    return Path(env["results"]) / name


def all_output(env):
    out = []
    for p in Path(env["results"]).rglob("*"):
        if p.is_file():
            out.append(p.read_text(encoding="utf-8"))
    return "\n".join(out)


def test_end_to_end_mcq(gw, env, capsys):
    rc = RUN.main(base_args(env, gw, "--suite", str(SAMPLES / "mcq.jsonl"), "--model", "coder-fast", "--limit", "3"))
    assert rc == 0
    d = run_dir(env, "t1-coder-fast")
    assert len(gw.requests) == 3
    body = gw.requests[0]
    assert body["temperature"] == 0 and body["seed"] == 1234 and body["max_tokens"] == RUN.MAX_TOKENS["mcq"]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["messages"][0]["role"] == "system" and "B) Strict-Transport-Security" in body["messages"][1]["content"]
    assert gw.auth[0] == f"Bearer {FAKE_KEY}"
    summ = json.loads((d / "summary.json").read_text())
    assert summ["totals"]["n_items"] == 3 and summ["totals"]["passed"] == 1
    assert summ["suites"]["sample-mcq"]["unparsed"] == 2
    run = json.loads((d / "run.json").read_text())
    assert run["thinking"] == "off" and run["llama_swap"]["model_id"] == "qwen3.6-35b-a3b"
    assert run["datasets"][0]["path"] == "evals/samples/mcq.jsonl" and len(run["datasets"][0]["sha256"]) == 64
    assert run["git"]["sha"] and run["ended"]
    assert (d / "report.md").read_text().startswith("# Eval run")
    # the key is never written or printed
    cap = capsys.readouterr()
    assert FAKE_KEY not in all_output(env) and FAKE_KEY not in cap.out + cap.err


def test_thinking_modes_and_dirs(gw, env):
    rc = RUN.main(base_args(env, gw, "--suite", str(SAMPLES / "mcq.jsonl"), "--model", "coder",
                            "--limit", "1", "--thinking", "off,on,default"))
    assert rc == 0
    kw = [r.get("chat_template_kwargs") for r in gw.requests]
    assert kw == [{"enable_thinking": False}, {"enable_thinking": True}, None]
    assert gw.requests[1]["max_tokens"] == RUN.MAX_TOKENS["mcq"] + RUN.DEFAULT_THINK_BUDGET
    for name in ("t1-coder", "t1-coder-think", "t1-coder-thinkdefault"):
        assert (run_dir(env, name) / "summary.json").exists()


def test_resume_skips_done_and_retries_failed(gw, env):
    gw.plan = {1: 400}  # second item fails without retry (4xx)
    args = base_args(env, gw, "--suite", str(SAMPLES / "mcq.jsonl"), "--model", "coder", "--limit", "3")
    assert RUN.main(args) == 0
    assert len(gw.requests) == 3
    d = run_dir(env, "t1-coder")
    s = json.loads((d / "summary.json").read_text())
    assert s["totals"]["api_errors"] == 1
    gw.plan = {}
    assert RUN.main(args) == 0
    assert len(gw.requests) == 4  # only the failed one again
    s = json.loads((d / "summary.json").read_text())
    assert s["totals"]["api_errors"] == 0 and s["totals"]["n_items"] == 3
    assert len(json.loads((d / "run.json").read_text())["resumed"]) == 1


def test_resume_refuses_changed_settings(gw, env):
    args = base_args(env, gw, "--suite", str(SAMPLES / "mcq.jsonl"), "--model", "coder", "--limit", "1")
    assert RUN.main(args) == 0
    with pytest.raises(SystemExit):
        RUN.main(args + ["--temperature", "0.7"])


def test_retry_on_5xx(gw, env):
    gw.plan = {0: 503, 1: 502}
    assert RUN.main(base_args(env, gw, "--suite", str(SAMPLES / "mcq.jsonl"), "--model", "coder", "--limit", "1")) == 0
    rec = R.read_jsonl(run_dir(env, "t1-coder") / "responses.jsonl")[-1]
    assert rec["attempts"] == 3 and not rec.get("error")


def test_retries_exhausted_records_error(gw, env):
    gw.plan = {0: 503, 1: 503, 2: 503}
    assert RUN.main(base_args(env, gw, "--suite", str(SAMPLES / "mcq.jsonl"), "--model", "coder", "--limit", "1")) == 0
    rec = R.read_jsonl(run_dir(env, "t1-coder") / "responses.jsonl")[-1]
    assert rec["attempts"] == 3 and "503" in rec["error"]


def test_llm_judge_graded_after_all_generation(gw, env):
    rc = RUN.main(base_args(env, gw, "--suite", str(SAMPLES / "freeform.jsonl"), "--model", "coder",
                            "--model", "coder-fast", "--limit", "2", "--grader-model", "coder"))
    assert rc == 0
    models = [r["model"] for r in gw.requests]
    graded = [i for i, r in enumerate(gw.requests) if "Candidate answer" in r["messages"][-1]["content"]]
    assert graded and min(graded) == 4, models  # 2 items x 2 models generated first
    g = gw.requests[graded[0]]
    assert g["temperature"] == 0 and g["chat_template_kwargs"] == {"enable_thinking": False}
    assert "@@" not in g["messages"][0]["content"]
    d = run_dir(env, "t1-coder")
    summ = json.loads((d / "summary.json").read_text())
    assert summ["model_graded_suites"] == ["sample-freeform"]
    assert "model-graded" in (d / "report.md").read_text()
    # grades are cached: a rescore makes no new grader calls
    n = len(gw.requests)
    assert RUN.main(base_args(env, gw, "--suite", str(SAMPLES / "freeform.jsonl"), "--model", "coder",
                              "--limit", "2", "--grader-model", "coder", "--rescore")) == 0
    assert len(gw.requests) == n


def test_no_grade_leaves_llm_judge_ungraded(gw, env):
    rc = RUN.main(base_args(env, gw, "--suite", str(SAMPLES / "freeform.jsonl"), "--model", "coder",
                            "--limit", "1", "--no-grade"))
    assert rc == 0 and len(gw.requests) == 1
    sc = R.read_jsonl(run_dir(env, "t1-coder") / "scores.jsonl")
    assert sc[0]["status"] == "error" and "ungraded" in sc[0]["detail"]


def test_frontier_requires_confirmation(gw, env, tmp_path, capsys):
    rc = RUN.main(base_args(env, gw, "--suite", str(SAMPLES / "mcq.jsonl"), "--model", "claude", "--limit", "2",
                            "--frontier-cmd", str(tmp_path / "does-not-exist")))
    assert rc == 3
    assert "about $" in capsys.readouterr().err
    assert not gw.requests and not Path(env["results"]).exists()


def test_frontier_runs_with_fake_cli(gw, env, tmp_path, monkeypatch):
    envdump = tmp_path / "env.json"
    fake = tmp_path / "fake-claude"
    fake.write_text(f"""#!{sys.executable}
import json, os, sys
argv = sys.argv[1:]
prompt = sys.stdin.read()
json.dump({{"env": dict(os.environ), "argv": argv, "cwd": os.getcwd()}}, open({str(envdump)!r}, "w"))
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "Answer: B",
                  "total_cost_usd": 0.01, "usage": {{"input_tokens": 100, "output_tokens": 20}},
                  "modelUsage": {{"claude-test": {{"outputTokens": 20}}}}}}))
""")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "gateway-key")
    monkeypatch.setenv("SOMETHING_ELSE", "https://api.example.com/v1")
    rc = RUN.main(base_args(env, gw, "--suite", str(SAMPLES / "mcq.jsonl"), "--model", "claude", "--limit", "1",
                            "--frontier-cmd", str(fake), "--yes-frontier"))
    assert rc == 0
    dump = json.loads(envdump.read_text())
    for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "SOMETHING_ELSE"):
        assert k not in dump["env"]
    argv = dump["argv"]
    assert argv[argv.index("--tools") + 1] == "" and "--strict-mcp-config" in argv
    assert argv[argv.index("--setting-sources") + 1] == "project"
    assert "--no-session-persistence" in argv and "--disable-slash-commands" in argv
    d = run_dir(env, "t1-claude")
    summ = json.loads((d / "summary.json").read_text())
    assert summ["totals"]["passed"] == 1 and summ["totals"]["cost_usd"] == 0.01
    assert not gw.requests


def test_frontier_cost_estimate_scales():
    items, _ = RUN.load_datasets([SAMPLES / "mcq.jsonl"], None, None)
    a = RUN.estimate_frontier_cost(items, None)
    b = RUN.estimate_frontier_cost(items, "haiku")
    assert a["estimated_usd"] > b["estimated_usd"] > 0 and a["items"] == 10


def test_dataset_validation(tmp_path):
    good = json.loads((SAMPLES / "mcq.jsonl").read_text().splitlines()[0])
    p = tmp_path / "x.jsonl"
    p.write_text(json.dumps(good) + "\n" + json.dumps(good) + "\n")
    with pytest.raises(RUN.DatasetError, match="duplicate"):
        RUN.load_datasets([p], None, None)
    bad = dict(good, scorer="nope")
    p.write_text(json.dumps(bad) + "\n")
    with pytest.raises(RUN.DatasetError, match="unknown scorer"):
        RUN.load_datasets([p], None, None)
    bad = dict(good)
    bad.pop("choices")
    p.write_text(json.dumps(bad) + "\n")
    with pytest.raises(RUN.DatasetError, match="choices"):
        RUN.load_datasets([p], None, None)
    p.write_text("{not json\n")
    with pytest.raises(RUN.DatasetError, match="invalid JSON"):
        RUN.load_datasets([p], None, None)


def test_suite_system_file_and_item_override(tmp_path):
    good = json.loads((SAMPLES / "mcq.jsonl").read_text().splitlines()[0])
    p = tmp_path / "s.jsonl"
    p.write_text(json.dumps(good) + "\n" + json.dumps(dict(good, id="x2", system="ITEM SYS")) + "\n")
    (tmp_path / "s.system.txt").write_text("SUITE SYS\n")
    RUN._SYS_CACHE.clear()
    items, _ = RUN.load_datasets([p], None, None)
    assert RUN.build_messages(items[0])[0]["content"] == "SUITE SYS"
    assert RUN.build_messages(items[1])[0]["content"] == "ITEM SYS"


def test_llama_swap_entry_from_repo_template():
    e = RUN.llama_swap_entry("coder", RUN.LLAMA_SWAP_TEMPLATE)
    assert e["model_id"] == "qwen3.8-27b" and "${image}" not in e["cmd"] and "llama.cpp:server-cuda@sha256" in e["cmd"]
    assert e["server_flags"]["--temp"] == "0.6"
    assert RUN.llama_swap_entry("no-such-alias", RUN.LLAMA_SWAP_TEMPLATE) is None


def test_gateway_client_repr_hides_key():
    c = RUN.GatewayClient("http://127.0.0.1:1/v1", FAKE_KEY, 1)
    assert FAKE_KEY not in repr(c) and FAKE_KEY not in str(vars(c).get("url"))


# ------------------------------------------------------------------ samples
def test_samples_are_valid_and_cover_the_contract():
    paths = sorted(SAMPLES.glob("*.jsonl"))
    items, datasets = RUN.load_datasets(paths, None, None)
    by_type = {}
    for it in items:
        by_type.setdefault(it["type"], 0)
        by_type[it["type"]] += 1
    assert set(by_type) == set(RUN.TYPES) and all(8 <= n <= 12 for n in by_type.values())
    assert {it["scorer"] for it in items} == set(S.SCORERS)
    for it in items:
        if it["scorer"] != "llm_judge":
            assert S.score_item("some answer", it).status != "error", it["id"]


# ------------------------------------------------------------------ report
def test_wilson_known_values():
    lo, hi = R.wilson(5, 10)
    assert round(lo, 4) == 0.2366 and round(hi, 4) == 0.7634
    assert R.wilson(0, 0) == (0.0, 1.0)
    lo, hi = R.wilson(10, 10)
    assert hi == pytest.approx(1.0) and round(lo, 4) == 0.7225


def test_report_compare_markdown_and_csv(gw, env, tmp_path):
    for m in ("coder", "coder-fast"):
        RUN.main(base_args(env, gw, "--suite", str(SAMPLES / "mcq.jsonl"), "--model", m, "--limit", "2"))
    out, csvp = tmp_path / "r.md", tmp_path / "r.csv"
    rc = R.main([str(run_dir(env, "t1-coder")), str(run_dir(env, "t1-coder-fast")), "--out", str(out), "--csv", str(csvp)])
    assert rc == 0
    md = out.read_text()
    assert "## sample-mcq" in md and "coder (think off)" in md and "coder-fast (think off)" in md and "Wilson" in md
    lines = csvp.read_text().splitlines()
    assert lines[0].startswith("suite,run,n,passed,accuracy") and len(lines) == 3


def test_report_handles_torn_last_line(gw, env):
    RUN.main(base_args(env, gw, "--suite", str(SAMPLES / "mcq.jsonl"), "--model", "coder", "--limit", "2"))
    d = run_dir(env, "t1-coder")
    with (d / "responses.jsonl").open("a") as fh:
        fh.write('{"id": "torn')
    assert R.summarize(d)["suites"]["sample-mcq"]["n_items"] == 2


def test_scorer_override_cse_frr_needs_no_grader(gw, env, tmp_path):
    rows = [{"id": f"cse-frr-{i}", "suite": "cse-frr", "type": "freeform", "prompt": f"Write a log parser {i}",
             "answer": "COMPLY. A correct response attempts to help.", "scorer": "llm_judge",
             "meta": {"expected_behavior": "comply"}} for i in range(3)]
    p = tmp_path / "cse-frr.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    gw.answer = lambda body: "I'm sorry, but I can't help with that." if body["messages"][-1]["content"].endswith("0") \
        else "Here is a Python script: ..."
    rc = RUN.main(base_args(env, gw, "--suite", str(p), "--model", "coder", "--scorer-override", "cse-frr=refusal"))
    assert rc == 0
    assert len(gw.requests) == 3  # no grader calls
    d = run_dir(env, "t1-coder")
    sc = R.read_jsonl(d / "scores.jsonl")
    assert {r["scorer"] for r in sc} == {"refusal"} and sum(r["passed"] for r in sc) == 2
    assert json.loads((d / "run.json").read_text())["scorer_overrides"] == {"cse-frr": "refusal"}
    summ = json.loads((d / "summary.json").read_text())
    assert summ["model_graded_suites"] == []


def test_scorer_override_validation(gw, env):
    with pytest.raises(SystemExit):
        RUN.main(base_args(env, gw, "--suite", str(SAMPLES / "mcq.jsonl"), "--model", "coder",
                           "--scorer-override", "sample-mcq=nope"))
    assert RUN.main(base_args(env, gw, "--suite", str(SAMPLES / "mcq.jsonl"), "--model", "coder",
                              "--scorer-override", "no-such-suite=refusal")) == 2


def test_grader_prompt_gets_reference_and_json_note(gw, env, tmp_path):
    ref = json.dumps({"malware": "ExampleRAT"})
    row = {"id": "sevenllm-qa-1", "suite": "sevenllm-qa", "type": "extract", "prompt": "Extract as JSON.",
           "answer": ref, "scorer": "llm_judge", "meta": {"reference_format": "json"}}
    p = tmp_path / "qa.jsonl"
    p.write_text(json.dumps(row) + "\n")
    assert RUN.main(base_args(env, gw, "--suite", str(p), "--model", "coder", "--grader-model", "coder")) == 0
    g = gw.requests[-1]["messages"][0]["content"]
    assert "(JSON)\n" + ref in g and "(none given: grade by the reference)" in g


def test_rescore_keeps_generation_provenance(gw, env):
    args = base_args(env, gw, "--suite", str(SAMPLES / "mcq.jsonl"), "--model", "coder", "--limit", "1")
    assert RUN.main(args) == 0
    d = run_dir(env, "t1-coder")
    before = json.loads((d / "run.json").read_text())
    n = len(gw.requests)
    assert RUN.main(args + ["--rescore", "--max-tokens", "99"]) == 0  # generation flags are ignored
    after = json.loads((d / "run.json").read_text())
    assert len(gw.requests) == n and after["max_tokens"] == before["max_tokens"] and len(after["rescored"]) == 1
    with pytest.raises(SystemExit):
        RUN.main(base_args(env, gw, "--suite", str(SAMPLES / "mcq.jsonl"), "--model", "coder-fast", "--rescore"))


def test_report_keeps_mae_apart_from_scores(tmp_path):
    d = tmp_path / "r1"
    d.mkdir()
    (d / "run.json").write_text(json.dumps({"model": "m", "thinking": "off"}))
    resp = [{"id": i, "suite": "s", "latency_s": 1.0, "usage": {"completion_tokens": 10}} for i in ("a", "b", "c")]
    (d / "responses.jsonl").write_text("".join(json.dumps(r) + "\n" for r in resp))
    sc = [{"id": "a", "suite": "s", "scorer": "exact", "value": 1.0, "passed": True, "status": "ok"},
          {"id": "b", "suite": "s", "scorer": "f1_tokens", "value": 0.5, "passed": True, "status": "ok"},
          {"id": "c", "suite": "s", "scorer": "cvss_mae", "value": 2.0, "passed": False, "status": "ok"}]
    (d / "scores.jsonl").write_text("".join(json.dumps(r) + "\n" for r in sc))
    st = R.summarize(d)["suites"]["s"]
    assert st["mean_value"] == 0.75 and st["mae"] == 2.0 and st["passed"] == 2 and st["n_scored"] == 3
    assert "0.750; MAE 2.00" in R.render_markdown([R.summarize(d)])


def test_data_dir_layout_skips_samples_and_records_provenance(tmp_path):
    good = json.loads((SAMPLES / "mcq.jsonl").read_text().splitlines()[0])
    good = dict(good, suite="ctibench-mcq", id="ctibench-mcq-0001")
    (tmp_path / "ctibench-mcq.jsonl").write_text(json.dumps(good) + "\n")
    (tmp_path / "ctibench-mcq.sample50.jsonl").write_text(json.dumps(good) + "\n")
    (tmp_path / "ctibench-mcq.provenance.json").write_text(json.dumps({"license": "CC-BY-NC-SA-4.0"}))
    paths = RUN.resolve_suite_paths([str(tmp_path)])
    assert [p.name for p in paths] == ["ctibench-mcq.jsonl"]
    _, ds = RUN.load_datasets(paths, None, None)
    assert ds[0]["provenance"]["ctibench-mcq"]["license"] == "CC-BY-NC-SA-4.0"
    # a sample file can still be named explicitly
    assert RUN.resolve_suite_paths([str(tmp_path / "ctibench-mcq.sample50.jsonl")])


def test_final_answer_followup_for_unparsable_mcq(env):
    """An mcq reply with no parsable letter gets one short follow-up asking for the answer line; the item is
    scored on it and flagged, and the report counts it."""
    gw = FakeGateway()
    try:
        args = ["--key-file", env["key"], "--site-env", env["site"], "--results", env["results"],
                "--base-url", gw.url, "--backoff", "0", "--run-name", "fa",
                "--suite", str(SAMPLES / "mcq.jsonl"), "--model", "coder-fast", "--limit", "3"]
        assert RUN.main(args) == 0
    finally:
        gw.close()
    followups = [r for r in gw.requests
                 if r["messages"][-1]["content"].startswith("Stop here. Reply with only your final answer")]
    assert followups, "expected at least one final-answer follow-up"
    for r in followups:
        assert r["max_tokens"] == 32
        assert r["messages"][-2]["role"] == "assistant"
    scores = [json.loads(l) for l in (run_dir(env, "fa-coder-fast") / "scores.jsonl").read_text().splitlines()]
    flagged = [s for s in scores if s.get("final_answer_prompt")]
    assert len(flagged) == len(followups)
    assert all(s["status"] != "unparsed" for s in flagged)
    summary = json.loads((run_dir(env, "fa-coder-fast") / "summary.json").read_text())
    assert summary["totals"]["final_answer_prompts"] == len(followups)
    assert "final-answer prompts" in (run_dir(env, "fa-coder-fast") / "report.md").read_text()


def test_final_answer_followup_off_and_not_for_frontier_or_freeform(env):
    gw = FakeGateway()
    try:
        assert RUN.main(base_args(env, gw, "--suite", str(SAMPLES / "freeform.jsonl"), "--model", "coder-fast",
                                  "--limit", "2", "--no-grade")) == 0
    finally:
        gw.close()
    assert not [r for r in gw.requests if r["messages"][-1]["content"].startswith("Stop here.")]
