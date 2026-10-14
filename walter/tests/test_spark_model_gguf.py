"""walter/models/lib/gguf.py: the bounded GGUF header parser and the test writer.

Round trips through build_test_gguf, every value type, ggml type sizes, and hostile input (truncation, huge counts,
huge lengths, wrong magic, bad versions): always GGUFError, never MemoryError or a big allocation.
Run: python3 -B -m pytest -q walter/tests/test_spark_model_gguf.py
"""
import random
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "models" / "lib"))
import gguf  # noqa: E402
from gguf import GGUFError, Typed, build_test_gguf, parse_header  # noqa: E402


def reader(data: bytes):
    calls = []

    def read(offset, n):
        calls.append((offset, n))
        return data[offset:offset + n]
    read.calls = calls
    return read


def parse(data: bytes, **kw):
    return parse_header(reader(data), **kw)


KV = {
    "general.architecture": "llama",
    "general.name": "tiny",
    "llama.block_count": 2,
    "llama.context_length": 4096,
    "llama.rope.freq_base": 10000.0,
    "tokenizer.ggml.tokens": ["<s>", "a", "b", "ü"],
    "tokenizer.ggml.scores": [0.0, -1.0, -2.0, -3.0],
    "tokenizer.ggml.token_type": [3, 1, 1, 1],
    "tokenizer.chat_template": "{% for m in messages %}{{ m.content }}{% endfor %}",
    "flag": True,
}
TENSORS = [
    ("token_embd.weight", (64, 4), "Q8_0"),
    ("blk.0.attn_q.weight", (256, 64), "Q4_K"),
    ("output_norm.weight", (64,), "F32"),
]


def test_round_trip():
    data = build_test_gguf(KV, TENSORS)
    h = parse(data)
    assert h.version == 3 and h.alignment == 32
    assert h.kv["general.architecture"] == "llama"
    assert h.kv["tokenizer.ggml.tokens"] == ["<s>", "a", "b", "ü"]
    assert h.kv["tokenizer.ggml.scores"] == [0.0, -1.0, -2.0, -3.0]
    assert h.kv["tokenizer.ggml.token_type"] == [3, 1, 1, 1]
    assert h.kv["flag"] is True
    assert h.kv_types["llama.block_count"] == gguf.T_UINT32
    assert h.kv_types["tokenizer.ggml.scores"] == gguf.T_ARRAY
    assert h.header_bytes == len(data) and h.header_bytes % 32 == 0
    names = [t.name for t in h.tensors]
    assert names == [t[0] for t in TENSORS]
    t = h.tensors[1]
    assert t.shape == (256, 64) and t.ggml_type == 12 and t.nbytes == 64 * 144
    assert h.tensors[0].nbytes == 4 * 2 * 34 and h.tensors[2].nbytes == 256
    assert h.tensors[1].offset == -(-h.tensors[0].nbytes // 32) * 32
    assert h.arch == "llama"


def test_all_scalar_types_and_arrays():
    kv = {}
    vals = {"u8": 255, "i8": -128, "u16": 65535, "i16": -32768, "u32": 2**32 - 1, "i32": -2**31,
            "f32": 1.5, "bool": False, "str": "hé", "u64": 2**64 - 1, "i64": -2**63, "f64": 2.0**-1000}
    for t, v in vals.items():
        kv["s." + t] = Typed(t, v)
        if t != "str":
            kv["a." + t] = Typed("array", [v, v], elem=t)
    kv["a.str"] = Typed("array", ["x", ""], elem="str")
    kv["a.empty"] = Typed("array", [], elem="u8")
    h = parse(build_test_gguf(kv, []))
    for t, v in vals.items():
        assert h.kv["s." + t] == v, t
        assert h.kv_types["s." + t] == gguf.GGUF_TYPES[t]
    assert h.kv["a.i64"] == [-2**63, -2**63]
    assert h.kv["a.bool"] == [False, False]
    assert h.kv["a.str"] == ["x", ""]
    assert h.kv["a.empty"] == []
    assert h.tensors == [] and h.header_bytes == len(build_test_gguf(kv, []))


def test_version_2_and_alignment():
    kv = {"general.alignment": 64, "general.architecture": "qwen3"}
    data = build_test_gguf(kv, [("a", (32,), "F16"), ("b", (32,), "F16")], version=2)
    h = parse(data)
    assert h.version == 2 and h.alignment == 64
    assert h.header_bytes % 64 == 0 and h.tensors[1].offset == 64


def test_parse_file_and_with_data(tmp_path):
    p = tmp_path / "m.gguf"
    data = build_test_gguf(KV, TENSORS, with_data=True)
    p.write_bytes(data)
    h = gguf.parse_file(p)
    last = h.tensors[-1]
    assert h.header_bytes + last.offset + last.nbytes == len(data)


def test_split_info():
    h = parse(build_test_gguf({"split.no": Typed("u16", 1), "split.count": Typed("u16", 3)}, []))
    assert gguf.split_info(h) == (1, 3)
    assert gguf.split_info(parse(build_test_gguf({}, []))) is None


@pytest.mark.parametrize("name,blck,size", [
    ("F32", 1, 4), ("F16", 1, 2), ("BF16", 1, 2), ("Q4_0", 32, 18), ("Q4_1", 32, 20), ("Q5_0", 32, 22),
    ("Q5_1", 32, 24), ("Q8_0", 32, 34), ("Q2_K", 256, 84), ("Q3_K", 256, 110), ("Q4_K", 256, 144),
    ("Q5_K", 256, 176), ("Q6_K", 256, 210), ("Q8_K", 256, 292), ("IQ2_XXS", 256, 66), ("IQ2_XS", 256, 74),
    ("IQ2_S", 256, 82), ("IQ3_XXS", 256, 98), ("IQ3_S", 256, 110), ("IQ1_S", 256, 50), ("IQ1_M", 256, 56),
    ("IQ4_NL", 32, 18), ("IQ4_XS", 256, 136), ("TQ1_0", 256, 54), ("TQ2_0", 256, 66), ("MXFP4", 32, 17),
    ("NVFP4", 64, 36), ("Q1_0", 128, 18), ("Q2_0", 64, 18), ("I8", 1, 1), ("I64", 1, 8), ("F64", 1, 8),
])
def test_ggml_type_sizes(name, blck, size):
    tid = gguf.GGML_TYPE_IDS[name]
    assert gguf.GGML_TYPES[tid][1:] == (blck, size)
    assert gguf.tensor_nbytes((blck * 4, 3), tid) == 12 * size


def test_every_type_parses():
    tensors = [(f"t{tid}", (gguf.GGML_TYPES[tid][1] * 2, 2), tid) for tid in gguf.GGML_TYPES]
    h = parse(build_test_gguf({}, tensors))
    assert len(h.tensors) == len(gguf.GGML_TYPES)
    assert len(gguf.GGML_TYPES) == 35   # b11277: GGML_TYPE_COUNT 43 minus the 8 removed ids (4, 5, 31-33, 36-38)


# --- hostile input -------------------------------------------------------------------------------------------
HDR = b"GGUF" + struct.pack("<I", 3)


def test_wrong_magic():
    with pytest.raises(GGUFError, match="magic"):
        parse(b"GGML" + b"\0" * 100)
    with pytest.raises(GGUFError):
        parse(b"")


@pytest.mark.parametrize("version", [0, 1, 4, 0x03000000])
def test_bad_versions(version):
    with pytest.raises(GGUFError):
        parse(b"GGUF" + struct.pack("<IQQ", version, 0, 0))


def test_truncated_everywhere():
    data = build_test_gguf(KV, TENSORS)
    end_of_records = len(data.rstrip(b"\0"))
    for cut in range(0, end_of_records - 1):
        with pytest.raises(GGUFError):
            parse(data[:cut])


def test_huge_counts():
    for nt, nkv in [(2**64 - 1, 0), (0, 2**64 - 1), (gguf.MAX_TENSORS + 1, 0), (10**6, 10**4)]:
        with pytest.raises(GGUFError):
            parse(HDR + struct.pack("<QQ", nt, nkv) + b"\0" * 64)


def test_huge_string_length():
    data = HDR + struct.pack("<QQ", 0, 1) + struct.pack("<Q", 2**63)
    with pytest.raises(GGUFError, match="cap|limit"):
        parse(data)
    key = b"k"
    data = HDR + struct.pack("<QQ", 0, 1) + struct.pack("<Q", 1) + key + struct.pack("<IQ", 8, 100 << 20)
    with pytest.raises(GGUFError):
        parse(data)
    # length within caps but beyond max_bytes: refused before any read of that size
    r = reader(HDR + struct.pack("<QQ", 0, 1) + struct.pack("<Q", 1) + key + struct.pack("<IQ", 8, 10 << 20))
    with pytest.raises(GGUFError, match="limit"):
        parse_header(r, max_bytes=1 << 20)
    assert all(n <= 1 << 20 for _o, n in r.calls)


def test_huge_array_length():
    for etype in (gguf.T_UINT8, gguf.T_UINT64, gguf.T_STRING):
        data = HDR + struct.pack("<QQ", 0, 1) + struct.pack("<Q", 1) + b"k" + struct.pack("<IIQ", 9, etype, 2**62)
        with pytest.raises(GGUFError):
            parse(data)
        data = HDR + struct.pack("<QQ", 0, 1) + struct.pack("<Q", 1) + b"k" + struct.pack("<IIQ", 9, etype, 2**24)
        with pytest.raises(GGUFError):   # within the element cap, but 16M elements are not there
            parse(data)


def test_nested_array_and_bad_types():
    nested = HDR + struct.pack("<QQ", 0, 1) + struct.pack("<Q", 1) + b"k" + struct.pack("<IIQ", 9, 9, 1)
    with pytest.raises(GGUFError, match="nested"):
        parse(nested + b"\0" * 32)
    for vt in (13, 2**32 - 1):
        with pytest.raises(GGUFError, match="type"):
            parse(HDR + struct.pack("<QQ", 0, 1) + struct.pack("<Q", 1) + b"k" + struct.pack("<I", vt) + b"\0" * 16)


def test_tensor_checks():
    good = build_test_gguf({}, [("a", (32,), "F16")])
    assert parse(good).tensors[0].nbytes == 64

    def one(name=b"a", dims=(32,), gt=1, offset=0, ndims=None):
        b = HDR + struct.pack("<QQ", 1, 0) + struct.pack("<Q", len(name)) + name
        b += struct.pack("<I", len(dims) if ndims is None else ndims) + struct.pack(f"<{len(dims)}q", *dims)
        return b + struct.pack("<iQ", gt, offset) + b"\0" * 64

    with pytest.raises(GGUFError, match="dims"):
        parse(one(ndims=5, dims=(1, 1, 1, 1, 1)))
    with pytest.raises(GGUFError, match="negative"):
        parse(one(dims=(-32,)))
    with pytest.raises(GGUFError, match="unknown ggml type"):
        parse(one(gt=4))
    with pytest.raises(GGUFError, match="unknown ggml type"):
        parse(one(gt=-1))
    with pytest.raises(GGUFError, match="block size"):
        parse(one(gt=12, dims=(100,)))
    with pytest.raises(GGUFError, match="offset"):
        parse(one(offset=32))
    with pytest.raises(GGUFError, match="too long"):
        parse(one(name=b"x" * 64))
    with pytest.raises(GGUFError, match="overflow"):
        parse(one(dims=(2**62, 2**62)))


def test_duplicates_and_alignment():
    def kvs(*pairs):
        b = HDR + struct.pack("<QQ", 0, len(pairs))
        for k, t, v in pairs:
            b += struct.pack("<Q", len(k)) + k + struct.pack("<I", t) + v
        return b
    with pytest.raises(GGUFError, match="duplicate"):
        parse(kvs((b"a", 4, struct.pack("<I", 1)), (b"a", 4, struct.pack("<I", 2))))
    with pytest.raises(GGUFError, match="empty"):
        parse(kvs((b"", 4, struct.pack("<I", 1))))
    with pytest.raises(GGUFError, match="u32"):
        parse(kvs((b"general.alignment", 10, struct.pack("<Q", 32))))
    with pytest.raises(GGUFError, match="power of 2"):
        parse(kvs((b"general.alignment", 4, struct.pack("<I", 48))))
    with pytest.raises(GGUFError, match="duplicate tensor"):
        parse(build_test_gguf({}, [("a", (32,), "F16"), ("a", (32,), "F16")]))


def test_reader_errors_become_gguf_errors():
    def bad(offset, n):
        raise OSError("disk on fire")
    with pytest.raises(GGUFError, match="disk on fire"):
        parse_header(bad)


def test_random_mutations_never_escape():
    rng = random.Random(1234)
    base = build_test_gguf(KV, TENSORS)
    for _ in range(1500):
        b = bytearray(base)
        for _k in range(rng.randint(1, 6)):
            i = rng.randrange(len(b))
            b[i] = rng.randrange(256)
        if rng.random() < 0.3:
            b = b[:rng.randrange(len(b))]
        try:
            parse(bytes(b), max_bytes=1 << 20)
        except GGUFError:
            pass


def test_reads_are_bounded_by_max_bytes():
    r = reader(build_test_gguf(KV, TENSORS))
    parse_header(r, max_bytes=4096)
    assert all(o + n <= 4096 for o, n in r.calls)
    with pytest.raises(GGUFError, match="limit"):
        parse_header(reader(build_test_gguf(KV, TENSORS)), max_bytes=100)
