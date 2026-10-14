"""walter/models/lib/fit.py: VRAM estimate, placement and speed class, calibrated against the live models.

FIXTURES reproduce the live GGUF headers' relevant metadata (the keys fit.py reads) and per-layer tensor bytes
(KiB; dense vs routed experts), derived from read-only header reads on Walter on 2026-10-14. They are rebuilt into
GGUF headers with gguf.build_test_gguf (one I8 tensor per layer and kind, so the byte totals are exact), parsed
back, and estimated. MEASURED holds nvidia-smi MiB after load (P1-SPEC "Calibration", llama-swap/BENCHMARKS.md).
Run: python3 -B -m pytest -q walter/tests/test_spark_model_fit.py
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "models" / "lib"))
import fit  # noqa: E402
import gguf  # noqa: E402
from gguf import Typed, build_test_gguf, parse_header  # noqa: E402

# fmt: off
FIXTURES = {'coder': [{'kv': {'general.architecture': 'qwen35',
                   'qwen35.block_count': 65,
                   'qwen35.context_length': 262144,
                   'qwen35.embedding_length': 5120,
                   'qwen35.attention.head_count': 24,
                   'qwen35.attention.head_count_kv': 4,
                   'qwen35.attention.key_length': 256,
                   'qwen35.attention.value_length': 256,
                   'qwen35.nextn_predict_layers': 1,
                   'qwen35.ssm.conv_kernel': 4,
                   'qwen35.ssm.state_size': 128,
                   'qwen35.ssm.group_count': 16,
                   'qwen35.ssm.inner_size': 6144,
                   'qwen35.full_attention_interval': 4,
                   'qwen35.rope.dimension_count': 64,
                   'qwen35.vocab_size': 248320,
                   'tokenizer.chat_template': '{% if tools %}<tool_call>{% endif %}'},
            'layers': {'first': 0,
                       'dense_kib': [219551, 213431, 217271, 242522, 226791, 230471, 242951, 215082, 213191, 206631,
                                     213191, 211762, 210471, 200511, 185111, 199282, 209351, 217511, 206631, 202842,
                                     240391, 233591, 241751, 248282, 261751, 261751, 257671, 236162, 250871, 206631,
                                     229351, 217082, 219591, 213031, 233191, 234882, 240391, 238791, 240391, 243642,
                                     249671, 251271, 251271, 244522, 206631, 219271, 215751, 205682, 244071, 239271,
                                     257911, 256642, 255351, 251271, 251271, 244882, 273311, 273311, 284871, 287242,
                                     269231, 257671, 261751, 291322, 342782]},
            'other_kib': [('output.weight', 1018500), ('token_embd.weight', 698400), ('output_norm.weight', 20)]}],
 'coder_fast': [{'kv': {'general.architecture': 'qwen35moe',
                        'qwen35moe.block_count': 40,
                        'qwen35moe.context_length': 262144,
                        'qwen35moe.embedding_length': 2048,
                        'qwen35moe.attention.head_count': 16,
                        'qwen35moe.attention.head_count_kv': 2,
                        'qwen35moe.expert_count': 256,
                        'qwen35moe.expert_used_count': 8,
                        'qwen35moe.attention.key_length': 256,
                        'qwen35moe.attention.value_length': 256,
                        'qwen35moe.ssm.conv_kernel': 4,
                        'qwen35moe.ssm.state_size': 128,
                        'qwen35moe.ssm.group_count': 16,
                        'qwen35moe.ssm.inner_size': 4096,
                        'qwen35moe.full_attention_interval': 4,
                        'qwen35moe.rope.dimension_count': 64,
                        'qwen35moe.vocab_size': 248320,
                        'tokenizer.chat_template': '{% if tools %}<tool_call>{% endif %}'},
                 'layers': {'first': 0,
                            'dense_kib': [40793, 40793, 40793, 33626, 40793, 40793, 40793, 33626, 40793, 40793,
                                          40793, 33626, 40793, 40793, 40793, 33626, 40793, 40793, 40793, 33626,
                                          40793, 40793, 40793, 33626, 40793, 40793, 40793, 33626, 40793, 40793,
                                          40793, 33626, 40793, 40793, 40793, 33626, 40793, 40793, 40793, 33626],
                            'exps_kib': [372736, 475136, 442368, 442368, 372736, 372736, 372736, 442368, 442368,
                                         372736, 442368, 442368, 372736, 372736, 442368, 442368, 372736, 372736,
                                         372736, 442368, 372736, 372736, 372736, 372736, 372736, 372736, 372736,
                                         442368, 372736, 372736, 372736, 442368, 442368, 442368, 509952, 442368,
                                         442368, 442368, 509952, 509952]},
                 'other_kib': [('output.weight', 407400), ('token_embd.weight', 527680),
                               ('output_norm.weight', 8)]}],
 'big': [{'kv': {'general.architecture': 'qwen3next',
                 'qwen3next.block_count': 48,
                 'qwen3next.context_length': 262144,
                 'qwen3next.embedding_length': 2048,
                 'qwen3next.attention.head_count': 16,
                 'qwen3next.attention.head_count_kv': 2,
                 'qwen3next.expert_count': 512,
                 'qwen3next.expert_used_count': 10,
                 'qwen3next.attention.key_length': 256,
                 'qwen3next.attention.value_length': 256,
                 'qwen3next.ssm.conv_kernel': 4,
                 'qwen3next.ssm.state_size': 128,
                 'qwen3next.ssm.group_count': 16,
                 'qwen3next.ssm.inner_size': 4096,
                 'qwen3next.full_attention_interval': 4,
                 'qwen3next.rope.dimension_count': 64,
                 'qwen3next.vocab_size': 151936,
                 'tokenizer.chat_template': '{% if tools %}<tool_call>{% endif %}'},
          'layers': {'first': 0,
                     'dense_kib': [42401, 42401, 42401, 35674, 42401, 42401, 42401, 35674, 42401, 42401, 42401,
                                   35674, 42401, 42401, 42401, 35674, 42401, 42401, 42401, 35674, 42401, 42401,
                                   42401, 35674, 42401, 42401, 42401, 35674, 42401, 42401, 42401, 35674, 42401,
                                   42401, 42401, 35674, 42401, 42401, 42401, 35674, 42401, 42401, 42401, 35674,
                                   42401, 42401, 42401, 35674],
                     'exps_kib': [745472, 745472, 745472, 745472, 745472, 745472, 745472, 745472, 745472, 745472,
                                  745472, 745472, 745472, 745472, 745472, 745472, 745472, 745472, 745472, 745472,
                                  745472, 745472, 745472, 745472, 745472, 745472, 745472, 745472, 745472, 745472,
                                  745472, 745472, 745472, 745472, 745472, 745472, 745472, 745472, 745472, 745472,
                                  745472, 745472, 745472, 745472, 745472, 745472, 745472, 745472]},
          'other_kib': [('output.weight', 249270), ('token_embd.weight', 322864), ('output_norm.weight', 8)]}],
 'vision': [{'kv': {'general.architecture': 'gemma4',
                    'gemma4.block_count': 60,
                    'gemma4.context_length': 262144,
                    'gemma4.embedding_length': 5376,
                    'gemma4.attention.head_count': 32,
                    'gemma4.attention.head_count_kv': [16, 16, 16, 16, 16, 4, 16, 16, 16, 16, 16, 4, 16, 16, 16, 16,
                                                       16, 4, 16, 16, 16, 16, 16, 4, 16, 16, 16, 16, 16, 4, 16, 16,
                                                       16, 16, 16, 4, 16, 16, 16, 16, 16, 4, 16, 16, 16, 16, 16, 4,
                                                       16, 16, 16, 16, 16, 4, 16, 16, 16, 16, 16, 4],
                    'gemma4.attention.key_length': 512,
                    'gemma4.attention.value_length': 512,
                    'gemma4.attention.sliding_window': 1024,
                    'gemma4.attention.shared_kv_layers': 0,
                    'gemma4.attention.sliding_window_pattern': [True, True, True, True, True, False, True, True,
                                                                True, True, True, False, True, True, True, True,
                                                                True, False, True, True, True, True, True, False,
                                                                True, True, True, True, True, False, True, True,
                                                                True, True, True, False, True, True, True, True,
                                                                True, False, True, True, True, True, True, False,
                                                                True, True, True, True, True, False, True, True,
                                                                True, True, True, False],
                    'gemma4.attention.key_length_swa': 256,
                    'gemma4.attention.value_length_swa': 256,
                    'gemma4.rope.dimension_count': 512,
                    'gemma4.vocab_size': 262144,
                    'tokenizer.chat_template': '{% if tools %}<tool_call>{% endif %}'},
             'layers': {'first': 0,
                        'dense_kib': [263174, 263174, 263174, 263174, 263174, 293416, 263174, 263174, 263174,
                                      263174, 263174, 293416, 263174, 263174, 263174, 263174, 263174, 293416,
                                      263174, 263174, 263174, 263174, 263174, 293416, 263174, 263174, 263174,
                                      263174, 263174, 293416, 263174, 263174, 263174, 263174, 263174, 293416,
                                      263174, 263174, 263174, 263174, 263174, 293416, 263174, 263174, 263174,
                                      263174, 263174, 293416, 263174, 263174, 263174, 263174, 263174, 293416,
                                      263174, 263174, 263174, 263174, 263174, 293416]},
             'other_kib': [('token_embd.weight', 774144), ('output_norm.weight', 22)]}],
 'hermes': [{'kv': {'general.architecture': 'seed_oss',
                    'seed_oss.block_count': 64,
                    'seed_oss.context_length': 524288,
                    'seed_oss.embedding_length': 5120,
                    'seed_oss.attention.head_count': 80,
                    'seed_oss.attention.head_count_kv': 8,
                    'seed_oss.attention.key_length': 128,
                    'seed_oss.attention.value_length': 128,
                    'seed_oss.vocab_size': 155136,
                    'tokenizer.chat_template': '{% if tools %}<tool_call>{% endif %}'},
             'layers': {'first': 0,
                        'dense_kib': [333688, 333688, 333688, 333688, 333688, 333688, 333688, 333688, 296728,
                                      296728, 333688, 296728, 296728, 333688, 296728, 296728, 333688, 296728,
                                      296728, 333688, 296728, 296728, 333688, 296728, 296728, 333688, 296728,
                                      296728, 333688, 296728, 296728, 333688, 296728, 296728, 333688, 296728,
                                      296728, 333688, 296728, 296728, 333688, 296728, 296728, 333688, 296728,
                                      296728, 333688, 296728, 296728, 333688, 296728, 296728, 333688, 296728,
                                      296728, 333688, 333688, 333688, 333688, 333688, 333688, 333688, 333688,
                                      333688]},
             'other_kib': [('output.weight', 636300), ('token_embd.weight', 436320), ('output_norm.weight', 20)]}],
 'flash': [{'kv': {'general.architecture': 'qwen4exp',
                   'qwen4exp.block_count': 48,
                   'qwen4exp.context_length': 262144,
                   'qwen4exp.embedding_length': 2560,
                   'qwen4exp.attention.head_count': 24,
                   'qwen4exp.attention.head_count_kv': 2,
                   'qwen4exp.expert_count': 512,
                   'qwen4exp.expert_used_count': 10,
                   'qwen4exp.attention.key_length': 256,
                   'qwen4exp.attention.value_length': 256,
                   'qwen4exp.ssm.conv_kernel': 4,
                   'qwen4exp.ssm.state_size': 128,
                   'qwen4exp.ssm.group_count': 16,
                   'qwen4exp.ssm.inner_size': 6144,
                   'qwen4exp.full_attention_interval': 4,
                   'qwen4exp.rope.dimension_count': 64,
                   'qwen4exp.attention.indexer.key_length': 128,
                   'split.no': 0,
                   'split.count': 2,
                   'qwen4exp.vocab_size': 248320,
                   'tokenizer.chat_template': '{% if tools %}<tool_call>{% endif %}'},
            'layers': {'first': 0,
                       'dense_kib': [73936, 138696, 73918, 61318, 78558, 70388, 73918, 64858, 69496, 73936, 74546,
                                     66530, 70931, 67336, 71846, 65690, 73506, 77896, 71866, 70783, 77896, 75856,
                                     74348, 70528, 78096, 78308, 73331, 70028, 78771, 74696, 73936, 74358, 76318,
                                     70496, 70306, 74570, 74811, 74598, 72456, 74208, 78096, 67386, 70468, 75033,
                                     70586, 70958, 70878, 75208],
                       'exps_kib': [1088000, 755200, 985600, 985600, 985600, 985600, 985600, 1088000, 755200,
                                    985600, 755200, 985600, 857600, 755200, 1088000, 985600, 1088000, 1164800,
                                    1088000, 985600, 857600, 934400, 1088000, 985600, 985600, 1164800, 985600,
                                    1164800, 1164800, 1164800, 857600, 1088000, 985600, 1088000, 985600, 1164800,
                                    755200, 1088000, 1088000, 985600, 1164800, 1088000, 1088000, 1088000, 1164800,
                                    1164800, 1088000, 1331200]},
            'other_kib': [('output.weight', 509250), ('token_embd.weight', 329800), ('output_norm.weight', 12840)]},
           {'kv': {'split.no': 1, 'split.count': 2},
            'layers': {'first': 0, 'dense_kib': []},
            'other_kib': [('per_layer_token_embd.weight', 28125135)]}],
 'flash_mtp': [{'kv': {'general.architecture': 'qwen4exp',
                       'qwen4exp.block_count': 49,
                       'qwen4exp.context_length': 262144,
                       'qwen4exp.embedding_length': 2560,
                       'qwen4exp.attention.head_count': 24,
                       'qwen4exp.attention.head_count_kv': 2,
                       'qwen4exp.expert_count': 512,
                       'qwen4exp.expert_used_count': 10,
                       'qwen4exp.attention.key_length': 256,
                       'qwen4exp.attention.value_length': 256,
                       'qwen4exp.nextn_predict_layers': 1,
                       'qwen4exp.ssm.conv_kernel': 4,
                       'qwen4exp.ssm.state_size': 128,
                       'qwen4exp.ssm.group_count': 16,
                       'qwen4exp.ssm.inner_size': 6144,
                       'qwen4exp.full_attention_interval': 4,
                       'qwen4exp.rope.dimension_count': 64,
                       'qwen4exp.attention.indexer.key_length': 128,
                       'qwen4exp.vocab_size': 248320,
                       'tokenizer.chat_template': '{% if tools %}<tool_call>{% endif %}'},
                'layers': {'first': 48, 'dense_kib': [99368], 'exps_kib': [2611200]},
                'other_kib': [('output.weight', 659600), ('token_embd.weight', 659600)]}]}
# fmt: on


def headers(key):
    """FIXTURES[key] -> list of parsed GGUFHeader (one per shard)."""
    out = []
    for shard in FIXTURES[key]:
        lay = shard["layers"]
        tensors = []
        for i, kib in enumerate(lay.get("dense_kib", [])):
            if kib:
                tensors.append((f"blk.{lay['first'] + i}.attn_q.weight", (kib * 1024,), "I8"))
        for i, kib in enumerate(lay.get("exps_kib", [])):
            if kib:
                tensors.append((f"blk.{lay['first'] + i}.ffn_up_exps.weight", (kib * 1024,), "I8"))
        tensors += [(name, (kib * 1024,), "I8") for name, kib in shard["other_kib"]]
        data = build_test_gguf(shard["kv"], tensors)
        out.append(parse_header(lambda o, n, d=data: d[o:o + n]))
    return out


MM = 1200726496          # mmproj-BF16.gguf bytes (walter/models.md)
L = lambda *r: [x for a, b in r for x in range(a, b + 1)]  # noqa: E731

# name: (fixture, ctx, estimate kwargs, measured MiB per GPU (None = GPU unused), measured decode tok/s or None)
MEASURED = {
    "coder (MTP n2)": ("coder", 131072, dict(placement="gpu0", mtp=True), (22640, None), None),
    "coder (no MTP)": ("coder", 131072, dict(placement="gpu0"), (21300, None), 40.5),
    "vision tensor": ("vision", 131072, dict(placement="both", split="tensor", mmproj_bytes=MM), (14640, 13342), 54.7),
    "vision layer": ("vision", 131072, dict(placement="both", split="layer", mmproj_bytes=MM), (14448, 13366), 39.5),
    "hermes tensor": ("hermes", 65536, dict(placement="both", split="tensor"), (15342, 15342), 50.4),
    "hermes layer": ("hermes", 65536, dict(placement="both", split="layer"), (15550, 15318), 34.4),
    "flash V7 (live)": ("flash", 131072, dict(placement="both+cpu-experts", cpu_expert_layers=L((0, 5), (24, 33)),
                                              draft="flash_mtp", draft_gpu=1), (21494, 23484), None),
    "flash V0": ("flash", 131072, dict(placement="both+cpu-experts", cpu_expert_layers=L((0, 6), (24, 30))),
                 (20402, 22394), 35.7),
    "flash V1": ("flash", 131072, dict(placement="both+cpu-experts", cpu_expert_layers=L((0, 4), (24, 29))),
                 (22330, 23232), 38.5),
    "flash V6": ("flash", 131072, dict(placement="both+cpu-experts", cpu_expert_layers=L((0, 9), (24, 30))),
                 (17640, 22394), 32.7),
    "flash V5": ("flash", 131072, dict(placement="both+cpu-experts", cpu_expert_layers=L((0, 9), (24, 30)),
                                       draft="flash_mtp", draft_gpu=0), (21798, 22488), None),
    "coder-fast": ("coder_fast", 131072, dict(placement="gpu1"), (None, None), 153.2),
    "big layer": ("big", 131072, dict(placement="both", split="layer"), (None, None), 125.5),
}


def run(name):
    key, ctx, kw, meas, tps = MEASURED[name]
    kw = dict(kw)
    if "draft" in kw:
        kw["draft_headers"] = headers(kw.pop("draft"))
    return fit.estimate(headers(key), ctx, **kw), meas, tps


@pytest.mark.parametrize("name", [n for n, v in MEASURED.items() if any(v[3])])
def test_calibration_vram(name):
    f, meas, _tps = run(name)
    for g, m in enumerate(meas):
        if m is None:
            continue
        res = f.vram_gb[g] - m / 1024
        assert abs(res) <= 1.5, f"{name} GPU{g}: estimate {f.vram_gb[g]} vs measured {m / 1024:.2f} GiB"
        assert abs(res) <= 0.7, f"{name} GPU{g}: residual {res:+.2f} GiB drifted from the documented calibration"


@pytest.mark.parametrize("name", [n for n, v in MEASURED.items() if v[4]])
def test_calibration_speed(name):
    f, _meas, tps = run(name)
    assert abs(f.tok_s / tps - 1) <= 0.10, f"{name}: {f.tok_s} vs measured {tps} tok/s"


def test_auto_placement_of_live_models():
    """What `fit` says about each live model at its served context: matches how it is actually deployed."""
    f = fit.estimate(headers("coder"), 131072)
    assert (f.placement, f.split, f.speed_class, f.fits) == ("gpu1", "none", "B", True)
    f = fit.estimate(headers("coder"), 131072, mtp=True)
    assert f.fits and f.vram_gb[1] <= fit.USABLE_GIB
    f = fit.estimate(headers("coder_fast"), 131072)
    assert (f.placement, f.speed_class) == ("gpu1", "A")
    f = fit.estimate(headers("big"), 131072)
    assert (f.placement, f.split, f.speed_class) == ("both", "layer", "A")
    f = fit.estimate(headers("vision"), 131072, mmproj_bytes=MM)
    assert (f.placement, f.split, f.speed_class) == ("both", "tensor", "B")
    assert f.vram_gb[0] > f.vram_gb[1]            # the projector sits on GPU0
    f = fit.estimate(headers("hermes"), 65536)
    assert (f.placement, f.split) == ("both", "tensor")
    f = fit.estimate(headers("flash"), 131072)
    assert f.placement == "both+cpu-experts" and f.split == "layer" and f.fits
    assert 8 <= len(f.cpu_expert_layers) <= 16 and f.speed_class == "B"
    assert f.cpu_override.startswith("blk\\.(0|1|") and f.cpu_override.endswith(")\\.ffn_.*_exps\\.=CPU")
    assert all(v <= fit.USABLE_GIB for v in f.vram_gb)
    assert f.ram_gb > 28                           # the n-gram table (shard 2) never goes to a GPU


def test_hybrid_kv_counts_attention_layers_only():
    f = fit.estimate(headers("flash"), 131072, placement="both", split="layer")
    assert f.model["n_attn_layers"] == 12 and f.model["n_layer"] == 48
    f = fit.estimate(headers("coder"), 131072, placement="gpu0")
    assert f.model["n_attn_layers"] == 16 and f.model["n_nextn"] == 1


def test_swa_layers_are_capped_at_the_window():
    f = fit.estimate(headers("vision"), 131072, placement="gpu0")
    assert f.model["n_swa_layers"] == 50 and f.model["n_attn_layers"] == 60
    small = fit.estimate(headers("vision"), 8192, placement="gpu0")
    # 10 global layers x 4 heads x (512+512) x 1.0625 B; SWA layers stay at 1536 cells
    per_ctx = 10 * 4 * 1024 * 34 / 32
    assert f.breakdown["kv"][0] - small.breakdown["kv"][0] == pytest.approx(per_ctx * (131072 - 8192) / fit.GIB,
                                                                             abs=0.01)


def test_ctx_and_kv_type_scale_kv():
    a = fit.estimate(headers("hermes"), 32768, placement="both", split="tensor")
    b = fit.estimate(headers("hermes"), 65536, placement="both", split="tensor")
    c = fit.estimate(headers("hermes"), 65536, placement="both", split="tensor", kv_type="f16")
    assert b.breakdown["kv"][0] == pytest.approx(2 * a.breakdown["kv"][0], rel=1e-3)
    assert c.breakdown["kv"][0] == pytest.approx(b.breakdown["kv"][0] * 2 / (34 / 32), rel=1e-3)


# --- synthetic models -------------------------------------------------------------------------------------------
TMPL = "{%- if tools %}<tool_call>{{ tools }}</tool_call>{% endif %}{% for m in messages %}{{ m.content }}{% endfor %}"


def model(arch="llama", n_layer=32, layer_mib=400, exp_mib=0, kv_heads=8, head_dim=128, vocab=128000, extra=None,
          template=TMPL, tied=False, n_expert=0):
    kv = {"general.architecture": arch, f"{arch}.block_count": n_layer, f"{arch}.embedding_length": 4096,
          f"{arch}.attention.head_count": 32, f"{arch}.attention.head_count_kv": kv_heads,
          f"{arch}.attention.key_length": head_dim, f"{arch}.attention.value_length": head_dim,
          f"{arch}.vocab_size": vocab, f"{arch}.context_length": 131072}
    if n_expert:
        kv.update({f"{arch}.expert_count": n_expert, f"{arch}.expert_used_count": 8})
    if template is not None:
        kv["tokenizer.chat_template"] = template
    kv.update(extra or {})
    t = [("token_embd.weight", (vocab * 4096,), "I8")]
    if not tied:
        t.append(("output.weight", (vocab * 4096,), "I8"))
    for il in range(n_layer):
        t.append((f"blk.{il}.attn_q.weight", (layer_mib << 20,), "I8"))
        if exp_mib:
            t.append((f"blk.{il}.ffn_up_exps.weight", (exp_mib << 20,), "I8"))
    data = build_test_gguf(kv, t)
    return [parse_header(lambda o, n: data[o:o + n])]


def test_small_dense_goes_on_one_gpu():
    f = fit.estimate(model(), 32768)
    assert (f.placement, f.split, f.fits) == ("gpu1", "none", True)
    assert f.vram_gb[0] == 0.0 and 12 < f.vram_gb[1] < 22.5
    assert fit.estimate(model(), 32768, prefer_gpu=0).placement == "gpu0"
    assert f.ram_gb == pytest.approx(128000 * 4096 / fit.GIB, abs=0.01)   # token_embd stays on the CPU


def test_tied_embeddings_put_a_copy_on_the_gpu():
    a = fit.estimate(model(), 8192)
    b = fit.estimate(model(tied=True), 8192)
    assert a.vram_gb[1] == pytest.approx(b.vram_gb[1], abs=0.01)     # output dup == the output tensor it replaces


def test_dense_too_big_for_one_uses_tensor_split():
    f = fit.estimate(model(layer_mib=900), 32768)
    assert (f.placement, f.split) == ("both", "tensor") and f.vram_gb[0] == f.vram_gb[1]


def test_moe_uses_layer_split_then_cpu_experts_then_none():
    f = fit.estimate(model(layer_mib=100, exp_mib=1000, n_expert=128), 32768)
    assert (f.placement, f.split) == ("both", "layer")
    f = fit.estimate(model(layer_mib=100, exp_mib=1500, n_expert=128), 32768)
    assert f.placement == "both+cpu-experts" and f.cpu_expert_layers and f.fits
    assert f.ram_gb <= fit.RAM_BUDGET_GIB + 1
    g0 = [il for il in f.cpu_expert_layers if il / 33 < 0.5]
    assert g0 == list(range(len(g0)))             # lowest layers of each GPU's half first
    f = fit.estimate(model(layer_mib=100, exp_mib=4000, n_expert=128), 32768)
    assert f.placement == "none" and not f.fits and any("does not fit" in w for w in f.warnings)
    f = fit.estimate(model(layer_mib=100, exp_mib=1500, n_expert=128), 32768, ram_budget_gb=4)
    assert f.placement == "none"


def test_dense_too_big_for_two_does_not_fit():
    f = fit.estimate(model(layer_mib=1600), 32768)
    assert f.placement == "none" and f.split is None


def test_speed_classes():
    assert fit.estimate(model(layer_mib=100), 8192).speed_class == "A"
    assert fit.estimate(model(layer_mib=400), 8192).speed_class == "B"
    f = fit.estimate(model(layer_mib=100, exp_mib=1500, n_expert=8), 32768)   # all experts active, many on CPU
    assert f.placement in ("both+cpu-experts", "none")
    if f.placement == "both+cpu-experts" or f.speed_class == "D":
        assert f.speed_class in ("C", "D")
    slow = fit.estimate(model(layer_mib=100, exp_mib=1500, n_expert=8), 32768, placement="both+cpu-experts",
                        cpu_expert_layers=range(0, 32))
    assert slow.speed_class == "D" and not slow.fits and any("class D" in w for w in slow.warnings)


def test_mla_kv():
    extra = {"deepseek2.attention.kv_lora_rank": 512, "deepseek2.rope.dimension_count": 64}
    f = fit.estimate(model("deepseek2", n_layer=10, layer_mib=10, extra=extra), 100000, placement="gpu0")
    assert f.breakdown["kv"][0] == pytest.approx(10 * (512 + 64) * 34 / 32 * 100000 / fit.GIB, rel=1e-3)


def test_swa_default_pattern_and_shared_kv():
    extra = {"gemma3.attention.sliding_window": 1024}
    f = fit.estimate(model("gemma3", n_layer=12, layer_mib=10, extra=extra), 65536, placement="gpu0")
    assert f.model["n_swa_layers"] == 10          # pattern 6: 5 SWA + 1 global
    extra = {"gemma3n.attention.shared_kv_layers": 4}
    f = fit.estimate(model("gemma3n", n_layer=12, layer_mib=10, extra=extra), 8192, placement="gpu0")
    assert f.model["n_attn_layers"] == 8


def test_recurrent_arch_has_no_kv():
    extra = {"mamba2.ssm.conv_kernel": 4, "mamba2.ssm.state_size": 128, "mamba2.ssm.inner_size": 8192,
             "mamba2.ssm.group_count": 8}
    f = fit.estimate(model("mamba2", n_layer=8, layer_mib=10, extra=extra), 1_000_000, placement="gpu0")
    assert f.model["n_attn_layers"] == 0
    assert f.breakdown["kv"][0] == pytest.approx(8 * 4 * (3 * (8192 + 2 * 8 * 128) + 128 * 8192) / fit.GIB, abs=1e-3)


def test_warnings():
    f = fit.estimate(model("not-an-arch"), 8192)
    assert any("not in llama.cpp b11277" in w for w in f.warnings)
    assert not any("b11277" in w for w in fit.estimate(model(), 8192).warnings)
    assert any("no chat template" in w for w in fit.estimate(model(template=None), 8192).warnings)
    assert any("no tool-call markers" in w for w in fit.estimate(model(template="{{ messages }}"), 8192).warnings)
    assert any("exceeds the trained" in w for w in fit.estimate(model(), 262144).warnings)
    h = model(extra={"split.no": Typed("u16", 0), "split.count": Typed("u16", 2)})
    assert any("split.count" in w for w in fit.estimate(h, 8192).warnings)
    assert any("mtp requested" in w for w in fit.estimate(model(), 8192, mtp=True).warnings)


def test_arch_list_is_the_frozen_b11277_one():
    assert fit.LLAMA_CPP_BUILD == "b11277"
    assert len(fit.SUPPORTED_ARCHS) == 152
    for a in ("llama", "qwen35", "qwen35moe", "qwen3next", "qwen4exp", "gemma4", "seed_oss", "clip", "gpt-oss",
              "deepseek2"):
        assert a in fit.SUPPORTED_ARCHS
    assert "(unknown)" not in fit.SUPPORTED_ARCHS


def test_draft_bytes_and_mmproj_land_where_asked():
    base = fit.estimate(model(), 8192, placement="both", split="tensor")
    d = fit.estimate(model(), 8192, placement="both", split="tensor", draft_bytes=1 << 30, draft_gpu=1)
    assert d.vram_gb[1] - base.vram_gb[1] == pytest.approx(1 + fit.DRAFT_COMPUTE, abs=0.01)
    assert d.vram_gb[0] == base.vram_gb[0]
    m = fit.estimate(model(), 8192, placement="gpu1", mmproj_bytes=1 << 30)
    assert m.vram_gb[1] - fit.estimate(model(), 8192, placement="gpu1").vram_gb[1] == pytest.approx(
        1 + fit.MMPROJ_COMPUTE, abs=0.01)


def test_as_dict_is_json_ready():
    import json
    d = fit.estimate(model(), 8192).as_dict()
    json.dumps(d)
    assert set(d) >= {"placement", "vram_gb", "speed_class", "warnings", "split", "fits"}
