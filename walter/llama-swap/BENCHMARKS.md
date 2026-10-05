# Walter benchmarks

## 2026-10-01: MTP speculative decoding for `coder` (qwen3.8-27b, GPU0)

Setup: llama.cpp b11277 (pinned digest), Qwen3.8-27B-UD-Q4_K_XL with its embedded MTP head (`blk.64.nextn.*`, no separate draft GGUF needed). Flags: `-c 131072 -ctk/-ctv q8_0 -np 1 --fit off -ngl all -sm none`. Sampling is the server default (temp 0.6, top-p 0.95, top-k 20). Requests went straight to llama-swap `$BACKEND_WG_IP:8080` from Walter. Numbers are from `timings.predicted_per_second`. Thinking off: 3 prompts x 3 runs (mean). Thinking on: 1 run per prompt, capped at 4096 tokens. (Since 2026-10-03 the live config also passes `-n 32768`, an output backstop; it does not change speed, and every benchmark request set its own limit anyway.)
Prompts: code = LRU cache plus pytest (~1000 tokens), prose = speculative decoding explainer (~550 tokens), refactor = 2070-token prompt holding a ~170-line module (~1000 tokens out).

| config | code tok/s | prose tok/s | refactor tok/s | mean (think off) | think-on mean (code/prose/refactor) | acceptance (code/prose/refactor) | GPU0 VRAM | ctx |
|---|---|---|---|---|---|---|---|---|
| baseline (no spec) | 40.8 | 40.6 | 40.2 | **40.5** | 40.1 (40.3/40.2/39.7) | n/a | 21.3 GB | 131072 |
| MTP n-max 1 | 66.8 | 60.0 | 63.0 | 63.3 (+56%) | n/a | 0.95/0.75/0.86 | 22.5 GB | 131072 |
| **MTP n-max 2 (chosen)** | 82.2 | 65.7 | 73.9 | **73.9 (+83%)** | 69.9 (71.3/76.8/61.5) (+74%) | 0.93/0.65/0.82 | 22.6 GB | 131072 |
| MTP n-max 3 | 88.3 | 61.4 | 75.7 | 75.1 (+86%) | n/a | 0.89/0.52/0.73 | 22.7-23.1 GB | 131072 |
| MTP n-max 3, p-min 0.5 | 84.9 | 59.5 | 72.0 | 72.1 | n/a | 0.89/0.66/0.77 | 22.7 GB | 131072 |
| MTP n-max 3, -ctkd/-ctvd q8_0 | 88.2 | 61.7 | 75.7 | 75.2 | 71.1 (71.0/83.6/58.8) | 0.89/0.52/0.73 | 23.0 GB | 131072 |

Spread between runs was within about 2 tok/s for each prompt. Prompt processing is unchanged by MTP: cold, it ran at about 1160 tok/s with MTP against 1250 tok/s at baseline on the 2K refactor prompt. Repeat runs hit the prompt cache, so only 4 tokens were processed. Cold load of `coder` to its first token took about 3.5 s with the GGUF already in the page cache, both with and without MTP. Draft-KV type flags made no measurable difference, and the greedy output was identical, so the MTP context appears to follow the target KV settings.

Correctness, checked with n-max 2:
- Greedy check (temperature 0, top-k 1, 2 prompts, 400 and 300 tokens): output was byte-identical to baseline for n-max 2 and 3. n-max 1 and the n-max 3 p-min 0.5 run each diverged on one prompt after about 15 lines. Both versions are valid code, so this looks like a numeric difference from the batched verify kernels, not a sampling bug.
- Tool calling with `/v1/chat/completions` and `get_weather`: `finish_reason=tool_calls` with `{"city":"Paris","unit":"c"}`, both with thinking off and on.
- Anthropic `/v1/messages` streaming with thinking: first event after 0.32 s, with both a thinking block and a text block, and the event sequence was well formed.

Decision: **keep MTP with `--spec-type draft-mtp --spec-draft-n-max 2`**. Decode speed went up 74-83%, with no correctness regression and the full 131072 context kept, at a cost of about 1.3 GB of VRAM (about 1.9 GB headroom left). n-max 3 was within noise on the mean (about 1.5%). It was worse on the low-acceptance cases (prose, and refactor with thinking on) and used more VRAM, so n-max 2 is the better choice.

## 2026-10-05: tensor split for `hermes` and `vision` (after the x8/x8 riser)

Setup: llama.cpp b11277 (pinned digest), both 3090s at PCIe Gen4 x8 on CPU root ports, no P2P (GeForce under vfio), so NCCL uses its SHM transport through host memory. Each run used a transient container on `127.0.0.1:18080`, with the production args and image; llama-swap had everything unloaded. Only the split flags differed: `-sm layer -ts 1,1` against `-sm tensor` plus `--shm-size 2g`. Throughput comes from raw `/completion` with exact token-id prompts of Python stdlib source, `cache_prompt=false`, temperature 0, `ignore_eos`, 128 generated tokens and one warm-up request first. The values are llama-server `timings`, in tokens/s.

| model | split | pp 512 | pp 8k | pp 32k | tg @512 | tg @8k | tg @32k | VRAM GPU0 / GPU1 (MiB) |
|---|---|---|---|---|---|---|---|---|
| vision (gemma-4-31b + mmproj) | layer | 1198 | 1990 | 1800 | 39.5 | 36.1 | 29.7 | 14 448 / 13 366 |
| vision | **tensor** | 1398 (+17 %) | 1592 (−20 %) | 1405 (−22 %) | **54.7 (+39 %)** | **51.3 (+42 %)** | **43.6 (+47 %)** | 14 634 / 13 336 |
| hermes (hermes-4.3-36b) | layer | 1156 | 1587 | — | 34.4 | 27.0 | — | 15 550 / 15 318 |
| hermes | **tensor** | 1342 (+16 %) | 1225 (−23 %) | — | **50.4 (+47 %)** | **41.4 (+53 %)** | — | 15 338 / 15 338 |
| hermes, riser test (same day) | layer / tensor | 1182 / 1461 | 1625 / 1270 | 1044 / 874 | 34.5 / 50.7 | 27.2 / 41.9 | 16.0 / 26.1 | 60k needle: pp 728 / 643, tg 10.8 / 18.0 |

Load time, measured from `docker run` to the first `/health` 200:

| model | split | warm page cache | cold (`drop_caches`) |
|---|---|---|---|
| vision | layer | 4.5 s | 10.4 s |
| vision | tensor | 3.4, 4.6, 4.4, 4.9 s | 11.6 s |
| hermes | layer | 4.3, 5.7 s | 12.1 s |
| hermes | tensor | 4.8, 4.8, 4.6, 4.8 s | 12.4 s |

Correctness and stability:
- Tensor split was loaded 3 times per model. Every load answered `17*23` and `TENSOR-OK` correctly, and wrote an `is_prime` that passed 10 test values.
- Vision also passed the image test every time. The image was an 800×500 PNG with "TENSOR-OK 57" above a green triangle, a red circle and a blue square, and the model returned the exact text and the shapes, colours and order correctly. Image requests took 3.0–3.3 s, against 3.9 s with layer split, with 205 prompt tokens and tg 52–55 t/s against 39.9.
- A 32k needle test on vision with tensor split was correct.
- The container logs had no error lines. Walter's `dmesg` had no new Xid, NVRM or AER lines, only docker veth messages.

Decision: **`hermes` and `vision` use `-sm tensor`**. `vision` traffic is mostly short prompts with an image and a long, thinking-heavy answer, so decode dominates. The prompt-processing loss shows only on prompts of 8k tokens and above. `big` stays on layer split: tensor gave −7 to +8 % decode and −15 % prompt processing on its 3B-active MoE (riser test). Row split (`-sm row`) does not load on b11277 ("does not support split buffers"). Tensor split is experimental upstream, so re-run this check after every image bump. The fallback is in the comments above each model in `config.yaml.tmpl`.
