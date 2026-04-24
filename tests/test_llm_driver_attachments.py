"""llm_driver — attachments parameter (per-turn system-reminder injection).

Claude-Code port: attachments are wrapped as `<system-reminder>...`
user messages and appear AFTER the system prompt but BEFORE the
caller's messages, on every iteration. They must not mutate the
caller's `messages` list.
"""
from __future__ import annotations

import pytest

from yuxu.bundled.llm_driver.handler import LlmDriver
from yuxu.core.bus import Bus

pytestmark = pytest.mark.asyncio


def _register_llm_capture(bus, responses):
    """Fake llm_service that records each request payload and replays
    the given responses in order."""
    seen: list[dict] = []
    idx = [0]

    async def handler(msg):
        seen.append(dict(msg.payload) if isinstance(msg.payload, dict) else {})
        i = idx[0]
        idx[0] += 1
        if i >= len(responses):
            return {"ok": True, "content": "done", "tool_calls": [], "usage": {}}
        return responses[i]

    bus.register("llm_service", handler)
    return seen


async def test_attachments_injected_after_system_before_messages():
    bus = Bus()
    seen = _register_llm_capture(bus, [
        {"ok": True, "content": "ok", "tool_calls": [],
         "stop_reason": "end_turn", "usage": {}},
    ])
    driver = LlmDriver(bus)
    caller_msgs = [{"role": "user", "content": "hi"}]
    await driver.run_turn(
        system_prompt="SYS", messages=caller_msgs, pool="p", model="m",
        attachments=["<available_skills>x</available_skills>"],
    )
    assert len(seen) == 1
    api_msgs = seen[0]["messages"]
    # Order: system, attachment(user-wrapped), caller user
    assert api_msgs[0] == {"role": "system", "content": "SYS"}
    assert api_msgs[1]["role"] == "user"
    assert api_msgs[1]["content"].startswith("<system-reminder>\n")
    assert "<available_skills>x</available_skills>" in api_msgs[1]["content"]
    assert api_msgs[1]["content"].endswith("\n</system-reminder>")
    assert api_msgs[2] == {"role": "user", "content": "hi"}


async def test_multiple_attachments_preserved_in_order():
    bus = Bus()
    seen = _register_llm_capture(bus, [
        {"ok": True, "content": "ok", "tool_calls": [],
         "stop_reason": "end_turn", "usage": {}},
    ])
    driver = LlmDriver(bus)
    await driver.run_turn(
        system_prompt="SYS",
        messages=[{"role": "user", "content": "hi"}],
        pool="p", model="m",
        attachments=["A", "B", "C"],
    )
    api_msgs = seen[0]["messages"]
    assert api_msgs[0]["role"] == "system"
    assert "A" in api_msgs[1]["content"]
    assert "B" in api_msgs[2]["content"]
    assert "C" in api_msgs[3]["content"]
    assert api_msgs[4] == {"role": "user", "content": "hi"}


async def test_attachments_do_not_mutate_caller_messages():
    bus = Bus()
    _register_llm_capture(bus, [
        {"ok": True, "content": "ok", "tool_calls": [],
         "stop_reason": "end_turn", "usage": {}},
    ])
    driver = LlmDriver(bus)
    caller_msgs = [{"role": "user", "content": "hi"}]
    snapshot_before = list(caller_msgs)
    await driver.run_turn(
        system_prompt="SYS", messages=caller_msgs, pool="p", model="m",
        attachments=["X"],
    )
    # Caller list should still start with the same user msg, unchanged.
    assert caller_msgs[0] == snapshot_before[0]
    # No attachment leaked into the messages list itself.
    assert all("<system-reminder>" not in (m.get("content") or "")
                for m in caller_msgs)


async def test_attachments_reinjected_every_iteration():
    """Per CC semantics: attachment is a per-turn reminder; if tool_call
    causes a second iteration, attachment must be present again.
    """
    bus = Bus()
    seen = _register_llm_capture(bus, [
        {"ok": True, "content": None,
         "tool_calls": [{"id": "c1", "name": "noop", "input": {}}],
         "stop_reason": "tool_use", "usage": {}},
        {"ok": True, "content": "final", "tool_calls": [],
         "stop_reason": "end_turn", "usage": {}},
    ])

    async def _noop(msg):
        return {"ok": True, "output": "noop_result"}
    bus.register("noop", _noop)

    driver = LlmDriver(bus)
    await driver.run_turn(
        system_prompt="SYS",
        messages=[{"role": "user", "content": "go"}],
        pool="p", model="m",
        tools=[{"name": "noop", "description": "d", "parameters": {"type": "object"}}],
        tool_dispatch={"noop": "noop"},
        attachments=["REMINDER"],
    )
    assert len(seen) == 2
    for call in seen:
        msgs = call["messages"]
        assert msgs[0]["role"] == "system"
        assert "REMINDER" in msgs[1]["content"]


async def test_no_attachments_backward_compatible():
    bus = Bus()
    seen = _register_llm_capture(bus, [
        {"ok": True, "content": "ok", "tool_calls": [],
         "stop_reason": "end_turn", "usage": {}},
    ])
    driver = LlmDriver(bus)
    await driver.run_turn(
        system_prompt="SYS",
        messages=[{"role": "user", "content": "hi"}],
        pool="p", model="m",
    )
    api_msgs = seen[0]["messages"]
    assert len(api_msgs) == 2
    assert api_msgs[0]["role"] == "system"
    assert api_msgs[1] == {"role": "user", "content": "hi"}


async def test_empty_attachments_list_equivalent_to_none():
    bus = Bus()
    seen = _register_llm_capture(bus, [
        {"ok": True, "content": "ok", "tool_calls": [],
         "stop_reason": "end_turn", "usage": {}},
    ])
    driver = LlmDriver(bus)
    await driver.run_turn(
        system_prompt="SYS",
        messages=[{"role": "user", "content": "hi"}],
        pool="p", model="m",
        attachments=[],
    )
    api_msgs = seen[0]["messages"]
    assert len(api_msgs) == 2  # no injection from empty list


async def test_attachments_via_bus_payload():
    """Verify the bus entrypoint (handle() -> run_turn) accepts
    attachments via payload, not just the Python API."""
    bus = Bus()
    seen = _register_llm_capture(bus, [
        {"ok": True, "content": "ok", "tool_calls": [],
         "stop_reason": "end_turn", "usage": {}},
    ])
    driver = LlmDriver(bus)
    bus.register("llm_driver", driver.handle)

    r = await bus.request("llm_driver", {
        "op": "run_turn",
        "system_prompt": "SYS",
        "messages": [{"role": "user", "content": "hi"}],
        "pool": "p", "model": "m",
        "attachments": ["BUS_ATT"],
    })
    assert r["ok"] is True
    assert "BUS_ATT" in seen[0]["messages"][1]["content"]
