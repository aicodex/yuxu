from __future__ import annotations

import json

import httpx
import pytest

from yuxu.bundled.llm_service.handler import LLMService, LLMServiceError
from yuxu.bundled.rate_limit_service.handler import RateLimitService
from yuxu.core.bus import Bus
from yuxu.core.loader import Loader

pytestmark = pytest.mark.asyncio


def _mock_transport(route_fn):
    return httpx.MockTransport(route_fn)


def _make_rate_limiter(accounts):
    """Return (rate_limiter callable, RateLimitService) for a single 'minimax' pool."""
    svc = RateLimitService({
        "minimax": {"max_concurrent": 2, "accounts": accounts},
    })
    return svc.acquire, svc


# -- normalize ---------------------------------------------------


async def test_normalize_plain_text():
    rl, _ = _make_rate_limiter([{"id": "k", "api_key": "x", "base_url": "http://x/v1"}])
    svc = LLMService(rl)
    n = svc._normalize({
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10},
    })
    assert n["content"] == "hi"
    assert n["tool_calls"] == []
    assert n["stop_reason"] == "end_turn"
    assert n["usage"] == {"prompt_tokens": 10}


async def test_normalize_tool_call():
    rl, _ = _make_rate_limiter([{"id": "k", "api_key": "x", "base_url": "http://x/v1"}])
    svc = LLMService(rl)
    n = svc._normalize({
        "choices": [{
            "message": {
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "function": {"name": "get_price", "arguments": '{"sym":"MSFT"}'},
                }],
            },
            "finish_reason": "tool_calls",
        }],
    })
    assert n["stop_reason"] == "tool_use"
    assert n["tool_calls"] == [{"id": "call_1", "name": "get_price", "input": {"sym": "MSFT"}}]


async def test_normalize_empty_choices():
    rl, _ = _make_rate_limiter([{"id": "k", "api_key": "x", "base_url": "http://x/v1"}])
    svc = LLMService(rl)
    n = svc._normalize({"choices": []})
    assert n["content"] is None
    assert n["tool_calls"] == []


async def test_normalize_bad_tool_arguments_still_parses():
    rl, _ = _make_rate_limiter([{"id": "k", "api_key": "x", "base_url": "http://x/v1"}])
    svc = LLMService(rl)
    n = svc._normalize({
        "choices": [{
            "message": {"tool_calls": [{
                "id": "c", "function": {"name": "f", "arguments": "not json"},
            }]},
            "finish_reason": "tool_calls",
        }],
    })
    assert n["tool_calls"][0]["input"] == {}


# -- chat() HTTP round-trip --------------------------------------


async def test_chat_happy_path():
    captured = {}

    def route(req: httpx.Request):
        captured["url"] = str(req.url)
        captured["auth"] = req.headers.get("authorization")
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        })

    rl, _ = _make_rate_limiter([
        {"id": "k1", "api_key": "secretkey", "base_url": "https://api.example.com/v1"},
    ])
    svc = LLMService(rl, client=httpx.AsyncClient(transport=_mock_transport(route)))
    try:
        r = await svc.chat(
            pool="minimax", model="m1",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.3,
        )
        assert r["content"] == "hello"
        assert r["usage"]["prompt_tokens"] == 5
        assert captured["url"] == "https://api.example.com/v1/chat/completions"
        assert captured["auth"] == "Bearer secretkey"
        assert captured["body"]["temperature"] == 0.3
    finally:
        await svc.close()


async def test_chat_tools_and_json_mode_body_fields():
    captured = {}

    def route(req):
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}]})

    rl, _ = _make_rate_limiter([{"id": "k", "api_key": "x", "base_url": "http://a/v1"}])
    svc = LLMService(rl, client=httpx.AsyncClient(transport=_mock_transport(route)))
    try:
        await svc.chat(
            pool="minimax", model="m", messages=[],
            tools=[{"type": "function", "function": {"name": "f"}}],
            json_mode=True, extra_body={"custom": 1},
        )
    finally:
        await svc.close()
    body = captured["body"]
    assert body["tools"][0]["function"]["name"] == "f"
    assert body["response_format"] == {"type": "json_object"}
    assert body["custom"] == 1


async def test_chat_http_error_raises():
    def route(req):
        return httpx.Response(500, text="server exploded")
    rl, _ = _make_rate_limiter([{"id": "k", "api_key": "x", "base_url": "http://a/v1"}])
    svc = LLMService(rl, client=httpx.AsyncClient(transport=_mock_transport(route)))
    try:
        with pytest.raises(LLMServiceError, match="HTTP 500"):
            await svc.chat(pool="minimax", model="m", messages=[])
    finally:
        await svc.close()


async def test_chat_connect_error_raises():
    def route(req):
        raise httpx.ConnectError("boom")
    rl, _ = _make_rate_limiter([{"id": "k", "api_key": "x", "base_url": "http://a/v1"}])
    svc = LLMService(rl, client=httpx.AsyncClient(transport=_mock_transport(route)))
    try:
        with pytest.raises(LLMServiceError, match="request error"):
            await svc.chat(pool="minimax", model="m", messages=[])
    finally:
        await svc.close()


async def test_chat_missing_api_key_raises():
    rl, _ = _make_rate_limiter([{"id": "k", "base_url": "http://a/v1"}])
    svc = LLMService(rl, client=httpx.AsyncClient(transport=_mock_transport(lambda r: httpx.Response(200))))
    try:
        with pytest.raises(LLMServiceError, match="no api_key"):
            await svc.chat(pool="minimax", model="m", messages=[])
    finally:
        await svc.close()


async def test_chat_missing_base_url_raises():
    rl, _ = _make_rate_limiter([{"id": "k", "api_key": "x"}])
    svc = LLMService(rl, client=httpx.AsyncClient(transport=_mock_transport(lambda r: httpx.Response(200))))
    try:
        with pytest.raises(LLMServiceError, match="no base_url"):
            await svc.chat(pool="minimax", model="m", messages=[])
    finally:
        await svc.close()


# -- handle() dispatch -------------------------------------------


class _Msg:
    def __init__(self, payload):
        self.payload = payload


async def test_handle_ok_response():
    def route(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        })
    rl, _ = _make_rate_limiter([{"id": "k", "api_key": "x", "base_url": "http://a/v1"}])
    svc = LLMService(rl, client=httpx.AsyncClient(transport=_mock_transport(route)))
    try:
        r = await svc.handle(_Msg({"pool": "minimax", "model": "m", "messages": []}))
        assert r["ok"] is True
        assert r["content"] == "ok"
    finally:
        await svc.close()


async def test_handle_missing_fields():
    rl, _ = _make_rate_limiter([{"id": "k", "api_key": "x", "base_url": "http://a/v1"}])
    svc = LLMService(rl)
    r = await svc.handle(_Msg({"pool": "minimax"}))
    assert r["ok"] is False
    assert "missing fields" in r["error"]


async def test_handle_unknown_op():
    rl, _ = _make_rate_limiter([])
    svc = LLMService(rl)
    r = await svc.handle(_Msg({"op": "foo"}))
    assert r["ok"] is False


async def test_handle_unknown_pool():
    rl, _ = _make_rate_limiter([{"id": "k", "api_key": "x", "base_url": "http://a/v1"}])
    svc = LLMService(rl)
    r = await svc.handle(_Msg({"pool": "ghost", "model": "m", "messages": []}))
    assert r["ok"] is False
    assert "unknown pool" in r["error"]


# -- bus integration (loader + rate_limit_service + llm_service) --


async def test_full_stack_via_bus(tmp_path, monkeypatch, bundled_dir):
    cfg = tmp_path / "rate.yaml"
    cfg.write_text(
        "minimax:\n"
        "  max_concurrent: 2\n"
        "  accounts:\n"
        "    - id: key1\n"
        "      api_key: sk-abc\n"
        "      base_url: http://mock/v1\n"
    )
    monkeypatch.setenv("RATE_LIMITS_CONFIG", str(cfg))

    def route(req):
        assert req.headers["authorization"] == "Bearer sk-abc"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "full-stack"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3},
        })

    bus = Bus()
    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()
    await loader.ensure_running("llm_service")
    assert bus.query_status("rate_limit_service") == "ready"
    assert bus.query_status("llm_service") == "ready"

    llm_service = loader.get_handle("llm_service")
    llm_service._client = httpx.AsyncClient(transport=_mock_transport(route))
    llm_service._owned_client = True

    r = await bus.request(
        "llm_service",
        {"pool": "minimax", "model": "m", "messages": [{"role": "user", "content": "hi"}]},
        timeout=2.0,
    )
    assert r["ok"] is True
    assert r["content"] == "full-stack"
