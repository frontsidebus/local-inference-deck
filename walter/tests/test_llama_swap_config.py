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
        assert mem >= 64 * 2**30, (mid, mem)   # flash: ~47 GB of mapped experts + n-gram pages + prompt cache
    else:
        assert 16 * 2**30 <= mem <= 48 * 2**30, (mid, mem)
    assert mem <= 72 * 2**30, "leave room in the 80 GiB VM for the gateway and the other stacks"


@pytest.mark.parametrize("mid", server_ids())
def test_moe_cache_settings(config, mid):
    """--moe-cache-mib: a build that has it on several GPUs, experts on the CPU for it to cache, and an even split."""
    args, server = docker_args(config, mid)
    mib = values(server, "--moe-cache-mib")
    if not mib:
        return
    assert "${image-lab}" in config["models"][mid]["cmd"], "the MoE cache needs b11749 (#29887 + #30112 multi-GPU)"
    assert any("_exps" in a and "=CPU" in a for a in values(server, "-ot", "--override-tensor")), "nothing to cache"
    gpus = values(args, "--gpus")[0].count(",") + 1
    if gpus > 1:
        # the size is a TOTAL split over the GPUs by -ts (by free memory without it): keep the split explicit and even
        assert values(server, "-ts", "--tensor-split") == [",".join(["1"] * gpus)], mid
    assert 0 < int(mib[0]) <= gpus * 16 * 1024, mid


# ---- the Maya lab engine (walter/maya) ------------------------------------------------------------

# llama-swap id -> (run config in walter/maya, its path in the container)
MAYA_RUN = {"lab-glm-5.3-flash": ("glm-5.3-flash.json", "/cfg/glm.json"),
            "lab-deepseek-v4.1-flash": ("deepseek-v4.1-flash.json", "/cfg/ds4.json")}


def test_maya_ids_have_run_configs():
    assert sorted(maya_ids()) == sorted(MAYA_RUN)


@pytest.mark.parametrize("mid", maya_ids())
def test_maya_entry(config, mid):
    args, server = docker_args(config, mid)
    m = config["models"][mid]
    name, cfg_path = MAYA_RUN[mid]
    assert mid.startswith("lab-") and m.get("unlisted") is True, "the gateway's lab guard keys on the lab- id"
    assert m["capabilities"]["tools"] is False and not m.get("aliases")
    vols = sorted(values(args, "-v", "--volume"))
    assert vols == sorted([f"{MODELS_DIR}/gguf:/models/gguf:ro", f"{MODELS_DIR}/maya-data:/data",
                           f"/etc/maya/{name}:{cfg_path}:ro"]), vols
    assert "backups" not in "".join(vols)
    # no --api-key on the command line, no MCP config, the run config is the mounted one
    assert server == ["--config", cfg_path, "--port", "8080"], server
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


def test_maya_ds4_run_config(config):
    import json
    cfg = json.loads((MAYA / "deepseek-v4.1-flash.json").read_text())
    assert "vision" not in cfg and "mcp_servers" not in cfg and "api_key" not in cfg
    assert cfg["exe"] == "/opt/maya/build/maya-ds4" and cfg["port"] == 8080 and cfg["gpu"] == [0, 1]
    a = cfg["args"]
    opt = {a[i]: a[i + 1] for i in range(len(a) - 1) if a[i].startswith("--")}
    # image input off (it fetches URLs / reads files), no drafter, pack and profile on the rw data mount
    assert opt["--vision"] == "off" and opt["--dspark"] == "none"
    assert opt["--pack"].startswith("/data/ds4/") and opt["--expert-profile"].startswith(opt["--pack"] + "/")
    ctx = config["models"]["lab-deepseek-v4.1-flash"]["capabilities"]["context"]
    assert int(opt["--max-context"]) == ctx
    assert int(opt["--cpu-threads"]) <= 12, "leave vCPUs for the gateway and the other stacks"
    # the debug dump variables write files wherever they point
    assert not any(k.startswith(("DS4_DUMP", "DS4_DSPARK_DUMP")) for k in cfg.get("env") or {})
    assert cfg["log"].startswith("/data/") and cfg["tokenizer"].startswith(opt["--pack"] + "/")
    # the engine sizes its page-cache tier from the cgroup: the cap is the tier, below the VM's 80 GiB
    args, _ = docker_args(config, "lab-deepseek-v4.1-flash")
    assert mem_bytes(values(args, "--memory")[0]) == 72 * 2**30


def test_maya_entrypoint_allows_only_the_api():
    import ast
    tree = ast.parse((MAYA / "lab_entry.py").read_text())
    allow = next(n.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                 and any(getattr(t, "id", "") == "ALLOW" for t in n.targets))
    allow = ast.literal_eval(allow)
    assert allow == {"GET": {"/health", "/v1/models", "/models", "/props", "/slots"},
                     "POST": {"/v1/chat/completions", "/v1/messages"}}
    assert 'ENTRYPOINT ["python3", "/opt/maya/lab_entry.py"' in (MAYA / "Dockerfile").read_text()


def _lab_entry(monkeypatch):
    """lab_entry.py loaded against a stub serve.server (the real one is only in the image)."""
    import runpy, sys, types

    class Base:
        def __init__(self, path, body=b""):
            self.path, self.raw, self.sent, self.seen = path, body, None, None

        def _json(self, status, obj):
            self.sent = status

        def _body(self):
            return self.raw

        def do_GET(self):
            self.seen = "GET"

        def do_POST(self):
            self.seen = ("POST", self._body())

    stub = types.ModuleType("serve.server")
    stub.make_handler = lambda svc: Base
    monkeypatch.setitem(sys.modules, "serve", types.ModuleType("serve"))
    monkeypatch.setitem(sys.modules, "serve.server", stub)
    ns = runpy.run_path(str(MAYA / "lab_entry.py"))
    return ns, stub.make_handler(None)


@pytest.mark.parametrize("method", ["OPTIONS", "HEAD", "DELETE", "PUT", "PATCH", "TRACE", "CONNECT"])
def test_maya_entrypoint_refuses_other_methods(monkeypatch, method):
    ns, H = _lab_entry(monkeypatch)
    h = H("/v1/responses/abc")
    getattr(h, "do_" + method)()
    assert h.sent == 404 and h.seen is None
    h = H("/admin")
    h.do_GET()
    assert h.sent == 404 and h.seen is None


@pytest.mark.parametrize("req,want", [
    ({}, 100), ({"max_tokens": 0}, 100), ({"max_tokens": -1}, 100), ({"max_tokens": 5000}, 100),
    ({"max_completion_tokens": 5000, "max_tokens": 10}, 100), ({"max_tokens": True}, 100),
    ({"max_tokens": "50"}, 100), ({"max_tokens": 50}, 50), ({"max_completion_tokens": 100}, 100),
])
def test_maya_entrypoint_clamps_output(monkeypatch, req, want):
    import json
    ns, H = _lab_entry(monkeypatch)
    ns["clamp_body"].__globals__["CAP"] = 100
    for path in ("/v1/chat/completions", "/v1/messages?beta=true"):
        h = H(path, json.dumps(dict(req, messages=[])).encode())
        h.do_POST()
        out = json.loads(h.seen[1])
        assert out.get("max_completion_tokens", out.get("max_tokens")) == want and out["messages"] == []
    # no cap configured, or a body that is not a JSON object: passed through untouched
    assert ns["clamp_body"](b'{"max_tokens": 9}', "/v1/chat/completions", 0) == b'{"max_tokens": 9}'
    assert ns["clamp_body"](b"[1]", "/v1/chat/completions", 100) == b"[1]"
    assert ns["clamp_body"](b"not json", "/v1/chat/completions", 100) == b"not json"


def test_maya_entrypoint_reads_the_cap(monkeypatch, tmp_path):
    import json
    ns, _ = _lab_entry(monkeypatch)
    ds4 = json.loads((MAYA / "deepseek-v4.1-flash.json").read_text())
    assert ns["_config_cap"](["x", "--config", str(MAYA / "deepseek-v4.1-flash.json")]) == ds4["lab_max_tokens"]
    assert ns["_config_cap"](["x", "--config", str(MAYA / "glm-5.3-flash.json")]) == 0
    assert ds4["fit_max_tokens"] is True, "a long prompt plus the cap would otherwise be a 400"
    bad = tmp_path / "bad.json"
    for v in ("16384", -1, True, 1.5):
        bad.write_text(json.dumps({"lab_max_tokens": v}))
        with pytest.raises(SystemExit):
            ns["_config_cap"](["x", "--config", str(bad)])


def test_maya_image_ships_the_ds4_profile():
    # the ds4 pack's expert-profile.bin is seeded from it (README.md); without it placement starts by expert index
    assert "data/expert-profile-ds4.bin" in (MAYA / "Dockerfile").read_text()


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
