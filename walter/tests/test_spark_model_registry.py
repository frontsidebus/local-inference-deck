"""walter/models/lib/registry.py: strict registry schema, atomic save, and the llama-swap config renderer.

The renderer's contract (walter/models/README.md): with no ENABLED lab model the output is the base config byte for
byte (the real template rendered with site.env.example by scripts/render.sh); with lab models the base text is kept
and only lab entries, matrix vars and matrix sets are inserted. Run:
    python3 -B -m pytest -q walter/tests/test_spark_model_registry.py
"""
import copy
import difflib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML (python3-yaml) is needed for the registry")

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "walter" / "models" / "lib"))
import registry as R  # noqa: E402

TEMPLATE = REPO / "walter" / "llama-swap" / "config.yaml.tmpl"
EXAMPLE_ENV = REPO / "site.env.example"
EXAMPLE_REG = REPO / "walter" / "models" / "registry.example.yaml"
SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    out = tmp_path_factory.mktemp("base") / "config.yaml"
    r = subprocess.run(["bash", str(REPO / "scripts/render.sh"), "-e", str(EXAMPLE_ENV), str(TEMPLATE), str(out)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return out.read_text()


def entry(eid="m1", placement="gpu1", **kw):
    e = {"id": eid, "repo": "someorg/Some-Model-GGUF", "revision": SHA,
         "files": [{"path": f"Q4/{eid}-Q4_K_M.gguf", "bytes": 1000, "sha256": "a" * 64, "role": "model"}],
         "placement": placement, "ctx": 32768, "class": "abliterated", "added": "2026-10-14T00:00:00Z"}
    e.update(kw)
    return e


def reg(*entries):
    return {"schema": R.SCHEMA, "models": list(entries)}


def inserted_only(base, out):
    """True when out is base with lines inserted (no base line changed, removed or moved)."""
    sm = difflib.SequenceMatcher(a=base.split("\n"), b=out.split("\n"), autojunk=False)
    return all(op in ("equal", "insert") for op, *_ in sm.get_opcodes())


# ---- byte-identical when nothing is enabled ------------------------------------------------------------------

def test_no_registry_and_disabled_models_render_the_base_byte_for_byte(base, tmp_path):
    assert R.render(base, R.load(tmp_path / "absent.yaml")) == base
    assert R.render(base, R.empty()) == base
    ex = R.load(EXAMPLE_REG)
    assert ex["models"] and not any(m["enabled"] for m in ex["models"])
    assert R.render(base, ex) == base
    many = reg(*(entry(f"m{i}", p) for i, p in enumerate(["gpu0", "gpu1", "both"] * 3)))
    assert R.render(base, many) == base


def test_example_registry_is_valid_and_defaults_fill():
    ex = R.load(EXAMPLE_REG)
    m = ex["models"][0]
    assert m["revision"] == SHA and m["enabled"] is False and m["added"] == "2026-10-14T00:00:00Z"
    e = R.validate(reg(entry()))["models"][0]
    assert e["n_max"] == 8192 and e["image"] == "image" and e["ttl"] == 900 and e["tools_allowed"] is False
    assert e["template"] == {"source": "embedded", "flags": [], "reviewed": False}
    assert R.validate(reg(entry(ctx=4096)))["models"][0]["n_max"] == 4096


# ---- validation: strict, every problem at once ----------------------------------------------------------------

@pytest.mark.parametrize("change,msg", [
    ({"id": "Bad_ID"}, "id 'Bad_ID' must match"),
    ({"id": "x"}, "must match"),
    ({"revision": "main"}, "40-hex"),
    ({"repo": "no-slash"}, "<org>/<name>"),
    ({"placement": "gpu2"}, "placement"),
    ({"ctx": 100}, "ctx must be"),
    ({"class": "evil"}, "class"),
    ({"tempp": 1}, "unknown key 'tempp'"),
    ({"image": "ghcr.io/x"}, "image must be one of"),
    ({"n_max": 99999}, "n_max must be"),
    ({"ttl": 5}, "ttl must be"),
    ({"enabled": "yes"}, "enabled must be true or false"),
    ({"sampling": {"temp": 9}}, "sampling.temp"),
    ({"sampling": {"mirostat": 1}}, "unknown key 'mirostat'"),
    ({"extra_args": ["--rpc", "1.2.3.4:5"]}, "not whitelisted"),
    ({"extra_args": ["--ctx-checkpoints", "8; rm -rf /"]}, "--ctx-checkpoints"),
    ({"extra_args": ["--ctx-checkpoints"]}, "needs a value"),
    ({"extra_args": ["--chat-template-file", "/etc/passwd"]}, "--chat-template-file"),
    ({"extra_args": ["--chat-template-file", "/models/gguf/lab/other/t.jinja"]}, "--chat-template-file"),
    ({"cpu_experts": "blk\\.0\\.ffn_.*_exps\\."}, "needs placement: both"),
    ({"placement": "both", "cpu_experts": 'x"; touch /tmp/p'}, "cpu_experts must be"),
    ({"files": []}, "non-empty list"),
    ({"files": [{"path": "../x.gguf", "bytes": 1, "sha256": "a" * 64, "role": "model"}]}, "repo-relative"),
    ({"files": [{"path": "x.safetensors", "bytes": 1, "sha256": "a" * 64, "role": "model"}]}, "*.gguf"),
    ({"files": [{"path": "x.gguf", "bytes": 1, "sha256": "A" * 64, "role": "model"}]}, "sha256 must be"),
    ({"files": [{"path": "x.gguf", "bytes": 0, "sha256": "a" * 64, "role": "model"}]}, "bytes must be"),
    ({"files": [{"path": "x.gguf", "bytes": 1, "sha256": "a" * 64, "role": "mmproj"}]}, "no role: model"),
    ({"files": [{"path": "a/x.gguf", "bytes": 1, "sha256": "a" * 64, "role": "model"},
                {"path": "b/x.gguf", "bytes": 1, "sha256": "b" * 64, "role": "model"}]}, "stored flat"),
    ({"license": {"id": "mit", "flag": "maybe"}}, "license.flag"),
    ({"template": {"flags": ["large"], "reviewed": False}, "enabled": True}, "not reviewed"),
    ({"fit": {"vram_gb": [1]}}, "fit.vram_gb"),
    ({"added": "yesterday"}, "added"),
])
def test_entry_validation(change, msg):
    e = entry()
    e.update(change)
    with pytest.raises(R.RegistryError) as x:
        R.validate(reg(e))
    assert msg in str(x.value), str(x.value)


def test_all_problems_reported_with_locations():
    bad = entry("ok-one")
    bad2 = entry("Bad!", placement="nowhere", ctx=-1)
    with pytest.raises(R.RegistryError) as x:
        R.validate({"schema": "v0", "models": [bad, bad2, entry("ok-one")], "extra": 1})
    p = x.value.problems
    assert len(p) >= 6, p
    joined = "\n".join(p)
    for s in ("schema must be", "unknown top-level key 'extra'", "models[1]: id 'Bad!'", "models[1]: placement",
              "duplicate id 'ok-one'"):
        assert s in joined, joined


def test_yaml_errors_and_top_level_shape():
    with pytest.raises(R.RegistryError, match="not valid YAML"):
        R.loads("models: [")
    with pytest.raises(R.RegistryError, match="top level must be a mapping"):
        R.loads("- 1\n")
    assert R.loads("") == R.empty()


# ---- save / load ---------------------------------------------------------------------------------------------

def test_save_is_atomic_and_round_trips(tmp_path):
    p = tmp_path / "registry.yaml"
    r = reg(entry("a1"), entry("b2", "gpu0", pinned=True))
    assert R.save(p, r) is True
    assert oct(p.stat().st_mode & 0o777) == "0o644"
    assert p.read_text().startswith("# spark-model lab registry")
    assert R.load(p) == R.validate(r)
    m = p.stat().st_mtime_ns
    assert R.save(p, R.load(p)) is False and p.stat().st_mtime_ns == m      # unchanged: not rewritten
    assert sorted(x.name for x in tmp_path.iterdir()) == ["registry.yaml"]  # no temp files left
    # digit-only hashes survive (YAML would read them as ints unquoted)
    e = entry("c3")
    e["files"][0]["sha256"] = "1" * 64
    R.save(p, reg(e))
    assert R.load(p)["models"][0]["files"][0]["sha256"] == "1" * 64


def test_save_refuses_invalid(tmp_path):
    with pytest.raises(R.RegistryError):
        R.save(tmp_path / "r.yaml", reg(entry("Bad")))
    assert not (tmp_path / "r.yaml").exists()


def test_atomic_write_keeps_prev(tmp_path):
    p = tmp_path / "config.yaml"
    assert R.atomic_write(p, "one\n", mode=0o640, keep_prev=True)
    assert not (tmp_path / "config.yaml.prev").exists() and oct(p.stat().st_mode & 0o777) == "0o640"
    assert R.atomic_write(p, "two\n", keep_prev=True)
    assert p.read_text() == "two\n" and (tmp_path / "config.yaml.prev").read_text() == "one\n"
    assert oct(p.stat().st_mode & 0o777) == "0o640"
    assert not R.atomic_write(p, "two\n", keep_prev=True)
    assert (tmp_path / "config.yaml.prev").read_text() == "one\n"


# ---- render with lab models -----------------------------------------------------------------------------------

def lab_reg():
    mm = {"path": "mmproj-F16.gguf", "bytes": 10, "sha256": "b" * 64, "role": "mmproj"}
    return reg(
        entry("one-gpu1", "gpu1", enabled=True, sampling={"temp": 0.7, "top_k": 20},
              extra_args=["--ctx-checkpoints", "8", "--swa-full"]),
        entry("two-gpu0", "gpu0", enabled=True, image="image-next", n_max=4096,
              files=[{"path": "x-00001-of-00002.gguf", "bytes": 5, "sha256": "c" * 64, "role": "model"},
                     {"path": "x-00002-of-00002.gguf", "bytes": 5, "sha256": "d" * 64, "role": "model"}, mm]),
        entry("off-model", "gpu0"),
        entry("big-both", "both", enabled=True, split="tensor", ttl=600, tools_allowed=True, **{"class": "specialized"}),
        entry("moe-both", "both", enabled=True, cpu_experts=r"blk\.([0-5]|2[4-9])\.ffn_.*_exps\.",
              files=[{"path": "moe.gguf", "bytes": 5, "sha256": "e" * 64, "role": "model"},
                     {"path": "mtp-moe-Q8_0.gguf", "bytes": 5, "sha256": "f" * 64, "role": "draft"}]),
    )


def test_render_inserts_only_and_keeps_the_base(base):
    out = R.render(base, lab_reg())
    assert out != base and inserted_only(base, out)
    assert R.render(base, lab_reg()) == out                       # deterministic
    assert R.check_config(out, base) == []
    cfg, b = yaml.safe_load(out), yaml.safe_load(base)
    core = {k: v for k, v in cfg["models"].items() if not k.startswith("lab-") or k in b["models"]}
    assert core == b["models"] and list(core) == list(b["models"])
    assert [k for k in cfg["models"] if k.startswith("lab-") and k not in b["models"]] == ["lab-one-gpu1", "lab-two-gpu0", "lab-big-both",
                                                                  "lab-moe-both"]   # disabled one left out
    for k in set(b) - {"models", "routing"}:
        assert cfg[k] == b[k]


def test_matrix_vars_and_sets(base):
    out = R.render(base, lab_reg())
    mx = yaml.safe_load(out)["routing"]["router"]["settings"]["matrix"]
    bmx = yaml.safe_load(base)["routing"]["router"]["settings"]["matrix"]
    assert list(mx["vars"])[:len(bmx["vars"])] == list(bmx["vars"])        # core vars first, same order
    lab_vars = {k: v for k, v in mx["vars"].items() if k not in bmx["vars"]}
    assert lab_vars == {"l1": "lab-one-gpu1", "l2": "lab-two-gpu0", "l3": "lab-big-both", "l4": "lab-moe-both"}
    assert mx["evict_costs"] == bmx["evict_costs"]
    assert list(mx["sets"])[:len(bmx["sets"])] == list(bmx["sets"])        # core sets first: coding wins ties
    assert {k: mx["sets"][k] for k in bmx["sets"]} == bmx["sets"]
    lab_sets = {k: v for k, v in mx["sets"].items() if k not in bmx["sets"]}
    assert lab_sets == {"lab-gpu-pairs": "(c | l2) & (f | l1)", "lab-big-both": "l3", "lab-moe-both": "l4"}
    for name, dsl in mx["sets"].items():
        ids, refs = R.dsl_names(dsl)
        assert all(i in mx["vars"] or i in yaml.safe_load(out)["models"] for i in ids) and not refs


def test_pairs_with_one_side_only(base):
    out = R.render(base, reg(entry("only-gpu1", "gpu1", enabled=True), entry("also-gpu1", "gpu1", enabled=True)))
    sets = yaml.safe_load(out)["routing"]["router"]["settings"]["matrix"]["sets"]
    assert sets["lab-gpu-pairs"] == "c & (f | l1 | l2)"
    out = R.render(base, reg(entry("only-both", "both", enabled=True)))
    sets = yaml.safe_load(out)["routing"]["router"]["settings"]["matrix"]["sets"]
    assert "lab-gpu-pairs" not in sets and sets["lab-only-both"] == "l1"


def test_lab_entry_commands(base):
    m = yaml.safe_load(R.render(base, lab_reg()))["models"]
    one, two, big, moe = (m[f"lab-{i}"] for i in ("one-gpu1", "two-gpu0", "big-both", "moe-both"))
    for e in (one, two, big, moe):
        assert e["cmd"].startswith("${run} ") and e["cmdStop"] == "${stop}" and e["proxy"] == "http://127.0.0.1:${PORT}"
        assert e["unlisted"] is True and e["metadata"]["lab"] is True
        assert e["cmd"].count("${mem}") + e["cmd"].count("${mem-moe}") == 1
        assert "${common} -c 32768 -n " in e["cmd"]
    assert '--gpus "\\"device=1\\"" ${image}' in one["cmd"] and "-sm none" in one["cmd"]
    assert "-m /models/gguf/lab/one-gpu1/one-gpu1-Q4_K_M.gguf" in one["cmd"]
    assert "--temp 0.7 --top-k 20" in one["cmd"] and "--ctx-checkpoints 8 --swa-full" in one["cmd"]
    assert one["ttl"] == 900 and one["metadata"] == {"lab": True, "class": "abliterated"}
    assert one["capabilities"] == {"in": ["text"], "out": ["text"], "tools": False, "context": 32768}
    assert '--gpus "\\"device=0\\"" ${image-next}' in two["cmd"] and "-n 4096 -sm none" in two["cmd"]
    assert "-m /models/gguf/lab/two-gpu0/x-00001-of-00002.gguf" in two["cmd"]
    assert "--mmproj /models/gguf/lab/two-gpu0/mmproj-F16.gguf" in two["cmd"]
    assert two["capabilities"]["in"] == ["text", "image"]
    assert big["cmd"].startswith("${run} ${mem} ${tp} --gpus \"\\\"device=0,1\\\"\"") and "-sm tensor" in big["cmd"]
    assert big["ttl"] == 600 and big["capabilities"]["tools"] is True and big["metadata"]["class"] == "specialized"
    assert moe["cmd"].startswith("${run} ${mem-moe} --gpus") and "-sm layer -ts 1,1" in moe["cmd"]
    assert '-ot "blk\\.([0-5]|2[4-9])\\.ffn_.*_exps\\.=CPU"' in moe["cmd"]
    assert "-md /models/gguf/lab/moe-both/mtp-moe-Q8_0.gguf" in moe["cmd"]


def test_lab_entries_pass_the_core_hardening_checks(base):
    """The lab entries go through the same macro expansion and hardening checks as the core models."""
    sys.path.insert(0, str(Path(__file__).parent))
    import test_llama_swap_config as T
    cfg = yaml.safe_load(R.render(base, lab_reg()))
    core = yaml.safe_load(base)["models"]   # (its own lab- entry, the Maya engine, has its own test)
    for mid in (k for k in cfg["models"] if k.startswith("lab-") and k not in core):
        args, server = T.docker_args(cfg, mid)
        assert T.values(args, "--cap-drop") == ["ALL"] and "--read-only" in args
        assert T.values(args, "-v") == [f"{T.MODELS_DIR}/gguf:/models/gguf:ro"]
        for p in T.values(server, "-m", "--mmproj", "-md"):
            assert p.startswith(f"/models/gguf/lab/{mid[4:]}/"), p
        left = set(__import__("re").findall(r"\$\{([\w.-]+)\}", T.expand(cfg, cfg["models"][mid]["cmd"])))
        assert left <= {"MODEL_ID", "PORT"}, (mid, left)


def llama_swap_bin():
    p = os.environ.get("LLAMA_SWAP_BIN") or shutil.which("llama-swap")
    return p if p and os.access(p, os.X_OK) else None


@pytest.mark.skipif(not llama_swap_bin(), reason="no llama-swap binary (set LLAMA_SWAP_BIN to the v260 binary)")
def test_real_llama_swap_validates_the_rendered_config(base, tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(R.render(base, lab_reg()))
    r = subprocess.run([llama_swap_bin(), "-validate", "-config", str(p)], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "LLAMA_SWAP_API_KEY": "x"}, timeout=60)
    assert r.returncode == 0 and "config is valid: 10 model(s)" in r.stdout, r.stdout + r.stderr


# ---- render refusals and the checker ---------------------------------------------------------------------------

def test_render_refuses_a_base_without_a_matrix(base):
    no_matrix = base[:base.index("routing:")]
    assert R.render(no_matrix, reg(entry())) == no_matrix                 # nothing enabled: still the base
    with pytest.raises(R.RegistryError, match="matrix"):
        R.render(no_matrix, reg(entry(enabled=True)))
    with pytest.raises(R.RegistryError, match="GPU0"):
        R.render(base.replace("c: qwen3.8-27b", "c: qwen3-coder-next"), reg(entry(enabled=True)))


def test_render_refuses_set_name_collisions(base):
    clash = base.replace('          flash: "x"', '          flash: "x"\n          lab-gpu-pairs: "b"')
    with pytest.raises(R.RegistryError, match="lab-gpu-pairs"):
        R.render(clash, reg(entry(enabled=True)))


def test_check_config_finds_problems(base):
    out = R.render(base, lab_reg())
    assert R.check_config(out.replace('"(c | l2) & (f | l1)"', '"(c | l2) & (f | l9)"'))[0] \
        == "matrix set 'lab-gpu-pairs': unknown var or model 'l9'"
    assert any("in no matrix set" in p for p in R.check_config(out.replace('lab-big-both: "l3"', 'lab-big-both: "b"')))
    i = out.index('"lab-one-gpu1"')
    assert any("unknown macro ${nope}" in p for p in R.check_config(out[:i] + out[i:].replace("${run}", "${nope}", 1)))
    assert any("parenthesis" in p for p in R.check_config(out.replace('"(c | l2) & (f | l1)"', '"(c | l2 & f"')))
    assert any("core models differ" in p for p in R.check_config(out.replace("-c 65536", "-c 1024"), base))
    assert any("alias 'coder'" in p for p in R.check_config(out.replace("    unlisted: true\n", "    aliases: [coder]\n    unlisted: true\n", 1)))


def test_dsl_grammar():
    assert R.dsl_names("c & f") == (["c", "f"], [])
    assert R.dsl_names("(c | l1) & (f | +other)") == (["c", "l1", "f"], ["other"])
    for bad in ("", "c &", "(c", "c ! f", "c f"):
        with pytest.raises(ValueError):
            R.dsl_names(bad)


def test_block_locator_handles_comments_and_nesting():
    text = ("a:\n  # c\n  b:\n    x: 1   # trailing\n    # inner\n  c: |\n    b:\n      fake: 1\n"
            "# top comment\nd:\n  e: 1\n")
    lines = text.split("\n")
    assert R._block(lines, ("a", "b")) == (2, 4, 4)
    assert R._block(lines, ("d",)) == (9, 10, 2)
    with pytest.raises(R.RegistryError):
        R._block(lines, ("a", "fake"))


def test_registry_is_never_read_through_a_symlink(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("root:not-a-registry\n")
    link = tmp_path / "registry.yaml"
    os.symlink(secret, link)
    with pytest.raises(R.RegistryError, match="symlink"):
        R.load(link)
    with pytest.raises(R.RegistryError):
        R.save(link, R.empty())
    assert secret.read_text() == "root:not-a-registry\n"
    (tmp_path / "dir.yaml").mkdir()
    with pytest.raises(R.RegistryError, match="not a regular file"):
        R.load(tmp_path / "dir.yaml")


def test_every_registry_image_is_a_base_macro(base):
    macros = yaml.safe_load(base)["macros"]
    for img in R.IMAGES:
        assert img in macros and "@sha256:" in macros[img] and macros[img].startswith("ghcr.io/ggml-org/llama.cpp:"), img
