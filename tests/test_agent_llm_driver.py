from __future__ import annotations

import asyncio
import json

import pytest

from yuxu.bundled.llm_driver.handler import LlmDriver, _cap, _assistant_message
from yuxu.core.bus import Bus

pytestmark = pytest.mark.asyncio


# -- helpers ---------------------------------------------------


def _register_llm(bus, responses):
    """Register a fake llm_service that returns `responses` in order."""
    idx = [0]

    async def handler(msg):
        i = idx[0]
        idx[0] += 1
        if i >= len(responses):
            return {"ok": True, "content": "done", "tool_calls": [], "usage": {}}
        r = responses[i]
        return r if isinstance(r, dict) else r

    bus.register("llm_service", handler)
    return idx


def _register_llm_capture(bus, response):
    """Fake llm_service that records each request payload."""
    seen: list[dict] = []

    async def handler(msg):
        seen.append(dict(msg.payload) if isinstance(msg.payload, dict) else {})
        return response

    bus.register("llm_service", handler)
    return seen


# -- unit tests ------------------------------------------------


async def test_single_turn_no_tools():
    bus = Bus()
    _register_llm(bus, [
        {"ok": True, "content": "hi there", "tool_calls": [],
         "stop_reason": "end_turn", "usage": {"prompt_tokens": 3, "completion_tokens": 2}}
    ])
    driver = LlmDriver(bus)
    msgs = [{"role": "user", "content": "hi"}]
    r = await driver.run_turn(
        system_prompt="sys", messages=msgs, pool="p", model="m",
    )
    assert r["ok"] is True
    assert r["content"] == "hi there"
    assert r["stop_reason"] == "complete"
    assert r["iterations"] == 1
    assert r["usage"]["prompt_tokens"] == 3
    # assistant message appended
    assert msgs[-1]["role"] == "assistant"
    assert msgs[-1]["content"] == "hi there"


async def test_tool_call_then_final():
    bus = Bus()
    _register_llm(bus, [
        {
            "ok": True, "content": None,
            "tool_calls": [{"id": "c1", "name": "get_price", "input": {"sym": "X"}}],
            "stop_reason": "tool_use", "usage": {},
        },
        {"ok": True, "content": "price is 42", "tool_calls": [],
         "stop_reason": "end_turn", "usage": {}},
    ])

    async def price_tool(msg):
        assert msg.payload["input"] == {"sym": "X"}
        return {"output": {"price": 42}}

    bus.register("get_price", price_tool)
    driver = LlmDriver(bus)
    msgs = [{"role": "user", "content": "what price"}]
    r = await driver.run_turn(
        system_prompt="sys", messages=msgs, pool="p", model="m",
        tools=[{"name": "get_price", "description": "...", "parameters": {}}],
    )
    assert r["ok"] is True
    assert r["content"] == "price is 42"
    assert r["iterations"] == 2
    # messages: user, assistant (tool_use), tool result, assistant (final)
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant", "tool", "assistant"]
    tool_msg = msgs[2]
    assert tool_msg["tool_call_id"] == "c1"
    assert '"price"' in tool_msg["content"]


async def test_tool_dispatch_mapping():
    bus = Bus()
    _register_llm(bus, [
        {"ok": True, "content": None,
         "tool_calls": [{"id": "c1", "name": "price", "input": {}}],
         "stop_reason": "tool_use", "usage": {}},
        {"ok": True, "content": "ok", "tool_calls": [], "stop_reason": "end_turn", "usage": {}},
    ])
    called = []

    async def actual_agent(msg):
        called.append(msg.to)
        return {"output": 1}

    bus.register("price_agent_v2", actual_agent)
    driver = LlmDriver(bus)
    await driver.run_turn(
        system_prompt="s", messages=[{"role": "user", "content": "x"}],
        pool="p", model="m",
        tools=[{"name": "price", "parameters": {}}],
        tool_dispatch={"price": "price_agent_v2"},
    )
    assert called == ["price_agent_v2"]


async def test_tool_error_does_not_break_loop():
    bus = Bus()
    _register_llm(bus, [
        {"ok": True, "content": None,
         "tool_calls": [{"id": "c1", "name": "flaky", "input": {}}],
         "stop_reason": "tool_use", "usage": {}},
        {"ok": True, "content": "recovered", "tool_calls": [], "stop_reason": "end_turn", "usage": {}},
    ])

    async def bad_tool(msg):
        raise RuntimeError("boom")

    bus.register("flaky", bad_tool)
    driver = LlmDriver(bus)
    msgs = [{"role": "user", "content": "x"}]
    r = await driver.run_turn(
        system_prompt="s", messages=msgs, pool="p", model="m",
        tools=[{"name": "flaky", "parameters": {}}],
    )
    assert r["ok"] is True
    assert r["stop_reason"] == "complete"
    # tool message carries the error payload
    tool_msg = next(m for m in msgs if m["role"] == "tool")
    assert "boom" in tool_msg["content"]


async def test_tool_timeout_reported_as_error_message():
    bus = Bus()
    _register_llm(bus, [
        {"ok": True, "content": None,
         "tool_calls": [{"id": "c1", "name": "slow", "input": {}}],
         "stop_reason": "tool_use", "usage": {}},
        {"ok": True, "content": "done", "tool_calls": [], "stop_reason": "end_turn", "usage": {}},
    ])

    async def slow(msg):
        await asyncio.sleep(1.0)
        return {"output": "late"}

    bus.register("slow", slow)
    driver = LlmDriver(bus)
    msgs = [{"role": "user", "content": "x"}]
    r = await driver.run_turn(
        system_prompt="s", messages=msgs, pool="p", model="m",
        tools=[{"name": "slow", "parameters": {}}],
        tool_timeout=0.05,
    )
    assert r["ok"] is True
    tool_msg = next(m for m in msgs if m["role"] == "tool")
    assert "timed out" in tool_msg["content"]


async def test_max_iterations():
    bus = Bus()
    # Every turn returns a tool_call → never completes
    forever = [
        {"ok": True, "content": None,
         "tool_calls": [{"id": f"c{i}", "name": "noop", "input": {}}],
         "stop_reason": "tool_use", "usage": {}}
        for i in range(10)
    ]
    _register_llm(bus, forever)

    async def noop(msg):
        return {"output": "ok"}

    bus.register("noop", noop)
    driver = LlmDriver(bus)
    r = await driver.run_turn(
        system_prompt="s", messages=[{"role": "user", "content": "x"}],
        pool="p", model="m",
        tools=[{"name": "noop", "parameters": {}}],
        max_iterations=3,
    )
    assert r["ok"] is False
    assert r["stop_reason"] == "max_iter"
    assert r["iterations"] == 3


async def test_llm_service_error_terminates_with_error():
    bus = Bus()

    async def bad(msg):
        return {"ok": False, "error": "auth failed"}

    bus.register("llm_service", bad)
    driver = LlmDriver(bus)
    r = await driver.run_turn(
        system_prompt="s", messages=[{"role": "user", "content": "x"}],
        pool="p", model="m",
    )
    assert r["ok"] is False
    assert r["stop_reason"] == "error"
    assert "auth failed" in r["error"]


async def test_llm_service_missing_treated_as_error():
    bus = Bus()
    # no llm_service registered
    driver = LlmDriver(bus)
    r = await driver.run_turn(
        system_prompt="s", messages=[{"role": "user", "content": "x"}],
        pool="p", model="m", llm_timeout=0.2,
    )
    assert r["ok"] is False
    assert r["stop_reason"] == "error"


async def test_output_cap():
    big = "a" * 200_000
    assert len(_cap(big, 100).encode()) < 500  # truncated + marker


async def test_assistant_message_shape_without_tools():
    msg = _assistant_message({"content": "hi", "tool_calls": []})
    assert msg == {"role": "assistant", "content": "hi"}


async def test_assistant_message_shape_with_tools():
    msg = _assistant_message({
        "content": None,
        "tool_calls": [{"id": "c1", "name": "f", "input": {"a": 1}}],
    })
    assert msg["role"] == "assistant"
    assert msg["tool_calls"][0]["function"]["name"] == "f"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"a": 1}


async def test_tools_pre_wrapped_passthrough():
    """If user already wraps tool in {type, function}, don't double-wrap."""
    bus = Bus()
    captured = []

    async def llm(msg):
        captured.append(msg.payload)
        return {"ok": True, "content": "done", "tool_calls": [], "usage": {}}

    bus.register("llm_service", llm)
    driver = LlmDriver(bus)
    await driver.run_turn(
        system_prompt="s", messages=[{"role": "user", "content": "x"}],
        pool="p", model="m",
        tools=[{"type": "function", "function": {"name": "raw", "parameters": {}}}],
    )
    assert captured[0]["tools"] == [
        {"type": "function", "function": {"name": "raw", "parameters": {}}}
    ]


async def test_handle_via_bus():
    bus = Bus()
    _register_llm(bus, [
        {"ok": True, "content": "ok", "tool_calls": [], "stop_reason": "end_turn", "usage": {}},
    ])
    driver = LlmDriver(bus)
    bus.register("llm_driver", driver.handle)
    r = await bus.request("llm_driver", {
        "system_prompt": "s",
        "messages": [{"role": "user", "content": "hi"}],
        "pool": "p",
        "model": "m",
    }, timeout=2.0)
    assert r["ok"] is True
    assert r["content"] == "ok"
    # returned messages reflect the turn
    assert any(m["role"] == "assistant" for m in r["messages"])


async def test_handle_missing_fields():
    bus = Bus()
    driver = LlmDriver(bus)
    r = await driver.handle(type("M", (), {"payload": {"op": "run_turn"}})())
    assert r["ok"] is False
    assert "missing fields" in r["error"]


async def test_handle_unknown_op():
    bus = Bus()
    driver = LlmDriver(bus)
    r = await driver.handle(type("M", (), {"payload": {"op": "weird"}})())
    assert r["ok"] is False
    assert "unknown op" in r["error"]


async def test_strip_thinking_blocks_passes_through_to_llm_service():
    bus = Bus()
    seen = _register_llm_capture(bus, {
        "ok": True, "content": "clean", "tool_calls": [],
        "stop_reason": "end_turn", "usage": {},
    })
    driver = LlmDriver(bus)
    await driver.run_turn(
        system_prompt="sys", messages=[{"role": "user", "content": "hi"}],
        pool="p", model="m", strip_thinking_blocks=True,
    )
    assert seen[0].get("strip_thinking_blocks") is True


async def test_strip_thinking_blocks_default_off_omits_field():
    bus = Bus()
    seen = _register_llm_capture(bus, {
        "ok": True, "content": "x", "tool_calls": [],
        "stop_reason": "end_turn", "usage": {},
    })
    driver = LlmDriver(bus)
    await driver.run_turn(
        system_prompt="sys", messages=[{"role": "user", "content": "hi"}],
        pool="p", model="m",
    )
    assert "strip_thinking_blocks" not in seen[0]


async def test_token_budget_aborts_between_iterations():
    bus = Bus()
    # Each iteration burns 600 tokens and asks for another tool_call.
    burn = [
        {"ok": True, "content": "thinking",
         "tool_calls": [{"id": f"c{i}", "name": "noop", "input": {}}],
         "stop_reason": "tool_use",
         "usage": {"prompt_tokens": 500, "completion_tokens": 100}}
        for i in range(5)
    ]
    _register_llm(bus, burn)

    async def noop(msg):
        return {"output": "ok"}

    bus.register("noop", noop)
    driver = LlmDriver(bus)
    r = await driver.run_turn(
        system_prompt="s", messages=[{"role": "user", "content": "x"}],
        pool="p", model="m",
        tools=[{"name": "noop", "parameters": {}}],
        max_iterations=10,
        max_total_tokens=1000,
    )
    assert r["ok"] is False
    assert r["stop_reason"] == "token_budget"
    # First iter burns 600 (< 1000, dispatches tool); second hits 1200 → break.
    assert r["iterations"] == 2
    assert r["usage"]["prompt_tokens"] + r["usage"]["completion_tokens"] >= 1000
    assert "token budget" in (r["error"] or "").lower()


async def test_token_budget_default_unset_does_not_abort():
    bus = Bus()
    _register_llm(bus, [
        {"ok": True, "content": "done", "tool_calls": [],
         "stop_reason": "end_turn",
         "usage": {"prompt_tokens": 999_999, "completion_tokens": 999_999}}
    ])
    driver = LlmDriver(bus)
    r = await driver.run_turn(
        system_prompt="s", messages=[{"role": "user", "content": "x"}],
        pool="p", model="m",
    )
    assert r["stop_reason"] == "complete"


async def test_token_budget_does_not_kill_natural_completion():
    """If the LLM finishes (no tool_calls) on the same turn that crosses budget,
    we report 'complete' — the budget check only fires before another iteration."""
    bus = Bus()
    _register_llm(bus, [
        {"ok": True, "content": "answer", "tool_calls": [],
         "stop_reason": "end_turn",
         "usage": {"prompt_tokens": 5000, "completion_tokens": 0}}
    ])
    driver = LlmDriver(bus)
    r = await driver.run_turn(
        system_prompt="s", messages=[{"role": "user", "content": "x"}],
        pool="p", model="m",
        max_total_tokens=100,
    )
    assert r["ok"] is True
    assert r["stop_reason"] == "complete"


async def test_handle_passes_max_total_tokens():
    bus = Bus()
    seen = _register_llm_capture(bus, {
        "ok": True, "content": "x", "tool_calls": [],
        "stop_reason": "end_turn", "usage": {},
    })
    driver = LlmDriver(bus)
    await driver.handle(type("M", (), {"payload": {
        "op": "run_turn",
        "system_prompt": "s",
        "messages": [{"role": "user", "content": "hi"}],
        "pool": "p", "model": "m",
        "max_total_tokens": 4000,
    }})())
    # llm_service doesn't see the budget — driver enforces it locally.
    # We just verify the call still works end-to-end with the param accepted.
    assert seen[0]["model"] == "m"


async def test_handle_passes_strip_thinking_blocks():
    bus = Bus()
    seen = _register_llm_capture(bus, {
        "ok": True, "content": "x", "tool_calls": [],
        "stop_reason": "end_turn", "usage": {},
    })
    driver = LlmDriver(bus)
    await driver.handle(type("M", (), {"payload": {
        "op": "run_turn",
        "system_prompt": "s",
        "messages": [{"role": "user", "content": "hi"}],
        "pool": "p", "model": "m",
        "strip_thinking_blocks": True,
    }})())
    assert seen[0].get("strip_thinking_blocks") is True
