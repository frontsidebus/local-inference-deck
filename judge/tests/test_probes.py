import json
import subprocess
import sys

import pytest

from collector_testlib import FIXTURES, JUDGE_DIR, FakeRunner, make_env

import probe
from lib import config

SLOTS = (FIXTURES / "slots-walter.json").read_text()


@pytest.fixture
def env(tmp_path, monkeypatch):
    return make_env(tmp_path, monkeypatch)


@pytest.mark.parametrize("argv", [
    ["rm_rf", "/"],
    ["port_listening", "walter", "22; reboot"],
    ["port_listening", "elsewhere", "22"],
    ["port_listening", "walter", "70000"],
    ["port_listening", "walter"],
    ["unit_state", "walter", "nginx.service;id"],
    ["file_hash", "local", "/etc/../root/x"],
    ["file_hash", "walter", "relative/path"],
    ["file_hash", "walter", "/etc/$(id)"],
    ["http_status", "https://evil.test/"],
    ["http_status", "http://api.example.com/"],
    ["http_status", "https://api.example.com.evil.test/"],
    ["ssh_alias_test", "not-configured"],
    ["ssh_alias_test", "-oProxyCommand=x"],
    ["render_and_diff", "../etc/passwd.tmpl"],
    ["render_and_diff", "walter/x.tmpl", "walter:/etc/wireguard/wg0.conf"],
    ["render_and_diff", "walter/x.tmpl", "walter:/home/op/x"],
    ["check_sanitized", "relative"],
    ["slots", "extra"],
])
def test_bad_name_or_args_exit_64_without_running(env, argv):
    r = FakeRunner()
    rc, text = probe.run_probe(argv[0], argv[1:], runner=r)
    assert rc == 64 and text.startswith("probe:")
    assert r.calls == []


def test_main_exit_code_cli(env):
    cp = subprocess.run([sys.executable, str(JUDGE_DIR / "probes" / "probe.py"), "nope"], capture_output=True, text=True)
    assert cp.returncode == 64


def test_port_listening(env):
    r = FakeRunner([(r"ss -ltnH", (0, "LISTEN 0 4096 127.0.0.1:8080 0.0.0.0:*\n", ""))])
    rc, text = probe.run_probe("port_listening", ["walter", "8080"], runner=r)
    assert rc == 0 and "RESULT: port 8080 on walter: LISTENING" in text
    argv = r.calls[0]["argv"]
    assert argv[:3] == ["ssh", "-o", "BatchMode=yes"] and "operator@192.168.122.10" in argv
    assert argv[-1] == "ss -ltnH 'sport = :8080'"


def test_unreachable_host_reports_exit(env):
    r = FakeRunner(default=(255, "", "ssh: connect to host port 22: Connection timed out"))
    rc, text = probe.run_probe("unit_state", ["covenant", "nginx.service"], runner=r)
    assert rc == 255 and "# exit 255" in text and "Connection timed out" in text
    assert r.calls[0]["argv"][-2] == "edge-alias"


def test_http_status_and_ssh_alias(env):
    r = FakeRunner([(r"^curl", (0, "http_code=302 redirect_url=https://id.example.com/ ssl_verify_result=0\n", "")),
                    (r"ssh -G", (0, "user ubuntu\nhostname 203.0.113.10\nport 22\nidentityfile ~/.ssh/k\nciphers x\n", ""))])
    rc, text = probe.run_probe("http_status", ["https://telemetry.example.com/healthz"], runner=r)
    assert rc == 0 and "http_code=302" in text
    assert "--proto" in r.calls[0]["argv"] and r.calls[0]["argv"][-1] == "https://telemetry.example.com/healthz"
    rc, text = probe.run_probe("ssh_alias_test", ["edge-alias"], runner=r)
    assert "hostname 203.0.113.10" in text and "ciphers" not in text
    assert r.calls[-1]["argv"][-2:] == ["edge-alias", "true"]


def test_file_hash_local_and_remote(env):
    r = FakeRunner(default=(0, "abc  /etc/hosts\n", ""))
    probe.run_probe("file_hash", ["local", "/etc/hosts"], runner=r)
    assert r.calls[0]["argv"] == ["sha256sum", "--", "/etc/hosts"]
    probe.run_probe("file_hash", ["walter", "/etc/hosts"], runner=r)
    assert r.calls[-1]["argv"][-1].startswith("sha256sum -- /etc/hosts;")


def test_output_is_redacted(env):
    tok = "sk-" + "A1b2C3d4" * 3
    r = FakeRunner(default=(0, f"Environment=API_KEY={tok}\n", ""))
    _, text = probe.run_probe("unit_state", ["walter", "x.service"], runner=r)
    assert tok not in text and "<redacted>" in text


def test_check_sanitized_uses_judge_repo_script(env, tmp_path):
    (env["repo"] / "scripts").mkdir()
    (env["repo"] / "scripts" / "check-sanitized.sh").write_text("#!/bin/bash\necho ok\n")
    target = tmp_path / "wt"
    (target / ".git").mkdir(parents=True)
    r = FakeRunner(default=(0, "check-sanitized: OK (3 files)\n", ""))
    rc, text = probe.run_probe("check_sanitized", [str(target)], runner=r)
    assert rc == 0 and r.calls[0]["argv"] == ["bash", str(env["repo"] / "scripts" / "check-sanitized.sh")]
    assert r.calls[0]["cwd"] == str(target)


def test_render_and_diff(env):
    (env["repo"] / "scripts").mkdir()
    (env["repo"] / "walter").mkdir()
    (env["repo"] / "walter" / "x.conf.tmpl").write_text("server_name ${SPARK_API_HOST};\n")

    def fake_render(argv):
        open(argv[-1], "w").write("server_name api.example.com;\nlisten 443;\n")
        return (0, "", "")
    r = FakeRunner([(r"render\.sh", fake_render), (r"cat -- /etc/nginx/x.conf", (0, "server_name api.example.com;\n", ""))])
    rc, text = probe.run_probe("render_and_diff", ["walter/x.conf.tmpl", "covenant:/etc/nginx/x.conf"], runner=r)
    assert rc == 0 and "-listen 443;" in text
    rc, text = probe.run_probe("render_and_diff", ["walter/x.conf.tmpl"], runner=r)
    assert "server_name api.example.com;" in text


def _slots_runner(docker_out=None):
    docker_out = docker_out if docker_out is not None else \
        "llama-coder\t127.0.0.1:5801->8080/tcp\nllama-big\t127.0.0.1:5802->8080/tcp\n"
    return FakeRunner([
        (r"docker ps", (0, docker_out, "")),
        (r"/slots", (0, f"=== 5801\n{SLOTS}\n=== 5802\n<html>502</html>\n", "")),
    ])


def test_slots_summary(env):
    r = _slots_runner()
    res = probe.slots_summary(config.load_config(), runner=r)
    assert set(k for k in res if not k.startswith("_")) == {"llama-coder", "llama-big"}
    s0, s1 = res["llama-coder"]
    assert s0 == {"id": 0, "id_task": 4711, "is_processing": True, "n_decoded": 48213, "n_predict": -1,
                  "max_tokens": -1, "runaway_suspect": True}
    assert s1["runaway_suspect"] is False and s1["n_predict"] == 2048
    assert res["llama-big"] == [] and "llama-big" in res["_error"]
    # no api key is ever read: only docker ps and localhost curl
    joined = " ".join(" ".join(c["argv"]) for c in r.calls)
    assert "api-key" not in joined and "sudo" not in joined and "127.0.0.1:5801/slots" in joined


def test_slots_probe_cli_shape(env):
    rc, text = probe.run_probe("slots", [], runner=_slots_runner(""))
    body = json.loads(text[text.index("{"):text.rindex("}") + 1])
    assert rc == 0 and body["_note"].startswith("no llama-swap")
    rc, text = probe.run_probe("slots", [], runner=FakeRunner(default=(255, "", "unreachable")))
    assert rc == 255 and "_error" in text
