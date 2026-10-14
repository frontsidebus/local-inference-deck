## Fit calculator

`spark-model search` and `spark-model fit` run on the workstation. They use `walter/models/lib/` from the repo
checkout (stdlib only: `gguf.py`, `hf.py`, `fit.py`, `cli_fit.py`) and never download a model. `fit` reads only the
GGUF headers, with HTTP Range requests on `resolve/<sha>/<file>`. Each read starts at 8 MiB and doubles until the
header parses, up to a cap of 256 MiB.

```
spark-model search qwen3 [--author ORG] [--limit 20] [--json]
spark-model fit ORG/REPO[@REV] [--file '*Q4_K_M*'] [--ctx 65536] [--mmproj] [--kv-type q8_0] [--json]
```

- **search** lists GGUF repos with these badges:
  - `allowed` when the org is in `LAB_HF_ORGS`, read from the environment or the repo's `site.env`. `pull` enforces
    the allow-list; search only shows it.
  - licence and gated status;
  - a rough fit, from the Hub's parameter count at about Q4 (4.85 bits per parameter).
- **fit** resolves the revision to a sha and picks a quant:
  - with `--file`, the one that matches;
  - without it, the first available of Q4_K_M, UD-Q4_K_XL, … Q4_0.

  It then reads the header of every shard and prints:
  - per-GPU VRAM;
  - placement (`gpu0|gpu1|both|both+cpu-experts|none`), the `-sm` mode and an `-ot` regex for CPU experts;
  - estimated decode speed and class (A >80, B 30–80, C 10–30, D <10 tok/s; D is refused);
  - the licence flag and whether the repo is gated;
  - whether the GGUF has a chat template, and its size;
  - warnings: an architecture missing from llama.cpp b11277 (the frozen `LLM_ARCH_NAMES` list in `fit.py`), no
    template, no tool-call markers, a context longer than the training context, a shard-count mismatch, a file without
    an LFS sha256.

  The `registry_fit` field of `--json` is what `pull` stores in the registry entry's `fit`.
- **Token:** pass `--token-file PATH`. The token goes only in an `Authorization` header, and only to Hub hosts. It is
  dropped on redirects to the CDN or Xet bridge, as `huggingface_hub` does. It never appears in argv, URLs or output.

### The model

Units are GiB (2^30 bytes): nvidia-smi MiB / 1024. The serving flags are the repo's
`${common}` (`--fit off -ngl all -np 1 -fa on -ctk/-ctv q8_0`) with n_ubatch 512.

- **Weights:** exact tensor bytes from the headers, placed as llama.cpp b11277 places them:
  - `token_embd` and `per_layer_token_embd` stay on the CPU;
  - with tied embeddings, a copy of `token_embd` goes to the output GPU;
  - with `-sm layer -ts 1,1`, block i goes to GPU0 if i/(n_layer+1) < 0.5, else GPU1; the output goes to GPU1;
  - `-sm tensor` splits weights and KV evenly;
  - nextn/MTP layers load only with MTP.
- **KV** (q8_0 = 34/32 bytes per element):
  - attention layers only, which means `full_attention_interval` / `recurrent_layers` / zero-kv-head hybrids;
  - SWA layers hold pad256(window + 512) cells (the pattern comes from the GGUF or the per-arch default);
  - MLA stores `kv_lora_rank + rope` with no V;
  - layers after `shared_kv_layers` hold nothing;
  - indexer keys (QSA/DSA) and the F32 recurrent state are added, the state ×3 with MTP n_max 2.
- **Fixed costs per GPU:**
  - a 0.35 CUDA context;
  - a compute buffer of max(1.05, logits n_vocab × 512 × 4 B on the output GPU + 0.5 × the largest layer's f16 K+V);
  - mmproj file size + 0.30 (on the model's GPU, or GPU0 when the model uses both);
  - an MTP/draft context of 0.42, plus the draft's own weights, KV and logits for a separate draft file.
- **Fits:** ≤ 22.5 per GPU, which is 24 minus 1.5 headroom. Experts in RAM must stay within `--ram-budget` (32 GiB).
- **Auto placement** (lifecycle.md):
  1. one GPU (GPU1 by default);
  2. otherwise both: dense models use `-sm tensor`, MoE uses `-sm layer`;
  3. otherwise MoE with the routed experts of the fewest layers on the CPU, taking each GPU's half from its lowest
     layer up, as `flash` does;
  4. otherwise `none`.
- **Speed:** active bytes per token (non-expert plus expert × used/total) divided by bandwidth:
  - 0.74 × 936 GB/s for dense models, 0.40 × 936 GB/s for MoE;
  - `-sm tensor` × 1.45;
  - CPU experts at an effective 19 GB/s of guest RAM. lifecycle.md assumed 60; the flash variants measure about 19.

  Speculative decoding is not modelled.

### Calibration (2026-10-14; live headers read with `head -c`, measured VRAM after load, GiB)

| config | estimate GPU0 / GPU1 | measured | residual | decode est / measured |
|---|---|---|---|---|
| coder, 131072, MTP n2, GPU0 | 22.45 | 22.11 | +0.34 | (MTP not modelled) |
| coder, 131072, no MTP | 21.15 | 20.80 | +0.35 | 42.0 / 40.5 |
| vision + mmproj, 131072, tensor | 13.83 / 12.41 | 14.30 / 13.03 | −0.47 / −0.62 | 58.1 / 54.7 |
| vision + mmproj, 131072, layer | 13.72 / 12.52 | 14.11 / 13.05 | −0.39 / −0.53 | 40.1 / 39.5 |
| hermes, 65536, tensor | 15.57 / 15.57 | 14.98 / 14.98 | +0.59 / +0.59 | 47.1 / 50.4 |
| hermes, 65536, layer | 15.69 / 15.46 | 15.19 / 14.96 | +0.50 / +0.50 | 32.5 / 34.4 |
| flash V7 (live: 16 CPU layers, MTP head on GPU1) | 20.76 / 23.24 | 20.99 / 22.93 | −0.23 / +0.31 | (MTP) |
| flash V0 (14 CPU layers) | 19.82 / 22.01 | 19.92 / 21.87 | −0.10 / +0.14 | 35.7 / 35.7 |
| flash V1 (11) | 21.70 / 22.83 | 21.81 / 22.69 | −0.11 / +0.14 | 39.7 / 38.5 |
| flash V6 (17) | 17.12 / 22.01 | 17.23 / 21.87 | −0.11 / +0.14 | 32.4 / 32.7 |
| flash V5 (17, MTP head on GPU0) | 21.36 / 22.01 | 21.29 / 21.96 | +0.07 / +0.05 | (MTP) |
| coder-fast, 131072, GPU1 | – / 20.44 | not recorded | uncalibrated | 147.2 / 153.2 |
| big, 131072, layer | 20.98 / 19.71 | not recorded | uncalibrated | 126.0 / 125.5 |

- **Range:** residuals run from −0.62 to +0.59 GiB per GPU, inside the ±1.5 target. Decode estimates are within 7 %.
- **Bias by model:**
  - Gemma-4 (vision) comes out about 0.5 low, probably its vision encoder and 512-dim heads;
  - hermes comes out about 0.5 high.
- **Limits of the data:**
  - flash was measured on build 11459, not 11277;
  - the live flash config uses GPU1 beyond the 1.5 GiB headroom rule (22.93), so the calculator would not choose V7.
- **Tests:** `walter/tests/test_spark_model_fit.py` rebuilds these models from small synthetic headers (per-layer
  byte totals plus the metadata keys `fit.py` reads, made with `gguf.build_test_gguf`). It fails if a residual drifts
  past 0.7 GiB. After an image bump or a new model measurement, update `MEASURED` and the table above.

### Hub API: verified vs assumed

- **Verified on 2026-10-14**, against `huggingface_hub` main (`hf_api.py`, `file_download.py`, `utils/_http.py`)
  and read-only calls on public repos:
  - `GET /api/models/<repo>/revision/<rev>?blobs=true` returns `sha`, `siblings[{rfilename, size, lfs{sha256}}]`,
    `cardData{license, license_name, base_model, extra_gated_prompt}`, `gated` (false/"auto"/"manual") and
    `gguf{architecture, total, context_length, chat_template}`;
  - `GET /api/models/<repo>/tree/<sha>?recursive=true` returns `{type, path, size, lfs{oid}}` and paginates with
    `Link: rel="next"`;
  - `GET /api/models?search&author&filter=gguf&sort&direction=-1&limit&expand[]=…` works with
    `expand[]=gguf,cardData,gated,…`;
  - `resolve/<sha>/<path>` answers 302 to the Xet bridge CDN, and a Range request there gets a 206 with
    `Content-Range`;
  - errors come back as `X-Error-Code`: `GatedRepo` (401), `RevisionNotFound` (404). A missing repo answers 401
    "Invalid username or password." with no code.
- **Assumed:**
  - the `siblings` list is complete for large repos (the tree fallback runs only when sizes are missing);
  - gated repos behave the same with a token;
  - the search `gguf` summary describes the main model file. It is sometimes an mmproj or MTP file, so
    `search` shows `?` for those.
