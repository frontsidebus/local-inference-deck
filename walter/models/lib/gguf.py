"""Bounded GGUF header parser (v2/v3, little-endian), stdlib only.

The GGUF header (magic, version, counts, key/value metadata, tensor infos) sits at the start of the file, before the
tensor data. `parse_header` reads only that part, through a `read(offset, n)` callable, so the same code parses a
local file, an HTTP Range reader (hf.range_reader) or an in-memory buffer.

Untrusted input: every count and length is checked against hard caps and against `max_bytes` (the most of the file
the parser will ever look at) BEFORE anything is allocated or read, so a hostile header raises GGUFError instead of
exhausting memory. The checks mirror llama.cpp's own loader (ggml/src/gguf.cpp at build b11277): version 2 or 3,
no duplicate keys or tensor names, `general.alignment` a u32 power of two, at most 4 dims, a known ggml type with
the row size a multiple of the block size, tensor names < 64 bytes, tensor data offsets contiguous and aligned.
Nested arrays are refused (llama.cpp refuses them too). Big-endian files are refused.

Type sizes (`GGML_TYPES`) are the ggml_type enum and `type_traits` block sizes of llama.cpp b11277
(ggml/include/ggml.h, ggml/src/ggml.c; cross-checked with gguf-py GGML_QUANT_SIZES).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = [
    "GGUFError", "GGUFHeader", "TensorInfo", "Typed", "GGML_TYPES", "GGUF_TYPES",
    "parse_header", "parse_file", "split_info", "build_test_gguf", "tensor_nbytes", "type_name",
]

MAGIC = b"GGUF"
SUPPORTED_VERSIONS = (2, 3)
DEFAULT_ALIGNMENT = 32
MAX_DIMS = 4
MAX_NAME = 64                      # GGML_MAX_NAME: tensor names must be shorter

# Hard caps, far above anything real (largest public GGUFs: ~3k tensors, ~60 keys, 262k-entry vocab arrays,
# chat templates < 100 KB) and far below what could exhaust memory. `max_bytes` bounds everything as well.
MAX_TENSORS = 1 << 20
MAX_KV = 1 << 16
MAX_STRING = 64 << 20              # one string (key, value, tensor name, array element)
MAX_ARRAY = 1 << 25                # elements in one array
MAX_KEY = 1 << 16                  # one key


class GGUFError(ValueError):
    """The input is not a GGUF header this parser accepts (bad magic, truncated, out of bounds, malformed)."""


# --- ggml types: id -> (name, block size in elements, bytes per block); llama.cpp b11277 -----------------------
QK_K = 256
GGML_TYPES: dict[int, tuple[str, int, int]] = {
    0: ("F32", 1, 4),
    1: ("F16", 1, 2),
    2: ("Q4_0", 32, 2 + 16),
    3: ("Q4_1", 32, 2 + 2 + 16),
    # 4, 5: Q4_2 / Q4_3, removed
    6: ("Q5_0", 32, 2 + 4 + 16),
    7: ("Q5_1", 32, 2 + 2 + 4 + 16),
    8: ("Q8_0", 32, 2 + 32),
    9: ("Q8_1", 32, 2 + 2 + 32),
    10: ("Q2_K", QK_K, 2 + 2 + QK_K // 16 + QK_K // 4),
    11: ("Q3_K", QK_K, 2 + QK_K // 4 + QK_K // 8 + 12),
    12: ("Q4_K", QK_K, 2 + 2 + QK_K // 2 + 12),
    13: ("Q5_K", QK_K, 2 + 2 + QK_K // 2 + QK_K // 8 + 12),
    14: ("Q6_K", QK_K, 2 + QK_K // 2 + QK_K // 4 + QK_K // 16),
    15: ("Q8_K", QK_K, 4 + QK_K + QK_K // 8),
    16: ("IQ2_XXS", QK_K, 2 + QK_K // 4),
    17: ("IQ2_XS", QK_K, 2 + QK_K // 4 + QK_K // 32),
    18: ("IQ3_XXS", QK_K, 2 + QK_K // 4 + QK_K // 8),
    19: ("IQ1_S", QK_K, 2 + QK_K // 8 + QK_K // 16),
    20: ("IQ4_NL", 32, 2 + 16),
    21: ("IQ3_S", QK_K, 2 + QK_K // 4 + QK_K // 8 + QK_K // 32 + 4),
    22: ("IQ2_S", QK_K, 2 + QK_K // 4 + QK_K // 16),
    23: ("IQ4_XS", QK_K, 2 + 2 + QK_K // 2 + QK_K // 64),
    24: ("I8", 1, 1),
    25: ("I16", 1, 2),
    26: ("I32", 1, 4),
    27: ("I64", 1, 8),
    28: ("F64", 1, 8),
    29: ("IQ1_M", QK_K, QK_K // 8 + QK_K // 16 + QK_K // 32),
    30: ("BF16", 1, 2),
    # 31-33: Q4_0_4_4 / 4_8 / 8_8, removed from GGUF files
    34: ("TQ1_0", QK_K, 2 + 4 * 13),
    35: ("TQ2_0", QK_K, 2 + 64),
    # 36-38: IQ4_NL_4_4 / 4_8 / 8_8, removed
    39: ("MXFP4", 32, 1 + 16),
    40: ("NVFP4", 64, 4 + 32),
    41: ("Q1_0", 128, 2 + 16),
    42: ("Q2_0", 64, 2 + 16),
}
GGML_TYPE_IDS = {name: tid for tid, (name, _b, _s) in GGML_TYPES.items()}


def type_name(ggml_type: int) -> str:
    t = GGML_TYPES.get(ggml_type)
    return t[0] if t else f"type{ggml_type}"


def tensor_nbytes(shape: tuple, ggml_type: int) -> int:
    """Bytes of a tensor's data (unpadded), as ggml_nbytes; GGUFError for an unknown type or a bad row size."""
    if ggml_type not in GGML_TYPES:
        raise GGUFError(f"unknown ggml type {ggml_type} (not in llama.cpp b11277)")
    _name, blck, size = GGML_TYPES[ggml_type]
    ne = list(shape) + [1] * (MAX_DIMS - len(shape))
    if ne[0] % blck:
        raise GGUFError(f"row size {ne[0]} is not a multiple of the {type_name(ggml_type)} block size {blck}")
    n = 1
    for d in ne:
        n *= d
    return n // blck * size


# --- GGUF metadata value types -------------------------------------------------------------------------------
T_UINT8, T_INT8, T_UINT16, T_INT16, T_UINT32, T_INT32, T_FLOAT32, T_BOOL, T_STRING, T_ARRAY, T_UINT64, T_INT64, \
    T_FLOAT64 = range(13)
# type -> (struct code, size); strings and arrays are variable
_SCALAR = {
    T_UINT8: ("B", 1), T_INT8: ("b", 1), T_UINT16: ("H", 2), T_INT16: ("h", 2), T_UINT32: ("I", 4),
    T_INT32: ("i", 4), T_FLOAT32: ("f", 4), T_BOOL: ("b", 1), T_UINT64: ("Q", 8), T_INT64: ("q", 8),
    T_FLOAT64: ("d", 8),
}
GGUF_TYPES = {
    "u8": T_UINT8, "i8": T_INT8, "u16": T_UINT16, "i16": T_INT16, "u32": T_UINT32, "i32": T_INT32,
    "f32": T_FLOAT32, "bool": T_BOOL, "str": T_STRING, "array": T_ARRAY, "u64": T_UINT64, "i64": T_INT64,
    "f64": T_FLOAT64,
}


@dataclass(frozen=True)
class TensorInfo:
    name: str
    shape: tuple
    ggml_type: int
    offset: int          # relative to the start of the data section (header_bytes)
    nbytes: int


@dataclass
class GGUFHeader:
    version: int
    kv: dict[str, Any]
    tensors: list[TensorInfo]
    header_bytes: int    # file offset where tensor data begins (end of header, padded to alignment)
    alignment: int
    kv_types: dict[str, int] = field(default_factory=dict, repr=False)   # key -> GGUF value type (arrays: T_ARRAY)

    @property
    def arch(self) -> str | None:
        a = self.kv.get("general.architecture")
        return a if isinstance(a, str) else None

    @property
    def data_bytes(self) -> int:
        return sum(t.nbytes for t in self.tensors)


class _Cursor:
    """Sequential reader over read(offset, n) with a small buffer and a hard ceiling (max_bytes)."""

    CHUNK = 1 << 20

    def __init__(self, read: Callable[[int, int], bytes], max_bytes: int):
        self._read = read
        self.max = max_bytes
        self.pos = 0
        self._buf = b""
        self._buf_start = 0

    def need(self, n: int, what: str) -> None:
        if n < 0 or self.pos + n > self.max:
            raise GGUFError(f"{what}: {n} bytes at offset {self.pos} would pass the {self.max}-byte header limit")

    def take(self, n: int, what: str = "data") -> bytes:
        self.need(n, what)
        end = self.pos + n
        rel = self.pos - self._buf_start
        if rel < 0 or end > self._buf_start + len(self._buf):
            want = min(max(n, self.CHUNK), self.max - self.pos)
            try:
                data = self._read(self.pos, want)
            except GGUFError:
                raise
            except (OSError, ValueError) as e:
                raise GGUFError(f"read failed at offset {self.pos}: {e}") from None
            data = bytes(data[:want]) if data is not None else b""
            self._buf, self._buf_start, rel = data, self.pos, 0
            if len(data) < n:
                raise GGUFError(f"truncated: {what} needs {n} bytes at offset {self.pos}, got {len(data)}")
        out = self._buf[rel:rel + n]
        self.pos = end
        return out

    def unpack(self, fmt: str, size: int, what: str):
        return struct.unpack("<" + fmt, self.take(size, what))[0]

    def string(self, what: str, cap: int = MAX_STRING) -> str:
        n = self.unpack("Q", 8, what + " length")
        if n > cap:
            raise GGUFError(f"{what}: string length {n} exceeds the cap {cap}")
        raw = self.take(n, what)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("utf-8", errors="replace")


def _read_value(c: _Cursor, vtype: int, key: str, allow_array: bool = True):
    if vtype in _SCALAR:
        code, size = _SCALAR[vtype]
        v = c.unpack(code, size, key)
        return bool(v) if vtype == T_BOOL else v
    if vtype == T_STRING:
        return c.string(key)
    if vtype == T_ARRAY:
        if not allow_array:
            raise GGUFError(f"{key}: nested arrays are not supported")
        etype = c.unpack("I", 4, key + " array type")
        n = c.unpack("Q", 8, key + " array length")
        if n > MAX_ARRAY:
            raise GGUFError(f"{key}: array length {n} exceeds the cap {MAX_ARRAY}")
        if etype in _SCALAR:
            code, size = _SCALAR[etype]
            raw = c.take(n * size, key)   # need() bounds n*size by max_bytes before reading
            vals = list(struct.unpack(f"<{n}{code}", raw)) if n else []
            return [bool(v) for v in vals] if etype == T_BOOL else vals
        if etype == T_STRING:
            c.need(n * 8, key)            # every element has at least its 8-byte length
            return [c.string(key) for _ in range(n)]
        if etype == T_ARRAY:
            raise GGUFError(f"{key}: nested arrays are not supported")
        raise GGUFError(f"{key}: invalid array element type {etype}")
    raise GGUFError(f"{key}: invalid GGUF value type {vtype}")


def parse_header(read: Callable[[int, int], bytes], *, max_bytes: int = 256 << 20) -> GGUFHeader:
    """Parse a GGUF header via read(offset, n) -> up to n bytes. Raises GGUFError on anything malformed."""
    c = _Cursor(read, max_bytes)
    magic = c.take(4, "magic")
    if magic != MAGIC:
        raise GGUFError(f"not a GGUF file (magic {magic!r})")
    version = c.unpack("I", 4, "version")
    if version not in SUPPORTED_VERSIONS:
        if version & 0xFFFF == 0:
            raise GGUFError(f"GGUF version {version:#x}: big-endian files are not supported")
        raise GGUFError(f"unsupported GGUF version {version} (supported: 2, 3)")
    n_tensors = c.unpack("Q", 8, "tensor count")
    n_kv = c.unpack("Q", 8, "kv count")
    if n_tensors > MAX_TENSORS:
        raise GGUFError(f"tensor count {n_tensors} exceeds the cap {MAX_TENSORS}")
    if n_kv > MAX_KV:
        raise GGUFError(f"kv count {n_kv} exceeds the cap {MAX_KV}")
    c.need(n_kv * 12 + n_tensors * 24, "kv/tensor records")   # minimum record sizes

    kv: dict[str, Any] = {}
    kv_types: dict[str, int] = {}
    for i in range(n_kv):
        key = c.string(f"key {i}", cap=MAX_KEY)
        if not key:
            raise GGUFError(f"key {i} is empty")
        if key in kv:
            raise GGUFError(f"duplicate key {key!r}")
        vtype = c.unpack("I", 4, key + " type")
        kv[key] = _read_value(c, vtype, key)
        kv_types[key] = vtype

    alignment = DEFAULT_ALIGNMENT
    if "general.alignment" in kv:
        if kv_types["general.alignment"] != T_UINT32:
            raise GGUFError("general.alignment must be a u32")
        alignment = kv["general.alignment"]
        if alignment == 0 or alignment & (alignment - 1):
            raise GGUFError(f"alignment {alignment} is not a power of 2")

    tensors: list[TensorInfo] = []
    seen: set[str] = set()
    expected = 0
    for i in range(n_tensors):
        name = c.string(f"tensor {i} name", cap=MAX_NAME * 4)
        if len(name.encode("utf-8")) >= MAX_NAME:
            raise GGUFError(f"tensor {i} name is too long ({len(name)} >= {MAX_NAME})")
        if name in seen:
            raise GGUFError(f"duplicate tensor name {name!r}")
        seen.add(name)
        n_dims = c.unpack("I", 4, name + " n_dims")
        if n_dims > MAX_DIMS:
            raise GGUFError(f"tensor {name!r} has {n_dims} dims (max {MAX_DIMS})")
        shape = tuple(c.unpack("q", 8, name + " dim") for _ in range(n_dims))
        if any(d < 0 for d in shape):
            raise GGUFError(f"tensor {name!r} has a negative dimension {shape}")
        n_el = 1
        for d in shape:
            n_el *= d
        if n_el >= 1 << 63:
            raise GGUFError(f"tensor {name!r} element count overflows: {shape}")
        ggml_type = c.unpack("i", 4, name + " type")
        nbytes = tensor_nbytes(shape, ggml_type)
        offset = c.unpack("Q", 8, name + " offset")
        if offset != expected:
            raise GGUFError(f"tensor {name!r} has offset {offset}, expected {expected}")
        expected += -(-nbytes // alignment) * alignment
        tensors.append(TensorInfo(name, shape, ggml_type, offset, nbytes))

    header_bytes = c.pos
    if n_tensors:
        header_bytes = -(-c.pos // alignment) * alignment
    return GGUFHeader(version, kv, tensors, header_bytes, alignment, kv_types)


def parse_file(path, **kw) -> GGUFHeader:
    with open(path, "rb") as f:
        def read(offset: int, n: int) -> bytes:
            f.seek(offset)
            return f.read(n)
        return parse_header(read, **kw)


def split_info(h: GGUFHeader) -> tuple[int, int] | None:
    """(index, count) from split.no (0-based, as llama.cpp's gguf-split writes it) and split.count; None if unsplit."""
    no, count = h.kv.get("split.no"), h.kv.get("split.count")
    if not isinstance(count, int) or isinstance(count, bool):
        return None
    if not isinstance(no, int) or isinstance(no, bool):
        no = 0
    return (no, count)


# --- writer for tests ------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Typed:
    """Force a GGUF value type in build_test_gguf: Typed("u64", 5), Typed("array", [1, 2], elem="i32")."""
    type: str
    value: Any
    elem: str | None = None


def _pack_str(s: str | bytes) -> bytes:
    b = s.encode("utf-8") if isinstance(s, str) else s
    return struct.pack("<Q", len(b)) + b


def _infer(v) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        if 0 <= v < 1 << 32:
            return "u32"
        if -(1 << 31) <= v < 0:
            return "i32"
        return "i64" if v < 0 or v < 1 << 63 else "u64"
    if isinstance(v, float):
        return "f32"
    if isinstance(v, (str, bytes)):
        return "str"
    if isinstance(v, (list, tuple)):
        return "array"
    raise TypeError(f"cannot infer a GGUF type for {type(v).__name__}")


def _pack_value(t: str, v, elem: str | None = None) -> bytes:
    tid = GGUF_TYPES[t]
    if t == "str":
        return _pack_str(v)
    if t == "array":
        vals = list(v)
        if elem is None:
            if vals and all(isinstance(x, int) and not isinstance(x, bool) for x in vals):
                elem = "i32" if min(vals) >= -(1 << 31) and max(vals) < 1 << 31 else "i64"
            else:
                elem = _infer(vals[0]) if vals else "i32"
        out = struct.pack("<IQ", GGUF_TYPES[elem], len(vals))
        if elem in ("str",):
            return out + b"".join(_pack_str(x) for x in vals)
        if elem == "array":
            raise TypeError("nested arrays are not supported")
        code, _size = _SCALAR[GGUF_TYPES[elem]]
        return out + struct.pack(f"<{len(vals)}{code}", *vals)
    code, _size = _SCALAR[tid]
    return struct.pack("<" + code, v)


def build_test_gguf(kv: dict, tensors: list[tuple], *, version: int = 3, with_data: bool = False) -> bytes:
    """Serialize a GGUF header (kv metadata + tensor infos) for tests.

    Values: bool -> bool, int -> u32 (i32/i64/u64 when out of range), float -> f32, str -> string,
    list -> array (ints -> i32, others by the first element); wrap in Typed(...) to force a type.
    tensors: [(name, shape, ggml_type)] with ggml_type an int or a name ("Q4_K"). The output ends at the aligned
    start of the data section (header_bytes) unless with_data=True, which appends zero-filled tensor data.
    """
    body = bytearray()
    for k, v in kv.items():
        if isinstance(v, Typed):
            t, val, elem = v.type, v.value, v.elem
        else:
            t, val, elem = _infer(v), v, None
        body += _pack_str(k) + struct.pack("<I", GGUF_TYPES[t]) + _pack_value(t, val, elem)
    align = kv.get("general.alignment", DEFAULT_ALIGNMENT)
    align = align.value if isinstance(align, Typed) else align
    offset = 0
    sizes = []
    for name, shape, gt in tensors:
        gt = GGML_TYPE_IDS[gt] if isinstance(gt, str) else gt
        nb = tensor_nbytes(tuple(shape), gt)
        body += _pack_str(name) + struct.pack("<I", len(shape)) + struct.pack(f"<{len(shape)}q", *shape)
        body += struct.pack("<iQ", gt, offset)
        sizes.append(nb)
        offset += -(-nb // align) * align
    out = bytearray(MAGIC + struct.pack("<IQQ", version, len(tensors), len(kv))) + body
    if tensors:
        out += b"\0" * (-len(out) % align)
    if with_data:
        out += b"\0" * offset
    return bytes(out)
