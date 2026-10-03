"""
spark_hooks: LiteLLM pre-call hook for the Spark gateway.

Problem: clients (notably Claude Code >= 2.1 talking to an unrecognized model)
put system/developer-role messages in the middle of the conversation. The Qwen
chat templates served by llama-server either raise
"System message must be at the beginning." (Qwen3.8 -> HTTP 500) or silently
drop them (Qwen3.6). Only *leading* system messages are accepted.

Fix: every system/developer message that appears after the first non-system
message is rewritten as user-role text wrapped in <system-reminder> tags (the
same shape Claude Code itself uses when told the model can't take mid-conv
system messages) and merged into the adjacent user turn, so no two user turns
end up consecutive and the relative order of content is preserved:
  previous message is user  -> appended to it
  else (after skipping any tool-result messages/items that must stay
  attached to the preceding assistant tool call)
       next message is user -> prepended to it
  else                      -> standalone user message there
Leading system messages are left alone (the templates merge them).

Covers /v1/messages (anthropic_messages, incl. the native llama-server
passthrough), /v1/chat/completions and /v1/responses.

Second job: output cap. llama-server treats a request without max_tokens as
"generate until the context is full" (n_predict -1; one such request ran for
21 minutes to 131072 tokens). Every request therefore leaves the gateway with
an explicit output limit:
  no limit, or a non-positive / non-integer one -> DEFAULT_MAX_OUTPUT
  a limit above the model's maximum             -> clamped to that maximum
  a smaller limit                               -> passed through unchanged
Fields: max_tokens / max_completion_tokens (chat, text completion),
max_output_tokens (responses), max_tokens (messages). The per-model maxima
match model_info.max_tokens in litellm.yaml and the `-n` backstop in the
llama-swap config (llama-server's -n is only a default: a request that asks
for more gets more, so the clamp has to happen here).
"""

import logging
import os

from litellm.integrations.custom_logger import CustomLogger

log = logging.getLogger("LiteLLM Proxy")

SYSTEM_ROLES = ("system", "developer")


def _wrap(text):
    text = text.strip("\n")
    if text.startswith("<system-reminder>") and text.endswith("</system-reminder>"):
        return text
    return "<system-reminder>\n" + text + "\n</system-reminder>"


def _tool_name(tool):
    if not isinstance(tool, dict):
        return None
    if tool.get("type") == "tool_definition":
        return (tool.get("definition") or {}).get("name")
    return tool.get("name")


def _text_of(content, tool_defs=None):
    """Plain text of a system message's content (str or list of parts).

    Anthropic tool_addition / tool_removal blocks (Claude Code's
    "mid_conv_tool_change": late MCP tools announced in a system message) are
    turned into a short text note; inline tool definitions are collected into
    tool_defs so the caller can add them to the request's tools[].
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif not isinstance(p, dict):
                continue
            elif isinstance(p.get("text"), str):
                parts.append(p["text"])
            elif p.get("type") in ("tool_addition", "tool_removal"):
                tool = p.get("tool") or {}
                name = _tool_name(tool)
                if not name:
                    continue
                if p["type"] == "tool_addition":
                    parts.append("The tool `%s` is now available." % name)
                    if tool.get("type") == "tool_definition" and tool_defs is not None:
                        tool_defs.append(tool["definition"])
                else:
                    parts.append("The tool `%s` is no longer available; do not call it." % name)
    return "\n\n".join(x for x in parts if x)


def _as_parts(content, part_type):
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": part_type, "text": content}] if content else []
    return list(content)


def _merge(user_msg, texts, part_type, prepend):
    """Return a copy of user_msg with the wrapped texts merged in."""
    new_parts = [{"type": part_type, "text": t} for t in texts]
    parts = _as_parts(user_msg.get("content"), part_type)
    if prepend:
        # Anthropic: tool_result blocks must stay first in the user turn after a tool_use
        k = 0
        while k < len(parts) and isinstance(parts[k], dict) and parts[k].get("type") == "tool_result":
            k += 1
        parts[k:k] = new_parts
    else:
        parts += new_parts
    merged = dict(user_msg)
    merged["content"] = parts
    return merged


def _chat_is_tool_result(m):
    return isinstance(m, dict) and m.get("role") == "tool"


def _responses_is_tool_item(m):
    # function_call / function_call_output / custom_tool_call(_output) / reasoning ... items
    return isinstance(m, dict) and "role" not in m and m.get("type", "message") != "message"


def normalize(
    messages, part_type="text", is_user=None, is_tool_item=_chat_is_tool_result, new_msg_extra=None, tool_defs=None
):
    """Rewrite mid-conversation system/developer messages. Returns (new_list, n_rewritten)."""
    if not isinstance(messages, list):
        return messages, 0
    if is_user is None:
        def is_user(m):
            return isinstance(m, dict) and m.get("role") == "user"

    def is_sys(m):
        return isinstance(m, dict) and m.get("role") in SYSTEM_ROLES

    # leading system messages are fine
    lead = 0
    while lead < len(messages) and is_sys(messages[lead]):
        lead += 1
    if not any(is_sys(m) for m in messages[lead:]):
        return messages, 0

    out = list(messages[:lead])
    n = 0
    i = lead
    while i < len(messages):
        m = messages[i]
        if not is_sys(m):
            out.append(m)
            i += 1
            continue
        # collect a run of consecutive system messages
        texts = []
        while i < len(messages) and is_sys(messages[i]):
            t = _text_of(messages[i].get("content"), tool_defs)
            if t.strip():
                texts.append(_wrap(t))
            n += 1
            i += 1
        if not texts:
            continue
        if out and is_user(out[-1]):
            out[-1] = _merge(out[-1], texts, part_type, prepend=False)
            continue
        # don't split an assistant tool call from its results: move past them first
        while i < len(messages) and is_tool_item(messages[i]):
            out.append(messages[i])
            i += 1
        if i < len(messages) and is_user(messages[i]):
            out.append(_merge(messages[i], texts, part_type, prepend=True))
            i += 1
        else:
            msg = dict(new_msg_extra or {})
            msg.update(role="user", content=[{"type": part_type, "text": t} for t in texts])
            out.append(msg)
    return out, n


def _responses_is_user(m):
    return (
        isinstance(m, dict)
        and m.get("role") == "user"
        and m.get("type", "message") == "message"
    )


# --- output cap -------------------------------------------------------------
# Default output limit for a request that sets none.
DEFAULT_MAX_OUTPUT = int(os.environ.get("SPARK_DEFAULT_MAX_OUTPUT", "16384"))
# Per-alias maximum = litellm.yaml model_info.max_tokens = llama-swap `-n`.
MODEL_MAX_OUTPUT = {
    "coder": 32768,
    "coder-fast": 32768,
    "big": 32768,
    "vision": 16384,
    "hermes": 16384,
}
# local/<llama-swap model ID> -> the alias that ID serves
LOCAL_ID_ALIAS = {
    "qwen3.8-27b": "coder",
    "qwen3.6-35b-a3b": "coder-fast",
    "qwen3-coder-next": "big",
    "gemma-4-31b": "vision",
    "hermes-4.3-36b": "hermes",
}
# anything else (unknown local/* IDs): the smallest maximum
FALLBACK_MAX_OUTPUT = min(MODEL_MAX_OUTPUT.values())

# call_type -> (field to set when missing, all fields that carry a limit)
_CAP_FIELDS = {
    "completion": ("max_tokens", ("max_tokens", "max_completion_tokens")),
    "acompletion": ("max_tokens", ("max_tokens", "max_completion_tokens")),
    "text_completion": ("max_tokens", ("max_tokens",)),
    "atext_completion": ("max_tokens", ("max_tokens",)),
    "anthropic_messages": ("max_tokens", ("max_tokens",)),
    "responses": ("max_output_tokens", ("max_output_tokens",)),
    "aresponses": ("max_output_tokens", ("max_output_tokens",)),
}


def model_max_output(model):
    """Maximum output tokens for a requested model name (alias, claude-*, local/<id>)."""
    if not isinstance(model, str):
        return FALLBACK_MAX_OUTPUT
    if model in MODEL_MAX_OUTPUT:
        return MODEL_MAX_OUTPUT[model]
    if model.startswith("claude-"):  # safety-net route -> coder
        return MODEL_MAX_OUTPUT["coder"]
    if model.startswith("local/"):
        alias = LOCAL_ID_ALIAS.get(model[len("local/"):])
        if alias:
            return MODEL_MAX_OUTPUT[alias]
    return FALLBACK_MAX_OUTPUT


def _as_limit(v):
    """A usable positive int limit, or None (missing, -1, 0, junk)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v > 0 else None
    if isinstance(v, float) and v.is_integer():
        return int(v) if v > 0 else None
    if isinstance(v, str) and v.strip().isdigit():
        n = int(v.strip())
        return n if n > 0 else None
    return None


def cap_output(data, call_type):
    """Give the request an explicit, bounded output limit. Returns a list of changes (for the log)."""
    spec = _CAP_FIELDS.get(call_type)
    if spec is None or not isinstance(data, dict):
        return []
    primary, fields = spec
    limit = model_max_output(data.get("model"))
    default = min(DEFAULT_MAX_OUTPUT, limit)
    changes = []
    have = False
    for f in fields:
        if f not in data or data[f] is None:
            if f in data:
                del data[f]
            continue
        n = _as_limit(data[f])
        if n is None:
            changes.append("%s=%r dropped" % (f, data[f]))
            del data[f]
            continue
        have = True
        if n > limit:
            changes.append("%s %d->%d" % (f, n, limit))
            n = limit
        if data[f] != n:
            data[f] = n
    if not have:
        data[primary] = default
        changes.append("%s=%d (default)" % (primary, default))
    elif primary not in data:
        # only max_completion_tokens was given: mirror it into max_tokens, which llama-server reads
        data[primary] = min(_as_limit(data[f]) for f in fields if f in data)
    return changes


class SparkHooks(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        try:
            if call_type in ("anthropic_messages", "completion", "acompletion", "text_completion"):
                tool_defs = []
                new, n = normalize(data.get("messages"), "text", tool_defs=tool_defs)
                if n:
                    data["messages"] = new
                if tool_defs and call_type == "anthropic_messages":
                    tools = list(data.get("tools") or [])
                    have = {t.get("name") for t in tools if isinstance(t, dict)}
                    tools += [d for d in tool_defs if isinstance(d, dict) and d.get("name") not in have]
                    data["tools"] = tools
            elif call_type in ("responses", "aresponses"):
                n = 0
                inp = data.get("input")
                if isinstance(inp, list):
                    new, n = normalize(
                        inp, "input_text", _responses_is_user, _responses_is_tool_item, {"type": "message"}
                    )
                    if n:
                        data["input"] = new
            else:
                n = 0
            if n:
                log.info(
                    "spark_hooks: rewrote %d mid-conversation system message(s) as user <system-reminder> (%s, model=%s)",
                    n, call_type, data.get("model"),
                )
        except Exception as e:  # never break a request because of the hook
            log.warning("spark_hooks: normalization skipped: %r", e)
        try:
            changes = cap_output(data, call_type)
            if changes:
                log.info("spark_hooks: output cap %s (%s, model=%s)", ", ".join(changes), call_type, data.get("model"))
        except Exception as e:
            log.warning("spark_hooks: output cap skipped: %r", e)
        return data


proxy_handler_instance = SparkHooks()
