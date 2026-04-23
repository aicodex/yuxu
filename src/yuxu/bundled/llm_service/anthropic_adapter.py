"""Anthropic Messages API adapter.

Translates between yuxu's internal OpenAI-shaped message history and the
Anthropic Messages API wire format. Ported from OpenClaw's MiniMax
integration (extensions/minimax/*), where MiniMax exposes an
Anthropic-compatible endpoint at api.minimax.io/anthropic (or the CN
counterpart api.minimaxi.com/anthropic).

Conversion summary:

OpenAI messages                          Anthropic messages
-------------------                      -------------------
[{role:system, content}]                 system: <str> (top-level field)
{role:user, content}                     {role:user, content: str}
{role:assistant, content:str}            {role:assistant, content: str}
{role:assistant, tool_calls:[...]}       {role:assistant, content: [
                                           {type:text, text},
                                           {type:tool_use, id, name, input},
                                         ]}
{role:tool, tool_call_id, content}       {role:user, content: [
                                           {type:tool_result, tool_use_id, content},
                                         ]}

Response (content[] array) unpacks into:
  text blocks     -> concatenated as `content: str`
  thinking blocks -> concatenated as `reasoning: str`
  tool_use blocks -> tool_calls: [{id, name, input}]
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

# Thinking level presets. OpenClaw has 7 levels; yuxu starts with 5 matching
# the most useful span. Callers can also pass a raw dict through `thinking=`
# to use custom budget_tokens values.
#
# Note: MiniMax's Anthropic endpoint LEAKS thinking blocks as visible content
# in streaming mode (per OpenClaw's minimax-stream-wrappers.ts:45-51). Default
# is "off" (thinking: {type: disabled}). Only enable when you really need
# reasoning — it costs tokens and (on MiniMax non-streaming) works cleanly.
THINKING_PRESETS: dict[str, dict] = {
    "off": {"type": "disabled"},
    "low": {"type": "enabled", "budget_tokens": 1024},
    "medium": {"type": "enabled", "budget_tokens": 4096},
    "high": {"type": "enabled", "budget_tokens": 16384},
    "xhigh": {"type": "enabled", "budget_tokens": 32768},
}


def resolve_thinking(thinking: Any) -> Optional[dict]:
    """Normalize a `thinking` kwarg into the Anthropic wire shape.

    Accepts:
      - None -> None (caller doesn't want to set the field)
      - str in THINKING_PRESETS -> preset dict
      - dict -> returned as-is (passthrough for custom budget_tokens)
    """
    if thinking is None:
        return None
    if isinstance(thinking, str):
        preset = THINKING_PRESETS.get(thinking)
        if preset is None:
            raise ValueError(
                f"unknown thinking preset {thinking!r}; "
                f"valid: {sorted(THINKING_PRESETS)}"
            )
        return dict(preset)
    if isinstance(thinking, dict):
        return dict(thinking)
    raise TypeError(f"thinking must be str|dict|None, got {type(thinking).__name__}")


def convert_tools_openai_to_anthropic(tools: Optional[list[dict]]) -> list[dict]:
    """OpenAI tool schema -> Anthropic tool schema.

    OpenAI: {type: function, function: {name, description, parameters}}
       or raw {name, description, parameters}
    Anthropic: {name, description, input_schema}
    """
    if not tools:
        return []
    out: list[dict] = []
    for t in tools:
        if "function" in t and isinstance(t["function"], dict):
            t = t["function"]
        name = t.get("name")
        if not name:
            continue
        entry: dict = {"name": name}
        if "description" in t:
            entry["description"] = t["description"]
        # Anthropic requires input_schema (JSONSchema). Fall back to empty
        # object schema if parameters absent.
        entry["input_schema"] = t.get("parameters") or {
            "type": "object", "properties": {},
        }
        out.append(entry)
    return out


def convert_messages_openai_to_anthropic(
    messages: list[dict],
) -> tuple[Optional[str], list[dict]]:
    """Return (system_prompt, anthropic_messages).

    Accepts yuxu/OpenAI-shaped messages (optional leading role=system) and
    returns a separate system string + the rest converted to Anthropic shape.

    Consecutive role=tool messages become a single user message with multiple
    tool_result blocks — matches Anthropic's expectation after a tool-use turn.
    """
    system: Optional[str] = None
    out: list[dict] = []
    i = 0
    n = len(messages)

    # Pop leading system (yuxu usually prepends one in run_turn).
    while i < n and messages[i].get("role") == "system":
        # If multiple system messages (rare), concatenate.
        content = messages[i].get("content") or ""
        if isinstance(content, list):
            content = "".join(
                b.get("text", "") for b in content if isinstance(b, dict)
            )
        system = (system + "\n\n" + content) if system else content
        i += 1

    while i < n:
        m = messages[i]
        role = m.get("role")
        if role == "user":
            content = m.get("content")
            if isinstance(content, str):
                out.append({"role": "user", "content": content})
            elif isinstance(content, list):
                # Already Anthropic-style blocks — passthrough.
                out.append({"role": "user", "content": content})
            else:
                out.append({"role": "user", "content": ""})
            i += 1
        elif role == "assistant":
            blocks: list[dict] = []
            content = m.get("content")
            if isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                blocks.extend(content)
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (TypeError, json.JSONDecodeError):
                    args = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id"),
                    "name": fn.get("name"),
                    "input": args,
                })
            if not blocks:
                # Empty assistant turn. Anthropic requires non-empty content;
                # skip (follows Claude Code behavior).
                i += 1
                continue
            # Anthropic accepts str shorthand when single text block.
            if len(blocks) == 1 and blocks[0].get("type") == "text":
                out.append({"role": "assistant", "content": blocks[0]["text"]})
            else:
                out.append({"role": "assistant", "content": blocks})
            i += 1
        elif role == "tool":
            # Coalesce consecutive role=tool into one user message.
            tool_blocks: list[dict] = []
            while i < n and messages[i].get("role") == "tool":
                tm = messages[i]
                tool_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tm.get("tool_call_id") or tm.get("id"),
                    "content": tm.get("content") or "",
                })
                i += 1
            out.append({"role": "user", "content": tool_blocks})
        else:
            # Unknown role — skip with warning.
            log.warning("anthropic_adapter: skipping unknown role=%r", role)
            i += 1
    return system, out


def build_anthropic_request(
    *, model: str, messages: list[dict], max_tokens: int,
    tools: Optional[list[dict]] = None,
    temperature: Optional[float] = None,
    thinking: Any = None,
    extra_body: Optional[dict] = None,
) -> dict:
    """Assemble the request body for POST /v1/messages."""
    system, anthropic_messages = convert_messages_openai_to_anthropic(messages)
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": int(max_tokens),
        "messages": anthropic_messages,
    }
    if system:
        body["system"] = system
    if tools:
        body["tools"] = convert_tools_openai_to_anthropic(tools)
    if temperature is not None:
        body["temperature"] = float(temperature)
    resolved_thinking = resolve_thinking(thinking)
    if resolved_thinking is not None:
        body["thinking"] = resolved_thinking
    if extra_body:
        body.update(extra_body)
    return body


def parse_anthropic_response(api_resp: dict) -> dict:
    """Normalize Anthropic response into yuxu's shape.

    Returns {content, tool_calls, reasoning, stop_reason, usage}.
    reasoning is None if the response contained no thinking blocks.
    """
    content_blocks = api_resp.get("content") or []
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[dict] = []
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            t = block.get("text")
            if t:
                text_parts.append(str(t))
        elif btype == "thinking":
            t = block.get("thinking")
            if t:
                thinking_parts.append(str(t))
        elif btype == "tool_use":
            tool_calls.append({
                "id": block.get("id"),
                "name": block.get("name"),
                "input": block.get("input") or {},
            })
        # Other types (redacted_thinking, server_tool_use, ...) are ignored in v0.

    content = "".join(text_parts) if text_parts else None
    reasoning = "".join(thinking_parts) if thinking_parts else None

    stop = api_resp.get("stop_reason") or "end_turn"
    # yuxu uses "tool_use" / "end_turn" — Anthropic's enum matches exactly
    # for these. Other values (max_tokens, stop_sequence) flow through as-is.
    if tool_calls and stop == "end_turn":
        stop = "tool_use"

    # Usage: Anthropic returns input_tokens / output_tokens — map to yuxu's
    # prompt_tokens / completion_tokens / total_tokens.
    anth_usage = api_resp.get("usage") or {}
    pt = int(anth_usage.get("input_tokens") or 0)
    ct = int(anth_usage.get("output_tokens") or 0)
    usage = {
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "total_tokens": pt + ct,
    }
    # Preserve any cache-related counters the caller (e.g. minimax_budget) may
    # want later. Same keys Anthropic uses.
    for k in ("cache_creation_input_tokens", "cache_read_input_tokens"):
        if k in anth_usage:
            usage[k] = anth_usage[k]

    return {
        "content": content,
        "tool_calls": tool_calls,
        "reasoning": reasoning,
        "stop_reason": stop,
        "usage": usage,
    }
