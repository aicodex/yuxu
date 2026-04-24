"""invoke_skill — LLM tool wrapper that delegates to skill_index.read."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from yuxu.bundled.invoke_skill.handler import (
    NAME,
    TOOL_SCHEMA,
    _unwrap_args,
    execute,
)
from yuxu.core.bus import Bus

pytestmark = pytest.mark.asyncio


# -- helpers ---------------------------------------------------


def _make_ctx(bus):
    return SimpleNamespace(bus=bus, loader=None)


def _register_skill_index(bus, *, name="memory", body="# memory\nbody",
                              ok=True, err=None):
    captured: list[dict] = []

    async def handler(msg):
        captured.append(dict(msg.payload) if isinstance(msg.payload, dict) else {})
        if not ok:
            return {"ok": False, "error": err or "boom"}
        return {
            "ok": True,
            "name": name,
            "kind": "skill",
            "location": f"bundled/{name}/SKILL.md",
            "frontmatter": {"name": name},
            "body": body,
        }
    bus.register("skill_index", handler)
    return captured


# -- schema ----------------------------------------------------


async def test_tool_schema_shape():
    assert TOOL_SCHEMA["name"] == NAME
    assert NAME == "invoke_skill"
    assert TOOL_SCHEMA["parameters"]["type"] == "object"
    assert "name" in TOOL_SCHEMA["parameters"]["properties"]
    assert TOOL_SCHEMA["parameters"]["required"] == ["name"]


# -- unwrap ----------------------------------------------------


async def test_unwrap_direct():
    assert _unwrap_args({"name": "memory"}) == {"name": "memory"}


async def test_unwrap_llm_driver_envelope():
    wrapped = {"op": "execute", "input": {"name": "memory"}}
    assert _unwrap_args(wrapped) == {"name": "memory"}


async def test_unwrap_malformed():
    assert _unwrap_args(None) is None
    assert _unwrap_args("bad") is None
    assert _unwrap_args(42) is None


# -- execute ---------------------------------------------------


async def test_missing_name_returns_error():
    bus = Bus()
    _register_skill_index(bus)
    r = await execute({}, _make_ctx(bus))
    assert r["ok"] is False
    assert "name" in r["error"]


async def test_empty_name_returns_error():
    bus = Bus()
    _register_skill_index(bus)
    r = await execute({"name": "   "}, _make_ctx(bus))
    assert r["ok"] is False


async def test_non_string_name_returns_error():
    bus = Bus()
    _register_skill_index(bus)
    r = await execute({"name": 42}, _make_ctx(bus))
    assert r["ok"] is False


async def test_direct_call_success():
    bus = Bus()
    captured = _register_skill_index(bus, name="memory", body="# memory\nhello")
    r = await execute({"name": "memory"}, _make_ctx(bus))
    assert r["ok"] is True
    assert r["name"] == "memory"
    assert r["body"] == "# memory\nhello"
    assert r["kind"] == "skill"
    assert r["location"] == "bundled/memory/SKILL.md"
    # downstream got a read op, not the wrapped envelope
    assert captured == [{"op": "read", "name": "memory"}]


async def test_llm_driver_envelope_success():
    """Simulates the call shape llm_driver produces when dispatching a
    tool_call through the bus."""
    bus = Bus()
    captured = _register_skill_index(bus, name="session_compressor")
    r = await execute(
        {"op": "execute", "input": {"name": "session_compressor"}},
        _make_ctx(bus),
    )
    assert r["ok"] is True
    assert r["name"] == "session_compressor"
    assert captured == [{"op": "read", "name": "session_compressor"}]


async def test_skill_index_failure_propagates():
    bus = Bus()
    _register_skill_index(bus, ok=False, err="not found: foo")
    r = await execute({"name": "foo"}, _make_ctx(bus))
    assert r["ok"] is False
    assert "not found" in r["error"]


async def test_bus_exception_wrapped():
    bus = Bus()

    async def bad(msg):
        raise RuntimeError("transport failure")
    bus.register("skill_index", bad)
    r = await execute({"name": "x"}, _make_ctx(bus))
    assert r["ok"] is False
    assert "failed" in r["error"]


async def test_trims_whitespace_from_name():
    bus = Bus()
    captured = _register_skill_index(bus, name="memory")
    await execute({"name": "  memory  "}, _make_ctx(bus))
    assert captured == [{"op": "read", "name": "memory"}]


async def test_frontmatter_passed_through():
    bus = Bus()

    async def handler(msg):
        return {"ok": True, "name": "x", "kind": "skill",
                "location": "bundled/x/SKILL.md",
                "frontmatter": {"name": "x", "version": "0.1.0"},
                "body": "body"}
    bus.register("skill_index", handler)
    r = await execute({"name": "x"}, _make_ctx(bus))
    assert r["frontmatter"] == {"name": "x", "version": "0.1.0"}
