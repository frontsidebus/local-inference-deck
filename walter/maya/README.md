# Project Maya (lab engine)

[Project Maya](https://github.com/mw00/project-maya) (MIT) is a standalone MoE engine with an OpenAI-compatible server.
It keeps the hottest experts in VRAM, the next tier in RAM and the rest on NVMe. That lets Walter try models llama.cpp
cannot run here: **DeepSeek-V4.1-Flash-1017** (552B + 196B Engram, arch `deepseek41`, not in our llama.cpp) and
**GLM-5.3-Flash** (321B, ~18B active, arch `glm5-next`). It is a **lab** engine: not a judge, no tools, behind the
gateway like every other model, and it uses both GPUs (coder and coder-fast unload while it runs).

Status (2026-10-20): **GLM-5.3-Flash Maya-S24 serves** as llama-swap's `lab-glm-5.3-flash` (lab keys only, through the
gateway's lab guard): ~39 t/s decode, 375-916 t/s prompt (benchmark below). DeepSeek-V4.1-Flash is not set up yet.

## Image

`Dockerfile` builds `walter-maya:<commit7>-<n>` from pinned inputs only (`<n>`: this directory's revision of the
image; now `8d9a9b5-2`, the tag in llama-swap's `maya-image` macro):

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
docker build -t walter-maya:8d9a9b5-2 walter/maya          # ~20-40 min, no GPU needed
docker save walter-maya:8d9a9b5-2 | ssh "$BACKEND_SSH_USER@$BACKEND_LAN_IP" docker load
# check: the same layers on both sides (the image IDs can differ between Docker image stores)
docker image inspect walter-maya:8d9a9b5-2 --format '{{json .RootFS.Layers}}' | sha256sum
```

To update: change `MAYA_COMMIT` (and `LLAMA_CPP_COMMIT` to that commit's `third_party/ggml/VERSION.txt`), read the
diff of `serve/` and the engine sources first, and re-tag. Regenerate `requirements.lock` in `python:3.12-slim`:
`pip download --only-binary=:all: jinja2 regex 'numpy<2.4' pyyaml tqdm psutil`, then one
`name==version --hash=sha256:…` line per wheel.

The entrypoint is `lab_entry.py`: Maya's server unchanged, behind an allow-list of routes. Only `GET /health`,
`/v1/models`, `/models`, `/props`, `/slots` and `POST /v1/chat/completions`, `/v1/messages` answer; everything else
(the web app, `/settings`, `/api/*`, `/mcp`, `/metrics`, `/v1/strata/*`) is a 404 before Maya sees it. llama-swap
passes any path to a model through `/upstream/<id>/`, so without it every llama-swap key holder could restart the
engine or change its defaults.

## Sandbox (as the llama-server containers)

Run it with the same `docker run` restrictions as every llama-swap model (`${run}` in
`walter/llama-swap/config.yaml.tmpl`): uid 10010, `--read-only`, `--cap-drop ALL`, `no-new-privileges`, the
`llama-net` network (no egress), port published on 127.0.0.1 only, `--pids-limit`, a memory cap, and
`/models/gguf` read-only. Extra for Maya:

- a writable `/data` volume (packs, expert profile, engine log, slots) and a `/tmp` tmpfs;
- the run config (`/cfg/*.json`) mounted read-only, root-owned: it names the engine binary, its arguments and its
  environment, so it is trusted. The server's attempts to save settings next to it fail harmlessly (they are caught);
- no API key behind llama-swap: like the llama-server containers it listens on 127.0.0.1 only, llama-swap holds the
  key, and the allow-list keeps the admin routes away. A run by hand outside llama-swap sets one with
  `--env-file` (`STRATA_API_KEY=...`), never `--api-key` on the command line.

Measured from the source (security review of 8d9a9b5, 2026-10-20):

| Behaviour | Where | Handling |
|---|---|---|
| Image inputs: `http(s)://` fetch and **local file read** of any path in a request | `serve/server.py` `Vision.load` | GLM: no `vision` entry in the run config, so a request with an image is refused before any load; DeepSeek: `--vision off` |
| Admin POSTs (`/api/context` restarts the engine, `/settings`, `/api/update`) guarded only by the key | `serve/server.py` `_own_page` | `lab_entry.py` allow-list (404); the gateway forwards only `/v1/*` |
| Update check (GitHub releases API) on dashboard request | `serve/update.py` | `MAYA_UPDATE_CHECK=0`; no egress; no `.git` |
| MCP client (spawns commands / opens URLs from the config) | `serve/mcp.py` | no `mcp_servers` / `--mcp-config` in the config |
| Telemetry | `serve/telemetry.py` | local only (NVML, `/proc`) |
| Request bodies up to 256 MiB, unbounded threads | `serve/server.py` | `STRATA_MAX_BODY_MIB=32` (image default); the memory cap |
| Native code | `src/`, `include/` | no exec, sockets or network; reads `/proc`, `/sys`; needs no capability |

Memory: `maya-ds4` sizes its RAM use from the cgroup (`memory.max`, `ds4_model.cu` `ram_avail`) and by default reads
experts through the page cache (reclaimable), so the container cap holds. The GLM engine sizes its RAM tier from
`MemAvailable` minus 6 GB and is **not** cgroup-aware: always set `MAYA_GLM_RAM_GB` below the container cap.

## Models

| Model | Files (HF) | Size | Expected on 2x 3090 |
|---|---|---|---|
| GLM-5.3-Flash Maya-S24 | `peasantsmith/GLM-5.3-Flash-Maya-GGUF` `Maya-S24/` (3 shards, sha256 in Maya's `maya.py` MODELS) | 94.7 GB | measured ~39 t/s decode (below; Maya's README: 53 with 128 GB RAM) |
| DeepSeek-V4.1-Flash-1017 UD-Q4_K_XL | `unsloth/DeepSeek-V4.1-Flash-1017-GGUF` `UD-Q4_K_XL/` @ `c991b28e…` (9 shards, sha256 in `tools/ds4/download_gguf.json`) | 500.7 GB | low single-digit tok/s (NVMe-bound; no 2x 24 GB numbers published) |
| DeepSeek-V4.1-Flash-1017 Maya-S | `peasantsmith/DeepSeek-V4.1-Flash-1017-Maya-GGUF` `Maya-S/` @ `276e0bad…` | 339.5 GB | as above, less to read |

`peasantsmith` is Maya's own quant uploader (a single account): download by pinned revision and check every file
against the sha256 in Maya's manifests. Skip `dspark/`, the vision files and the DFlash2 drafter (CC BY-NC-ND).
These are not lab-registry models (`spark-model` handles llama-server GGUFs); they live under
`/models/gguf/maya/<model>/` and the packs under `/data`.

`fetch.sh` (installed as `/models/maya-fetch.sh`) downloads a model's files by pinned commit and checks each against
its sha256 before giving it its name: `nohup /models/maya-fetch.sh glm >> /models/logs/maya-fetch.log 2>&1 &`
(GLM: 94.7 GB, ~20 min on Walter's link). deploy.sh makes `/models/maya-data` (uid 10010, mounted rw at `/data`)
and the links to the GGUFs in it.

Packing is CPU-only (GLM: 9 s, 0.9 GB) and writes outside the model mount. Run it in the image, sandboxed:

```sh
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --user 10010:10010 \
  --tmpfs /tmp --memory 32g --memory-swap 32g -v /models/gguf:/models/gguf:ro -v /models/maya-data:/data \
  --entrypoint python3 walter-maya:8d9a9b5-2 /opt/maya/tools/iq_pack.py \
  --gguf /data/glm/Maya-S24/GLM-5.3-Flash-Maya-S24-IQ2_XXS_S-00001-of-00003.gguf --out /data/glm/Maya-S24/pack --compat-bf16
```

The commands inside the image:

```sh
# DeepSeek: --vision must name an empty dir (else <model>/../vision is packed); no dspark/ next to the quant dir
python3 /opt/maya/tools/ds4/pack_gguf.py --model /models/gguf/maya/DeepSeek-V4.1-Flash-1017/UD-Q4_K_XL \
  --out /data/ds4/UD-Q4_K_XL/maya-ds4 --vision /data/empty --shards <the 9 shard names>
# GLM: the engine opens the GGUFs at <pack>/..: symlink them into /data first
python3 /opt/maya/tools/iq_pack.py --gguf /data/glm/Maya-S24/<shard 1> --out /data/glm/Maya-S24/pack --compat-bf16
```

Run configs mirror Maya's `write_config` / `write_config_ds4` (`maya.py`), with no MCP and
`"log": "/data/logs/<model>.log"`. `glm-5.3-flash.json` (installed as `/etc/maya/glm-5.3-flash.json`): no `vision`
entry (the server then refuses images before reading them), 32768 context, both GPUs (auto layer split),
`MAYA_GLM_RAM_GB=46` under the 64g cap. DeepSeek's will add `--vision off --dspark none`.

## GLM-5.3-Flash on Walter (2026-10-20)

Maya-S24, `walter-maya:8d9a9b5`, 2x 3090 (Gen4 x8 each), 80 GiB VM. Load ~35 s with the files in page cache.
Placement (the engine's own, `--expert-cache auto`): layers 0-24 on GPU0, 25-44 on GPU1; 83 % of the routed experts
in VRAM (19.2 + 19.4 GB pools), the rest in a 46 GB pinned RAM tier, none read from disk; ~600 MiB VRAM left per
GPU. The container used 51 GiB of its 64 GiB cap and 22 GiB stayed available on the VM. The built-in MTP block drafts.

Method as BENCHMARKS.md's `flash` runs (temperature 0, thinking off; speeds from the response `timings`):

| Case | Prompt tokens | Prompt t/s | Output tokens | Decode t/s |
|---|---|---|---|---|
| 1.5k review prompt | 1749 | 375 | 300 | 38.9 |
| short | 24 | (23) | 24 | 29.0 |
| second 1.5k prompt | 1746 | 375 | 300 | 39.8 |
| first again | 1751 | 375 | 300 | 39.4 |
| 8k prompt | 9385 | 916 | 200 | 36.5 |
| thinking on (effort high), proof + Pell equation | 59 | | 2977 | 33.9 |
| story, temperature 1.0, streamed | 26 | | 864 | 29.8 (MTP acceptance 53 %) |

The answers were correct (the √2 proof; all Pell solutions below 100). GPUs peaked at 78 °C and 293 W. Maya's
README gives 53 t/s on 2x 3090 with 128 GB RAM.
