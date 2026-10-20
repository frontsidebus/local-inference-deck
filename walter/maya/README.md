# Project Maya (lab engine)

[Project Maya](https://github.com/mw00/project-maya) (MIT) is a standalone MoE engine with an OpenAI-compatible server.
It keeps the hottest experts in VRAM, the next tier in RAM and the rest on NVMe. That lets Walter try models llama.cpp
cannot run here: **DeepSeek-V4.1-Flash-1017** (552B + 196B Engram, arch `deepseek41`, not in our llama.cpp) and
**GLM-5.3-Flash** (321B, ~18B active, arch `glm5-next`). It is a **lab** engine: not a judge, no tools, behind the
gateway like every other model, and it uses both GPUs (coder and coder-fast unload while it runs).

Status (2026-10-20): step 1, the image. Nothing is downloaded, packed or served yet.

## Image

`Dockerfile` builds `walter-maya:<commit7>` from pinned inputs only:

| Input | Pin |
|---|---|
| CUDA 12.8.1 devel / runtime (Ubuntu 24.04) | image digests |
| Maya | commit `8d9a9b5d…` (v1.1.17), git-checked; the build refuses another HEAD |
| llama.cpp (ggml kernels, gguf-py) | commit `3cf03257…` = Maya's `third_party/ggml/VERSION.txt`, git-checked |
| Python packages | `requirements.lock`, `--require-hashes`, wheels only |

Maya's installer (`maya.sh` / `maya.py`) is not used: it installs unpinned PyPI packages, downloads llama.cpp as an
unchecked zip, needs the GPUs at build time and writes configs into the checkout. The image builds the two engines
(`maya` for GLM, `maya-ds4` for DeepSeek) for sm_86 only and copies the server, the pack tools and the licences. It has
no git, no `.git` and no compiler, so the server's self-update (`POST /api/update`: `git fetch` + `merge`) cannot run.

`GGML_NATIVE` is on (`-march=native`, as Maya builds it). Build on a CPU with the runtime CPU's vector extensions
(compare `grep -m1 flags /proc/cpuinfo` on both), or pass `--build-arg PORTABLE=ON` for an AVX2 baseline (slower CPU
experts).

```sh
docker build -t walter-maya:8d9a9b5 walter/maya            # ~20-40 min, no GPU needed
docker save walter-maya:8d9a9b5 | ssh "$BACKEND_SSH_USER@$BACKEND_LAN_IP" docker load
```

To update: change `MAYA_COMMIT` (and `LLAMA_CPP_COMMIT` to that commit's `third_party/ggml/VERSION.txt`), read the
diff of `serve/` and the engine sources first, and re-tag. Regenerate `requirements.lock` in `python:3.12-slim`:
`pip download --only-binary=:all: jinja2 regex 'numpy<2.4' pyyaml tqdm psutil`, then one
`name==version --hash=sha256:…` line per wheel.

## Sandbox (as the llama-server containers)

Run it with the same `docker run` restrictions as every llama-swap model (`${run}` in
`walter/llama-swap/config.yaml.tmpl`): uid 10010, `--read-only`, `--cap-drop ALL`, `no-new-privileges`, the
`llama-net` network (no egress), port published on 127.0.0.1 only, `--pids-limit`, a memory cap, and
`/models/gguf` read-only. Extra for Maya:

- a writable `/data` volume (packs, expert profile, engine log, slots) and a `/tmp` tmpfs;
- the run config (`/cfg/*.json`) mounted read-only, root-owned: it names the engine binary, its arguments and its
  environment, so it is trusted. The server's attempts to save settings next to it fail harmlessly (they are caught);
- the API key via `STRATA_API_KEY` (an env file), never `--api-key` on the command line.

Measured from the source (security review of 8d9a9b5, 2026-10-20):

| Behaviour | Where | Handling |
|---|---|---|
| Image inputs: `http(s)://` fetch and **local file read** of any path in a request | `serve/server.py` `Vision.load` | `--vision off` in the engine args (no vision tower in the pack either) |
| Admin POSTs (`/api/context` restarts the engine, `/settings`, `/api/update`) guarded only by the key | `serve/server.py` `_own_page` | the gateway forwards only `/v1/*`; the key stays with llama-swap |
| Update check (GitHub releases API) on dashboard request | `serve/update.py` | `MAYA_UPDATE_CHECK=0`; no egress; no `.git` |
| MCP client (spawns commands / opens URLs from the config) | `serve/mcp.py` | no `mcp_servers` / `--mcp-config` in the config |
| Telemetry | `serve/telemetry.py` | local only (NVML, `/proc`) |
| Request bodies up to 256 MiB, unbounded threads | `serve/server.py` | `STRATA_MAX_BODY_MIB=32` (image default); the memory cap |
| Native code | `src/`, `include/` | no exec, sockets or network; reads `/proc`, `/sys`; needs no capability |

Memory: `maya-ds4` sizes its RAM use from the cgroup (`memory.max`, `ds4_model.cu` `ram_avail`) and by default reads
experts through the page cache (reclaimable), so the container cap holds. The GLM engine sizes its RAM tier from
`MemAvailable` minus 6 GB and is **not** cgroup-aware: always set `MAYA_GLM_RAM_GB` below the container cap.

## Models (step 2+, each load needs the owner's OK)

| Model | Files (HF) | Size | Expected on 2x 3090 |
|---|---|---|---|
| GLM-5.3-Flash Maya-S24 | `peasantsmith/GLM-5.3-Flash-Maya-GGUF` `Maya-S24/` (3 shards, sha256 in Maya's `maya.py` MODELS) | 94.7 GB | README: 53 tok/s decode on 2x 3090 with 128 GB RAM; less here |
| DeepSeek-V4.1-Flash-1017 UD-Q4_K_XL | `unsloth/DeepSeek-V4.1-Flash-1017-GGUF` `UD-Q4_K_XL/` @ `c991b28e…` (9 shards, sha256 in `tools/ds4/download_gguf.json`) | 500.7 GB | low single-digit tok/s (NVMe-bound; no 2x 24 GB numbers published) |
| DeepSeek-V4.1-Flash-1017 Maya-S | `peasantsmith/DeepSeek-V4.1-Flash-1017-Maya-GGUF` `Maya-S/` @ `276e0bad…` | 339.5 GB | as above, less to read |

`peasantsmith` is Maya's own quant uploader (a single account): download by pinned revision and check every file
against the sha256 in Maya's manifests. Skip `dspark/`, the vision files and the DFlash2 drafter (CC BY-NC-ND).
These are not lab-registry models (`spark-model` handles llama-server GGUFs); they live under
`/models/gguf/maya/<model>/` and the packs under `/data`.

Packing is CPU-only and writes outside the model mount:

```sh
# DeepSeek: --vision must name an empty dir (else <model>/../vision is packed); no dspark/ next to the quant dir
python3 /opt/maya/tools/ds4/pack_gguf.py --model /models/gguf/maya/DeepSeek-V4.1-Flash-1017/UD-Q4_K_XL \
  --out /data/ds4/UD-Q4_K_XL/maya-ds4 --vision /data/empty --shards <the 9 shard names>
# GLM: the engine opens the GGUFs at <pack>/..: symlink them into /data first
python3 /opt/maya/tools/iq_pack.py --gguf /data/glm/Maya-S24/<shard 1> --out /data/glm/Maya-S24/pack --compat-bf16
```

Run configs mirror Maya's `write_config` / `write_config_ds4` (`maya.py`), with `--vision off`, `--dspark none`, no MCP
and `"log": "/data/logs/<model>.log"`.
