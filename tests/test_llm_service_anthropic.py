"""Anthropic Messages API path for llm_service.

Covers:
- anthropic_adapter pure conversion functions (messages, tools, response)
- end-to-end chat() via mock httpx with api=anthropic-messages
- thinking preset + raw dict passthrough + default-off semantics
"""
from __future__ import annotations

import json

import httpx
import pytest

from yuxu.bundled.llm_service.anthropic_adapter import (
    THINKING_PRESETS,
    build_anthropic_request,
    convert_messages_openai_to_anthropic,
    convert_tools_openai_to_anthropic,
    parse_anthropic_response,
    resolve_thinking,
)
from yuxu.bundled.llm_service.handler import LLMService
from yuxu.bundled.rate_limit_service.handler import RateLimitService

pytestmark = pytest.mark.asyncio


# -- adapter: resolve_thinking ----------------------------------


def test_resolve_thinking_none():
    assert resolve_thinking(None) is None


def test_resolve_thinking_preset_off():
    assert resolve_thinking("off") == {"type": "disabled"}


def test_resolve_thinking_preset_medium():
    r = resolve_thinking("medium")
    assert r == {"type": "enabled", "budget_tokens": 4096}


def test_resolve_thinking_raw_dict_passthrough():
    r = resolve_thinking({"type": "enabled", "budget_tokens": 9000})
    assert r == {"type": "enabled", "budget_tokens": 9000}


def test_resolve_thinking_unknown_preset_raises():
    with pytest.raises(ValueError, match="unknown thinking preset"):
        resolve_thinking("ultra")


def test_resolve_thinking_wrong_type_raises():
    with pytest.raises(TypeError):
        resolve_thinking(42)


# -- adapter: tool schema --------------------------------------


def test_convert_tools_openai_wrapped():
    tools = [{"type": "function", "function": {
        "name": "get_weather",
        "description": "look up weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    }}]
    out = convert_tools_openai_to_anthropic(tools)
    assert out == [{
        "name": "get_weather",
        "description": "look up weather",
        "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
    }]


def test_convert_tools_openai_raw():
    tools = [{"name": "f", "description": "d", "parameters": {"type": "object"}}]
    out = convert_tools_openai_to_anthropic(tools)
    assert out[0]["name"] == "f"
    assert out[0]["input_schema"] == {"type": "object"}


def test_convert_tools_missing_parameters_defaults_empty_schema():
    out = convert_tools_openai_to_anthropic([{"name": "f"}])
    assert out[0]["input_schema"] == {"type": "object", "properties": {}}


def test_convert_tools_empty_or_none():
    assert convert_tools_openai_to_anthropic(None) == []
    assert convert_tools_openai_to_anthropic([]) == []


# -- adapter: message conversion -------------------------------


def test_convert_messages_plain_user():
    system, out = convert_messages_openai_to_anthropic([
        {"role": "user", "content": "hi"},
    ])
    assert system is None
    assert out == [{"role": "user", "content": "hi"}]


def test_convert_messages_extracts_leading_system():
    system, out = convert_messages_openai_to_anthropic([
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "hi"},
    ])
    assert system == "you are helpful"
    assert out == [{"role": "user", "content": "hi"}]


def test_convert_messages_tool_call_roundtrip():
    # OpenAI shape that llm_driver appends after a tool_use turn:
    msgs = [
        {"role": "user", "content": "get price"},
        {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "get_price", "arguments": '{"sym":"X"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "c1", "content": '{"price": 42}'},
        {"role": "assistant", "content": "price is 42"},
    ]
    system, out = convert_messages_openai_to_anthropic(msgs)
    assert system is None
    assert out[0] == {"role": "user", "content": "get price"}
    # assistant with tool_use becomes blocks list
    assert out[1]["role"] == "assistant"
    assert isinstance(out[1]["content"], list)
    assert out[1]["content"][0]["type"] == "tool_use"
    assert out[1]["content"][0]["id"] == "c1"
    assert out[1]["content"][0]["name"] == "get_price"
    assert out[1]["content"][0]["input"] == {"sym": "X"}
    # tool result becomes user message with tool_result blocks
    assert out[2]["role"] == "user"
    assert out[2]["content"][0]["type"] == "tool_result"
    assert out[2]["content"][0]["tool_use_id"] == "c1"
    # final assistant
    assert out[3] == {"role": "assistant", "content": "price is 42"}


def test_convert_messages_coalesces_multiple_tool_results():
    msgs = [
        {"role": "tool", "tool_call_id": "c1", "content": "a"},
        {"role": "tool", "tool_call_id": "c2", "content": "b"},
        {"role": "assistant", "content": "done"},
    ]
    _, out = convert_messages_openai_to_anthropic(msgs)
    # Two tool results should coalesce into ONE user message with 2 blocks.
    assert out[0]["role"] == "user"
    assert len(out[0]["content"]) == 2
    assert out[0]["content"][0]["tool_use_id"] == "c1"
    assert out[0]["content"][1]["tool_use_id"] == "c2"


def test_convert_messages_drops_empty_assistant():
    msgs = [
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "hi"},
    ]
    _, out = convert_messages_openai_to_anthropic(msgs)
    assert out == [{"role": "user", "content": "hi"}]


# -- adapter: response parsing ----------------------------------


def test_parse_response_text_only():
    resp = {
        "content": [{"type": "text", "text": "hello world"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 3},
    }
    n = parse_anthropic_response(resp)
    assert n["content"] == "hello world"
    assert n["reasoning"] is None
    assert n["tool_calls"] == []
    assert n["stop_reason"] == "end_turn"
    assert n["usage"]["prompt_tokens"] == 10
    assert n["usage"]["completion_tokens"] == 3
    assert n["usage"]["total_tokens"] == 13


def test_parse_response_thinking_and_text():
    resp = {
        "content": [
            {"type": "thinking", "thinking": "hmm let me think"},
            {"type": "text", "text": "the answer is 42"},
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    n = parse_anthropic_response(resp)
    assert n["content"] == "the answer is 42"
    assert n["reasoning"] == "hmm let me think"


def test_parse_response_tool_use_flips_stop_reason():
    resp = {
        "content": [
            {"type": "text", "text": "calling tool"},
            {"type": "tool_use", "id": "t1", "name": "ping", "input": {"x": 1}},
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    n = parse_anthropic_response(resp)
    assert n["stop_reason"] == "tool_use"
    assert n["tool_calls"] == [{"id": "t1", "name": "ping", "input": {"x": 1}}]


def test_parse_response_preserves_cache_usage():
    resp = {
        "content": [{"type": "text", "text": "x"}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 5, "output_tokens": 1,
            "cache_creation_input_tokens": 7,
            "cache_read_input_tokens": 11,
        },
    }
    n = parse_anthropic_response(resp)
    assert n["usage"]["cache_creation_input_tokens"] == 7
    assert n["usage"]["cache_read_input_tokens"] == 11


# -- adapter: request builder -----------------------------------


def test_build_request_defaults_thinking_off():
    body = build_anthropic_request(
        model="m", messages=[{"role": "user", "content": "hi"}],
        max_tokens=1000, thinking="off",
    )
    assert body["thinking"] == {"type": "disabled"}
    assert body["max_tokens"] == 1000
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert "tools" not in body


def test_build_request_system_extracted():
    body = build_anthropic_request(
        model="m",
        messages=[
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi"},
        ],
        max_tokens=500, thinking="off",
    )
    assert body["system"] == "be terse"
    assert len(body["messages"]) == 1


def test_build_request_tools_converted():
    body = build_anthropic_request(
        model="m", messages=[{"role": "user", "content": "x"}],
        max_tokens=500, thinking="off",
        tools=[{"type": "function", "function": {"name": "f", "description": "d"}}],
    )
    assert body["tools"][0]["name"] == "f"


# -- integration: chat via mock transport -----------------------


def _mock_transport(route_fn):
    return httpx.MockTransport(route_fn)


def _anthropic_rate_limiter():
    svc = RateLimitService({
        "minimax_anthropic": {
            "max_concurrent": 2,
            "accounts": [{
                "id": "mm_global",
                "api_key": "testkey",
                "base_url": "https://api.minimax.io/anthropic",
                "api": "anthropic-messages",
            }],
        },
    })
    return svc.acquire, svc


async def test_chat_anthropic_routes_to_v1_messages():
    captured = {}

    def route(req: httpx.Request):
        captured["url"] = str(req.url)
        captured["auth"] = req.headers.get("authorization")
        captured["anthropic_version"] = req.headers.get("anthropic-version")
        captured["mm_source"] = req.headers.get("mm-api-source")
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "hi back"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 2},
        })

    rl, _ = _anthropic_rate_limiter()
    svc = LLMService(rl, client=httpx.AsyncClient(transport=_mock_transport(route)))
    try:
        r = await svc.chat(
            pool="minimax_anthropic", model="MiniMax-M2.7",
            messages=[
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hi"},
            ],
            max_tokens=500,
        )
    finally:
        await svc.close()
    assert r["content"] == "hi back"
    assert r["reasoning"] is None
    assert r["usage"]["prompt_tokens"] == 5
    assert r["usage"]["completion_tokens"] == 2
    assert captured["url"] == "https://api.minimax.io/anthropic/v1/messages"
    assert captured["auth"] == "Bearer testkey"
    assert captured["anthropic_version"] == "2023-06-01"
    assert captured["mm_source"] == "yuxu"
    # System extracted to top-level; thinking defaulted to disabled
    assert captured["body"]["system"] == "sys"
    assert captured["body"]["thinking"] == {"type": "disabled"}


async def test_chat_anthropic_thinking_preset_enabled():
    captured = {}

    def route(req):
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={
            "content": [
                {"type": "thinking", "thinking": "step 1 ... step 2"},
                {"type": "text", "text": "final"},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    rl, _ = _anthropic_rate_limiter()
    svc = LLMService(rl, client=httpx.AsyncClient(transport=_mock_transport(route)))
    try:
        r = await svc.chat(
            pool="minimax_anthropic", model="MiniMax-M2.7",
            messages=[{"role": "user", "content": "complex"}],
            thinking="medium", max_tokens=8000,
        )
    finally:
        await svc.close()
    assert captured["body"]["thinking"] == {
        "type": "enabled", "budget_tokens": 4096,
    }
    assert r["reasoning"] == "step 1 ... step 2"
    assert r["content"] == "final"


async def test_chat_anthropic_unknown_api_raises():
    svc = RateLimitService({
        "bad": {
            "accounts": [{
                "id": "x", "api_key": "k", "base_url": "http://x",
                "api": "some-alien-protocol",
            }],
        },
    })
    lsvc = LLMService(svc.acquire)
    try:
        with pytest.raises(Exception, match="unknown api"):
            await lsvc.chat(
                pool="bad", model="m",
                messages=[{"role": "user", "content": "x"}],
            )
    finally:
        await lsvc.close()


async def test_chat_anthropic_default_max_tokens():
    captured = {}

    def route(req):
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    rl, _ = _anthropic_rate_limiter()
    svc = LLMService(rl, client=httpx.AsyncClient(transport=_mock_transport(route)))
    try:
        await svc.chat(
            pool="minimax_anthropic", model="MiniMax-M2.7",
            messages=[{"role": "user", "content": "hi"}],
        )
    finally:
        await svc.close()
    # DEFAULT_ANTHROPIC_MAX_TOKENS from handler module
    from yuxu.bundled.llm_service.handler import DEFAULT_ANTHROPIC_MAX_TOKENS
    assert captured["body"]["max_tokens"] == DEFAULT_ANTHROPIC_MAX_TOKENS


async def test_chat_anthropic_tool_call_roundtrip():
    def route(req):
        return httpx.Response(200, json={
            "content": [
                {"type": "text", "text": "calling"},
                {"type": "tool_use", "id": "t1", "name": "ping", "input": {}},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    rl, _ = _anthropic_rate_limiter()
    svc = LLMService(rl, client=httpx.AsyncClient(transport=_mock_transport(route)))
    try:
        r = await svc.chat(
            pool="minimax_anthropic", model="MiniMax-M2.7",
            messages=[{"role": "user", "content": "use tool"}],
            tools=[{"name": "ping", "description": "", "parameters": {"type": "object"}}],
            max_tokens=1000,
        )
    finally:
        await svc.close()
    assert r["stop_reason"] == "tool_use"
    assert r["tool_calls"] == [{"id": "t1", "name": "ping", "input": {}}]


def test_thinking_presets_all_have_right_shape():
    # off must be disabled; enabled presets must have budget_tokens
    assert THINKING_PRESETS["off"] == {"type": "disabled"}
    for key in ("low", "medium", "high", "xhigh"):
        preset = THINKING_PRESETS[key]
        assert preset["type"] == "enabled"
        assert preset["budget_tokens"] > 0
