"""clients/bin/spark-model: the workstation dispatcher.

Remote verbs go through a fake `ssh` (SPARK_MODEL_SSH) that runs the remote command the way sshd would (one string
through sh -c), with the agent swapped in for `sudo -n /usr/local/sbin/spark-model-agent`, so quoting is exercised
end to end. The live-run guard is tested with a dummy process whose argv looks like evals/run.py and with pid files.
Run: python3 -B -m pytest -q walter/tests/test_spark_model_client.py
"""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("yaml", reason="PyYAML (python3-yaml) is needed for the registry")
HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "walter" / "models" / "lib"))
import registry as R  # noqa: E402
from spark_model_fakehub import SHA, FakeHub, model_gguf  # noqa: E402

CLIENT = REPO / "clients" / "bin" / "spark-model"
AGENT = REPO / "walter" / "models" / "spark-model-agent"
ORG_REPO = "someorg/Model-abliterated-GGUF"


def load_client():
    loader = importlib.machinery.SourceFileLoader("spark_model_client", str(CLIENT))
    spec = importlib.util.spec_from_loader("spark_model_client", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def hub():
    h = FakeHub().start()
    h.add(ORG_REPO, {"Q4/m-Q4_K_M.gguf": model_gguf(), "Q8/m-Q8_0.gguf": model_gguf()})
    yield h
    h.stop()


@pytest.fixture
def W(tmp_path, hub):
    """Workstation + backend under tmp_path: site.env, fake ssh, agent paths."""
    site = tmp_path / "site.env"
    site.write_text('BACKEND_SSH_USER=operator\nBACKEND_LAN_IP=192.168.122.10   # example\n')
    models = tmp_path / "models"
    (models / "gguf").mkdir(parents=True)
    etc = tmp_path / "etc"
    etc.mkdir()
    base = subprocess.run(["bash", str(REPO / "scripts/render.sh"), "-e", str(REPO / "site.env.example"),
                           str(REPO / "walter/llama-swap/config.yaml.tmpl"), str(etc / "config.base.yaml")],
                          capture_output=True, text=True)
    assert base.returncode == 0, base.stderr
    (tmp_path / "agent.env").write_text('LAB_HF_ORGS="someorg"\nLAB_FREE_RESERVE_GB=0\nLLAMA_SWAP_URL=off\n')
    log = tmp_path / "ssh.log"
    ssh = tmp_path / "fake-ssh"
    ssh.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import os, sys
        argv = sys.argv[1:]
        open({str(log)!r}, "a").write(repr(argv) + "\\n")
        i = 0
        while argv[i].startswith("-"):
            i += 2 if argv[i] in ("-o",) else 1
        dest, remote = argv[i], " ".join(argv[i + 1:])
        pre = "sudo -n /usr/local/sbin/spark-model-agent "
        assert remote.startswith(pre), remote
        os.execvp("sh", ["sh", "-c", {sys.executable!r} + " -B " + {str(AGENT)!r} + " " + remote[len(pre):]])
        """))
    ssh.chmod(0o755)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "OMP_NUM_THREADS": "1",
           "SPARK_SITE_ENV": str(site), "SPARK_MODEL_SSH": str(ssh), "SPARK_EVAL_REPOS": "",
           "SPARK_MODEL_ENV_FILE": str(tmp_path / "agent.env"), "SPARK_MODEL_MODELS_DIR": str(models),
           "SPARK_MODEL_CONFIG_DIR": str(etc), "SPARK_MODEL_LOCK": str(tmp_path / "lock"),
           "SPARK_MODEL_TOKEN_FILE": str(tmp_path / "no-token"), "SPARK_MODEL_LLAMA_SWAP": str(tmp_path / "none"),
           "SPARK_MODEL_HF_ENDPOINT": hub.endpoint}

    class W:
        pass
    w = W()
    w.tmp, w.env, w.log, w.models, w.etc = tmp_path, env, log, models, etc

    def run(*args, check=None, extra=None):
        r = subprocess.run([sys.executable, "-B", str(CLIENT), *args], capture_output=True, text=True, timeout=120,
                           env={**env, **(extra or {})})
        if check is not None:
            assert r.returncode == check, (r.returncode, r.stdout[-2000:], r.stderr[-2000:])
        return r
    w.run = run
    w.ssh_calls = lambda: [eval(l) for l in log.read_text().splitlines()] if log.exists() else []  # noqa: S307
    return w


def test_ssh_command_shape(W):
    W.run("ls", check=0)
    argv = W.ssh_calls()[-1]
    assert argv[:6] == ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-T", "operator@192.168.122.10"]
    assert argv[6:] == ["sudo", "-n", "/usr/local/sbin/spark-model-agent", "ls", "--json"]


def test_text_and_json_output(W):
    r = W.run("ls", check=0)
    assert r.stdout.startswith("lab models (0;") and "core models" in r.stdout and not r.stdout.startswith("{")
    d = json.loads(W.run("ls", "--json", check=0).stdout)
    assert d["ok"] and d["verb"] == "ls" and d["lab"] == []


def test_globs_survive_the_remote_shell(W):
    r = W.run("pull", ORG_REPO, "--file", "*Q4*", "--id", "m1", "--placement", "gpu1", check=0)
    assert "Q4/m-Q4_K_M.gguf" in r.stdout and "Q8" not in r.stdout and "Re-run with --yes" in r.stdout
    assert W.ssh_calls()[-1][-6:] == ["--file", "'*Q4*'", "--id", "m1", "--placement", "gpu1"]
    r = W.run("pull", ORG_REPO, "--file", "*Q4*", "--id", "m1", "--placement", "gpu1", "--yes", check=0)
    assert (W.models / "gguf/lab/m1/m-Q4_K_M.gguf").is_file()


@pytest.mark.parametrize("bad", ["a;b", "$(id)", "x y", "`id`", "a|b", "a\nb", "'"])
def test_unsafe_arguments_never_reach_ssh(W, bad):
    r = W.run("rm", bad, check=2)
    assert "does not send" in r.stderr and W.ssh_calls() == []


def test_agent_errors_and_exit_codes_pass_through(W):
    r = W.run("rm", "absent-model", "--yes", check=1)
    assert "not a lab model" in r.stderr
    r = W.run("pull", "evilorg/x", "--id", "m1", "--placement", "gpu0", "--class", "base", check=1)
    assert "not in LAB_HF_ORGS" in r.stderr


def test_render_apply_is_not_offered(W):
    r = W.run("render", "--apply", check=2)
    assert "deploy.sh renders and installs" in r.stderr and W.ssh_calls() == []
    r = W.run("render", check=0)
    assert "render:" in r.stdout


def test_missing_site_values_and_ssh_failure(W, tmp_path):
    (tmp_path / "empty.env").write_text("")
    r = W.run("ls", check=2, extra={"SPARK_SITE_ENV": str(tmp_path / "empty.env")})
    assert "BACKEND_SSH_USER and BACKEND_LAN_IP" in r.stderr
    bad = tmp_path / "ssh-down"
    bad.write_text("#!/bin/sh\necho 'ssh: connect to host: Connection refused' >&2\nexit 255\n")
    bad.chmod(0o755)
    r = W.run("ls", check=255, extra={"SPARK_MODEL_SSH": str(bad)})
    assert "ssh to the backend failed" in r.stderr


def test_unknown_verb_and_help(W):
    assert "unknown verb" in W.run("frob", check=2).stderr
    assert "spark-model pull REPO" in W.run("-h", check=0).stdout


# ---- the live eval/gate guard ------------------------------------------------------------------------------------

def test_live_run_detection(tmp_path, monkeypatch):
    c = load_client()
    monkeypatch.setattr(c, "_cmdlines", lambda: iter([(11, ["python3", "-B", "evals/run.py", "--run-name", "x"]),
                                                     (12, ["bash", "/r/evals/tools/gpu-guard.sh"]),
                                                     (13, ["vim", "notes about evals/run.py.txt"])]))
    found = c.live_runs(repos=[])
    assert len(found) == 2 and found[0].startswith("pid 11: python3 -B evals/run.py") and "pid 12" in found[1]
    monkeypatch.setattr(c, "_cmdlines", lambda: iter([]))
    runs = tmp_path / "evals/results/_runs/evalb"
    runs.mkdir(parents=True)
    (runs / "gpu-guard.pid").write_text(f"{os.getpid()}\n")
    (runs / "stale.pid").write_text("999999999\n")
    (runs / "junk.pid").write_text("nope\n")
    found = c.live_runs(repos=[tmp_path])
    assert found == [f"{runs / 'gpu-guard.pid'} (pid {os.getpid()} alive)"]
    assert c.live_runs(repos=[tmp_path / "nothing"]) == []


@pytest.fixture
def dummy_eval(tmp_path):
    """A process whose argv looks like a running evals/run.py."""
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "evals/run.py", "--run-name", "t"])
    yield p
    p.kill()
    p.wait()


def test_rm_refused_while_an_eval_runs_and_force_says_what(W, dummy_eval):
    R.save(W.models / "registry.yaml", {"schema": R.SCHEMA, "models": [{
        "id": "m1", "repo": ORG_REPO, "revision": SHA, "placement": "gpu1", "ctx": 8192, "class": "abliterated",
        "added": "2026-10-01T00:00:00Z", "files": [{"path": "m.gguf", "bytes": 1, "sha256": "a" * 64, "role": "model"}]}]})
    r = W.run("rm", "m1", "--yes", check=1)
    assert "rm refused: an eval/gate run is live" in r.stderr and f"pid {dummy_eval.pid}" in r.stderr
    assert W.ssh_calls() == []
    W.run("rm", "m1", check=0)                       # a plan only changes nothing: allowed
    W.run("ls", check=0)
    r = W.run("rm", "m1", "--yes", "--force", check=0)
    assert "--force: overriding the live-run guard" in r.stderr and f"pid {dummy_eval.pid}" in r.stderr
    assert R.load(W.models / "registry.yaml")["models"] == []


# ---- search / fit dispatch -------------------------------------------------------------------------------------

def test_search_and_fit_are_dispatched_to_cli_fit(W, tmp_path):
    fake = tmp_path / "repo"
    lib = fake / "walter/models/lib"
    lib.mkdir(parents=True)
    r = W.run("fit", "x", check=3, extra={"SPARK_REPO": str(fake)})
    assert "cli_fit.py could not be imported" in r.stderr
    (lib / "cli_fit.py").write_text(textwrap.dedent("""\
        def add_parsers(sub):
            for v in ("search", "fit"):
                p = sub.add_parser(v)
                p.add_argument("words", nargs="*")
        def run_search(args):
            print("SEARCH", args.words)
        def run_fit(args):
            print("FIT", args.words)
            return 4
        """))
    assert W.run("search", "a", "b", check=0, extra={"SPARK_REPO": str(fake)}).stdout == "SEARCH ['a', 'b']\n"
    assert W.run("fit", "c", check=4, extra={"SPARK_REPO": str(fake)}).stdout == "FIT ['c']\n"
    assert W.ssh_calls() == []


def test_install_writes_a_launcher(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    r = subprocess.run(["bash", str(REPO / "clients/install.sh"), "--site-env", str(REPO / "site.env.example")],
                       capture_output=True, text=True, timeout=60, env={"PATH": os.environ["PATH"], "HOME": str(home)})
    assert r.returncode == 0, r.stdout + r.stderr
    launcher = (home / ".local/bin/spark-model").read_text()
    assert launcher.startswith("#!/bin/sh\n") and f'exec python3 {REPO}/clients/bin/spark-model "$@"' in launcher
    assert os.access(home / ".local/bin/spark-model", os.X_OK)
    r = subprocess.run([str(home / ".local/bin/spark-model"), "-h"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout.startswith("spark-model -- lab models")
