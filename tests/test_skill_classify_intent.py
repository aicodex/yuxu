"""classify_intent skill — LLM-mediated NL → agent classification."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from yuxu.core.bus import Bus
from yuxu.bundled.classify_intent.handler import (
    _extract_json,
    _validate,
    execute,
)

pytestmark = pytest.mark.asyncio


# -- pure helpers (sync) -----------------------------------------


def test_extract_json_plain():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_with_fence_and_prose():
    text = "Sure, here it is:\n```json\n{\"x\": [1, 2]}\n```\n— that's the answer."
    assert _extract_json(text) == {"x": [1, 2]}


def test_extract_json_returns_none_on_garbage():
    assert _extract_json("not json at all") is None
    assert _extract_json("") is None


def test_validate_accepts_well_formed():
    obj = {
        "agent_type": "default",
        "suggested_name": "my_bot",
        "run_mode": "one_shot",
        "depends_on": ["llm_driver"],
        "driver": "python",
        "reasoning": "because",
    }
    ok, why = _validate(obj, ["default"])
    assert ok, why


def test_validate_rejects_missing_keys():
    ok, why = _validate({"agent_type": "default"}, ["default"])
    assert not ok
    assert "missing keys" in why


def test_validate_rejects_unknown_template():
    obj = {"agent_type": "exotic", "suggested_name": "x", "run_mode": "one_shot",
           "depends_on": [], "driver": "python", "reasoning": "."}
    ok, why = _validate(obj, ["default"])
    assert not ok and "agent_type" in why


def test_validate_rejects_bad_run_mode():
    obj = {"agent_type": "default", "suggested_name": "x", "run_mode": "weird",
           "depends_on": [], "driver": "python", "reasoning": "."}
    ok, why = _validate(obj, ["default"])
    assert not ok and "run_mode" in why


def test_validate_rejects_non_snake_case_name():
    obj = {"agent_type": "default", "suggested_name": "MyBot",
           "run_mode": "one_shot", "depends_on": [], "driver": "python",
           "reasoning": "."}
    ok, why = _validate(obj, ["default"])
    assert not ok and "snake_case" in why


# -- execute() with mocked llm_driver ----------------------------


def _ctx_with_llm(bus: Bus) -> SimpleNamespace:
    return SimpleNamespace(bus=bus)


def _register_llm(bus: Bus, response: dict):
    """Register a fake llm_driver returning `response`. Records seen payload."""
    seen: list[dict] = []

    async def handler(msg):
        seen.append(dict(msg.payload) if isinstance(msg.payload, dict) else {})
        return response

    bus.register("llm_driver", handler)
    return seen


async def test_execute_happy_path():
    bus = Bus()
    classification = {
        "agent_type": "default",
        "suggested_name": "weather_bot",
        "run_mode": "scheduled",
        "depends_on": ["llm_driver", "scheduler"],
        "driver": "hybrid",
        "reasoning": "polls a weather API on a cron and summarizes via LLM",
    }
    seen = _register_llm(bus, {
        "ok": True,
        "content": json.dumps(classification),
        "stop_reason": "complete",
        "usage": {"prompt_tokens": 50, "completion_tokens": 80},
    })

    r = await execute(
        {"description": "summarize today's weather every morning at 7am"},
        ctx=_ctx_with_llm(bus),
    )
    assert r["ok"] is True
    assert r["classification"] == classification
    # llm_driver was called with json_mode + strip_thinking
    assert seen[0]["json_mode"] is True
    assert seen[0]["strip_thinking_blocks"] is True
    assert seen[0]["op"] == "run_turn"


async def test_execute_missing_description_returns_error():
    bus = Bus()
    r = await execute({}, ctx=_ctx_with_llm(bus))
    assert r["ok"] is False
    assert "missing" in r["error"]


async def test_execute_llm_returns_garbage_surfaces_raw():
    bus = Bus()
    _register_llm(bus, {
        "ok": True,
        "content": "I'm sorry I can't follow JSON instructions today",
        "stop_reason": "complete", "usage": {},
    })
    r = await execute({"description": "make me a bot"}, ctx=_ctx_with_llm(bus))
    assert r["ok"] is False
    assert "parseable JSON" in r["error"]
    assert r["raw"]


async def test_execute_validation_failure_returns_parsed_for_inspection():
    bus = Bus()
    bad = {"agent_type": "exotic", "suggested_name": "x", "run_mode": "one_shot",
           "depends_on": [], "driver": "python", "reasoning": "."}
    _register_llm(bus, {
        "ok": True, "content": json.dumps(bad),
        "stop_reason": "complete", "usage": {},
    })
    r = await execute(
        {"description": "x", "available_templates": ["default"]},
        ctx=_ctx_with_llm(bus),
    )
    assert r["ok"] is False
    assert "validation" in r["error"]
    assert r["parsed"] == bad


async def test_execute_llm_driver_failure_propagated():
    bus = Bus()
    _register_llm(bus, {"ok": False, "error": "rate_limit"})
    r = await execute({"description": "x"}, ctx=_ctx_with_llm(bus))
    assert r["ok"] is False
    assert "rate_limit" in r["error"]


async def test_execute_passes_pool_and_model_overrides():
    bus = Bus()
    seen = _register_llm(bus, {
        "ok": True,
        "content": json.dumps({
            "agent_type": "default", "suggested_name": "x",
            "run_mode": "one_shot", "depends_on": [],
            "driver": "python", "reasoning": ".",
        }),
        "stop_reason": "complete", "usage": {},
    })
    await execute(
        {"description": "x", "pool": "minimax", "model": "abab6.5s-chat"},
        ctx=_ctx_with_llm(bus),
    )
    assert seen[0]["pool"] == "minimax"
    assert seen[0]["model"] == "abab6.5s-chat"
