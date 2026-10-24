import json
import re
import shutil
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
        self.think_only = False
        self.gets = []
        self.get_routes = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.gets.append((self.path, self.headers.get("Authorization")))
                body = outer.get_routes.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

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
                msg = {"role": "assistant", "content": content}
                if outer.think_only and not body["messages"][-1]["content"].startswith("Stop here."):
                    # a thinking reply cut off before the answer: no content, all of it in reasoning_content
                    msg = {"role": "assistant", "content": "", "reasoning_content": "step one... " * 5 + "so B?"}
                data = {"model": body["model"], "system_fingerprint": "b0-test",
                        "choices": [{"finish_reason": "length" if outer.think_only else "stop", "message": msg}],
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
                            "--model", "coder-fast", "--limit", "2", "--grader-model", "claude:judge-a"))
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
                              "--limit", "2", "--grader-model", "claude:judge-a", "--rescore")) == 0
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


# R2: this test no longer reads walter/'s template by default. It reads $SPARK_LLAMA_SWAP_TEMPLATE when set (the deck
# CI sets it to walter/llama-swap/config.yaml.tmpl while the template is in this repo), else the vendored C4 fixture
# (a copy of contracts/serving-identity/llama-swap.contract.yaml; the deck's C4 provider test asserts the same
# fields on the real template).
LLAMA_SWAP_FIXTURE = EVALS / "tests" / "fixtures" / "llama-swap.contract.yaml"


def llama_swap_template() -> Path:
    env = os.environ.get("SPARK_LLAMA_SWAP_TEMPLATE")
    return Path(env) if env else LLAMA_SWAP_FIXTURE


def test_llama_swap_entry_from_template():
    path = llama_swap_template()
    assert path.is_file(), f"SPARK_LLAMA_SWAP_TEMPLATE={path} does not exist"
    e = RUN.llama_swap_entry("coder", path)
    assert e["model_id"] == "qwen3.8-27b" and "${image}" not in e["cmd"] and "llama.cpp:server-cuda@sha256" in e["cmd"]
    assert e["server_flags"]["--temp"] == "0.6"
    assert RUN.llama_swap_entry("no-such-alias", path) is None


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
    assert RUN.main(base_args(env, gw, "--suite", str(p), "--model", "coder", "--grader-model", "claude:judge-a")) == 0
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
                "--base-url", gw.url, "--backoff", "0", "--run-name", "fa", "--final-answer",
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
    assert all(s["status"] != "unparsed" and s["followup_used"] is True for s in flagged)
    assert all(s["followup_used"] is False for s in scores if not s.get("final_answer_prompt"))
    assert json.loads((run_dir(env, "fa-coder-fast") / "run.json").read_text())["followup"]["enabled"] is True
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


def test_final_answer_followup_has_thinking_off_and_sees_the_reasoning(env):
    """Eval run (a) bug: with thinking on, the follow-up reused enable_thinking: true, spent its 32 tokens
    reasoning and came back empty. It must always send thinking off, and when the cut-off reply has no content,
    pass the tail of the reasoning as the assistant turn so the model can still state its answer."""
    gw = FakeGateway()
    gw.think_only = True
    try:
        args = ["--key-file", env["key"], "--site-env", env["site"], "--results", env["results"],
                "--base-url", gw.url, "--backoff", "0", "--run-name", "fat", "--thinking", "on", "--final-answer",
                "--suite", str(SAMPLES / "mcq.jsonl"), "--model", "coder", "--limit", "2"]
        assert RUN.main(args) == 0
    finally:
        gw.close()
    followups = [r for r in gw.requests if r["messages"][-1]["content"].startswith("Stop here.")]
    assert len(followups) == 2
    for r in followups:
        assert r["chat_template_kwargs"] == {"enable_thinking": False} and r["max_tokens"] == 32
        assert r["messages"][-2] == {"role": "assistant", "content": "step one... " * 5 + "so B?"}
    resp = [json.loads(l) for l in (run_dir(env, "fat-coder-think") / "responses.jsonl").read_text().splitlines()]
    assert all(r["final_answer"] == "Answer: A" and r["final_answer_context"] == "reasoning" for r in resp)
    scores = [json.loads(l) for l in (run_dir(env, "fat-coder-think") / "scores.jsonl").read_text().splitlines()]
    assert all(s["status"] == "ok" and s["final_answer_prompt"] for s in scores)


def test_sampling_defaults_by_thinking_mode(gw, env):
    """Thinking off is greedy; thinking on/default use 0.6 / top_p 0.95 (temperature 0 made thinking loop)."""
    rc = RUN.main(base_args(env, gw, "--suite", str(SAMPLES / "mcq.jsonl"), "--model", "coder",
                            "--limit", "1", "--thinking", "off,on,default"))
    assert rc == 0
    got = [(r["temperature"], r.get("top_p")) for r in gw.requests]
    assert got == [(0.0, 1.0), (0.6, 0.95), (0.6, 0.95)]
    run = json.loads((run_dir(env, "t1-coder-think") / "run.json").read_text())
    assert run["sampling"] == {**RUN.SAMPLING_PROFILES["thinking"], "seed": 1234, "profile": "thinking", "overrides": {}}
    assert json.loads((run_dir(env, "t1-coder") / "run.json").read_text())["sampling"]["profile"] == "off"


def test_explicit_sampling_flags_win(gw, env):
    rc = RUN.main(base_args(env, gw, "--suite", str(SAMPLES / "mcq.jsonl"), "--model", "coder", "--limit", "1",
                            "--thinking", "off,on", "--temperature", "0.2", "--top-p", "0.8"))
    assert rc == 0
    assert [(r["temperature"], r["top_p"]) for r in gw.requests] == [(0.2, 0.8), (0.2, 0.8)]


def test_resume_of_an_old_thinking_run_refuses_new_sampling_defaults(gw, env):
    """A thinking-on run made at temperature 0 must not be resumed silently at the new 0.6 default."""
    args = base_args(env, gw, "--suite", str(SAMPLES / "mcq.jsonl"), "--model", "coder", "--limit", "1",
                     "--thinking", "on")
    assert RUN.main(args + ["--temperature", "0"]) == 0
    with pytest.raises(SystemExit, match="sampling"):
        RUN.main(args)


def test_scores_record_label_source(gw, env, tmp_path):
    it = json.loads((SAMPLES / "mcq.jsonl").read_text().splitlines()[0])
    it["meta"] = dict(it.get("meta") or {}, label_source="nvd")
    f = tmp_path / "lab.jsonl"
    f.write_text(json.dumps(it) + "\n")
    assert RUN.main(base_args(env, gw, "--suite", str(f), "--model", "coder-fast")) == 0
    sc = json.loads((run_dir(env, "t1-coder-fast") / "scores.jsonl").read_text().splitlines()[0])
    assert sc["label_source"] == "nvd"


def _fake_run(d, model, scores, thinking="off"):
    d.mkdir()
    (d / "run.json").write_text(json.dumps({"model": model, "thinking": thinking}))
    (d / "responses.jsonl").write_text("".join(json.dumps({"id": s["id"], "suite": s["suite"]}) + "\n"
                                               for s in scores))
    (d / "scores.jsonl").write_text("".join(json.dumps(s) + "\n" for s in scores))
    return d


def test_mcnemar_exact():
    assert R.mcnemar_exact(0, 0) == 1.0
    assert R.mcnemar_exact(12, 4) == pytest.approx(0.0768, abs=1e-4)
    assert R.mcnemar_exact(2, 8) == pytest.approx(0.1094, abs=1e-4)
    assert R.mcnemar_exact(5, 5) == 1.0


def test_report_paired_comparisons_split_and_opener(tmp_path):
    ids = [f"i{k}" for k in range(6)]
    a_pass = [True, True, True, False, False, True]
    b_pass = [True, False, False, False, True, True]
    labs = ["cna", "cna", "nvd", "nvd", "cna", "nvd"]
    a = _fake_run(tmp_path / "a", "m1", [{"id": i, "suite": "s", "scorer": "exact", "passed": p, "value": float(p),
                                          "status": "ok", "label_source": l} for i, p, l in zip(ids, a_pass, labs)])
    # b predates label_source in scores.jsonl: its labels come from --items
    b = _fake_run(tmp_path / "b", "m2", [{"id": i, "suite": "s", "scorer": "exact", "passed": p, "value": float(p),
                                          "status": "ok"} for i, p in zip(ids, b_pass)])
    items = tmp_path / "s.jsonl"
    items.write_text("".join(json.dumps({"id": i, "meta": {"label_source": l}}) + "\n" for i, l in zip(ids, labs)))
    frr = _fake_run(tmp_path / "f", "m1", [
        {"id": "r1", "suite": "frr", "scorer": "refusal", "passed": True, "value": 1.0, "status": "ok",
         "extra": {"expected": "comply", "opener_refusal": True}},
        {"id": "r2", "suite": "frr", "scorer": "refusal", "passed": True, "value": 1.0, "status": "ok",
         "extra": {"expected": "comply", "opener_refusal": False}}])
    out, pcsv = tmp_path / "r.md", tmp_path / "p.csv"
    assert R.main([str(a), str(b), str(frr), "--split-label-source", "--items", str(items),
                   "--out", str(out), "--pairs-csv", str(pcsv)]) == 0
    md = out.read_text()
    assert "## s [label_source=cna]" in md and "## s [label_source=nvd]" in md
    assert "| s | m1 (think off) [a] | m2 (think off) | 6 | 4 | 3 | 2 | 1 | 1.000 |" in md
    assert "| s [label_source=nvd] | m1 (think off) [a] | m2 (think off) | 3 | 2 | 1 | 1 | 0 | 1.000 |" in md
    assert "- m1 (think off) [f]: 1/2 = 50.0%" in md
    assert pcsv.read_text().splitlines()[0] == ",".join(R.PAIR_COLUMNS)
    # two different runs of the same model and mode get their dir names, so the rows can be told apart
    # no split: no label rows
    assert "label_source=" not in R.render_markdown([R.summarize(a)])


def test_eval_base_url_setting_routes_requests_and_is_recorded(gw, env, monkeypatch):
    monkeypatch.setenv("EVAL_BASE_URL", gw.url)
    args = ["--key-file", env["key"], "--site-env", env["site"], "--results", env["results"], "--backoff", "0",
            "--run-name", "bu", "--no-final-answer", "--suite", str(SAMPLES / "mcq.jsonl"), "--model", "coder",
            "--limit", "1"]
    assert RUN.main(args) == 0
    assert len(gw.requests) == 1
    assert json.loads((run_dir(env, "bu-coder") / "run.json").read_text())["gateway"] == gw.url


def test_report_labels_runs_that_differ_only_by_seed(tmp_path):
    dirs = []
    for seed in (1234, 1235):
        d = tmp_path / f"s{seed}"
        d.mkdir()
        (d / "run.json").write_text(json.dumps({"model": "m", "thinking": "on", "sampling": {"seed": seed}}))
        (d / "responses.jsonl").write_text(json.dumps({"id": "a", "suite": "s"}) + "\n")
        (d / "scores.jsonl").write_text(json.dumps({"id": "a", "suite": "s", "scorer": "exact", "passed": True,
                                                    "value": 1.0, "status": "ok"}) + "\n")
        dirs.append(d)
    same = tmp_path / "same"
    shutil.copytree(dirs[0], same)
    out = tmp_path / "r.md"
    assert R.main([str(d) for d in dirs + [same]] + ["--out", str(out)]) == 0
    md = out.read_text()
    assert "m (think on) seed 1235" in md and "m (think on) seed 1234 [s1234]" in md and "[same]" in md


# ------------------------------------------------------------------ protocol and provenance (2026-10)
def _rj(env, name):
    return json.loads((run_dir(env, name) / "run.json").read_text())


def _scores(env, name):
    return [json.loads(l) for l in (run_dir(env, name) / "scores.jsonl").read_text().splitlines()]


def test_every_sampler_is_sent_so_server_defaults_cannot_leak(gw, env):
    """coder-fast's llama-swap cmd sets --presence-penalty 1.5; the request must override it (and every other knob)."""
    assert RUN.main(base_args(env, gw, "--suite", str(SAMPLES / "mcq.jsonl"), "--model", "coder-fast",
                              "--limit", "1", "--thinking", "off,on")) == 0
    off, on = gw.requests
    for body, prof in ((off, "off"), (on, "thinking")):
        for k, v in RUN.SAMPLING_PROFILES[prof].items():
            assert body[k] == v, k
    assert off["presence_penalty"] == 0.0 and off["repeat_penalty"] == 1.0 and on["top_k"] == 20
    run = _rj(env, "t1-coder-fast")
    assert run["serving"]["sampler_overrides"]["presence_penalty"] == {"server": "1.5", "sent": 0.0}
    assert run["serving"]["samplers_left_to_server"] == []
    assert set(RUN.SAMPLER_KEYS) <= set(run["request_params"]) and "messages" not in run["request_params"]
    assert run["request_params"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_sampler_overrides_are_sent_and_recorded(gw, env):
    assert RUN.main(base_args(env, gw, "--suite", str(SAMPLES / "mcq.jsonl"), "--model", "coder", "--limit", "1",
                              "--sampler", "top_k=5", "--sampler", "min-p=0.05", "--presence-penalty", "0.5")) == 0
    body = gw.requests[0]
    assert body["top_k"] == 5 and isinstance(body["top_k"], int) and body["min_p"] == 0.05
    assert body["presence_penalty"] == 0.5
    assert _rj(env, "t1-coder")["sampling"]["overrides"] == {"top_k": 5, "min_p": 0.05, "presence_penalty": 0.5}
    with pytest.raises(SystemExit):
        RUN.parse_args(["--suite", "x", "--model", "m", "--sampler", "mirostat=2"])


def test_legacy_sampling_reproduces_the_old_request(gw, env):
    assert RUN.main(base_args(env, gw, "--suite", str(SAMPLES / "mcq.jsonl"), "--model", "coder-fast", "--limit", "1",
                              "--legacy-sampling")) == 0
    body = gw.requests[0]
    assert not set(RUN.SAMPLER_KEYS) - {"temperature"} & set(body)
    run = _rj(env, "t1-coder-fast")
    assert run["sampling"] == {"temperature": 0.0, "seed": 1234}
    assert "presence_penalty" in run["serving"]["samplers_left_to_server"]


def test_seeds_give_one_dir_per_seed_and_each_response_records_its_seed(gw, env):
    assert RUN.main(base_args(env, gw, "--suite", str(SAMPLES / "mcq.jsonl"), "--model", "coder", "--limit", "2",
                              "--thinking", "on", "--seeds", "7,8,9")) == 0
    assert sorted(r["seed"] for r in gw.requests) == [7, 7, 8, 8, 9, 9]
    for seed in (7, 8, 9):
        name = f"t1-coder-think-seed{seed}"
        assert _rj(env, name)["sampling"]["seed"] == seed and _rj(env, name)["seed"] == seed
        resp = R.read_jsonl(run_dir(env, name) / "responses.jsonl")
        assert [r["seed"] for r in resp] == [seed, seed] and all(r["request"]["seed"] == seed for r in resp)
        assert all(s["seed"] == seed for s in _scores(env, name))
    # report.py tells them apart by seed
    out = Path(env["results"]) / "r.md"
    assert R.main([str(run_dir(env, f"t1-coder-think-seed{s}")) for s in (7, 8, 9)] + ["--out", str(out)]) == 0
    assert "coder (think on) seed 8" in out.read_text()
    with pytest.raises(SystemExit):
        RUN.parse_args(["--suite", "x", "--model", "m", "--seeds", "1,1"])


def test_budget_policy_is_declared_recorded_and_guards_resume(gw, env, tmp_path):
    pol = tmp_path / "budget.json"
    pol.write_text(json.dumps({"name": "conf-1", "suite_answer_tokens": {"sample-mcq": 300}, "think_budget": 1000}))
    args = base_args(env, gw, "--suite", str(SAMPLES / "mcq.jsonl"), "--model", "coder", "--limit", "1",
                     "--thinking", "off,on", "--budget-policy", str(pol))
    assert RUN.main(args) == 0
    assert [r["max_tokens"] for r in gw.requests] == [300, 1300]
    b = _rj(env, "t1-coder-think")["budget"]
    assert b["policy"]["name"] == "conf-1" and b["think_budget"] == 1000
    assert b["per_suite"]["sample-mcq"] == {"answer": 300, "max_tokens": 1300, "items_with_own_cap": 0}
    assert _rj(env, "t1-coder")["budget"]["per_suite"]["sample-mcq"]["max_tokens"] == 300
    # same policy for two models -> same hash (reports can check the comparison is fair)
    pol.write_text(json.dumps({"name": "conf-2", "suite_answer_tokens": {"sample-mcq": 400}}))
    with pytest.raises(SystemExit, match="budget policy"):
        RUN.main(args)
    with pytest.raises(SystemExit):
        RUN.load_budget_policy(str(tmp_path / "missing.json"))


def test_truncation_is_recorded_per_item(env):
    gw = FakeGateway()
    gw.think_only = True
    try:
        assert RUN.main(base_args(env, gw, "--suite", str(SAMPLES / "mcq.jsonl"), "--model", "coder", "--limit", "2",
                                  "--thinking", "on")) == 0
    finally:
        gw.close()
    sc = _scores(env, "t1-coder-think")
    assert all(s["truncated"] is True and s["finish_reason"] == "length" and s["followup_used"] is False for s in sc)


def test_final_answer_followup_is_off_by_default(gw, env):
    args = ["--key-file", env["key"], "--site-env", env["site"], "--results", env["results"], "--base-url", gw.url,
            "--backoff", "0", "--run-name", "nf", "--suite", str(SAMPLES / "mcq.jsonl"), "--model", "coder-fast",
            "--limit", "3"]
    assert RUN.main(args) == 0
    assert not [r for r in gw.requests if r["messages"][-1]["content"].startswith("Stop here.")]
    assert all(s["followup_used"] is False for s in _scores(env, "nf-coder-fast"))
    assert _rj(env, "nf-coder-fast")["followup"]["enabled"] is False
    with pytest.raises(SystemExit):
        RUN.parse_args(["--suite", "x", "--model", "m", "--final-answer", "--no-final-answer"])


def test_run_json_records_prompts_items_harness_and_unknown_serving(gw, env):
    assert RUN.main(base_args(env, gw, "--suite", str(SAMPLES / "mcq.jsonl"), "--model", "coder", "--limit", "2")) == 0
    run = _rj(env, "t1-coder")
    sent_system = gw.requests[0]["messages"][0]["content"]
    assert run["prompts"]["system_prompt_sha256"]["sample-mcq"] == RUN.sha256_text(sent_system)
    assert run["prompts"]["final_answer_prompt_sha256"] == RUN.sha256_text(RUN.Run.FINAL_ANSWER_PROMPT)
    ids = sorted(r["id"] for r in R.read_jsonl(run_dir(env, "t1-coder") / "responses.jsonl"))
    assert run["item_sets"]["sample-mcq"] == {"n": 2, "ids_sha256": RUN.sha256_text("\n".join(ids))}
    assert set(run["harness_sha256"]) == {"run.py", "scorers.py", "report.py"}
    assert run["scorer_version"]["value"] and run["scorer_version"]["source"]
    f = run["serving"]["fields"]
    # not probed, no static file: from the repo template, or explicitly unknown with a reason
    assert f["quant"] == {"value": "UD-Q4_K_XL", "source": "llama-swap template (the live config may differ) "
                                                           "(parsed from the file name)"}
    assert f["chat_template_sha256"]["value"] is None and "not probed" in f["chat_template_sha256"]["reason"]
    assert f["llama_cpp_build"] == {"value": "b0-test", "source": "response system_fingerprint"}
    assert run["serving"]["probe"] is None and gw.gets == []
    assert run["protocol"]["mode"] == "exploratory"


def test_quant_from_filename():
    q = RUN.quant_from_filename
    assert q("/models/x/Qwen3.6-35B-A3B-UD-IQ4_NL_XL.gguf") == "UD-IQ4_NL_XL"
    assert q("hermes-4_3_36b-Q4_K_M.gguf") == "Q4_K_M"
    assert q("Flash-Next-GSQ-RCO-IQ3_S-00001-of-00002.gguf") == "IQ3_S"
    assert q("model-BF16.gguf") == "BF16" and q("model.bin") is None and q(None) is None


def test_probe_serving_with_a_stub():
    calls = []
    tmpl = "{% for m in messages %}{{ m.content }}{% endfor %}"

    def fetch(url):
        calls.append(url)
        if url.endswith("/v1/models"):
            return {"data": [{"id": "other"}, {"id": "qwen-x", "aliases": ["coder"], "owned_by": "llama-swap"}]}
        if url.endswith("/upstream/coder/props"):
            return {"build_info": "b7001-abcdef0", "model_path": "/home/someone/models/Qwen-Q5_K_M.gguf",
                    "total_slots": 1, "chat_template": tmpl,
                    "default_generation_settings": {"n_ctx": 65536, "params": {
                        "temperature": 0.6, "top_k": 20, "presence_penalty": 1.5, "prompt": "never copied"}}}
        raise RuntimeError("HTTP 404")

    p = RUN.probe_serving(fetch, "http://gw.invalid:8080/v1", "coder")
    assert calls[:2] == ["http://gw.invalid:8080/v1/models", "http://gw.invalid:8080/upstream/coder/props"]
    assert p["models_entry"]["id"] == "qwen-x"
    props = p["props"]
    assert props["model_file"] == "Qwen-Q5_K_M.gguf" and props["build_info"] == "b7001-abcdef0"
    assert props["chat_template_sha256"] == RUN.sha256_text(tmpl) and "prompt" not in props["sampling_defaults"]
    assert "gw.invalid" not in json.dumps(p) and "someone" not in json.dumps(p)
    sv = RUN.resolve_serving("coder", None, p, None, RUN.SAMPLING_PROFILES["off"], True)
    f = sv["fields"]
    assert f["quant"]["value"] == "Q5_K_M" and f["llama_cpp_build"]["value"] == "b7001-abcdef0"
    assert f["ctx_size"]["value"] == 65536
    assert sv["sampler_overrides"]["presence_penalty"] == {"server": 1.5, "sent": 0.0}
    assert "temperature" in sv["sampler_overrides"] and "top_k" in sv["sampler_overrides"]

    # nothing reachable: every field unknown, with a reason
    def down(url):
        raise RuntimeError("connection refused")

    p2 = RUN.probe_serving(down, "http://gw.invalid/v1", "coder")
    assert [t["ok"] for t in p2["tried"]] == [False, False, False] and p2["props"] is None
    f2 = RUN.resolve_serving("coder", None, p2, None, {}, True)["fields"]
    assert all(v["value"] is None and "probe did not report" in v["reason"] for v in f2.values())


def test_probe_server_end_to_end_and_static_config_wins(gw, env, tmp_path):
    gw.get_routes = {"/v1/models": {"data": [{"id": "coder"}]},
                     "/upstream/coder/props": {"build_info": "b9999-live", "model_path": "/m/X-Q8_0.gguf",
                                               "default_generation_settings": {"n_ctx": 4096}}}
    cfg = tmp_path / "serving.json"
    cfg.write_text(json.dumps({"defaults": {"gpu": "2x 24GB"}, "models": {"coder": {"quant": "Q4_K_M-static"}}}))
    assert RUN.main(base_args(env, gw, "--suite", str(SAMPLES / "mcq.jsonl"), "--model", "coder", "--limit", "1",
                              "--probe-server", "--serving-config", str(cfg))) == 0
    assert [g[0] for g in gw.gets][:2] == ["/v1/models", "/upstream/coder/props"]
    assert all(a == f"Bearer {FAKE_KEY}" for _, a in gw.gets)
    sv = _rj(env, "t1-coder")["serving"]
    assert sv["fields"]["quant"] == {"value": "Q4_K_M-static", "source": "serving-config"}
    assert sv["fields"]["llama_cpp_build"] == {"value": "b9999-live", "source": "probe /props"}
    assert sv["fields"]["model_file"]["value"] == "X-Q8_0.gguf"
    assert sv["static"] == {"gpu": "2x 24GB", "quant": "Q4_K_M-static"} and len(sv["static_file"]["sha256"]) == 64
    assert FAKE_KEY not in all_output(env)


def test_verify_samplers_detects_a_gateway_that_drops_keys():
    sent = {**RUN.SAMPLING_PROFILES["off"], "seed": 7}
    used = {**sent, "top_k": 40, "presence_penalty": 1.5}  # what a server would use if top_k/presence were dropped
    fetch = lambda url: [{"id": 0, "prompt": "secret prompt text", "params": used}] if url.endswith(
        "/upstream/coder/slots") else (_ for _ in ()).throw(RuntimeError("404"))  # noqa: E731
    chk = RUN.verify_samplers(fetch, "http://x.invalid/v1", "coder", sent)
    assert chk["ok"] is False and set(chk["mismatch"]) == {"top_k", "presence_penalty"}
    assert "secret" not in json.dumps(chk)
    ok = RUN.verify_samplers(lambda url: {"slots": [{"params": sent}]} if url.endswith("/slots") and "upstream" not in url
                             else (_ for _ in ()).throw(RuntimeError("404")), "http://x.invalid/v1", "coder", sent)
    assert ok["ok"] is True and ok["tried"][0]["ok"] is False
    none = RUN.verify_samplers(lambda url: (_ for _ in ()).throw(RuntimeError("down")), "http://x/v1", "m", sent)
    assert none["ok"] is None and none["reason"]


def test_probe_server_records_the_sampler_check(gw, env):
    gw.get_routes = {"/upstream/coder/slots": [{"id": 0, "params": {**RUN.SAMPLING_PROFILES["off"], "seed": 1234}}]}
    assert RUN.main(base_args(env, gw, "--suite", str(SAMPLES / "mcq.jsonl"), "--model", "coder", "--limit", "1",
                              "--probe-server")) == 0
    chk = _rj(env, "t1-coder")["serving"]["sampler_check"]
    assert chk["ok"] is True and chk["observed"]["seed"] == 1234


# ------------------------------------------------------------------ confirmatory gate (round 2: loopholes L1-L6)
MCQ = SAMPLES / "mcq.jsonl"
MCQ_IDS = [json.loads(l)["id"] for l in MCQ.read_text().splitlines() if l.strip()]
FROZEN_SHA = "f" * 40


def _clean_git(monkeypatch, sha=FROZEN_SHA, **kw):
    monkeypatch.setattr(RUN, "git_info", lambda: {"sha": sha, "branch": "x", "dirty": False, "untracked": False, **kw})
    monkeypatch.setattr(RUN, "git_rev", lambda ref: FROZEN_SHA if ref in (FROZEN_SHA, "eval-protocol-test") else None)
    # the manifests and serving record live in tmp_path, not in a commit: those checks have their own tests
    monkeypatch.setattr(RUN, "manifest_problem", lambda path, frozen, head: None)
    monkeypatch.setattr(RUN, "in_repo", lambda path: True)
    monkeypatch.setattr(RUN, "pycache_dirs", lambda: [])   # the test process itself may have left bytecode


@pytest.fixture(autouse=True)
def _sealed_manifest_in_tmp(tmp_path, monkeypatch):
    """Every test's sealed manifest is tmp_path/sealed-heldout.json (written by _conf), never the real one."""
    monkeypatch.setattr(RUN, "HELDOUT_MANIFEST", tmp_path / "sealed-heldout.json")
    monkeypatch.setattr(RUN, "HELDOUT_MANIFEST_V11", tmp_path / "sealed-heldout-v1.1.json")
    monkeypatch.setattr(RUN, "ATTEMPTS_LEDGER", tmp_path / "attempts-C1.jsonl")


def _sealed_entries(items, ids):
    audit = RUN._audit()  # evals/datasets/audit.py: the sealed key; content binding as in heldout-v1.1
    return [f"{i}\t{audit.heldout_key(items[i])}\t{RUN.content_key(audit, items[i])}" for i in ids]


def _conf(tmp_path, ids=None, heldout=True, items_file=MCQ, sealed=True):
    """A complete confirmatory setup: the sealed manifest (content-bound entries) and a C1-style freeze manifest
    pinning files/ids/plan/ref, a plan, a serving config, and --probe-server."""
    ids = MCQ_IDS if ids is None else ids
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"protocol": "test", "contestants": ["coder", "coder-fast"], "families": []}))
    items = {json.loads(l)["id"]: json.loads(l) for l in Path(items_file).read_text().splitlines() if l.strip()}
    suite = {"n": len(ids), "ids_sha256": RUN.sha256_text("\n".join(sorted(ids))), "ids": list(ids)}
    cfg = tmp_path / "serving.json"
    cfg.write_text(json.dumps({"quant": "Q4_K_M", "llama_cpp_build": "b1"}))
    fz = tmp_path / "freeze.json"
    fz.write_text(json.dumps({"files": {RUN.rel(Path(items_file)): RUN.sha256_file(Path(items_file))},
                              "suites": {"sample-mcq": suite}, "comparisons_sha256": RUN.sha256_file(plan),
                              "harness_ref": FROZEN_SHA, "harness_sha256": RUN.harness_sha256(),
                              "serving_config_sha256": RUN.sha256_file(cfg)}))
    out = ["--protocol", "confirmatory", "--freeze", str(fz), "--protocol-plan", str(plan),
           "--serving-config", str(cfg), "--probe-server", "--select-frozen"]
    if sealed:
        sm = tmp_path / "sealed-heldout.json"
        sm.write_text(json.dumps({"version": "heldout-test", "suites": {"sample-mcq": {
            "n_sealed": len(ids), "entries": _sealed_entries(items, ids)}}}))
        out += ["--freeze", str(sm)]
    return out


def _ids_failed(capsys):
    import re as _re
    return set(_re.findall(r"\[(\w+)\]", capsys.readouterr().err))


def test_confirmatory_complete_setup_runs_and_records(gw, env, tmp_path, monkeypatch):
    _clean_git(monkeypatch)
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *_conf(tmp_path))) == 0
    prot = _rj(env, "t1-coder")["protocol"]
    assert prot["mode"] == "confirmatory" and prot["checks_failed"] == [] and prot["deviations"] == []
    assert prot["plan"]["contestants"] == ["coder", "coder-fast"] and len(prot["freeze"]["sha256"]) == 64
    # r4 E1: no --frozen-ref needed; the manifest's full SHA is the frozen ref, recorded as resolved
    assert prot["frozen_ref"] is None and prot["harness_ref"] == FROZEN_SHA and prot["frozen_sha"] == FROZEN_SHA


@pytest.mark.parametrize("extra,check", [
    (["--sampler", "repeat_penalty=1.3"], "sampler_override"),
    (["--presence-penalty", "1.5"], "sampler_override"),
    (["--top-p", "0.5"], "sampler_override"),
    (["--temperature", "0.3"], "sampler_override"),
    (["--scorer-override", "sample-mcq=regex"], "scorer_override"),
    (["--think-budget", "60000"], "think_budget"),
    (["--allow-self-grade"], "allow_self_grade"),
    (["--thinking", "on", "--temperature", "0", "--seeds", "1,2,3"], "thinking_greedy"),
    (["--thinking", "on"], "seeds"),
    (["--limit", "2"], "limit"),
    (["--frozen-ref", "some-other-tag"], "frozen_ref"),
])
def test_L1_every_knob_is_a_failed_check(gw, env, tmp_path, monkeypatch, capsys, extra, check):
    _clean_git(monkeypatch)
    rc = RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *_conf(tmp_path), *extra))
    assert rc == 2 and check in _ids_failed(capsys) and gw.requests == []


def test_L1_budget_policy_must_be_the_frozen_one(gw, env, tmp_path, monkeypatch, capsys):
    _clean_git(monkeypatch)
    pol = tmp_path / "pol.json"
    pol.write_text(json.dumps({"think_budget": 100}))
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *_conf(tmp_path),
                              "--budget-policy", str(pol))) == 2
    assert "budget_policy" in _ids_failed(capsys)


def test_L1_untracked_files_and_unfrozen_plan_and_unpinned_files_fail(gw, env, tmp_path, monkeypatch, capsys):
    _clean_git(monkeypatch, untracked=True)
    args = _conf(tmp_path)
    fz = Path(args[args.index("--freeze") + 1])
    man = json.loads(fz.read_text())
    man.pop("comparisons_sha256")
    man["files"] = {}
    fz.write_text(json.dumps(man))
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *args)) == 2
    assert {"git_untracked", "plan_unfrozen", "files_unpinned"} <= _ids_failed(capsys)


def test_L1_grader_must_not_be_a_protocol_contestant_or_relative(tmp_path):
    """The contestant list is the protocol's, not just this invocation's: coder-fast grading a coder-only run fails."""
    plan = RUN.load_plan(None) or {"contestants": ["coder", "coder-fast", "vision"], "sha256": "x"}
    git = {"sha": "a", "dirty": False, "untracked": False}

    def ids(grader):
        a = RUN.parse_args(["--suite", "x", "--model", "coder", "--protocol", "confirmatory", "--grader-model", grader])
        return {f["id"] for f in RUN.protocol_checks(a, [], [], git, plan, n_judge=1)}
    assert "grader_contestant" in ids("coder-fast")
    assert "grader_family" in ids("big") and "grader_contestant" not in ids("big")      # qwen, like coder
    assert "grader_family" in ids("mystery-model")                                      # unknown family: fail closed
    assert not {"grader_contestant", "grader_family"} & ids("claude")


def test_L2_heldout_items_are_verified_by_content_key(tmp_path):
    import audit
    items = [json.loads(l) for l in MCQ.read_text().splitlines() if l.strip()][:2]
    man = tmp_path / "h.json"
    man.write_text(json.dumps({"suites": {"sample-mcq": {"n_sealed": 2, "entries": [
        f"{it['id']}\t{audit.heldout_key(it)}" for it in items]}}}))
    assert RUN.check_freeze(str(man), [], items) == []
    edited = [dict(items[0], prompt="EDITED after seeing outputs"), items[1]]
    probs = RUN.check_freeze(str(man), [], edited)
    assert [c for c, _ in probs] == ["freeze_content"] and items[0]["id"] in probs[0][1]
    # bare-id manifests and the other checks still work
    assert any(c == "freeze_ids" for c, _ in RUN.check_freeze(str(man), [], items[:1]))
    other = items + [dict(items[0], id="x", suite="new")]
    assert any("not in the freeze" in m for _, m in RUN.check_freeze(str(man), [], other))


def test_L2_end_to_end_content_check_refuses_an_edited_item(gw, env, tmp_path, monkeypatch, capsys):
    _clean_git(monkeypatch)
    args = _conf(tmp_path, ids=MCQ_IDS[:3], heldout=True)
    lines = MCQ.read_text().splitlines()
    it = json.loads(lines[0])
    it["prompt"] += " (edited)"
    edited = tmp_path / "mcq.jsonl"
    edited.write_text("\n".join([json.dumps(it)] + lines[1:]) + "\n")
    fz = Path(args[args.index("--freeze") + 1])
    man = json.loads(fz.read_text())
    man["files"] = {RUN.rel(edited): RUN.sha256_file(edited)}  # even with the edited file pinned
    fz.write_text(json.dumps(man))
    assert RUN.main(base_args(env, gw, "--suite", str(edited), "--model", "coder", *args)) == 2
    assert "freeze_content" in _ids_failed(capsys)


def test_L3_exploratory_peek_cannot_be_resumed_as_confirmatory(gw, env, tmp_path, monkeypatch):
    _clean_git(monkeypatch)
    args = _conf(tmp_path)
    explo = [a for a in args if a not in ("--protocol", "confirmatory")]
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *explo)) == 0
    with pytest.raises(SystemExit, match="protocol mode"):
        RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *args))


def test_L4_confirmatory_resume_across_harness_commits_is_refused(gw, env, tmp_path, monkeypatch):
    args = _conf(tmp_path)
    _clean_git(monkeypatch)
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *args)) == 0
    # HEAD moved on by an allowed post-freeze commit (stage 2): the resume guard still refuses the SHA change
    monkeypatch.setattr(RUN, "git_info", lambda: {"sha": "b" * 40, "branch": "x", "dirty": False, "untracked": False})
    monkeypatch.setattr(RUN, "post_freeze_problems", lambda f, h: ([], [RUN.STAGE2_PREFIX + "x.json"]))
    with pytest.raises(SystemExit, match="harness SHA"):
        RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *args))


def test_L5_rescore_makes_the_run_exploratory_and_keeps_confirmatory_scores(gw, env, tmp_path, monkeypatch):
    _clean_git(monkeypatch)
    args = _conf(tmp_path)
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *args)) == 0
    d = run_dir(env, "t1-coder")
    before = (d / "scores.jsonl").read_text()
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", "--rescore")) == 0
    prot = _rj(env, "t1-coder")["protocol"]
    assert prot["mode"] == "rescored-exploratory" and prot["original"]["mode"] == "confirmatory"
    assert (d / "scores.confirmatory.jsonl").read_text() == before
    # a confirmatory run whose harness changed is not rescored silently
    d2 = tmp_path / "r2"
    env2 = dict(env, results=str(d2))
    assert RUN.main(base_args(env2, gw, "--suite", str(MCQ), "--model", "coder", *args)) == 0
    monkeypatch.setattr(RUN, "git_info", lambda: {"sha": "c" * 40, "branch": "x", "dirty": True, "untracked": True})
    with pytest.raises(SystemExit, match="rescore-exploratory"):
        RUN.main(base_args(env2, gw, "--suite", str(MCQ), "--model", "coder", "--rescore",
                           "--scorer-override", "sample-mcq=regex"))
    assert RUN.main(base_args(env2, gw, "--suite", str(MCQ), "--model", "coder", "--rescore",
                              "--rescore-exploratory")) == 0
    assert _rj(env2, "t1-coder")["rescored"][-1]["changed"] == ["harness SHA"]


def test_L6_each_failed_check_needs_its_own_deviation(gw, env, tmp_path, monkeypatch, capsys):
    """Only force_resume, serving_unrecorded and limit are waivable (R4); each needs its own deviation."""
    _clean_git(monkeypatch)
    base = base_args(env, gw, "--suite", str(MCQ), "--model", "coder",
                     *[x for x in _conf(tmp_path) if x != "--probe-server"], "--force-resume")
    assert RUN.main(base + ["--deviation", "x"]) == 2                                  # free text: refused
    assert RUN.main(base + ["--deviation", "force_resume=hardware stop"]) == 2         # serving_unrecorded unexcused
    assert "serving_unrecorded" in _ids_failed(capsys)
    ok = ["--deviation", "force_resume=hardware stop", "--deviation", "serving_unrecorded=probe down"]
    assert RUN.main(base + ok + ["--deviation", "limit=not failing"]) == 2           # a check that passed
    assert RUN.main(base + ok + ["--deviation", "seeds=x"]) == 2                     # an estimand check: never
    assert RUN.main(base + ok) == 0
    prot = _rj(env, "t1-coder")["protocol"]
    assert {d["id"]: d["text"] for d in prot["deviations"]} == {"force_resume": "hardware stop",
                                                                "serving_unrecorded": "probe down"}
    assert {f["id"] for f in prot["checks_failed"]} == {"force_resume", "serving_unrecorded"}


def test_R3_N8_integrity_checks_cannot_be_waived(gw, env, tmp_path, monkeypatch, capsys):
    """Inverted r3 N8: a dirty tree, a later commit, --limit (changes the frozen ids): no deviation accepts them."""
    monkeypatch.setattr(RUN, "git_info", lambda: {"sha": "d" * 40, "branch": "x", "dirty": True, "untracked": True})
    monkeypatch.setattr(RUN, "git_rev", lambda ref: FROZEN_SHA)
    monkeypatch.setattr(RUN, "git_changed", lambda a, b: ["evals/run.py"])
    monkeypatch.setattr(RUN, "manifest_problem", lambda *a: None)
    devs = ["--deviation", "git_dirty=wip", "--deviation", "git_untracked=wip", "--deviation", "frozen_ref=later fix",
            "--deviation", "limit=subset", "--deviation", "freeze_ids=subset"]
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *_conf(tmp_path), "--limit", "2",
                              *devs)) == 2
    err = capsys.readouterr().err
    assert "git_dirty is an integrity or estimand check" in err and "freeze_ids is an integrity or estimand" in err
    assert gw.requests == []
    assert RUN.NON_WAIVABLE >= {"git_dirty", "frozen_ref", "freeze_ids", "freeze_content", "plan_unfrozen",
                                "sealed_only"}


def test_select_frozen_runs_exactly_the_listed_ids(gw, env, tmp_path, monkeypatch):
    _clean_git(monkeypatch)
    ids = [MCQ_IDS[1], MCQ_IDS[3]]
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *_conf(tmp_path, ids=ids))) == 0
    assert sorted(r["id"] for r in R.read_jsonl(run_dir(env, "t1-coder") / "responses.jsonl")) == sorted(ids)
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", "--select-frozen",
                              "--run-name", "t2")) == 2


def test_exploratory_run_on_sealed_items_warns_loudly_and_records_it(gw, env, tmp_path, monkeypatch, capsys):
    man = tmp_path / "heldout.json"
    man.write_text(json.dumps({"suites": {"sample-mcq": {"n_sealed": 1, "entries": [f"{MCQ_IDS[0]}\tk"]}}}))
    monkeypatch.setattr(RUN, "HELDOUT_MANIFEST", man)
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", "--limit", "2")) == 0
    assert "SEALED held-out" in capsys.readouterr().err
    assert _rj(env, "t1-coder")["protocol"]["sealed_items_read"] == 1


# ------------------------------------------------------------------ judge defaults (round 2)
def test_llm_judge_items_need_an_explicit_grader(gw, env, capsys):
    rc = RUN.main(base_args(env, gw, "--suite", str(SAMPLES / "code.jsonl"), "--model", "coder"))
    err = capsys.readouterr().err
    assert rc == 2 and "no default" in err and "model family" in err and gw.requests == []


def test_grader_cache_key_includes_the_served_identity(tmp_path):
    a = RUN.parse_args(["--suite", "x", "--model", "coder", "--grader-model", "big"])
    ident = RUN.grader_identity("big", a)
    assert ident["model_file"].endswith(".gguf") and ident["quant"]
    base = {"model": "big", "thinking": "off", "temperature": 0, "prompt": "p", "prompt_sha256": "s",
            "max_candidate_chars": 1}
    with_id = RUN.judge_cfg({**base, "served_identity": {"big": ident}}, "big")
    assert RUN.grader_cache_cfg(with_id)["served_identity"] == ident
    other = RUN.judge_cfg({**base, "served_identity": {"big": dict(ident, quant="Q8_0")}}, "big")
    assert RUN.grader_cache_cfg(other) != RUN.grader_cache_cfg(with_id)
    assert RUN.grader_identity("judge-unknown", a) is None  # nothing known: the key is unchanged (old caches match)
    assert "served_identity" not in RUN.grader_cache_cfg(RUN.judge_cfg({**base, "served_identity": {}}, "x"))


def test_two_manifests_select_the_intersection_and_check_content(tmp_path):
    """The sealed split (entries with content keys) plus a C1 pool manifest (a subset of ids): the run is the
    intersection, and the content keys from the sealed manifest are still verified."""
    import audit
    items = [json.loads(l) for l in MCQ.read_text().splitlines() if l.strip()][:3]
    sealed = tmp_path / "sealed.json"
    sealed.write_text(json.dumps({"suites": {"sample-mcq": {"n_sealed": 3, "entries": [
        f"{it['id']}\t{audit.heldout_key(it)}" for it in items]}}}))
    pool = tmp_path / "pool.json"
    pool.write_text(json.dumps({"suites": {"sample-mcq": {"ids": [items[0]["id"], items[2]["id"]]}}}))
    assert RUN.frozen_ids([str(sealed), str(pool)])["sample-mcq"] == {items[0]["id"], items[2]["id"]}
    sel = [items[0], items[2]]
    assert RUN.check_freeze([str(sealed), str(pool)], [], sel) == []
    assert [c for c, _ in RUN.check_freeze([str(sealed), str(pool)], [], [dict(items[0], prompt="x"), items[2]])] \
        == ["freeze_content"]


# ------------------------------------------------------------------ protocol C1 <-> comparisons-C1.json
C1_PLAN = EVALS / "plans" / "comparisons-C1.json"
PROTOCOL_MD = EVALS / "PROTOCOL.md"


def _register_rows():
    text = PROTOCOL_MD.read_text(encoding="utf-8")
    block = text.split("<!-- C1-REGISTER-START -->", 1)[1].split("<!-- C1-REGISTER-END -->", 1)[0]
    rows = [l.strip() for l in block.splitlines() if l.strip().startswith("|")][2:]
    return [[c.strip() for c in r.strip("|").split("|")] for r in rows]


def _side(x):
    return x if isinstance(x, str) else f"{x[0].rsplit('-seed', 1)[0]} ×3 seeds"


def test_comparisons_c1_agrees_with_the_protocol_register():
    plan = json.loads(C1_PLAN.read_text(encoding="utf-8"))
    want = []
    for fam in plan["families"]:
        for c in fam["comparisons"]:
            want.append([c["id"], fam["test"], c["suite"], c["metric"], c["direction"],
                         str(c["margin"]) if "margin" in c else "–", _side(c["a"]), _side(c["b"]),
                         ", ".join(c["primary"]["exclude"]) or "–"])
    assert _register_rows() == want


def test_comparisons_c1_pins_exactly_one_primary_and_is_loadable():
    plan = R.load_comparisons(C1_PLAN)  # the report's own validation (ops, metrics, tests)
    assert plan["protocol"] == "C1" and len(plan["contestants"]) == 6
    for fam in plan["families"]:
        for c in fam["comparisons"]:
            spec = R.primary_spec(plan, c)
            assert spec["errors"] == "fail" and spec["impute"] == "baseline" and spec["constant"] == 7.5
            assert isinstance(c["primary"]["exclude"], list) and c["sensitivity"]
            if c["direction"] == "equivalent":
                assert c["margin"] in (0.05, 0.10)
            if c["metric"] in ("accuracy", "accuracy_either") and c["suite"].startswith("nvd-cwe"):
                assert "no_name_in_text" in c["primary"]["exclude"]   # the no-name stratum is primary for CWE
    # owner decisions 2026-10-11: D1 and D7 are exploratory (in no confirmatory family); D4's margin is 0.05
    assert {f["name"] for f in plan["families"]} == {"D2", "D3", "D4", "D5", "D6", "D8"}
    assert {f["name"]: f["test"] for f in plan["exploratory_families"]} == {"D1": "iut", "D7": "holm"}
    assert {c["margin"] for f in plan["families"] if f["name"] == "D4" for c in f["comparisons"]} == {0.05}
    assert "calibration_verdict" not in plan   # no judge calibration is required for C1
    assert set(plan["arms"]["arms"]) == {"b", "c"}
    d6 = next(f for f in plan["families"] if f["name"] == "D6")
    assert {c["metric"] for c in d6["comparisons"]} == {"refusal_refuse", "refusal_full"}
    d3 = next(f for f in plan["families"] if f["name"] == "D3")
    assert {c["metric"] for c in d3["comparisons"]} == {"mae"} and d3["test"] == "iut"


def test_comparisons_c1_suites_have_sealed_items():
    """No confirmatory comparison on a suite with 0 sealed items (e.g. cybermetric-500 in D5)."""
    man = json.loads((EVALS / "datasets" / "heldout" / "heldout-v1.json").read_text())
    plan = json.loads(C1_PLAN.read_text())
    for fam in plan["families"]:
        for c in fam["comparisons"]:
            if c["suite"].endswith("-prospective"):
                assert "nvd-recent" in man.get("prospective", {})
                continue
            assert man["suites"][c["suite"]]["n_sealed"] > 0, c["id"]


def test_comparisons_c1_contestants_are_the_protocols_and_block_contestant_judges():
    plan = RUN.load_plan(str(C1_PLAN))
    git = {"sha": "a", "dirty": False, "untracked": False}
    a = RUN.parse_args(["--suite", "x", "--model", "coder", "--protocol", "confirmatory", "--grader-model", "vision"])
    assert a.plan["sha256"] == plan["sha256"]   # confirmatory defaults to the C1 plan
    ids = {f["id"] for f in RUN.protocol_checks(a, [], [], git, plan, n_judge=1)}
    assert "grader_contestant" in ids   # vision is a C1 contestant though not in this invocation


# ------------------------------------------------------------------ round 3: r3-leakage loopholes, inverted
def test_R3_N1_copied_responses_in_a_fresh_dir_are_refused(gw, env, tmp_path, monkeypatch):
    """Peek exploratory, copy responses.jsonl into the confirmatory run dir: refused (outputs without run.json)."""
    _clean_git(monkeypatch)
    args = _conf(tmp_path)
    explo = [a for a in args if a not in ("--protocol", "confirmatory")]
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *explo, "--run-name", "peek")) == 0
    n_peek = len(gw.requests)
    dst = run_dir(env, "c1-coder")
    dst.mkdir(parents=True)
    shutil.copy(run_dir(env, "peek-coder") / "responses.jsonl", dst / "responses.jsonl")
    with pytest.raises(SystemExit, match="run.json does not"):
        RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *args, "--run-name", "c1"))
    assert len(gw.requests) == n_peek


def test_R3_N1b_deleting_run_json_does_not_launder_a_peek(gw, env, tmp_path, monkeypatch):
    _clean_git(monkeypatch)
    args = _conf(tmp_path)
    explo = [a for a in args if a not in ("--protocol", "confirmatory")]
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *explo)) == 0
    (run_dir(env, "t1-coder") / "run.json").unlink()
    with pytest.raises(SystemExit, match="run.json does not"):
        RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *args))


def test_R3_confirmatory_resume_needs_untouched_outputs(gw, env, tmp_path, monkeypatch):
    """A clean confirmatory resume works; one whose outputs were edited outside the runner is refused."""
    _clean_git(monkeypatch)
    args = _conf(tmp_path)
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *args)) == 0
    rj = _rj(env, "t1-coder")
    assert rj["outputs_sha256"] == rj["protocol"]["outputs_sha256"]
    assert rj["outputs_sha256"]["responses.jsonl"] == RUN.sha256_file(run_dir(env, "t1-coder") / "responses.jsonl")
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *args)) == 0   # resume: fine
    with (run_dir(env, "t1-coder") / "responses.jsonl").open("a") as fh:
        fh.write(json.dumps({"id": MCQ_IDS[0], "suite": "sample-mcq", "content": "Answer: B"}) + "\n")
    with pytest.raises(SystemExit, match="changed outside the runner"):
        RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *args))


def test_R3_N2_confirmatory_needs_the_sealed_manifest_and_sealed_items(gw, env, tmp_path, monkeypatch, capsys):
    _clean_git(monkeypatch)
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *_conf(tmp_path, sealed=False))) == 2
    assert "sealed_only" in _ids_failed(capsys)
    # sealed manifest present, but an item outside it, and one that is a near-copy of a dev item
    audit = RUN._audit()
    items = {json.loads(l)["id"]: json.loads(l) for l in MCQ.read_text().splitlines() if l.strip()}
    a = RUN.parse_args(["--suite", "x", "--model", "coder", "--protocol", "confirmatory"])
    sm = tmp_path / "sealed-heldout.json"
    sm.write_text(json.dumps({"suites": {"sample-mcq": {"entries": _sealed_entries(items, MCQ_IDS[:2])}}}))
    a.freeze = [str(sm)]
    fails = []
    sel = [items[MCQ_IDS[0]], dict(items[MCQ_IDS[1]], meta={"rigor": {"dev_neardup_exposed": True}}),
           items[MCQ_IDS[2]]]
    RUN._sealed_check(a, sel, lambda cid, msg: fails.append((cid, msg)))
    assert [c for c, _ in fails] == ["sealed_only", "sealed_only"]
    assert "not sealed" in fails[0][1] and "dev_neardup_exposed" in fails[1][1]
    assert audit is not None


def test_R3_N3_manifest_harness_ref_wins_over_the_flag(gw, env, tmp_path, monkeypatch, capsys):
    later = "e" * 40
    monkeypatch.setattr(RUN, "git_info", lambda: {"sha": later, "branch": "x", "dirty": False, "untracked": False})
    monkeypatch.setattr(RUN, "git_rev", lambda ref: {"eval-protocol-test": FROZEN_SHA, "HEAD": later}.get(ref))
    monkeypatch.setattr(RUN, "git_changed", lambda a, b: ["evals/scorers.py"])
    monkeypatch.setattr(RUN, "manifest_problem", lambda *a: None)
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *_conf(tmp_path),
                              "--frozen-ref", "HEAD")) == 2
    assert "frozen_ref" in _ids_failed(capsys)


def test_R3_stage2_commit_on_top_of_the_frozen_ref_is_allowed_and_recorded(gw, env, tmp_path, monkeypatch):
    later = "e" * 40
    monkeypatch.setattr(RUN, "git_info", lambda: {"sha": later, "branch": "x", "dirty": False, "untracked": False})
    _clean_git(monkeypatch, sha=later)
    monkeypatch.setattr(RUN, "post_freeze_problems",
                        lambda f, h: ([], [RUN.STAGE2_PREFIX + "freeze-C1-stage2.json"]))
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *_conf(tmp_path))) == 0
    prot = _rj(env, "t1-coder")["protocol"]
    assert prot["checks_failed"] == [] and prot["stage2"] == [RUN.STAGE2_PREFIX + "freeze-C1-stage2.json"]


def test_R3_N4_freeze_manifests_must_be_committed_at_the_frozen_ref(tmp_path):
    assert "outside the repo" in RUN.manifest_problem(str(tmp_path / "freeze.json"), FROZEN_SHA, FROZEN_SHA)
    # a tracked file compared with its committed version at HEAD (this repo): identical -> fine
    head = RUN.git_rev("HEAD")
    if head:
        tracked = RUN.REPO_DIR / "evals" / "scorers.py"
        import subprocess
        same = subprocess.run(["git", "show", f"HEAD:evals/scorers.py"], cwd=RUN.REPO_DIR,
                              capture_output=True).stdout == tracked.read_bytes()
        assert (RUN.manifest_problem(str(tracked), head, head) is None) == same
        assert "not committed" in (RUN.manifest_problem(str(RUN.REPO_DIR / "evals" / "no-such.json"), head, head)
                                   or "not committed")


def test_R3_N4_end_to_end_uncommitted_manifest_fails(gw, env, tmp_path, monkeypatch, capsys):
    _clean_git(monkeypatch)
    monkeypatch.setattr(RUN, "manifest_problem", lambda p, f, h: "outside the repo")
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *_conf(tmp_path))) == 2
    assert "freeze_untracked" in _ids_failed(capsys)


def test_R3_N5_cve_items_need_a_content_binding(tmp_path):
    audit = RUN._audit()
    it = {"id": "nvd-cvss-9999", "suite": "nvd-cvss", "type": "classify", "prompt": "CVE description: orig",
          "answer": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", "meta": {"cve": "CVE-2026-12345"}}
    edited = dict(it, prompt="CVE description: EDITED after peeking", answer="CVSS:3.1/AV:L/AC:H/PR:H/UI:R/S:U/C:N/I:N/A:L")
    v1 = tmp_path / "h.json"   # heldout-v1 style: the key is the CVE id only -> refused as unbound
    v1.write_text(json.dumps({"suites": {"nvd-cvss": {"n_sealed": 1, "entries": [f"{it['id']}\t{audit.heldout_key(it)}"]}}}))
    assert [c for c, _ in RUN.check_freeze(str(v1), [], [edited])] == ["freeze_content"]
    v11 = tmp_path / "h11.json"  # v1.1: content-bound -> the original passes, the edit fails
    v11.write_text(json.dumps({"suites": {"nvd-cvss": {"n_sealed": 1, "entries": [
        f"{it['id']}\t{audit.heldout_key(it)}\t{RUN.content_key(audit, it)}"]}}}))
    assert RUN.check_freeze(str(v11), [], [it]) == []
    assert [c for c, _ in RUN.check_freeze(str(v11), [], [edited])] == ["freeze_content"]


def test_R3_N6_rescore_leaves_the_confirmatory_hashes_that_no_longer_match(gw, env, tmp_path, monkeypatch):
    """After a rescore, restoring protocol.original by hand brings back outputs_sha256 that no longer match
    scores.jsonl, which report.py's gate checks (STATS)."""
    _clean_git(monkeypatch)
    args = _conf(tmp_path)
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *args)) == 0
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", "--rescore",
                              "--scorer-override", "sample-mcq=regex")) == 0
    rj = _rj(env, "t1-coder")
    forged = rj["protocol"]["original"]
    assert forged["mode"] == "confirmatory"
    now = RUN.outputs_sha256(run_dir(env, "t1-coder"))
    assert forged["outputs_sha256"]["scores.jsonl"] != now["scores.jsonl"]
    assert rj["outputs_sha256"] == now


def test_R3_unstripped_nvd_items_are_refused():
    audit = RUN._audit()
    it = {"id": "nvd-cvss-1", "suite": "nvd-cvss", "type": "extract", "scorer": "cvss_mae",
          "prompt": "CVE description: A flaw with high impact on confidentiality allows remote attackers to read files.",
          "answer": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N", "meta": {}}
    fails = []
    RUN._unstripped_check([it], lambda c, m: fails.append(c))
    assert fails == ["freeze_unstripped"]
    fails.clear()
    RUN._unstripped_check([dict(it, meta={"rigor": {"leak_stripped": ["unstrippable"]}})], lambda c, m: fails.append(c))
    assert fails == [] and audit is not None


def test_R3_stopping_rule_more_than_5pct_failures_invalidates_the_run(gw, env, tmp_path, monkeypatch):
    _clean_git(monkeypatch)
    gw.plan = {i: 400 for i in range(0, 200)}   # non-retryable failures from the first item on
    rc = RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *_conf(tmp_path), "--retries", "0"))
    assert rc == 4
    prot = _rj(env, "t1-coder")["protocol"]
    assert "more than 5%" in prot["invalid"]
    assert len([r for r in gw.requests if "messages" in r]) < len(MCQ_IDS)   # it stopped early
    with pytest.raises(SystemExit, match="stopped as invalid"):
        RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *_conf(tmp_path)))


def test_R3_stopping_rule_serving_identity_change(gw, env, tmp_path, monkeypatch):
    _clean_git(monkeypatch)
    calls = {"n": 0}
    orig = gw.answer

    def answer(body):
        calls["n"] += 1
        return orig(body)
    gw.answer = answer
    real_fill = RUN.Run._fill

    def fill(self, rec, data):
        real_fill(self, rec, data)
        if calls["n"] >= 3:
            rec["system_fingerprint"] = "b9-rebuilt"
    monkeypatch.setattr(RUN.Run, "_fill", fill)
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *_conf(tmp_path))) == 4
    assert "serving identity changed" in _rj(env, "t1-coder")["protocol"]["invalid"]


def test_R3_probe_server_is_required_in_confirmatory(gw, env, tmp_path, monkeypatch, capsys):
    _clean_git(monkeypatch)
    args = [a for a in _conf(tmp_path) if a != "--probe-server"]
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *args)) == 2
    assert "serving_unrecorded" in _ids_failed(capsys)


def test_R3_pinned_interventions(gw, env, tmp_path, monkeypatch):
    table = json.loads(RUN.INTERVENTIONS.read_text())["interventions"]
    d6 = table["D6-defensive-system-prompt"]
    assert RUN.sha256_text(d6["text"]) == d6["sha256"]
    assert table["D8-tuned-flash"]["cmd_sha256"] != table["D8-untuned-flash"]["cmd_sha256"]
    # D6: applied as a run option to every item, recorded
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder-fast", "--limit", "2",
                              "--intervention", "D6-defensive-system-prompt")) == 0
    assert all(r["messages"][0]["content"] == d6["text"] for r in gw.requests)
    assert _rj(env, "t1-coder-fast")["protocol"]["interventions"][0]["sha256"] == d6["sha256"]
    # D8: the repo template is the tuned flash; the untuned intervention needs the untuned live config
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", "--limit", "1", "--run-name", "t8",
                              "--intervention", "D8-tuned-flash")) == 0
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", "--limit", "1", "--run-name", "t9",
                              "--intervention", "D8-untuned-flash")) == 2
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", "--limit", "1", "--run-name", "t10",
                              "--intervention", "no-such")) == 2


def test_R3_family_is_resolved_from_the_served_model_file(tmp_path):
    """An alias that hides its lineage ('judge-x') served from a Qwen weights file overlaps a Qwen contestant."""
    cfg = tmp_path / "serving.json"
    cfg.write_text(json.dumps({"models": {"judge-x": {"model_file": "Qwen3-32B-Q8_0.gguf"}}}))
    a = RUN.parse_args(["--suite", "x", "--model", "coder", "--grader-model", "judge-x", "--serving-config", str(cfg)])
    pairs = RUN.family_pairs(["judge-x"], ["coder"], a)
    assert any(p["grader"] == "judge-x" and "family" in p["reason"] for p in pairs)
    clean = tmp_path / "serving2.json"
    clean.write_text(json.dumps({"models": {"judge-x": {"model_file": "gemma-4-31B-Q4.gguf"}}}))
    b = RUN.parse_args(["--suite", "x", "--model", "coder", "--grader-model", "judge-x", "--serving-config", str(clean)])
    assert not any("family" in p["reason"] for p in RUN.family_pairs(["judge-x"], ["coder"], b))


def test_R3_make_c1_generates_the_freeze_manifest_and_the_run_script(tmp_path):
    sys.path.insert(0, str(EVALS / "plans"))
    import make_c1
    items = [json.loads(l) for l in MCQ.read_text().splitlines() if l.strip()]
    data = tmp_path / "data"
    data.mkdir()
    items[1]["meta"] = {"rigor": {"dev_neardup_exposed": True}}
    (data / "sample-mcq.jsonl").write_text("".join(json.dumps(it) + "\n" for it in items))
    sealed = tmp_path / "sealed.json"
    sealed.write_text(json.dumps({"suites": {"sample-mcq": {"entries": [f"{it['id']}\tk" for it in items[:3]]}}}))
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"contestants": ["coder"], "families": [{"name": "F", "comparisons": [
        {"id": "F.1", "suite": "sample-mcq"}, {"id": "F.2", "suite": "nvd-cwe-prospective"}]}]}))
    with pytest.raises(ValueError):
        make_c1.build_freeze(data, plan, sealed, "HEAD")
    man = make_c1.build_freeze(data, plan, sealed, FROZEN_SHA)
    assert man["harness_ref"] == FROZEN_SHA and man["harness_sha256"] == RUN.harness_sha256()
    assert set(man["code_sha256"]) == {"report.py", "stats.py", "grading.py", "audit.py", "scorers.py", "run.py"}
    assert man["analysis"]["alpha"] == 0.05
    assert man["suites"]["sample-mcq"]["ids"] == sorted([items[0]["id"], items[2]["id"]])   # sealed, not exposed
    assert "nvd-cwe-prospective" not in man["suites"] and man["comparisons_sha256"] == RUN.sha256_file(plan)
    assert man["budget_policy_sha256"] == RUN.sha256_text(json.dumps(RUN.BUDGET_POLICY, sort_keys=True))
    sh = make_c1.build_runsh(C1_PLAN)
    assert "--protocol confirmatory" in sh and "D6-defensive-system-prompt" in sh and "D8-untuned-flash" in sh
    assert "--seeds 1234,1235,1236" in sh and "--probe-server" in sh and "--scorer-override" not in sh
    assert "stage2/prospective-C1.json" in sh and 'UNTUNED_LLAMA_SWAP:?' in sh
    import subprocess
    f = tmp_path / "run-c1.sh"
    f.write_text(sh)
    assert subprocess.run(["bash", "-n", str(f)]).returncode == 0


def test_R3_alias_and_served_file_disagreeing_fails_closed(tmp_path):
    cfg = tmp_path / "serving.json"
    cfg.write_text(json.dumps({"models": {"vision": {"model_file": "Qwen3-32B-Q8_0.gguf"}}}))
    a = RUN.parse_args(["--suite", "x", "--model", "coder", "--grader-model", "vision", "--serving-config", str(cfg)])
    pairs = RUN.family_pairs(["vision"], ["coder"], a)
    assert pairs and RUN.family_overlap(["vision"], ["coder"], a) == ["vision"]


# ------------------------------------------------------------------ round 4: r4 loopholes and e2e bypasses, inverted
def _git(repo, *a):
    import subprocess
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *a], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()


def _repo(tmp_path, monkeypatch):
    """A throwaway git repo standing in for REPO_DIR, with a committed harness."""
    repo = tmp_path / "repo"
    (repo / "evals" / "plans").mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / "evals" / "scorers.py").write_text("# v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "harness")
    monkeypatch.setattr(RUN, "REPO_DIR", repo)
    monkeypatch.setattr(RUN, "ATTEMPTS_LEDGER", repo / "evals" / "plans" / "attempts-C1.jsonl")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_R4_A1_harness_ref_must_be_a_full_sha(gw, env, tmp_path, monkeypatch, capsys):
    _clean_git(monkeypatch)
    args = _conf(tmp_path)
    fz = Path(args[args.index("--freeze") + 1])
    man = json.loads(fz.read_text())
    for ref in ("HEAD", "eval-protocol-test", "main"):
        man["harness_ref"] = ref
        fz.write_text(json.dumps(man))
        assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *args)) == 2
        assert "frozen_ref" in _ids_failed(capsys)


def test_R4_A1_post_freeze_commits_are_restricted_in_a_real_repo(tmp_path, monkeypatch):
    repo, frozen = _repo(tmp_path, monkeypatch)
    # allowed: add the freeze manifest once, add a stage-2 manifest once, append to the ledger
    (repo / "evals" / "plans" / "freeze-C1.json").write_text("{}")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "freeze")
    (repo / "evals" / "plans" / "stage2").mkdir()
    (repo / "evals" / "plans" / "stage2" / "prospective-C1.json").write_text("{}")
    (repo / "evals" / "plans" / "attempts-C1.jsonl").write_text('{"event": "start"}\n')
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "stage 2 + ledger")
    head = _git(repo, "rev-parse", "HEAD")
    probs, stage2 = RUN.post_freeze_problems(frozen, head)
    assert probs == [] and stage2 == ["evals/plans/stage2/prospective-C1.json"]
    # not allowed: a harness edit; a rewritten freeze manifest; a ledger line removed
    (repo / "evals" / "scorers.py").write_text("# v2 tuned after peeking\n")
    (repo / "evals" / "plans" / "freeze-C1.json").write_text('{"x": 1}')
    (repo / "evals" / "plans" / "attempts-C1.jsonl").write_text("")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "tamper")
    probs, _ = RUN.post_freeze_problems(frozen, _git(repo, "rev-parse", "HEAD"))
    text = " ".join(probs)
    assert "evals/scorers.py" in text and "rewritten" in text and "append-only" in text
    # a manifest that is part of neither the harness commit nor the allowed post-freeze paths is not frozen
    (repo / "evals" / "plans" / "mine.json").write_text("{}")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "own manifest")
    h2 = _git(repo, "rev-parse", "HEAD")
    assert "not part of the frozen harness commit" in RUN.manifest_problem(
        str(repo / "evals" / "plans" / "mine.json"), frozen, h2)
    assert RUN.manifest_problem(str(repo / "evals" / "plans" / "stage2" / "prospective-C1.json"), frozen, h2) is None


def test_R4_A2_stage2_manifests_cannot_override_stage1(tmp_path):
    bad = tmp_path / "s2.json"
    bad.write_text(json.dumps({"stage": 2, "comparisons_sha256": "x", "harness_ref": "y",
                               "suites": {"sample-mcq": {"ids": ["sample-mcq-0001"]},
                                          "nvd-cwe-prospective": {"ids": ["nvd-cwe-0001"]}},
                               "files": {"evals/data/nvd-cwe.jsonl": "z"}, "calibration_verdict": {"path": "v"}}))
    probs = " | ".join(RUN.manifest_conflicts([str(bad)]))
    for want in ("carries comparisons_sha256, harness_ref", "pins suite sample-mcq", "pins suite nvd-cwe-prospective",
                 "pins evals/data/nvd-cwe.jsonl", "calibration_verdict must be exactly"):
        assert want in probs, want
    ok = tmp_path / "s2ok.json"
    ok.write_text(json.dumps({"stage": 2, "window": ["2026-10-01", "2026-10-31"],
                              "suites": {"nvd-cwe-prospective": {"ids": ["nvdp-cwe-0001"]}},
                              "files": {"evals/data/nvd-cwe-prospective.jsonl": "a"},
                              "calibration_verdict": {"path": "v.json", "sha256": "b"}}))
    assert RUN.manifest_conflicts([str(ok)]) == []
    # two manifests that disagree on a pin, a file hash or an id list
    m1, m2 = tmp_path / "m1.json", tmp_path / "m2.json"
    m1.write_text(json.dumps({"comparisons_sha256": "a", "files": {"f": "1"}, "suites": {"s": {"ids": ["1"]}}}))
    m2.write_text(json.dumps({"comparisons_sha256": "b", "files": {"f": "2"}, "suites": {"s": {"ids": ["2"]}}}))
    assert len(RUN.manifest_conflicts([str(m1), str(m2)])) == 3
    assert "freeze_conflict" in RUN.NON_WAIVABLE and "stage2_missing" in RUN.NON_WAIVABLE


def test_R4_prospective_items_need_a_stage2_pin_window_and_namespace(tmp_path, monkeypatch):
    sm = tmp_path / "sealed-heldout.json"
    sm.write_text(json.dumps({"suites": {}}))
    s2 = tmp_path / "s2.json"
    s2.write_text(json.dumps({"stage": 2, "window": ["2026-10-01", "2026-10-31"],
                              "suites": {"nvd-cwe-prospective": {"ids": ["nvdp-cwe-0001", "nvdp-cwe-0002"]}}}))
    monkeypatch.setattr(RUN, "_is_stage2", lambda p: Path(p).name == "s2.json")
    a = RUN.parse_args(["--suite", "x", "--model", "coder", "--protocol", "confirmatory"])
    a.freeze = [str(sm), str(s2)]
    it = lambda i, day: {"id": i, "suite": "nvd-cwe-prospective", "meta": {"first_public_date": day}}  # noqa: E731
    fails = []
    RUN._sealed_check(a, [it("nvdp-cwe-0001", "2026-10-05"), it("nvdp-cwe-0002", "2026-09-01"),
                          it("nvdp-cwe-0003", "2026-10-05")], lambda c, m: fails.append(m))
    assert len(fails) == 1 and "2 selected item(s) are not sealed" in fails[0]
    assert "outside the declared window" in fails[0] and "not listed by a stage-2 manifest" in fails[0]


def test_R4_B1_each_run_name_needs_its_declared_interventions(gw, env, tmp_path, monkeypatch, capsys):
    _clean_git(monkeypatch)
    args = _conf(tmp_path)
    plan = Path(args[args.index("--protocol-plan") + 1])
    p = json.loads(plan.read_text())
    p["run_interventions"] = {"c1": [], "c1sys": ["D6-defensive-system-prompt"]}
    plan.write_text(json.dumps(p))
    fz = Path(args[args.index("--freeze") + 1])
    man = json.loads(fz.read_text())
    man["comparisons_sha256"] = RUN.sha256_file(plan)
    fz.write_text(json.dumps(man))
    run = lambda name, *x: RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *args,  # noqa: E731
                                              "--run-name", name, *x))
    assert run("c1sys") == 2 and "intervention" in _ids_failed(capsys)      # c1sys without its prompt
    assert run("c1pre") == 2 and "intervention" in _ids_failed(capsys)      # a run name the plan does not declare
    assert run("c1sys", "--intervention", "D6-defensive-system-prompt") == 0
    assert run("c1") == 0


def test_R4_B2_intervention_mismatch_cannot_be_waived(gw, env, tmp_path, monkeypatch, capsys):
    _clean_git(monkeypatch)
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *_conf(tmp_path),
                              "--intervention", "D8-untuned-flash", "--deviation", "intervention=cmd differs")) == 2
    assert "intervention is an integrity or estimand check" in capsys.readouterr().err


def test_R4_B3_live_props_model_file_is_checked_for_D8(gw, env, tmp_path, monkeypatch, capsys):
    _clean_git(monkeypatch)
    iv = json.loads(RUN.INTERVENTIONS.read_text())["interventions"]["D8-tuned-flash"]
    gw.get_routes = {"/upstream/flash/props": {"model_path": "/m/Some-Other-Model-Q4_K_M.gguf"}}
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *_conf(tmp_path),
                              "--intervention", "D8-tuned-flash")) == 2
    err = capsys.readouterr().err
    assert "live server serves Some-Other-Model-Q4_K_M.gguf" in err
    want = re.search(r"-m\s+(\S+)", iv["cmd"]).group(1).rsplit("/", 1)[1]
    gw.get_routes = {"/upstream/flash/props": {"model_path": "/x/" + want}}
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *_conf(tmp_path),
                              "--intervention", "D8-tuned-flash", "--run-name", "t2")) == 0
    rec = _rj(env, "t2-coder")["protocol"]["interventions"][0]
    assert rec["live_model_file"] == want and "honour-system" in rec["live_check"]


def _judge_setup(tmp_path, alias="claude:judge", **pin):
    ff = SAMPLES / "freeform.jsonl"
    items = {json.loads(l)["id"]: json.loads(l) for l in ff.read_text().splitlines() if l.strip()}
    ids = sorted(i for i, it in items.items() if it["scorer"] == "llm_judge")[:3]
    args = _conf(tmp_path, ids=ids, items_file=ff)
    sm = Path(args[args.index("--freeze", args.index("--freeze") + 1) + 1])
    sm.write_text(json.dumps({"suites": {"sample-freeform": {"entries": _sealed_entries(items, ids)}}}))
    fz = Path(args[args.index("--freeze") + 1])
    man = json.loads(fz.read_text())
    man["suites"] = {"sample-freeform": man["suites"].pop("sample-mcq")}
    man["judge"] = {"alias": alias, "prompt_sha256": RUN.sha256_file(RUN.GRADER_PROMPT), "max_candidate_chars": 12000,
                    **pin}
    fz.write_text(json.dumps(man))
    return ff, args


def test_R4_B4_judge_prompt_cap_alias_and_weights_are_pinned(gw, env, tmp_path, monkeypatch, capsys):
    _clean_git(monkeypatch)
    ff, args = _judge_setup(tmp_path)
    run = lambda *x: RUN.main(base_args(env, gw, "--suite", str(ff), "--model", "coder", *args, *x))  # noqa: E731
    other = tmp_path / "my_prompt.md"
    other.write_text(RUN.GRADER_PROMPT.read_text() + "\nBe generous.\n")
    assert run("--grader-model", "claude:judge", "--grader-prompt", str(other)) == 2
    assert "judge_pin" in _ids_failed(capsys)
    assert run("--grader-model", "claude:judge", "--grader-max-chars", "40") == 2
    assert "judge_pin" in _ids_failed(capsys)
    assert run("--grader-model", "claude:other") == 2
    assert "judge_pin" in _ids_failed(capsys)
    assert run("--grader-model", "claude:judge") == 0


def test_R4_B4_judge_family_comes_from_the_live_server(gw, env, tmp_path, monkeypatch, capsys):
    """serving.json claims a gemma file for 'judge-x'; the live /props says Qwen: identity conflict, fail closed."""
    _clean_git(monkeypatch)
    ff, args = _judge_setup(tmp_path, alias="judge-x", model_file="gemma-4-31B-it-Q4_K_M.gguf")
    cfg = Path(args[args.index("--serving-config") + 1])
    cfg.write_text(json.dumps({"defaults": {"quant": "Q4_K_M"},
                               "models": {"judge-x": {"model_file": "gemma-4-31B-it-Q4_K_M.gguf"}}}))
    fz = Path(args[args.index("--freeze") + 1])
    man = json.loads(fz.read_text())
    man["serving_config_sha256"] = RUN.sha256_file(cfg)
    fz.write_text(json.dumps(man))
    gw.get_routes = {"/upstream/judge-x/props": {"model_path": "/m/Qwen3-32B-Q8_0.gguf"}}
    assert RUN.main(base_args(env, gw, "--suite", str(ff), "--model", "coder", *args,
                              "--grader-model", "judge-x")) == 2
    err = capsys.readouterr().err
    assert "grader_family" in err and "Qwen3-32B-Q8_0.gguf" in err and "judge_pin" in err


def test_R4_C1_every_confirmatory_attempt_is_in_the_ledger(gw, env, tmp_path, monkeypatch):
    _clean_git(monkeypatch)
    args = _conf(tmp_path)
    for root in ("res-a", "res-b"):
        argv = base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *args)
        argv[argv.index("--results") + 1] = str(tmp_path / root)
        assert RUN.main(argv) == 0
    led = [json.loads(l) for l in RUN.ATTEMPTS_LEDGER.read_text().splitlines()]
    assert [(r["event"], r["results_root"]) for r in led] == [("start", "res-a"), ("end", "res-a"),
                                                             ("start", "res-b"), ("end", "res-b")]
    assert all(r["run_id"] == "t1-coder" and r["seed"] == 1234 for r in led)
    assert led[1]["outputs_sha256"]["responses.jsonl"]
    # exploratory runs are not ledgered
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", "--limit", "1", "--run-name", "x")) == 0
    assert len(RUN.ATTEMPTS_LEDGER.read_text().splitlines()) == 4


def test_R4_E_sampler_check_runs_after_each_seed(gw, env, tmp_path, monkeypatch):
    """Each seed is checked right after its own generation: a server that keeps seed 1235's params (as a single
    check at the end would see) fails seeds 1234 and 1236."""
    _clean_git(monkeypatch)
    calls = []

    def fake_verify(fetch, base, model, sent):
        calls.append(sent["seed"])
        return {"ok": sent["seed"] == 1235, "mismatch": {} if sent["seed"] == 1235 else {"seed": "x"}}
    monkeypatch.setattr(RUN, "verify_samplers", fake_verify)
    order = []
    real_gen = RUN.Run.generate

    def gen(self):
        out = real_gen(self)
        order.append(("gen", self.seed))
        return out
    monkeypatch.setattr(RUN.Run, "generate", gen)
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *_conf(tmp_path), "--thinking", "on",
                              "--seeds", "1234,1235,1236")) == 0
    assert calls == [1234, 1235, 1236]
    oks = [_rj(env, f"t1-coder-think-seed{s}")["serving"]["sampler_check"]["ok"] for s in (1234, 1235, 1236)]
    assert oks == [False, True, False]


def test_R4_E_judge_identity_change_mid_grading_invalidates(gw, env, tmp_path, monkeypatch):
    _clean_git(monkeypatch)
    ff, args = _judge_setup(tmp_path)
    n = {"k": 0}
    real = RUN._grader_call

    def call(client, body, a):
        text, meta = real(client, body, a)
        n["k"] += 1
        if n["k"] > 1:
            meta["system_fingerprint"] = "b-rebuilt"
        return text, meta
    monkeypatch.setattr(RUN, "_grader_call", call)
    assert RUN.main(base_args(env, gw, "--suite", str(ff), "--model", "coder", *args,
                              "--grader-model", "claude:judge")) == 4
    assert "judge's serving identity changed" in _rj(env, "t1-coder")["protocol"]["invalid"]


def test_R4_E_bytecode_harness_pins_and_named_untracked_files(gw, env, tmp_path, monkeypatch, capsys):
    _clean_git(monkeypatch, untracked=True, untracked_files=["evals/plans/notes.txt"])
    monkeypatch.setattr(RUN, "pycache_dirs", lambda: ["evals/__pycache__"])
    args = _conf(tmp_path)
    fz = Path(args[args.index("--freeze") + 1])
    man = json.loads(fz.read_text())
    man["harness_sha256"] = dict(man["harness_sha256"], **{"evals/scorers.py": "0" * 64})
    fz.write_text(json.dumps(man))
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *args)) == 2
    err = capsys.readouterr().err
    assert "[bytecode]" in err and "[harness_files]" in err and "evals/scorers.py" in err
    assert "evals/plans/notes.txt" in err
    assert sys.dont_write_bytecode is True


def test_R4_serving_record_must_be_in_the_repo_and_pinned(gw, env, tmp_path, monkeypatch, capsys):
    _clean_git(monkeypatch)
    monkeypatch.setattr(RUN, "in_repo", lambda p: False)
    assert RUN.main(base_args(env, gw, "--suite", str(MCQ), "--model", "coder", *_conf(tmp_path))) == 2
    assert "serving_pin" in _ids_failed(capsys)


def test_R4_power_table_in_protocol_equals_the_tool_output():
    """PROTOCOL C1.11 quotes evals/plans/power_table.md (written by evals/tools/power.py) verbatim."""
    table = (EVALS / "plans" / "power_table.md").read_text(encoding="utf-8").strip()
    assert table and table in PROTOCOL_MD.read_text(encoding="utf-8")
    assert f"plan sha256: {RUN.sha256_file(C1_PLAN)}" in table   # the table is for the current comparisons file
