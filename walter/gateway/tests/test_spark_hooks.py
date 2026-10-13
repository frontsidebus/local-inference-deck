"""Unit tests for walter/gateway/hooks/spark_hooks.py (output cap + a normalization smoke test).

Run: python3 -m pytest walter/gateway/tests -q -p no:cacheprovider
LiteLLM is not needed: its CustomLogger base class is stubbed.
"""

import asyncio
import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
WALTER = HERE.parents[1]
HOOK = WALTER / "gateway" / "hooks" / "spark_hooks.py"


def _load_hook():
    if "litellm.integrations.custom_logger" not in sys.modules:
        litellm = types.ModuleType("litellm")
        integrations = types.ModuleType("litellm.integrations")
        custom_logger = types.ModuleType("litellm.integrations.custom_logger")
        custom_logger.CustomLogger = type("CustomLogger", (), {})
        sys.modules.update({
            "litellm": litellm,
            "litellm.integrations": integrations,
            "litellm.integrations.custom_logger": custom_logger,
        })
    spec = importlib.util.spec_from_file_location("spark_hooks_under_test", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


H = _load_hook()


def run_hook(data, call_type):
    return asyncio.run(H.proxy_handler_instance.async_pre_call_hook(None, None, data, call_type))


# --- defaults -------------------------------------------------------------------

@pytest.mark.parametrize("call_type", ["acompletion", "completion", "text_completion", "anthropic_messages"])
def test_missing_max_tokens_gets_default(call_type):
    d = run_hook({"model": "coder-fast", "messages": [{"role": "user", "content": "hi"}]}, call_type)
    assert d["max_tokens"] == 16384


@pytest.mark.parametrize("call_type", ["responses", "aresponses"])
def test_responses_missing_limit_gets_default(call_type):
    d = run_hook({"model": "coder", "input": "hi"}, call_type)
    assert d["max_output_tokens"] == 16384
    assert "max_tokens" not in d


def test_default_never_exceeds_model_max(monkeypatch):
    monkeypatch.setattr(H, "DEFAULT_MAX_OUTPUT", 50000)
    d = run_hook({"model": "hermes", "messages": []}, "acompletion")
    assert d["max_tokens"] == 16384
    d = run_hook({"model": "coder", "messages": []}, "acompletion")
    assert d["max_tokens"] == 32768


@pytest.mark.parametrize("bad", [None, -1, 0, "abc", True, 1.5, [], "-5"])
def test_invalid_limit_replaced_by_default(bad):
    d = run_hook({"model": "coder", "messages": [], "max_tokens": bad}, "acompletion")
    assert d["max_tokens"] == 16384


# --- small limits pass, large ones are clamped -----------------------------------

@pytest.mark.parametrize("call_type,field", [
    ("acompletion", "max_tokens"),
    ("acompletion", "max_completion_tokens"),
    ("text_completion", "max_tokens"),
    ("anthropic_messages", "max_tokens"),
    ("aresponses", "max_output_tokens"),
])
def test_small_limit_unchanged(call_type, field):
    d = run_hook({"model": "coder-fast", "messages": [], field: 50}, call_type)
    assert d[field] == 50


@pytest.mark.parametrize("call_type,field", [
    ("acompletion", "max_tokens"),
    ("acompletion", "max_completion_tokens"),
    ("anthropic_messages", "max_tokens"),
    ("aresponses", "max_output_tokens"),
])
def test_large_limit_clamped(call_type, field):
    d = run_hook({"model": "coder", "messages": [], field: 200000}, call_type)
    assert d[field] == 32768
    d = run_hook({"model": "vision", "messages": [], field: 32000}, call_type)
    assert d[field] == 16384


def test_claude_code_32000_on_coder_passes():
    d = run_hook({"model": "coder", "messages": [], "max_tokens": 32000}, "anthropic_messages")
    assert d["max_tokens"] == 32000


def test_numeric_string_normalized():
    d = run_hook({"model": "coder", "messages": [], "max_tokens": "100"}, "acompletion")
    assert d["max_tokens"] == 100


def test_max_completion_tokens_only_is_mirrored_into_max_tokens():
    d = run_hook({"model": "coder", "messages": [], "max_completion_tokens": 99999}, "acompletion")
    assert d["max_completion_tokens"] == 32768
    assert d["max_tokens"] == 32768


def test_both_chat_fields_each_clamped():
    d = run_hook({"model": "big", "messages": [], "max_tokens": 64, "max_completion_tokens": 90000}, "acompletion")
    assert d["max_tokens"] == 64
    assert d["max_completion_tokens"] == 32768


# --- model name resolution -----------------------------------------------------

@pytest.mark.parametrize("model,expected", [
    ("coder", 32768), ("coder-fast", 32768), ("big", 32768), ("vision", 16384), ("hermes", 16384), ("flash", 32768),
    ("claude-sonnet-4-5-20250929", 32768),          # safety-net route -> coder
    ("local/qwen3.8-27b", 32768), ("local/gemma-4-31b", 16384), ("local/hermes-4.3-36b", 16384),
    ("local/qwen3.8-flash-next", 32768),
    ("local/something-new", 16384), ("unknown", 16384), (None, 16384),
])
def test_model_max_output(model, expected):
    assert H.model_max_output(model) == expected


def test_unknown_call_type_untouched():
    data = {"model": "coder", "input": "x"}
    assert run_hook(dict(data), "aembedding") == data


def test_hook_never_raises_on_odd_data():
    assert run_hook({"model": "coder", "messages": "not a list", "max_tokens": {"x": 1}}, "acompletion")["max_tokens"] == 16384


# --- normalization still works alongside the cap ---------------------------------

def test_mid_conversation_system_still_rewritten():
    d = run_hook({"model": "coder", "messages": [
        {"role": "system", "content": "lead"},
        {"role": "user", "content": "q"},
        {"role": "system", "content": "late"},
    ]}, "acompletion")
    assert [m["role"] for m in d["messages"]] == ["system", "user"]
    assert "<system-reminder>" in d["messages"][1]["content"][-1]["text"]
    assert d["max_tokens"] == 16384


# --- the three places that hold the per-model maximum agree ---------------------

def test_limits_match_litellm_and_llama_swap():
    ll = yaml.safe_load((WALTER / "gateway" / "litellm.yaml.tmpl").read_text())
    info = {m["model_name"]: m.get("model_info", {}) for m in ll["model_list"]}
    for alias, mx in H.MODEL_MAX_OUTPUT.items():
        assert info[alias]["max_tokens"] == mx, alias
        assert info[alias]["max_output_tokens"] == mx, alias

    ls = yaml.safe_load((WALTER / "llama-swap" / "config.yaml.tmpl").read_text())
    seen = set()
    for model_id, m in ls["models"].items():
        found = re.findall(r"(?:^|\s)-n\s+(\d+)(?=\s)", m["cmd"])
        assert len(found) == 1, (model_id, "every model needs exactly one -n")
        alias = m["aliases"][0]
        assert H.LOCAL_ID_ALIAS[model_id] == alias
        assert int(found[0]) == H.MODEL_MAX_OUTPUT[alias], model_id
        seen.add(model_id)
    assert seen == set(H.LOCAL_ID_ALIAS)


# --- presence_penalty defaults (measured 2026-10-05, llama-swap/BENCHMARKS.md) -----

def test_presence_penalty_defaults():
    """coder-fast gets a server-side default of 1.5 (a request can still override it); coder and big
    stay at llama-server's 0. Changing this needs a re-measurement, see BENCHMARKS.md."""
    ls = yaml.safe_load((WALTER / "llama-swap" / "config.yaml.tmpl").read_text())
    want = {"qwen3.8-27b": None, "qwen3.6-35b-a3b": "1.5", "qwen3-coder-next": None}
    for model_id, value in want.items():
        found = re.findall(r"--presence-penalty\s+(\S+)", ls["models"][model_id]["cmd"])
        assert found == ([value] if value else []), model_id
        # the window is llama-server's default (64 tokens); the measurement did not change it
        assert "--repeat-last-n" not in ls["models"][model_id]["cmd"], model_id


# --- lab guard -----------------------------------------------------------------------

class Key:
    def __init__(self, metadata=None, key_alias="someone"):
        self.metadata = metadata
        self.key_alias = key_alias


LAB_NAMES = ["lab/uncensored-x", "local/lab-uncensored-x", "LAB/x", "local/LAB-x", " local/lab-x",
             "openai/lab-x", "local/local/lab-x", "local/openai/lab-x", "local/ lab-x", "lab-x"]


@pytest.mark.parametrize("model", LAB_NAMES)
@pytest.mark.parametrize("key", [None, Key(), Key({}), Key({"lab": False}), Key({"lab": "true"}),
                                 Key({"lab": 1}), Key({"kind": "user"})])
def test_lab_model_refused_without_lab_metadata(model, key):
    with pytest.raises(Exception) as e:
        asyncio.run(H.proxy_handler_instance.async_pre_call_hook(
            key, None, {"model": model, "messages": [{"role": "user", "content": "secret prompt"}]}, "acompletion"))
    assert getattr(e.value, "status_code", None) == 403


@pytest.mark.parametrize("model", LAB_NAMES)
def test_lab_model_allowed_with_lab_metadata(model):
    d = asyncio.run(H.proxy_handler_instance.async_pre_call_hook(
        Key({"lab": True, "kind": "user"}), None, {"model": model, "messages": []}, "anthropic_messages"))
    assert d["model"] == model


@pytest.mark.parametrize("model", ["coder", "coder-fast", "big", "flash", "vision", "hermes",
                                   "local/qwen3.8-flash-next", "claude-sonnet-4-5-20250929",
                                   "local/collab-model", "labrador", "local/my-lab-model", None])
def test_non_lab_models_pass_for_any_key(model):
    d = asyncio.run(H.proxy_handler_instance.async_pre_call_hook(None, None, {"model": model, "messages": []}, "acompletion"))
    assert d["model"] == model


@pytest.mark.parametrize("call_type", ["acompletion", "anthropic_messages", "aresponses", "aembedding", "atext_completion"])
def test_lab_guard_covers_every_call_type(call_type):
    with pytest.raises(Exception) as e:
        asyncio.run(H.proxy_handler_instance.async_pre_call_hook(Key(), None, {"model": "local/lab-x", "input": "x"}, call_type))
    assert e.value.status_code == 403


def test_lab_refusal_log_has_no_prompt(caplog):
    caplog.set_level("INFO", logger="LiteLLM Proxy")
    with pytest.raises(Exception):
        asyncio.run(H.proxy_handler_instance.async_pre_call_hook(Key({}, "alice"), None, {"model": "lab/x", "messages": [
            {"role": "user", "content": "PROMPT-MARKER-123"}]}, "acompletion"))
    text = caplog.text
    assert "refused lab model" in text and "'alice'" in text and "lab/x" in text
    assert "PROMPT-MARKER-123" not in text


def test_lab_routes_in_config_are_named_lab():
    """A static LiteLLM entry targets openai/lab-* iff it is named lab/*, and no lab wildcard exists,
    so the hook's name check is the whole alias resolution."""
    ll = yaml.safe_load((WALTER / "gateway" / "litellm.yaml.tmpl").read_text())
    for m in ll["model_list"]:
        name, target = m["model_name"], m["litellm_params"]["model"]
        assert H.is_lab_model(name) == H.is_lab_model(target), name
        if H.is_lab_model(name):
            assert "*" not in name and name.startswith("lab/"), name
    ls = yaml.safe_load((WALTER / "llama-swap" / "config.yaml.tmpl").read_text())
    for model_id, m in ls["models"].items():
        aliases = m.get("aliases") or []
        if model_id.startswith("lab-"):
            assert not aliases, (model_id, "lab entries must not have aliases (the hook sees only names)")
        assert not any(H.is_lab_model(a) for a in aliases), model_id
