"""Unit tests for walter/gateway/provision-keys.py.tmpl: allow-list classes, the /key/update
migration and its dry-run diff. The template is rendered with scripts/render.sh from
site.env.example; LiteLLM is replaced by a fake `api`, so nothing leaves the process.

Run: python3 -m pytest walter/gateway/tests -q -p no:cacheprovider
"""

import hashlib
import json
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
WALTER = HERE.parents[1]
REPO = WALTER.parent
TMPL = WALTER / "gateway" / "provision-keys.py.tmpl"
KEY = "sk-" + "x" * 30          # fake key values: must never appear in the output
KEY2 = "sk-" + "y" * 30


def render(tmp_path, **overrides):
    text = (REPO / "site.env.example").read_text()
    for k, v in overrides.items():
        text, n = re.subn(rf"^{k}=.*$", f'{k}="{v}"', text, flags=re.M)
        if not n:
            text += f'\n{k}="{v}"\n'
    env = tmp_path / "site.env"
    env.write_text(text)
    out = tmp_path / "provision-keys.py"
    r = subprocess.run(["bash", str(REPO / "scripts" / "render.sh"), "-e", str(env), str(TMPL), str(out)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return out


def load(path, tmp_path, dry=False):
    argv, sys.argv = sys.argv, ["provision-keys.py"] + (["--dry-run"] if dry else []) + ["--root", str(tmp_path)]
    try:
        spec = importlib.util.spec_from_file_location("provision_keys_under_test_%d" % id(path), path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.argv = argv
    return mod


@pytest.fixture
def pk(tmp_path):
    return load(render(tmp_path), tmp_path)


def fake_litellm(mod, keys, calls):
    """keys: list of LiteLLM key objects; calls collects (method, path, body)."""
    def api(method, path, master, body=None):
        calls.append((method, path, body))
        if path.startswith("/key/list"):
            return {"keys": keys, "total_pages": 1}
        if path == "/key/update":
            return {}
        raise AssertionError(path)
    mod.api = api


def key_obj(alias, models, key=KEY, metadata=None):
    return {"token": hashlib.sha256(key.encode()).hexdigest(), "key_alias": alias,
            "models": models, "metadata": metadata if metadata is not None else {"kind": "user"}}


# --- classes ---------------------------------------------------------------------------

def test_served_matches_litellm_static_models(pk):
    ll = yaml.safe_load((WALTER / "gateway" / "litellm.yaml.tmpl").read_text())
    static = [m["model_name"] for m in ll["model_list"] if "*" not in m["model_name"]]
    wild = {m["model_name"] for m in ll["model_list"] if "*" in m["model_name"]}
    assert sorted(pk.SERVED) == sorted(static)
    for cls, models in pk.KEY_CLASSES.items():
        assert models, cls
        if cls == "open-webui-lab":   # lab models only, through the local/* route
            assert all(pk_lab(m) for m in models) and "local/*" in wild
            continue
        for m in models:
            assert m in static or m in wild, (cls, m)
        assert not any(pk_lab(m) for m in models), cls


def pk_lab(m):
    return m == "lab" or m.startswith("lab/") or m.startswith("local/lab")


def test_classes(pk):
    full = pk.SERVED + ["local/*", "claude-*"]
    for h in ("claude-code", "codex", "opencode", "hermes", "cline"):
        assert pk.desired(h, "harness") == (full, False)
    assert pk.desired("open-webui", "harness") == (pk.SERVED + ["local/*"], False)
    assert pk.desired("digest", "harness") == (pk.SERVED, False)
    assert pk.desired("alice", "user") == (full, False)


def test_lab_aliases_get_lab_group_and_flag(tmp_path):
    mod = load(render(tmp_path, LAB_KEY_ALIASES="alice"), tmp_path)
    models, lab = mod.desired("alice", "user")
    assert lab is True and models[-1] == "lab"
    assert mod.desired("bob", "user")[1] is False
    mod.check_lab_aliases()


@pytest.mark.parametrize("bad", ["hermes", "carol", "alice open-webui"])
def test_lab_aliases_only_users(tmp_path, bad):
    mod = load(render(tmp_path, LAB_KEY_ALIASES=bad), tmp_path)
    with pytest.raises(SystemExit):
        mod.check_lab_aliases()


def test_example_renders_without_lab(pk):
    assert pk.LAB_KEY_ALIASES == []


# --- migration -------------------------------------------------------------------------

def test_plan_update(pk):
    full = pk.desired("alice", "user")[0]
    assert pk.plan_update("alice", "user", key_obj("alice", [])) == {"models": full}
    assert pk.plan_update("alice", "user", key_obj("alice", list(reversed(full)))) is None
    body = pk.plan_update("alice", "user", key_obj("alice", full, metadata={"kind": "user", "lab": True}))
    assert body == {"metadata": {"kind": "user", "lab": False}}


def test_dry_run_prints_diff_without_writing(tmp_path, capsys):
    mod = load(render(tmp_path, LAB_KEY_ALIASES="alice"), tmp_path, dry=True)
    calls = []
    fake_litellm(mod, [key_obj("alice", []), key_obj("codex", ["coder"], KEY2),
                       key_obj("bob", mod.desired("bob", "user")[0]), key_obj("manual", [])], calls)
    mod.sync_models([("codex", "harness"), ("alice", "user"), ("bob", "user"), ("carol", "user")], "master")
    err = capsys.readouterr().err
    assert all(c[1].startswith("/key/list") for c in calls)
    assert "would update alice: [] (ALL models) -> [coder," in err
    assert "; lab false -> true" in err
    assert "would update codex: [coder] -> [coder, coder-fast" in err
    assert "ok      bob:" in err
    assert "missing carol" in err
    assert "unmanaged manual: models [] (ALL models)" in err
    assert "2 key(s) would change" in err
    assert KEY not in err and KEY2 not in err and hashlib.sha256(KEY.encode()).hexdigest() not in err


def test_update_is_idempotent_and_never_logs_keys(tmp_path, capsys):
    mod = load(render(tmp_path), tmp_path)
    kd = tmp_path / "srv" / "gateway" / "keys"
    kd.mkdir(parents=True)
    (kd / "alice.key").write_text(KEY + "\n")
    keys = [key_obj("alice", []), key_obj("bob", [], KEY2)]   # bob has no key file
    calls = []
    fake_litellm(mod, keys, calls)
    names = [("alice", "user"), ("bob", "user")]
    mod.sync_models(names, "master")
    updates = [c[2] for c in calls if c[1] == "/key/update"]
    assert [u["key"] for u in updates] == [KEY, keys[1]["token"]]   # key file when it matches, else token hash
    assert all(u["models"] == mod.desired("alice", "user")[0] for u in updates)
    # apply, re-run: nothing to do
    for k in keys:
        k["models"] = mod.desired(k["key_alias"], "user")[0]
    calls.clear()
    mod.sync_models(names, "master")
    assert not [c for c in calls if c[1] == "/key/update"]
    err = capsys.readouterr().err
    assert KEY not in err and KEY2 not in err


def test_stale_key_file_uses_token(tmp_path):
    mod = load(render(tmp_path), tmp_path)
    kd = tmp_path / "srv" / "gateway" / "keys"
    kd.mkdir(parents=True)
    (kd / "alice.key").write_text(KEY2 + "\n")       # file holds another key than LiteLLM's alice
    cur = key_obj("alice", [])
    assert mod.key_ref("alice", cur) == cur["token"]


def test_dry_run_offline_prints_targets(pk, capsys):
    pk.sync_models([("digest", "harness")], None)
    err = capsys.readouterr().err
    assert "would set digest (digest): models [coder, coder-fast, big, flash, vision, hermes] (live keys not read)" in err


def test_failed_update_exits_nonzero(pk):
    def api(method, path, master, body=None):
        if path.startswith("/key/list"):
            return {"keys": [key_obj("alice", [])], "total_pages": 1}
        raise pk.urllib.error.HTTPError(path, 500, "boom", {}, None)
    pk.api = api
    with pytest.raises(SystemExit):
        pk.sync_models([("alice", "user")], "master")


def litellm_key_allows(model, key_models):
    """Mirror of LiteLLM v1.103.1 key model check for our case (no access groups, no team):
    exact name, or a wildcard entry matched as ^pattern-with-*-as-.*$ (auth_checks.py:6127)."""
    return model in key_models or any(
        "*" in p and re.match("^" + p.replace("*", ".*") + "$", model) for p in key_models)


@pytest.mark.parametrize("model", ["coder", "flash", "local/qwen3.8-flash-next", "local/qwen3.8-27b",
                                   "claude-sonnet-4-5-20250929", "claude-haiku-4-5"])
def test_harness_models_still_reachable(pk, model):
    for name in ("claude-code", "hermes", "codex", "opencode"):
        assert litellm_key_allows(model, pk.desired(name, "harness")[0]), (name, model)
    assert litellm_key_allows(model, pk.desired("alice", "user")[0])
    assert not litellm_key_allows("lab/x", pk.desired("alice", "user")[0])


# --- Open WebUI lab connection (LAB_WEBUI) ---------------------------------------------

LAB_KEY = "sk-" + "z" * 30
OWUI_KEY = "sk-" + "o" * 30
HERMES_KEY = "hk-" + "h" * 30
URLS = "http://10.0.0.1:4000/v1;http://10.0.0.9:8642/v1"


def test_webui_lab_key_class(tmp_path, pk):
    on = load(render(tmp_path, LAB_WEBUI="1"), tmp_path)
    assert on.desired("open-webui-lab", "harness") == (["local/lab-*", "lab"], True)
    assert pk.desired("open-webui-lab", "harness") == (["local/lab-*", "lab"], False)
    assert pk.LAB_WEBUI is False
    # the shared Open WebUI key never gets lab access, with or without LAB_WEBUI
    assert on.desired("open-webui", "harness") == (on.SERVED + ["local/*"], False)
    # always synced (so turning it off clears the flag), listed once even when in HARNESS_KEYS
    assert [n for n, _ in on.managed_names()].count("open-webui-lab") == 1
    assert ("open-webui-lab", "harness") in pk.managed_names()


def test_webui_lab_key_cannot_be_a_lab_alias(tmp_path):
    mod = load(render(tmp_path, LAB_KEY_ALIASES="open-webui-lab", LAB_WEBUI="1"), tmp_path)
    with pytest.raises(SystemExit):
        mod.check_lab_aliases()


def test_lab_model_ids_from_llama_swap_config(pk, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text('models:\n  "qwen3.8-27b":\n    cmd: x\n  lab-glm-5.3-flash:\n    cmd: |\n      lab-not-a-key:\n'
                   '  "lab-nemotron-bolt-abl":\n    name: y\n  "lab-qwen38-flash-abl":   # generated\n'
                   'routing:\n  lab-gpu-pairs: "c"\n  "lab-nemotron-bolt-abl":\n')
    assert pk.lab_model_ids(str(cfg)) == ["local/lab-glm-5.3-flash", "local/lab-nemotron-bolt-abl",
                                          "local/lab-qwen38-flash-abl"]
    assert pk.lab_model_ids(str(tmp_path / "missing.yaml")) == []


def test_lab_model_ids_from_the_repo_template(pk):
    ids = pk.lab_model_ids(str(WALTER / "llama-swap" / "config.yaml.tmpl"))
    ls = yaml.safe_load((WALTER / "llama-swap" / "config.yaml.tmpl").read_text())
    assert ids == ["local/" + m for m in ls["models"] if m.startswith("lab-")] and ids


def _cur(keys, urls=URLS, configs=None):
    return {"OPENAI_API_KEYS": keys, "OPENAI_API_BASE_URLS": urls, "OPENAI_API_CONFIGS": configs}


def test_webui_target_without_lab(pk):
    t = pk.webui_target(_cur("CHANGEME"), OWUI_KEY, "", "", [])
    assert t == {"OPENAI_API_KEYS": OWUI_KEY + ";PENDING", "OPENAI_API_BASE_URLS": URLS, "OPENAI_API_CONFIGS": None}
    assert pk.webui_target(_cur("CHANGEME"), "", "", "", []) is None
    t = pk.webui_target(_cur(OWUI_KEY + ";PENDING"), OWUI_KEY, HERMES_KEY, "", [])
    assert t["OPENAI_API_KEYS"] == OWUI_KEY + ";" + HERMES_KEY
    # a lab key file alone does nothing while LAB_WEBUI is off
    t = pk.webui_target(_cur(OWUI_KEY + ";PENDING"), OWUI_KEY, "", LAB_KEY, ["local/lab-x"])
    assert t["OPENAI_API_KEYS"] == OWUI_KEY + ";PENDING" and t["OPENAI_API_CONFIGS"] is None


def test_webui_target_with_lab_and_removal(tmp_path):
    on = load(render(tmp_path, LAB_WEBUI="1"), tmp_path)
    ids = ["local/lab-a", "local/lab-b"]
    t = on.webui_target(_cur(OWUI_KEY + ";PENDING"), OWUI_KEY, "", LAB_KEY, ids)
    assert t["OPENAI_API_KEYS"] == OWUI_KEY + ";PENDING;" + LAB_KEY
    assert t["OPENAI_API_BASE_URLS"] == URLS + ";" + on.GATEWAY_URL
    assert json.loads(t["OPENAI_API_CONFIGS"]) == {"2": {"enable": True, "model_ids": ids, "tags": [{"name": "lab"}]}}
    # Hermes filled later: the lab slot stays third
    t2 = on.webui_target(_cur(t["OPENAI_API_KEYS"], t["OPENAI_API_BASE_URLS"], t["OPENAI_API_CONFIGS"]),
                         OWUI_KEY, HERMES_KEY, LAB_KEY, ids)
    assert t2["OPENAI_API_KEYS"] == OWUI_KEY + ";" + HERMES_KEY + ";" + LAB_KEY
    # no lab key yet (first run, creation failed): no lab connection
    assert on.webui_target(_cur(OWUI_KEY + ";PENDING"), OWUI_KEY, "", "", ids)["OPENAI_API_CONFIGS"] is None
    # LAB_WEBUI turned off: back to two connections
    off = load(render(tmp_path), tmp_path)
    t3 = off.webui_target(_cur(t2["OPENAI_API_KEYS"], t2["OPENAI_API_BASE_URLS"], t2["OPENAI_API_CONFIGS"]),
                          OWUI_KEY, HERMES_KEY, LAB_KEY, ids)
    assert t3 == {"OPENAI_API_KEYS": OWUI_KEY + ";" + HERMES_KEY, "OPENAI_API_BASE_URLS": URLS,
                  "OPENAI_API_CONFIGS": None}


def _webui_root(tmp_path, keys="CHANGEME"):
    (tmp_path / "srv" / "webui").mkdir(parents=True)
    (tmp_path / "srv" / "gateway" / "keys").mkdir(parents=True)
    (tmp_path / "etc" / "llama-swap").mkdir(parents=True)
    env = tmp_path / "srv" / "webui" / ".env"
    env.write_text("WEBUI_URL=https://chat.example\nOPENAI_API_BASE_URLS=%s\nOPENAI_API_KEYS=%s\n"
                   "AIOHTTP_CLIENT_TIMEOUT=900\n" % (URLS, keys))
    env.chmod(0o600)
    (tmp_path / "srv" / "gateway" / "keys" / "open-webui.key").write_text(OWUI_KEY + "\n")
    (tmp_path / "srv" / "gateway" / "keys" / "open-webui-lab.key").write_text(LAB_KEY + "\n")
    (tmp_path / "etc" / "llama-swap" / "config.yaml").write_text('models:\n  "coder":\n    x: 1\n  "lab-a":\n    x: 1\n')
    return env


def test_webui_env_writes_lab_connection_idempotently_without_logging_keys(tmp_path, capsys):
    env = _webui_root(tmp_path)
    on = load(render(tmp_path, LAB_WEBUI="1"), tmp_path)
    on.webui_env()
    text = env.read_text()
    assert "OPENAI_API_KEYS=%s;PENDING;%s\n" % (OWUI_KEY, LAB_KEY) in text
    assert 'OPENAI_API_CONFIGS={"2":{"enable":true,"model_ids":["local/lab-a"],"tags":[{"name":"lab"}]}}' in text
    assert text.startswith("WEBUI_URL=") and "AIOHTTP_CLIENT_TIMEOUT=900\n" in text and text.endswith("\n")
    assert oct(env.stat().st_mode & 0o777) == "0o600"
    before = env.read_text()
    on.webui_env()
    assert env.read_text() == before
    err = capsys.readouterr().err
    assert "lab connection: 1 model(s) local/lab-a" in err and "keep    Open WebUI connections" in err
    for k in (OWUI_KEY, LAB_KEY):
        assert k not in err
    off = load(render(tmp_path), tmp_path)
    off.webui_env()
    text = env.read_text()
    assert "OPENAI_API_CONFIGS" not in text and "OPENAI_API_KEYS=%s;PENDING\n" % OWUI_KEY in text
    assert "OPENAI_API_BASE_URLS=%s\n" % URLS in text and LAB_KEY not in text


def test_webui_env_dry_run_writes_nothing(tmp_path, capsys):
    env = _webui_root(tmp_path, keys=OWUI_KEY + ";PENDING")
    before = env.read_text()
    on = load(render(tmp_path, LAB_WEBUI="1"), tmp_path, dry=True)
    on.webui_env()
    assert env.read_text() == before
    err = capsys.readouterr().err
    line = next(l for l in err.splitlines() if "would set" in l)
    for v in ("OPENAI_API_KEYS", "OPENAI_API_BASE_URLS", "OPENAI_API_CONFIGS", "local/lab-a"):
        assert v in line
    assert LAB_KEY not in err and OWUI_KEY not in err


def test_existing_webui_env_is_left_alone_without_lab(tmp_path):
    env = _webui_root(tmp_path, keys=OWUI_KEY + ";" + HERMES_KEY)
    before = env.read_text()
    load(render(tmp_path), tmp_path).webui_env()
    assert env.read_text() == before
