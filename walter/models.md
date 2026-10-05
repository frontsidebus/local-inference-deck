# Models served by llama-swap

All files live under `${MODELS_DIR}/gguf/<subdir>/` on the host. Inside the llama-server
containers that directory is always mounted at `/models`, so `llama-swap/config.yaml.tmpl`
keeps the in-container paths `/models/gguf/...` unchanged whatever `MODELS_DIR` is.

Download with `llama-swap/fetch-models.sh` (installed as `${MODELS_DIR}/fetch-models.sh`, run as
`BACKEND_SSH_USER`). It resumes, retries, and checks every file against the size of the live copy.
Total is about 117 GB (about 109 GiB).

| alias | llama-swap model ID | GPU | HF repo | file | bytes | source status |
|---|---|---|---|---|---|---|
| `coder` | `qwen3.8-27b` | 0 | `unsloth/Qwen3.8-27B-GGUF` | `Qwen3.8-27B-UD-Q4_K_XL.gguf` | 17559178144 | verified (download log) |
| `coder-fast` | `qwen3.6-35b-a3b` | 1 | `unsloth/Qwen3.6-35B-A3B-GGUF` | `Qwen3.6-35B-A3B-UD-IQ4_NL_XL.gguf` | 19500506080 | verified (download log) |
| `big` | `qwen3-coder-next` | 0+1 | `unsloth/Qwen3-Coder-Next-GGUF` | `Qwen3-Coder-Next-UD-IQ4_NL.gguf` | 39234725888 | verified (download log) |
| `vision` | `gemma-4-31b` | 0+1 | `unsloth/gemma-4-31B-it-qat-GGUF` | `gemma-4-31B-it-qat-UD-Q4_K_XL.gguf` | 17287670048 | verified (download log) |
| `vision` (projector) | `gemma-4-31b` | 0+1 | `unsloth/gemma-4-31B-it-qat-GGUF` | `mmproj-BF16.gguf` | 1200726496 | verified (download log) |
| `hermes` | `hermes-4.3-36b` | 0+1 | `NousResearch/Hermes-4.3-36B-GGUF` (?) | `hermes-4_3_36b-Q4_K_M.gguf` | 21762145216 | **UNKNOWN**: copied from an earlier manual download; the repo is a best guess |

"verified" means the repo and file names come from the recorded `fetch.sh <repo> <file>` invocations
used to populate the live disk. Sizes are from the live files.

Notes
- `coder` uses llama.cpp's built-in MTP head (`--spec-type draft-mtp --spec-draft-n-max 2`). The
  Qwen3.8 GGUF embeds it, so no separate draft model is needed. See `llama-swap/BENCHMARKS.md`.
- Qwen and Gemma think by default. A small `max_tokens` can return empty content.
- `presence_penalty`: `coder-fast` runs with a server default of 1.5 (`--presence-penalty 1.5`); `coder` and `big`
  stay at 0. A request's own `presence_penalty` overrides the default. llama-server applies it only to the last 64
  tokens (`--repeat-last-n`), so it cannot break long-period loops; the output cap is still the runaway guard. Numbers
  and the trade-off are in `llama-swap/BENCHMARKS.md`.
- Split modes: `big` uses layer split (`-sm layer -ts 1,1`); `hermes` and `vision` use llama.cpp's experimental
  tensor split (`-sm tensor`, plus `--shm-size 2g` for the container through the `${tp}` macro), which gave them
  +39–67 % decode. Numbers and the fallback are in `llama-swap/BENCHMARKS.md`.
- Row split (`-sm row`) does not load on the pinned build ("does not support split buffers").
- The llama.cpp image is pinned by digest in `config.yaml.tmpl` (build 11277). Change model
  files and the image together and re-run the checks in [`llama-swap/BENCHMARKS.md`](llama-swap/BENCHMARKS.md).
- Load times since the x8/x8 riser: cold loads are disk-bound (about 20 s for `big`, 10–12 s for
  `vision` and `hermes`, 10–12 s for the coding pair at boot); a swap back to a set whose files are
  still in the page cache is much faster (`big` 7 s, `hermes` 4 s). About 112 GB of model files
  share roughly 73 GB of guest page cache, so cycling through every set keeps loads cold.
