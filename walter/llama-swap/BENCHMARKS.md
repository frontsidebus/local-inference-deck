# Walter benchmarks

## 2026-10-01: MTP speculative decoding for `coder` (qwen3.8-27b, GPU0)

Setup: llama.cpp b11277 (pinned digest), Qwen3.8-27B-UD-Q4_K_XL with its embedded MTP head (`blk.64.nextn.*`, no separate draft GGUF needed). Flags: `-c 131072 -ctk/-ctv q8_0 -np 1 --fit off -ngl all -sm none`. Sampling is the server default (temp 0.6, top-p 0.95, top-k 20). Requests went straight to llama-swap `$BACKEND_WG_IP:8080` from Walter. Numbers are from `timings.predicted_per_second`. Thinking off: 3 prompts x 3 runs (mean). Thinking on: 1 run per prompt, capped at 4096 tokens.
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
