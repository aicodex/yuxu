"""compactor — port of CC 2.1.88's microcompact + full_compact.

Mechanism-only tests. Auto-triggers are documented as TODOs at the
consumer sites (llm_driver, gateway); no trigger behaviour to verify
yet.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from yuxu.bundled.compactor.handler import (
    CLEARED_MARKER,
    COMPACT_BOUNDARY_MARKER,
    DEFAULT_KEEP_RECENT,
    DEFAULT_KEEP_RECENT_TURNS,
    FULL_COMPACT_SYSTEM_PROMPT,
    NAME,
    execute,
    full_compact,
    microcompact,
)
from yuxu.core.bus import Bus

pytestmark = pytest.mark.asyncio


# ----------------------------------------------------------------------------
# microcompact
# ----------------------------------------------------------------------------


def _msg(role: str, content: str, **kwargs) -> dict:
    return {"role": role, "content": content, **kwargs}


async def test_microcompact_passthrough_when_fewer_than_keep():
    messages = [
        _msg("user", "hi"),
        _msg("assistant", "ok", tool_calls=[{"id": "t1"}]),
        _msg("tool", "result1", tool_call_id="t1"),
    ]
    r = microcompact(messages, keep_recent=5)
    assert r["cleared_count"] == 0
    assert r["tool_count"] == 1
    assert r["messages"] == messages


async def test_microcompact_clears_older_tools_preserves_last_N():
    messages = []
    for i in range(8):
        messages.append(_msg("assistant", f"call {i}",
                                tool_calls=[{"id": f"t{i}"}]))
        messages.append(_msg("tool", f"big tool output {i}" * 10,
                                tool_call_id=f"t{i}"))
    r = microcompact(messages, keep_recent=3)
    assert r["tool_count"] == 8
    assert r["cleared_count"] == 5
    # Last 3 tool results kept verbatim
    tool_msgs = [m for m in r["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 8
    assert all(CLEARED_MARKER in t["content"] for t in tool_msgs[:5])
    assert all(CLEARED_MARKER not in t["content"] for t in tool_msgs[5:])


async def test_microcompact_preserves_role_and_tool_call_id():
    messages = [
        _msg("tool", "old content", tool_call_id="t1"),
        _msg("tool", "old content 2", tool_call_id="t2"),
        _msg("tool", "recent", tool_call_id="t3"),
    ]
    r = microcompact(messages, keep_recent=1)
    cleared = [m for m in r["messages"] if m["content"] == CLEARED_MARKER]
    assert {m["tool_call_id"] for m in cleared} == {"t1", "t2"}
    for m in cleared:
        assert m["role"] == "tool"


async def test_microcompact_does_not_mutate_input():
    messages = [_msg("tool", "x" * 100, tool_call_id=f"t{i}") for i in range(6)]
    snapshot = [dict(m) for m in messages]
    microcompact(messages, keep_recent=2)
    assert messages == snapshot  # input untouched


async def test_microcompact_keep_zero_clears_all_tools():
    messages = [_msg("tool", "a", tool_call_id="t1"),
                _msg("tool", "b", tool_call_id="t2")]
    r = microcompact(messages, keep_recent=0)
    assert r["cleared_count"] == 2
    assert all(m["content"] == CLEARED_MARKER for m in r["messages"])


async def test_microcompact_ignores_non_tool_messages():
    messages = [
        _msg("user", "big user message " * 50),
        _msg("assistant", "big assistant reply " * 50),
        _msg("tool", "old tool1", tool_call_id="t1"),
        _msg("tool", "old tool2", tool_call_id="t2"),
        _msg("tool", "old tool3", tool_call_id="t3"),
        _msg("tool", "old tool4", tool_call_id="t4"),
        _msg("tool", "recent tool", tool_call_id="t5"),
    ]
    r = microcompact(messages, keep_recent=1)
    assert r["cleared_count"] == 4
    # user + assistant untouched even though they're big
    assert "big user message" in r["messages"][0]["content"]
    assert "big assistant reply" in r["messages"][1]["content"]


async def test_microcompact_non_list_input_returns_unchanged():
    r = microcompact("not a list", keep_recent=5)  # type: ignore[arg-type]
    assert r["cleared_count"] == 0


async def test_microcompact_default_keep_recent_is_five():
    assert DEFAULT_KEEP_RECENT == 5


# ----------------------------------------------------------------------------
# full_compact
# ----------------------------------------------------------------------------


async def test_full_compact_rejects_empty_messages():
    bus = Bus()
    r = await full_compact([], bus=bus, pool="p", model="m")
    assert r["ok"] is False
    assert "non-empty" in r["error"]


async def test_full_compact_skipped_when_not_enough_turns():
    """If total turns ≤ keep_recent_turns, skip LLM call, return unchanged."""
    bus = Bus()
    messages = [
        _msg("user", "q1"),
        _msg("assistant", "a1"),
        _msg("user", "q2"),
        _msg("assistant", "a2"),
    ]
    r = await full_compact(messages, bus=bus, pool="p", model="m",
                              keep_recent_turns=5)
    assert r["ok"] is True
    assert r["cleared_count"] == 0
    assert r["messages"] == messages
    assert "skipped" in r


async def test_full_compact_happy_path_emits_boundary_and_summary():
    """Enough turns to compact → LLM called, result has summary +
    compact_boundary + preserved tail."""
    bus = Bus()
    captured = []

    async def fake_llm(msg):
        captured.append(msg.payload)
        return {"ok": True,
                "content": "1. Primary Request:\n   user wanted X.\n...",
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                "elapsed_ms": 500}
    bus.register("llm_driver", fake_llm)

    messages = []
    for i in range(10):  # 10 turns
        messages.append(_msg("user", f"question {i}"))
        messages.append(_msg("assistant", f"answer {i}"))
    r = await full_compact(messages, bus=bus, pool="minimax", model="m",
                              keep_recent_turns=3)
    assert r["ok"] is True
    assert r["cleared_count"] > 0
    # new structure: [summary_user, boundary, *last_3_turns]
    assert r["messages"][0]["role"] == "user"
    assert "summary of the prior conversation" in r["messages"][0]["content"]
    assert r["messages"][1]["role"] == "system"
    assert "compact_boundary" in r["messages"][1]["content"]
    # tail = last 3 turns (3 user + 3 assistant = 6 messages)
    tail = r["messages"][2:]
    assert len(tail) == 6
    assert tail[0] == _msg("user", "question 7")
    assert tail[-1] == _msg("assistant", "answer 9")
    # LLM call shape
    assert captured[0]["system_prompt"] == FULL_COMPACT_SYSTEM_PROMPT


async def test_full_compact_handles_llm_failure():
    bus = Bus()

    async def failing_llm(msg):
        return {"ok": False, "error": "timeout"}
    bus.register("llm_driver", failing_llm)

    messages = []
    for i in range(10):
        messages.append(_msg("user", f"q{i}"))
        messages.append(_msg("assistant", f"a{i}"))
    r = await full_compact(messages, bus=bus, pool="p", model="m",
                              keep_recent_turns=3)
    assert r["ok"] is False
    assert "timeout" in r["error"]


async def test_full_compact_empty_summary_returns_error():
    bus = Bus()

    async def empty_llm(msg):
        return {"ok": True, "content": "   "}
    bus.register("llm_driver", empty_llm)

    messages = [_msg("user", f"q{i}") for i in range(10)]
    r = await full_compact(messages, bus=bus, pool="p", model="m",
                              keep_recent_turns=2)
    assert r["ok"] is False
    assert "empty summary" in r["error"]


# ----------------------------------------------------------------------------
# execute dispatcher
# ----------------------------------------------------------------------------


async def test_execute_microcompact_via_dispatcher():
    ctx = SimpleNamespace(bus=Bus())
    r = await execute({
        "op": "microcompact",
        "messages": [_msg("tool", "a", tool_call_id="t1")],
        "keep_recent": 0,
    }, ctx)
    assert r["ok"] is True
    assert r["cleared_count"] == 1


async def test_execute_full_compact_via_dispatcher():
    bus = Bus()

    async def fake_llm(msg):
        return {"ok": True, "content": "summary", "usage": {}}
    bus.register("llm_driver", fake_llm)

    ctx = SimpleNamespace(bus=bus)
    r = await execute({
        "op": "full_compact",
        "messages": [_msg("user", f"q{i}") for i in range(6)],
        "pool": "minimax", "model": "m",
        "keep_recent_turns": 2,
    }, ctx)
    assert r["ok"] is True


async def test_execute_full_compact_requires_pool_and_model():
    ctx = SimpleNamespace(bus=Bus())
    r = await execute({
        "op": "full_compact",
        "messages": [_msg("user", "q")],
    }, ctx)
    assert r["ok"] is False
    assert "pool + model" in r["error"]


async def test_execute_unknown_op():
    ctx = SimpleNamespace(bus=Bus())
    r = await execute({"op": "frobnicate", "messages": []}, ctx)
    assert r["ok"] is False
    assert "unknown op" in r["error"]


async def test_execute_unwraps_llm_driver_envelope():
    """When invoked as an LLM tool via llm_driver, the request arrives
    wrapped as {"op":"execute","input":{...}}."""
    ctx = SimpleNamespace(bus=Bus())
    r = await execute({
        "op": "execute",
        "input": {
            "op": "microcompact",
            "messages": [_msg("tool", "x", tool_call_id="t1")],
            "keep_recent": 0,
        },
    }, ctx)
    assert r["ok"] is True
    assert r["cleared_count"] == 1


# ----------------------------------------------------------------------------
# constants pinned (regression against accidental reword)
# ----------------------------------------------------------------------------


async def test_cleared_marker_matches_cc_verbatim():
    # Verbatim from CC services/compact/microCompact.ts:36 — must not drift.
    assert CLEARED_MARKER == "[Old tool result content cleared]"


async def test_full_compact_prompt_has_nine_sections():
    # Verbatim from CC services/compact/prompt.ts:61-131 — 9 numbered
    # sections. If this breaks, someone reworded the CC port and that
    # needs review against source.
    for i in range(1, 10):
        assert f"{i}. " in FULL_COMPACT_SYSTEM_PROMPT


async def test_compact_boundary_marker_is_system_role():
    assert COMPACT_BOUNDARY_MARKER["role"] == "system"
    assert "compact_boundary" in COMPACT_BOUNDARY_MARKER["content"]


async def test_default_keep_recent_turns_is_five():
    assert DEFAULT_KEEP_RECENT_TURNS == 5


async def test_name_constant():
    assert NAME == "compactor"
