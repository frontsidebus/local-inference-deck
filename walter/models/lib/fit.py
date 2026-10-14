"""VRAM / placement / speed estimate for a GGUF model on Walter (2x RTX 3090 24 GiB, llama.cpp b11277), stdlib only.

Input: the parsed headers of every shard (gguf.GGUFHeader) plus the serving options. The model mirrors what
llama-server does with the repo's common args (`--fit off -ngl all -np 1 -fa on -ctk/-ctv q8_0`, n_ubatch 512):

- Weights: exact tensor bytes from the headers, assigned to devices as llama_model::load_tensors does: the input
  layer (token_embd, per_layer_token_embd) stays on the CPU; block i goes to GPU0 if i/(n_layer+1) < 0.5 under
  `-sm layer -ts 1,1`, else GPU1; the output layer (and a duplicate of token_embd when output.weight is absent,
  i.e. tied embeddings) goes to the last GPU. `-sm tensor` splits weights, KV and buffers evenly. Routed-expert
  tensors (`*_exps`) of chosen layers can go to the CPU (`-ot blk\\.(..)\\.ffn_.*_exps\\.=CPU`). MTP/nextn
  layers load only with MTP on.
- KV: per attention layer `cells x (n_kv_heads x (k_len + v_len))`, at q8_0 34/32 bytes per element:
  full attention: cells = ctx; SWA layers: cells = pad256(min(ctx, window + n_ubatch)); recurrent layers (hybrids:
  full_attention_interval / recurrent_layers / zero kv heads) have none; MLA: one K of kv_lora_rank + rope dims
  and no V; layers after `shared_kv_layers` reuse earlier caches; DSA/QSA indexer keys add one 1-head cache.
  Recurrent state (F32): (d_conv-1)(d_inner+2 n_group d_state) + d_state d_inner per recurrent layer, times
  (1 + draft n_max) with MTP.
- Fixed costs per GPU in use: CUDA context + compute buffer (base + logits n_vocab x n_ubatch x 4 on the output
  GPU + a context term), the mmproj and MTP/draft heads at their size plus their compute buffers.
  Calibrated on the live models (walter/models/README.md "Fit calculator", tests/test_spark_model_fit.py).
- Headroom: 1.5 GiB per card, so a GPU "fits" at <= 22.5 GiB.

All sizes are GiB (2^30 bytes); nvidia-smi "MiB" / 1024 compares directly.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

try:
    from gguf import GGUFHeader
except ImportError:  # pragma: no cover - package-style import
    from .gguf import GGUFHeader  # type: ignore

GIB = float(1 << 30)
CARD_GIB = 24.0
HEADROOM_GIB = 1.5
USABLE_GIB = CARD_GIB - HEADROOM_GIB          # 22.5
N_UBATCH = 512
KV_BYTES = {"f32": 4.0, "f16": 2.0, "bf16": 2.0, "q8_0": 34 / 32, "q5_1": 24 / 32, "q5_0": 22 / 32,
            "q4_1": 20 / 32, "q4_0": 18 / 32, "iq4_nl": 18 / 32}

# --- calibrated constants (GiB unless noted); see README "Fit calculator" for the data and residuals ----------
CUDA_CTX = 0.35            # per GPU in use: CUDA context + cuBLAS/NCCL workspaces as nvidia-smi counts them
COMPUTE_BASE = 1.05        # per GPU in use: graph intermediates at n_ubatch 512
LOGITS_K = 1.0             # x n_vocab x n_ubatch x 4 bytes of logits on the output GPU
FA_TEMP = 0.5              # x the largest attention layer's K+V at f16 over its cells: FA dequant scratch
TENSOR_SPLIT_EXTRA = 0.0   # per GPU, extra buffers of -sm tensor (NCCL)
MMPROJ_COMPUTE = 0.30      # vision encoder compute buffer, on GPU0
DRAFT_COMPUTE = 0.42       # compute buffer of an MTP / draft context (BENCHMARKS.md: 430 MiB)
MTP_N_MAX = 2              # --spec-draft-n-max used on Walter

# --- speed model ------------------------------------------------------------------------------------------------
GPU_BW = 936e9             # RTX 3090 bytes/s
RAM_BW = 19e9              # effective guest RAM bytes/s for CPU experts (calibrated on flash)
BW_EFF = 0.74              # achieved fraction of GPU_BW, dense models
BW_EFF_MOE = 0.40          # MoE: routing and small expert matmuls waste bandwidth
TENSOR_SPEEDUP = 1.45      # -sm tensor vs layer, measured +39..67 % decode on Walter
SPEED_CLASSES = ((80.0, "A"), (30.0, "B"), (10.0, "C"), (0.0, "D"))
RAM_BUDGET_GIB = 32.0      # default budget for experts in RAM (the page cache uses ~73 GB of the 80 GiB VM)

# --- llama.cpp b11277 facts -------------------------------------------------------------------------------------
# src/llama-arch.cpp LLM_ARCH_NAMES at tag b11277 (frozen; "clip" is the mmproj architecture).
LLAMA_CPP_BUILD = "b11277"
SUPPORTED_ARCHS = frozenset("""
clip llama llama4 deci falcon grok gpt2 gptj gptneox mpt baichuan starcoder refact bert modern-bert nomic-bert
nomic-bert-moe neo-bert jina-bert-v2 jina-bert-v3 eurobert bloom stablelm qwen qwen2 qwen2moe qwen2vl qwen3 qwen3moe
qwen3next qwen3vl qwen3vlmoe qwen35 qwen35moe qwen4exp phi2 phi3 phimoe plamo plamo2 plamo3 codeshell orion internlm2
minicpm minicpm3 gemma gemma2 gemma3 gemma3n gemma4 gemma4-assistant gemma-embedding starcoder2 mamba mamba2 maple
jamba falcon-h1 xverse command-r cohere2 cohere2moe dbrx olmo olmo2 olmoe muse-glimmer openelm arctic deepseek
deepseek2 deepseek2-ocr deepseek32 deepseek4 chatglm glm4 glm4moe glm-dsa bitnet t5 t5encoder jais jais2 nemotron
nemotron_h nemotron_h_moe exaone exaone4 exaone-moe rwkv6 rwkv6qwen2 rwkv7 arwkv7 granite granitemoe granitehybrid
graniteswitch granite_swa chameleon wavtokenizer-dec plm bailingmoe bailingmoe2 bailingmoe3 dots1 dots3note arcee
afmoe laguna ernie4_5 ernie4_5-moe hunyuan-moe hunyuan-dense hunyuan_vl hy_v3 hy_v4 smollm3 gpt-oss lfm2 lfm2moe
dream smallthinker llada llada-moe seed_oss grovemoe apertus minimax-01 hrm_text minimax-m2 minimax-m3 cogvlm rnd1
pangu-embedded mistral3 eagle3 dflash mistral4 paddleocr mimo2 step35 spark2_5 llama-embed maincoder kimi-linear
kimi-k3 talkie mellum nanbeige qwen3tts pockettts
""".split())
RECURRENT_ARCHS = {"mamba", "mamba2", "rwkv6", "rwkv6qwen2", "rwkv7", "arwkv7"}
# default SWA pattern when the GGUF has a sliding_window but no sliding_window_pattern (src/models/*.cpp):
# arch -> (n_pattern, dense_first). Layer il is SWA iff il % n < n-1 (dense_first: il % n != 0).
SWA_DEFAULT = {"gemma2": (2, False), "gemma3": (6, False), "gemma-embedding": (6, False), "gemma3n": (5, False),
               "cohere2": (4, False), "cohere2moe": (4, True), "exaone4": (4, False), "exaone-moe": (4, False),
               "afmoe": (4, False), "llama4": (4, False), "gpt-oss": (2, False), "muse-glimmer": (4, False),
               "mellum": (4, False), "laguna": (4, True), "plamo3": (8, False), "modern-bert": (3, True)}
EXPERT_RE = re.compile(r"^blk\.(\d+)\.ffn_[a-z_]*_exps\b")
INPUT_TENSORS = ("token_embd.", "per_layer_token_embd.")
TOOL_MARKERS = ("tool_call", "tools", "<tool", "function_call", "\"function\"", "functions")


@dataclass
class Fit:
    ctx: int
    arch: str | None
    placement: str                     # gpu0 | gpu1 | both | both+cpu-experts | none
    split: str | None                  # none | layer | tensor (llama-server -sm), None when it does not fit
    vram_gb: list = field(default_factory=lambda: [0.0, 0.0])   # GiB per GPU, fixed costs included
    ram_gb: float = 0.0                # GiB of weights in host RAM (input embeddings + CPU experts)
    cpu_expert_layers: list = field(default_factory=list)
    cpu_override: str | None = None    # -ot value for the CPU experts
    tok_s: float = 0.0                 # rough decode estimate at short context, no speculative decoding
    speed_class: str = "D"
    fits: bool = False
    warnings: list = field(default_factory=list)
    breakdown: dict = field(default_factory=dict)
    model: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


# --- metadata helpers -------------------------------------------------------------------------------------------
def _kv(h: GGUFHeader, arch: str, key: str, default=None):
    return h.kv.get(f"{arch}.{key}", default)


def _per_layer(v, n: int, default=0) -> list:
    if isinstance(v, list):
        return [(x if isinstance(x, (int, float)) else default) for x in v[:n]] + [default] * max(0, n - len(v))
    return [v if isinstance(v, (int, float)) and not isinstance(v, bool) else default] * n


def _pad(n: int, to: int) -> int:
    return -(-n // to) * to


@dataclass
class _Arch:
    arch: str | None
    n_layer: int          # block_count (includes nextn layers)
    n_nextn: int
    n_vocab: int
    n_embd: int
    n_expert: int
    n_expert_used: int
    attn: list            # per main layer: KV bytes per cell (0 = none)
    swa: list             # per main layer: bool
    n_swa: int
    recurrent: list       # per main layer: bool
    rs_bytes: int         # recurrent state bytes per recurrent layer (one copy)
    idx_bytes: int        # indexer bytes per cell per attention layer
    nextn_kv: int         # KV bytes per cell of one nextn layer
    f16_full: int         # largest full-attention layer K+V bytes per cell at f16
    f16_swa: int          # largest SWA layer K+V bytes per cell at f16
    template: str | None


def _arch_info(h: GGUFHeader, kv_type: str) -> _Arch:
    a = h.arch or ""
    eb = KV_BYTES.get(kv_type, KV_BYTES["q8_0"])
    n_layer = int(_kv(h, a, "block_count", 0) or 0)
    n_nextn = int(_kv(h, a, "nextn_predict_layers", 0) or 0)
    n_main = max(0, n_layer - n_nextn)
    n_embd = int(_kv(h, a, "embedding_length", 0) or 0)
    n_head = _per_layer(_kv(h, a, "attention.head_count", 0), n_layer)
    n_head_kv = _per_layer(_kv(h, a, "attention.head_count_kv", None), n_layer, None)
    n_head_kv = [hk if hk is not None else hq for hk, hq in zip(n_head_kv, n_head)]
    head_dim = (n_embd // n_head[0]) if n_head and n_head[0] else 0
    k_len = int(_kv(h, a, "attention.key_length", head_dim) or head_dim)
    v_len = int(_kv(h, a, "attention.value_length", head_dim) or head_dim)
    k_swa = int(_kv(h, a, "attention.key_length_swa", k_len) or k_len)
    v_swa = int(_kv(h, a, "attention.value_length_swa", v_len) or v_len)
    vocab = h.kv.get("tokenizer.ggml.tokens")
    n_vocab = int(_kv(h, a, "vocab_size", 0) or 0) or (len(vocab) if isinstance(vocab, list) else 0)

    # sliding window
    n_swa = int(_kv(h, a, "attention.sliding_window", 0) or 0)
    pat = _kv(h, a, "attention.sliding_window_pattern")
    if isinstance(pat, list):
        swa = [bool(x) for x in pat[:n_main]] + [False] * max(0, n_main - len(pat))
    elif n_swa:
        n_pat, dense_first = (pat, False) if isinstance(pat, int) and not isinstance(pat, bool) \
            else SWA_DEFAULT.get(a, (1, False))
        swa = [(n_pat == 0 or (il % n_pat != 0 if dense_first else il % n_pat < n_pat - 1)) for il in range(n_main)]
    else:
        swa = [False] * n_main

    # recurrent layers
    rec_arr = _kv(h, a, "attention.recurrent_layers")
    interval = _kv(h, a, "full_attention_interval")
    if a in RECURRENT_ARCHS:
        recurrent = [True] * n_main
    elif isinstance(rec_arr, list):
        recurrent = [bool(x) for x in rec_arr[:n_main]] + [False] * max(0, n_main - len(rec_arr))
    elif isinstance(interval, int) and interval > 0:
        recurrent = [(il + 1) % interval != 0 for il in range(n_main)]
    elif a == "falcon-h1":
        recurrent = [True] * n_main
    elif isinstance(_kv(h, a, "attention.head_count_kv"), list):
        recurrent = [n_head_kv[il] == 0 and _kv(h, a, "ssm.state_size") is not None for il in range(n_main)]
    else:
        recurrent = [False] * n_main
    has_attn = [not r or a == "falcon-h1" for r in recurrent]

    # MLA
    lora = _kv(h, a, "attention.kv_lora_rank")
    rope = int(_kv(h, a, "rope.dimension_count", 0) or 0)
    shared = int(_kv(h, a, "attention.shared_kv_layers", 0) or 0)
    attn = []
    for il in range(n_main):
        if not has_attn[il] or n_head_kv[il] == 0 or (shared and il >= n_layer - shared):
            attn.append(0)
        elif isinstance(lora, int) and lora > 0:
            attn.append(int((lora + rope) * eb))
        elif swa[il]:
            attn.append(int(n_head_kv[il] * (k_swa + v_swa) * eb))
        else:
            attn.append(int(n_head_kv[il] * (k_len + v_len) * eb))
    nextn_kv = int(n_head_kv[-1] * (k_len + v_len) * eb) if n_nextn and n_head_kv else 0
    if n_nextn and isinstance(lora, int) and lora > 0:
        nextn_kv = int((lora + rope) * eb)

    # recurrent state (F32), mamba/gated-delta formula
    d_conv = int(_kv(h, a, "ssm.conv_kernel", 0) or 0)
    d_inner = int(_kv(h, a, "ssm.inner_size", 0) or 0)
    d_state = int(_kv(h, a, "ssm.state_size", 0) or 0)
    n_group = int(_kv(h, a, "ssm.group_count", 0) or 0)
    rs_bytes = 4 * (max(d_conv - 1, 0) * (d_inner + 2 * n_group * d_state) + d_state * d_inner)
    idx_len = int(_kv(h, a, "attention.indexer.key_length", 0) or 0)
    f16 = [0.0, 0.0]
    for il, b in enumerate(attn):
        if b:
            f16[1 if swa[il] else 0] = max(f16[1 if swa[il] else 0], b * 2.0 / eb)
    tmpl = h.kv.get("tokenizer.chat_template")
    return _Arch(a or None, n_layer, n_nextn, n_vocab, n_embd, int(_kv(h, a, "expert_count", 0) or 0),
                 int(_kv(h, a, "expert_used_count", 0) or 0), attn, swa, n_swa, recurrent, rs_bytes,
                 int(idx_len * eb), nextn_kv, int(f16[0]), int(f16[1]), tmpl if isinstance(tmpl, str) else None)


# --- weights by device ------------------------------------------------------------------------------------------
def _layer_gpu(il: int, n_layer: int, split: str) -> int:
    if split != "layer":
        return 0
    return 0 if il / (n_layer + 1) < 0.5 else 1


def _weights(tensors, info: _Arch, split: str, gpu: int, cpu_layers: set, mtp: bool):
    """-> (gpu bytes [2], cpu bytes, expert bytes per layer {il: bytes}, active bytes per token [gpu0, gpu1, cpu])"""
    gpu_b = [0.0, 0.0]
    act = [0.0, 0.0, 0.0]
    cpu_b = 0.0
    exp_layer: dict[int, float] = {}
    names = {t.name for t in tensors}
    n_main = info.n_layer - info.n_nextn
    out_dev = 1 if split == "layer" else gpu
    frac = (info.n_expert_used / info.n_expert) if info.n_expert else 1.0
    for t in tensors:
        name, nb = t.name, float(t.nbytes)
        m = re.match(r"^blk\.(\d+)\.", name)
        is_exp = bool(EXPERT_RE.match(name))
        if name.startswith(INPUT_TENSORS):
            cpu_b += nb
            if name.startswith("token_embd.") and "output.weight" not in names:
                gpu_b[out_dev] += nb           # tied embeddings: llama.cpp duplicates it as the output
                act[out_dev] += nb
            continue
        if m:
            il = int(m.group(1))
            if il >= n_main and not mtp:
                continue                       # nextn/MTP layers are skipped without MTP
            dev = _layer_gpu(il, info.n_layer, split) if split == "layer" else gpu
            if is_exp:
                exp_layer[il] = exp_layer.get(il, 0.0) + nb
                if il in cpu_layers:
                    cpu_b += nb
                    act[2] += nb * frac
                    continue
            gpu_b[dev] += nb
            if il < n_main:
                act[dev] += nb * (frac if is_exp else 1.0)
            continue
        gpu_b[out_dev] += nb
        act[out_dev] += nb
    return gpu_b, cpu_b, exp_layer, act


# --- main entry -------------------------------------------------------------------------------------------------
def estimate(headers: list, ctx: int, *, kv_type: str = "q8_0", mmproj_bytes: int = 0, draft_bytes: int = 0,
             placement: str = "auto", split: str | None = None, cpu_expert_layers=None, mtp: bool = False,
             draft_headers: list | None = None, draft_gpu: int = 1, prefer_gpu: int = 1,
             ram_budget_gb: float = RAM_BUDGET_GIB) -> Fit:
    """Estimate the per-GPU VRAM of serving `headers` (all shards of one model) at context `ctx`.

    placement "auto" applies the placement rules (walter/models/README.md, "Fit calculator"): one GPU if <= 22.5 GiB; else both (dense: -sm tensor, MoE:
    -sm layer -ts 1,1); else MoE with the experts of the fewest layers on the CPU within ram_budget_gb; else none.
    Pass placement ("gpu0"|"gpu1"|"both"|"both+cpu-experts") with split / cpu_expert_layers to evaluate a fixed
    configuration (that is how the calibration runs). mtp=True loads the model's own nextn/MTP layers;
    draft_headers / draft_bytes add a separate draft model on draft_gpu.
    """
    if not headers:
        raise ValueError("no headers")
    main = next((h for h in headers if h.arch), headers[0])
    tensors = [t for h in headers for t in h.tensors]
    info = _arch_info(main, kv_type)
    warns = _warnings(main, info, headers)
    trained = _kv(main, info.arch or "", "context_length")
    if isinstance(trained, int) and ctx > trained:
        warns.append(f"ctx {ctx} exceeds the trained context_length {trained}")
    if mtp and not info.n_nextn:
        warns.append("mtp requested but the model has no nextn/MTP layers")
        mtp = False

    def build(pl: str, sp: str, cpu_layers: set) -> Fit:
        gpu = 0 if pl == "gpu0" else 1 if pl == "gpu1" else 0
        gpus = [gpu] if pl in ("gpu0", "gpu1") else [0, 1]
        wsplit = "layer" if sp == "layer" else "single"
        g_w, cpu_w, exp_layer, act = _weights(tensors, info, wsplit, gpu, cpu_layers, mtp)
        if sp == "tensor":
            tot = g_w[0] + g_w[1]
            g_w = [tot / 2, tot / 2]
        n_main = info.n_layer - info.n_nextn
        kv = [0.0, 0.0]
        swa_cells = min(ctx, _pad(info.n_swa + N_UBATCH, 256)) if info.n_swa else ctx
        has_attn = [False, False]
        for il in range(n_main):
            per_cell = info.attn[il]
            if not per_cell and not info.recurrent[il]:
                continue
            dev = _layer_gpu(il, info.n_layer, "layer") if sp == "layer" else gpu
            cells = swa_cells if info.swa[il] else ctx
            b = per_cell * cells
            if per_cell and info.idx_bytes:
                b += info.idx_bytes * ctx
            if info.recurrent[il]:
                b += info.rs_bytes * (1 + (MTP_N_MAX if mtp else 0))
            if per_cell:
                has_attn[dev] = True
            kv[dev] += b
        if sp == "tensor":
            tot = kv[0] + kv[1]
            kv = [tot / 2, tot / 2]
            has_attn = [True, True]
        out_dev = 1 if sp == "layer" else gpu
        fixed = [0.0, 0.0]
        logits = info.n_vocab * N_UBATCH * 4.0
        for g in gpus:
            # compute buffer: the graph allocator reuses memory, so the peak is the larger of the base
            # intermediates and the output-side buffers (logits) plus the FA scratch of the largest layer
            fa = max(info.f16_full * ctx, info.f16_swa * swa_cells) * FA_TEMP if has_attn[g] else 0.0
            lg = logits / 2 if sp == "tensor" else (logits if g == out_dev else 0.0)
            if sp == "tensor":
                fa /= 2
            fixed[g] += CUDA_CTX * GIB + max(COMPUTE_BASE * GIB, fa + lg * LOGITS_K)
            if sp == "tensor":
                fixed[g] += TENSOR_SPLIT_EXTRA * GIB
        extra = [0.0, 0.0]
        if mmproj_bytes:
            extra[gpu if len(gpus) == 1 else 0] += mmproj_bytes + MMPROJ_COMPUTE * GIB
        if mtp:
            mdev = gpu if len(gpus) == 1 else (draft_gpu if draft_gpu in gpus else gpus[-1])
            extra[mdev] += info.nextn_kv * ctx * info.n_nextn + DRAFT_COMPUTE * GIB
        dgpu = draft_gpu if draft_gpu in gpus else gpus[-1]
        if draft_headers:
            d_info = _arch_info(next((h for h in draft_headers if h.arch), draft_headers[0]), kv_type)
            d_t = [t for h in draft_headers for t in h.tensors]
            d_gpu, _d_cpu, _e, _a = _weights(d_t, d_info, "single", dgpu, set(), True)
            mtp_only = d_info.n_nextn > 0 and not any(t.name.startswith("blk.0.") for t in d_t)
            d_kv = d_info.nextn_kv * ctx * d_info.n_nextn         # MTP-only file: just its nextn layer(s)
            if not mtp_only:
                d_kv += sum(d_info.attn) * ctx
            extra[dgpu] += d_gpu[dgpu] + d_kv + DRAFT_COMPUTE * GIB + d_info.n_vocab * N_UBATCH * 4.0
        elif draft_bytes:
            extra[dgpu] += draft_bytes + DRAFT_COMPUTE * GIB
        vram = [(g_w[i] + kv[i] + fixed[i] + extra[i]) / GIB if i in gpus else 0.0 for i in (0, 1)]
        # speed: layer split runs the GPUs one after the other; tensor split in parallel
        eff = BW_EFF_MOE if info.n_expert else BW_EFF
        t_gpu = (act[0] + act[1]) / (GPU_BW * eff)
        if sp == "tensor":
            t_gpu /= TENSOR_SPEEDUP
        t = t_gpu + act[2] / RAM_BW
        tok_s = 1.0 / t if t > 0 else 0.0
        cls = next(c for lim, c in SPEED_CLASSES if tok_s > lim or lim == 0.0)
        cpu_exp = sum(exp_layer[il] for il in cpu_layers if il in exp_layer) / GIB
        fits = all(vram[g] <= USABLE_GIB for g in gpus) and cpu_exp <= ram_budget_gb
        cl = sorted(cpu_layers)
        f = Fit(ctx=ctx, arch=info.arch, placement=pl, split=sp, vram_gb=[round(v, 2) for v in vram],
                ram_gb=round(cpu_w / GIB, 2), cpu_expert_layers=cl, cpu_override=_override(cl), tok_s=round(tok_s, 1),
                speed_class=cls, fits=fits, warnings=list(warns),
                breakdown={"weights": [round(x / GIB, 3) for x in g_w], "kv": [round(x / GIB, 3) for x in kv],
                           "fixed": [round(x / GIB, 3) for x in fixed], "extra": [round(x / GIB, 3) for x in extra],
                           "experts_gib": round(sum(exp_layer.values()) / GIB, 3)},
                model=_model_summary(main, info, tensors))
        f._exp_layer = exp_layer  # type: ignore[attr-defined]
        return f

    if placement != "auto":
        sp = split or ("none" if placement in ("gpu0", "gpu1") else ("layer" if info.n_expert else "tensor"))
        if placement == "both+cpu-experts":
            sp = "layer"
        f = build(placement, sp, set(cpu_expert_layers or ()))
        return _finish(f)

    # auto
    single = build("gpu1" if prefer_gpu == 1 else "gpu0", "none", set())
    if single.fits:
        return _finish(single)
    both = build("both", split or ("layer" if info.n_expert else "tensor"), set())
    if both.fits:
        return _finish(both)
    if info.n_expert:
        f = _cpu_experts(build, both, info, ram_budget_gb)
        if f is not None:
            return _finish(f)
    both.placement, both.split, both.fits = "none", None, False
    both.warnings.append("does not fit: try a smaller quant or a shorter context")
    return _finish(both)


def _cpu_experts(build, both: Fit, info: _Arch, ram_budget_gb: float):
    """Move the experts of the fewest layers to the CPU (per GPU, from the lowest layer up, as flash does)."""
    exp_layer = both._exp_layer  # type: ignore[attr-defined]
    n_main = info.n_layer - info.n_nextn
    per_gpu = {g: [il for il in sorted(exp_layer) if il < n_main and _layer_gpu(il, info.n_layer, "layer") == g]
               for g in (0, 1)}
    cpu: set = set()
    f = both
    for _ in range(n_main + 1):
        over = [g for g in (0, 1) if f.vram_gb[g] > USABLE_GIB]
        if not over:
            break
        g = max(over, key=lambda x: f.vram_gb[x] - USABLE_GIB)
        cand = [il for il in per_gpu[g] if il not in cpu]
        if not cand:
            return None
        cpu.add(cand[0])
        if sum(exp_layer[il] for il in cpu) / GIB > ram_budget_gb:
            return None              # experts in RAM would exceed the budget
        f = build("both+cpu-experts", "layer", cpu)
    if not f.fits:
        return None
    return f


def _override(layers: list) -> str | None:
    if not layers:
        return None
    alts = "|".join(str(i) for i in layers)
    return f"blk\\.({alts})\\.ffn_.*_exps\\.=CPU"


def _finish(f: Fit) -> Fit:
    if f.speed_class == "D" and f.placement != "none":
        f.warnings.append(f"estimated {f.tok_s} tok/s (class D, < 10): refused")
        f.fits = False
    if hasattr(f, "_exp_layer"):
        del f._exp_layer
    return f


def _warnings(h: GGUFHeader, info: _Arch, headers: list) -> list:
    w = []
    if not info.arch:
        w.append("no general.architecture in the header")
    elif info.arch not in SUPPORTED_ARCHS:
        w.append(f"architecture {info.arch!r} is not in llama.cpp {LLAMA_CPP_BUILD}")
    if info.template is None:
        w.append("no chat template (tokenizer.chat_template) in the GGUF")
    elif not any(m in info.template for m in TOOL_MARKERS):
        w.append("chat template has no tool-call markers: no tool support")
    sp = [split_count(x) for x in headers]
    counts = {c for c in sp if c}
    if counts and (len(counts) > 1 or max(counts) != len(headers)):
        w.append(f"split.count {sorted(counts)} does not match the {len(headers)} shard header(s) given")
    return w


def split_count(h: GGUFHeader) -> int | None:
    c = h.kv.get("split.count")
    return c if isinstance(c, int) and not isinstance(c, bool) else None


def _model_summary(h: GGUFHeader, info: _Arch, tensors) -> dict:
    n_attn = sum(1 for x in info.attn if x)
    exp = sum(t.nbytes for t in tensors if EXPERT_RE.match(t.name))
    return {
        "arch": info.arch, "n_layer": info.n_layer, "n_nextn": info.n_nextn, "n_attn_layers": n_attn,
        "n_swa_layers": sum(1 for il, x in enumerate(info.attn) if x and info.swa[il]),
        "n_expert": info.n_expert, "n_expert_used": info.n_expert_used,
        "file_gib": round(sum(t.nbytes for t in tensors) / GIB, 3), "experts_gib": round(exp / GIB, 3),
        "context_length": _kv(h, info.arch or "", "context_length"),
        "chat_template_bytes": len(info.template.encode()) if info.template else 0,
        "name": h.kv.get("general.name"), "license": h.kv.get("general.license"),
    }
