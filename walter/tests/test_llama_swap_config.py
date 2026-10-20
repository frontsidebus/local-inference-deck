"""walter/llama-swap/config.yaml.tmpl: the model containers are hardened and llama-swap captures nothing.

The template is rendered with scripts/render.sh and site.env.example, the llama-swap macros are expanded the way
llama-swap does (${MODEL_ID} and ${PORT} stay), and every model's docker command is checked:
  - non-root --user, --cap-drop ALL, no-new-privileges, --read-only (+ a /tmp tmpfs), --pids-limit, one --memory;
  - --network llama-net (the no-egress bridge from deploy.sh + firewall/docker-user-rules.sh), port on 127.0.0.1;
  - the only bind mount is ${MODELS_DIR}/gguf, read-only: never MODELS_DIR's root or its backups/;
  - every /models/... path the server is given (-m, --mmproj, -md) lies under that mount and is a file that
    fetch-models.sh downloads.
The Maya lab engine (walter/maya, image walter-maya) is the one exception, checked on its own: besides that mount it
gets ${MODELS_DIR}/maya-data (rw: packs, log) and its run config from /etc/maya (ro), and serves only the API routes.
Run: python3 -m pytest walter/tests -q -p no:cacheprovider
"""
import re
import shlex
import subprocess
from pathlib import Path, PurePosixPath

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "walter" / "llama-swap" / "config.yaml.tmpl"
MAYA = REPO / "walter" / "maya"
FETCH = REPO / "walter" / "llama-swap" / "fetch-models.sh.tmpl"
DEPLOY = REPO / "walter" / "deploy.sh"
FW = REPO / "walter" / "firewall" / "docker-user-rules.sh.tmpl"
EXAMPLE = REPO / "site.env.example"


def site_value(name):
    m = re.search(rf"^{name}=(\S+)", EXAMPLE.read_text(), re.M)
    return m.group(1).strip("\"'")


MODELS_DIR = PurePosixPath(site_value("MODELS_DIR"))


@pytest.fixture(scope="module")
def config(tmp_path_factory):
    out = tmp_path_factory.mktemp("ls") / "config.yaml"
    r = subprocess.run(["bash", str(REPO / "scripts/render.sh"), "-e", str(EXAMPLE), str(TEMPLATE), str(out)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return yaml.safe_load(out.read_text())


def expand(cfg, text):
    macros = {k: str(v) for k, v in cfg["macros"].items()}
    for _ in range(5):
        text = re.sub(r"\$\{([\w-]+)\}", lambda m: macros.get(m.group(1), m.group(0)), text)
    return text


def docker_args(cfg, model_id):
    """The `docker run` args of a model's cmd (everything before the image), and the server args after it."""
    argv = shlex.split(expand(cfg, cfg["models"][model_id]["cmd"]))
    assert argv[0] == "/usr/local/sbin/spark-docker-run.sh" and argv[1] == "${MODEL_ID}", argv[:2]
    img = next(i for i, a in enumerate(argv) if a.startswith(("ghcr.io/ggml-org/llama.cpp:", "walter-maya:")))
    return argv[2:img], argv[img + 1:]


def values(args, *flags):
    """Values of the given docker flags, in `--flag value` or `--flag=value` form."""
    out = []
    for i, a in enumerate(args):
        for f in flags:
            if a == f:
                out.append(args[i + 1])
            elif a.startswith(f + "="):
                out.append(a.split("=", 1)[1])
    return out


def mem_bytes(v):
    n, unit = re.fullmatch(r"(\d+)([kmg]?)", v.lower()).groups()
    return int(n) * {"": 1, "k": 2**10, "m": 2**20, "g": 2**30}[unit]


def model_ids():
    return list(yaml.safe_load(TEMPLATE.read_text())["models"])


def is_maya(cfg, mid):
    return "${maya-image}" in cfg["models"][mid]["cmd"]


def server_ids():
    """The llama-server models (every model but Maya's)."""
    cfg = yaml.safe_load(TEMPLATE.read_text())
    return [m for m in cfg["models"] if not is_maya(cfg, m)]


def maya_ids():
    cfg = yaml.safe_load(TEMPLATE.read_text())
    return [m for m in cfg["models"] if is_maya(cfg, m)]


# ---- llama-swap itself ---------------------------------------------------------------------------

def test_capture_buffer_is_zero(config):
    # v260 default is 5 MB of request/response bodies, served at /api/captures/{id} to any API-key holder.
    assert "captureBuffer" in config and config["captureBuffer"] == 0


def test_no_unknown_macros_left(config):
    for mid in config["models"]:
        left = set(re.findall(r"\$\{([\w.-]+)\}", expand(config, config["models"][mid]["cmd"])))
        assert left <= {"MODEL_ID", "PORT"}, (mid, left)


# ---- every model container is hardened -----------------------------------------------------------

@pytest.mark.parametrize("mid", model_ids())
def test_run_is_hardened(config, mid):
    args, _ = docker_args(config, mid)
    users = values(args, "--user", "-u")
    assert len(users) == 1, users
    uid, _, gid = users[0].partition(":")
    assert uid.isdigit() and int(uid) >= 1000 and gid.isdigit() and int(gid) >= 1000, users
    assert values(args, "--cap-drop") == ["ALL"]
    assert values(args, "--security-opt") == ["no-new-privileges"]
    assert "--read-only" in args
    assert any(t.split(":")[0] == "/tmp" for t in values(args, "--tmpfs"))
    pids = values(args, "--pids-limit")
    assert len(pids) == 1 and 0 < int(pids[0]) <= 4096, pids
    mem, swap = values(args, "--memory", "-m"), values(args, "--memory-swap")
    assert len(mem) == 1 and swap == mem, (mem, swap)
    assert values(args, "--network", "--net") == ["llama-net"]
    assert values(args, "--runtime") == ["nvidia"] and len(values(args, "--gpus")) == 1
    assert all(p.startswith("127.0.0.1:") for p in values(args, "-p", "--publish")), args
    for bad in ("--privileged", "--cap-add", "--pid", "--ipc", "--userns", "--device", "--mount",
                "--volumes-from"):
        assert not any(a == bad or a.startswith(bad + "=") for a in args), (mid, bad)


@pytest.mark.parametrize("mid", server_ids())
def test_mounts_only_model_files(config, mid):
    args, _ = docker_args(config, mid)
    vols = values(args, "-v", "--volume")
    assert vols, "no model mount"
    for v in vols:
        src, dst, *opts = v.split(":")
        src = PurePosixPath(src)
        assert opts == ["ro"], v
        assert src != MODELS_DIR and MODELS_DIR in src.parents, f"{v}: must be a subtree of {MODELS_DIR}"
        assert "backups" not in src.parts, v
        assert src == MODELS_DIR / "gguf", f"{v}: only {MODELS_DIR}/gguf holds model files"
        assert dst == "/models/gguf", v


def fetched_files():
    """{dest subdir/basename} of every file fetch-models.sh downloads."""
    text = FETCH.read_text()
    block = re.search(r"^MODELS='\n(.*?)^'", text, re.S | re.M).group(1)
    out = set()
    for line in block.splitlines():
        if line.strip():
            _alias, _repo, path, sub, _size = line.split("|")
            out.add(f"{sub}/{PurePosixPath(path).name}")
    return out


@pytest.mark.parametrize("mid", server_ids())
def test_model_paths_resolve(config, mid):
    args, server = docker_args(config, mid)
    mounts = {PurePosixPath(v.split(":")[1]): PurePosixPath(v.split(":")[0]) for v in values(args, "-v", "--volume")}
    paths = values(server, "-m", "--model", "--mmproj", "-md", "--model-draft")
    assert values(server, "-m", "--model"), "no -m"
    have = fetched_files()
    for p in map(PurePosixPath, paths):
        dst = next((d for d in mounts if d in p.parents), None)
        assert dst is not None, f"{mid}: {p} is not under a mount ({sorted(map(str, mounts))})"
        host = mounts[dst] / p.relative_to(dst)
        assert str(host.relative_to(MODELS_DIR / "gguf")) in have, f"{mid}: {host} is not fetched by fetch-models.sh"
    if mid == "qwen3.8-flash-next":   # the experimental build: main GGUF + MTP head
        assert len(paths) == 2
    if mid == "gemma-4-31b":          # vision: model + projector
        assert len(paths) == 2


@pytest.mark.parametrize("mid", model_ids())
def test_cpu_offload_models_get_the_large_memory_cap(config, mid):
    args, server = docker_args(config, mid)
    cpu = any("=CPU" in a for a in values(server, "-ot", "--override-tensor")) or \
        any(a in server for a in ("--n-cpu-moe", "-ncmoe", "--cpu-moe", "-cmoe")) or is_maya(config, mid)
    mem = mem_bytes(values(args, "--memory")[0])
    if cpu:
        assert mem >= 64 * 2**30, (mid, mem)   # ~17 GB of experts + n-gram pages + prompt cache
    else:
        assert 16 * 2**30 <= mem <= 48 * 2**30, (mid, mem)
    assert mem <= 72 * 2**30, "leave room in the 80 GiB VM for the gateway and the other stacks"


# ---- the Maya lab engine (walter/maya) ------------------------------------------------------------

@pytest.mark.parametrize("mid", maya_ids())
def test_maya_entry(config, mid):
    args, server = docker_args(config, mid)
    m = config["models"][mid]
    assert mid.startswith("lab-") and m.get("unlisted") is True, "the gateway's lab guard keys on the lab- id"
    assert m["capabilities"]["tools"] is False and not m.get("aliases")
    vols = sorted(values(args, "-v", "--volume"))
    assert vols == sorted([f"{MODELS_DIR}/gguf:/models/gguf:ro", f"{MODELS_DIR}/maya-data:/data",
                           "/etc/maya/glm-5.3-flash.json:/cfg/glm.json:ro"]), vols
    assert "backups" not in "".join(vols)
    # no --api-key on the command line, no MCP config, the run config is the mounted one
    assert server == ["--config", "/cfg/glm.json", "--port", "8080"], server
    assert values(args, "-e", "--env", "--env-file") == ["HOME=/tmp"]
    run = yaml.safe_load(expand(config, "x: ${maya-image}"))["x"]
    tag = run.split(":", 1)[1]
    assert re.fullmatch(r"[0-9a-f]{7}-\d+", tag), run
    assert tag.split("-")[0] in (MAYA / "Dockerfile").read_text(), "the tag names the pinned MAYA_COMMIT"


def test_maya_run_config():
    import json
    cfg = json.loads((MAYA / "glm-5.3-flash.json").read_text())
    assert "vision" not in cfg and "mcp_servers" not in cfg and "api_key" not in cfg
    env = cfg.get("env") or {}
    ram = float(env["MAYA_GLM_RAM_GB"])   # the GLM engine is not cgroup-aware: its RAM tier must fit the cap
    cap = 64   # ${mem-moe}
    assert ram + 12 <= cap, (ram, cap)
    assert cfg["log"].startswith("/data/") and cfg["tokenizer"].startswith("/data/")
    assert cfg["exe"] == "/opt/maya/build/maya" and cfg["port"] == 8080


def test_maya_entrypoint_allows_only_the_api():
    import ast
    tree = ast.parse((MAYA / "lab_entry.py").read_text())
    allow = next(n.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                 and any(getattr(t, "id", "") == "ALLOW" for t in n.targets))
    allow = ast.literal_eval(allow)
    assert allow == {"GET": {"/health", "/v1/models", "/models", "/props", "/slots"},
                     "POST": {"/v1/chat/completions", "/v1/messages"}}
    assert 'ENTRYPOINT ["python3", "/opt/maya/lab_entry.py"' in (MAYA / "Dockerfile").read_text()


# ---- the names that tie config, deploy.sh and the firewall together ------------------------------

def test_network_bridge_and_uid_agree_with_deploy_and_firewall(config):
    deploy = DEPLOY.read_text()
    net = re.search(r"\bLLAMA_NET=(\S+)", deploy).group(1)
    bridge = re.search(r"\bLLAMA_BRIDGE=(\S+)", deploy).group(1)
    uid = re.search(r"\bLLAMA_UID=(\d+)", deploy).group(1)
    assert re.search(rf"^LLAMA_IF={re.escape(bridge)}\b", FW.read_text(), re.M)
    for mid in config["models"]:
        args, _ = docker_args(config, mid)
        assert values(args, "--network") == [net]
        assert values(args, "--user")[0].split(":")[0] == uid
    assert f"uid {uid}" in FETCH.read_text()   # fetch-models.sh's umask comment names the reader
