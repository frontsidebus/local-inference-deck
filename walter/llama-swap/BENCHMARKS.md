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

## 2026-10-05: x8/x8 riser, layer-split baseline

After the bifurcation riser (both 3090s Gen4 x8 on CPU root ports; GPU1 was x1 behind the chipset before), with the production config (all split models still on `-sm layer -ts 1,1`). Same method as the tensor-split section below: raw `/completion`, exact token-id prompts of Python stdlib source, `cache_prompt=false`, temperature 0, `ignore_eos`, 128 generated tokens, one warm-up; tokens/s from llama-server `timings`.

| model | split | pp 512 | pp 8k | pp 32k | tg @512 | tg @8k | tg @32k |
|---|---|---|---|---|---|---|---|
| coder (MTP n=2) | none, GPU0 | 1063 | 1215 | 1081 | 81.3 | 79.0 | 63.3 |
| coder-fast | none, GPU1 | 2880 | 3201 | 2907 | 153.2 | 139.5 | 117.0 |
| big | layer | 1879 | 2880 | 2829 | 125.5 | 117.4 | 99.9 |
| vision | layer | 1187 | 1972 | 1789 | 39.3 | 36.0 | 29.7 |
| hermes | layer | 1162 | 1597 | 1030 | 34.5 | 27.2 | 15.9 |

- **Decode is unchanged** by the wider link under layer split (only 20–60 MB/s cross PCIe per token). coder's 81 t/s reflects MTP acceptance on source code (the code prompt above gave 82.2).
- **Loads:** weight uploads run at 10–13 GB/s per GPU. The cold boot preload of `coder-fast` on GPU1 went from 18–22 s to 12 s; `coder` on GPU0 is unchanged (8–9 s before, 10 s now). Cold loads are now disk-bound (about 2.8 GB/s from the models disk): `big` 20 s, `vision` 10–11 s, `hermes` 12 s. Warm (files still in page cache): `big` 7 s, `hermes` 4 s, `vision` 6 s.
- **Long context:** `big` found a needle at 15/50/85 % depth of a 98.9k-token prompt (3/3, pp 2300, tg 66); `hermes` at 59.8k tokens (3/3, pp 728, tg 10.8). VRAM stayed flat (KV preallocated).
- **Concurrency** (`big`, `-np 1`): 4 simultaneous requests all returned 200, queued in order on the one slot, each at about 124 t/s.
- **Row split** (`-sm row`) does not load at all on build 11277, for any model or flag set: `device CUDA0 does not support split buffers`.
- **Errors:** no Xid, AER or PCIe replays on host or guest during the whole run.

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

## 2026-10-05: `presence_penalty` for the Qwen models

Question: does a presence penalty (Qwen's model cards suggest up to 1.5) cut repetition loops on `coder` and `coder-fast`, and what does it cost on code and JSON? `big` was not measured: it would have swapped out the coding pair, and its model card lists no presence penalty.

Setup: build 11277, production config, requests to llama-swap `$BACKEND_WG_IP:8080` with a per-request `presence_penalty` of 0 / 0.5 / 1.0 / 1.5, a fixed seed and the server's sampling (temp 0.6, top-p 0.95, top-k 20) unless noted. One request at a time per model.

How llama-server applies it: `presence_penalty` is the `penalties` sampler, first in the chain (before top-k and temperature). It subtracts the value from the logit of every token seen in the **last `repeat_last_n` tokens (default 64)**, prompt included, not the whole output as in vLLM. `--repeat-penalty` (1.0 = off) and `--frequency-penalty` (0) share that window and stay off. A CLI `--presence-penalty X` is only a default: a request's `presence_penalty` overrides it (checked through `generation_settings`). Clients that cannot send the field (for example over the Anthropic `/v1/messages` API) get the default.

Loop probes (thinking off unless noted, `max_tokens` 4096, 2 seeds):
- `enum_q`: "list 200 distinct English nouns starting with q" (there are not 200), scored as distinct items out of items written;
- `selfsum`: 40 rounds of summarizing the previous summary, counted as looped when 10 or more lines repeat or 12-gram repetition exceeds 10 %;
- `think_imp`: an impossible digit puzzle with thinking on, counted when it hits 4096;
- free JSON: an 80-object array with no grammar, counted as parseable (more seeds where noted).

| model | pp | `enum_q` distinct/items (seed 1, seed 2) | `selfsum` looped | `think_imp` hit 4096 | free JSON parses |
|---|---|---|---|---|---|
| coder | 0 | 19/200, 19/200 | 1/2 | 2/2 | 3/3 |
| coder | 0.5 | 47/200, 200/200 | 1/2 | 1/2 | 2/2 |
| coder | 1.0 | 20/200, 76/200 | 0/2 | 1/2 | 2/3 (populations count down to `00`) |
| coder | 1.5 | 31/200, 60/62 | 0/2 | 2/2 | 3/3 (switches to one-line JSON) |
| coder | 1.5, `repeat_last_n` 256 / 1024 | 63/200, 124/124 / 101/200, 172/204 | | | |
| coder-fast | 0 | 21/200, 40/153 | 1/2 | 2/2 | 4/7 |
| coder-fast | 0.5 | 2/2 (declines), 26/200 | 1/2 | 2/2 | 2/2 |
| coder-fast | 1.0 | 19/23, 36/53 | 1/2 | 2/2 | 6/7 |
| coder-fast | 1.5 | 7/7 (declines), 119/183 | 0/2 | 2/2 | 6/7 |
| coder-fast | 1.5, `repeat_last_n` 256 / 1024 | 3/3, 77/93 / 3/3, 83/93 | | | |

`coder-fast`'s invalid free JSON is a model quirk at every setting (a dropped opening quote, `"canton": Capellen"`), not a penalty effect. `coder`'s cycling list (about 20 nouns repeated to 200) has a period longer than 64 tokens, so the default window misses it. It eases only with a wider window. DRY (`dry_multiplier` 0.8) did not change `enum_q` either, because a newline is one of its sequence breakers.

Quality (thinking on for code, the way coding harnesses run):
- code 1: run-length encode/decode plus a duration parser with a slightly ambiguous spec (12288-token cap);
- code 2: an LRU cache, top-k words and word wrap;
- both scored by local asserts.

| model | pp | code 1 pass / reasoning ran to 12288 / mean tokens | code 2 pass / mean tokens | code 1, thinking off (pass) | `json_schema` extraction (fields right) | judge-style, temp 0, `json_object` (items) |
|---|---|---|---|---|---|---|
| coder | 0 | 2/5, 3/5, 9070 | | 4/4 | 10/10, 10/10 | 5 |
| coder | 0.5 | 0/2, 2/2, 12288 | | | 10/10, 10/10 | |
| coder | 1.0 | 1/2, 1/2, 8084 | | 4/4 | 10/10, 10/10 | |
| coder | 1.5 | 2/5, 3/5, 10544 | | 4/4 | 10/10, 10/10 | 7 |
| coder-fast | 0 | 7/12, 5/12, 10152 | 7/8, 4884 | 12/12 | 10/10, 10/10 | 6 |
| coder-fast | 0.5 | 1/2, 1/2, 10330 | | | 10/10, 10/10 | 6 |
| coder-fast | 1.0 | 3/6, 3/6, 10548 | | 4/4 | 10/10, 10/10 | 6 |
| coder-fast | 1.5 | **11/12, 1/12, 8029** | 7/8, 4912 | 11/12 | 10/10, 10/10 | 6 (same findings) |

Digest curation, using the real `pipeline.curate_with_llm` over invented items with `json_schema` and thinking off. The penalty was injected into each request.

| model | pp | ai-research selected/sent | ai-security | default | repairs, splits |
|---|---|---|---|---|---|
| coder (the digest's model) | 0 | 26/27 | 7/27 | 7/20 | 0, 0 |
| coder | 1.5 | 21/27 | 7/27 | 6/20 | 0, 0 |
| coder-fast | 0 | 21/27 | 5/27 | 4/20 | 0, 0 |
| coder-fast | 1.5 | 19/27 | 5/27 | 4/20 | 0, 0 |

Decision:
- **`coder-fast`: `--presence-penalty 1.5`** as the server default. Runaway reasoning on the ambiguous code task fell from 5/12 to 1/12 and its passes rose from 7/12 to 11/12 (both code tasks: 14/20 → 18/20). That is Fisher p ≈ 0.15 (two-sided) on code 1, so it is a moderate signal, not a proof. Nothing measured got worse beyond noise:
  - code 2 and code with thinking off;
  - `json_schema`;
  - the judge's greedy `json_object` (same six findings);
  - digest curation (0 repairs).

  On impossible enumerations it now declines or stops short instead of cycling.
- **`coder`: leave it off.** Runaway reasoning was unchanged (3/5 at 0 and at 1.5). The list loop survives the 64-token window. 1.0 corrupted numbers in free JSON (`"population": 00`), and 1.5 made the judge-style answer longer (5 → 7 items) and the digest pick fewer items. Its model card also gives 0 for thinking mode.
- **`big`: not measured, left off.**
- The output cap (gateway `max_tokens` clamp, `-n` backstop) stays the guard against runaway generations; a penalty only lowers how often they happen. Clients can still send `presence_penalty` (0 restores the old behaviour on `coder-fast`). Re-measure after an image or model bump.
