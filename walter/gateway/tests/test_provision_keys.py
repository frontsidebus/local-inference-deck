"""Unit tests for walter/gateway/provision-keys.py.tmpl: allow-list classes, the /key/update
migration and its dry-run diff. The template is rendered with scripts/render.sh from
site.env.example; LiteLLM is replaced by a fake `api`, so nothing leaves the process.

Run: python3 -m pytest walter/gateway/tests -q -p no:cacheprovider
"""

import hashlib
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
