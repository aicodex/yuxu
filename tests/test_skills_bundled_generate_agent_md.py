"""generate_agent_md skill — LLM authors AGENT.md from a spec."""
from __future__ import annotations

from textwrap import dedent
from types import SimpleNamespace

import pytest

from yuxu.core.bus import Bus
from yuxu.skills_bundled.generate_agent_md.handler import (
    _build_system_prompt,
    _check_consistency,
    _strip_outer_fence,
    execute,
)

pytestmark = pytest.mark.asyncio


# -- pure helpers (sync) -----------------------------------------


def test_strip_outer_fence_with_lang():
    src = "```markdown\n---\nx: 1\n---\nbody\n```"
    assert _strip_outer_fence(src) == "---\nx: 1\n---\nbody"


def test_strip_outer_fence_without_fence_passthrough():
    src = "---\nx: 1\n---\nbody"
    assert _strip_outer_fence(src) == src


def test_check_consistency_clean():
    fm = {"driver": "python", "run_mode": "one_shot",
          "scope": "user", "depends_on": ["llm_driver"]}
    body = "# my_bot\nbody"
    warns = _check_consistency(
        frontmatter=fm, body=body, name="my_bot",
        run_mode="one_shot", driver="python", scope="user",
        depends_on=["llm_driver"],
    )
    assert warns == []


def test_check_consistency_flags_mismatches():
    fm = {"driver": "llm", "run_mode": "persistent",
          "scope": "system", "depends_on": []}
    body = "no title here"
    warns = _check_consistency(
        frontmatter=fm, body=body, name="my_bot",
        run_mode="one_shot", driver="python", scope="user",
        depends_on=["llm_driver"],
    )
    assert any("run_mode" in w for w in warns)
    assert any("driver" in w for w in warns)
    assert any("scope" in w for w in warns)
    assert any("depends_on" in w for w in warns)
    assert any("H1" in w for w in warns)


def test_build_system_prompt_includes_spec_and_hints():
    p = _build_system_prompt(
        name="x", description="desc", run_mode="one_shot",
        driver="python", scope="user", depends_on=["a"],
        extra_hints="be terse",
    )
    assert "name: x" in p
    assert "run_mode: one_shot" in p
    assert "depends_on: ['a']" in p
    assert "be terse" in p


def test_build_system_prompt_omits_hints_block_when_empty():
    p = _build_system_prompt(
        name="x", description="d", run_mode="one_shot",
        driver="python", scope="user", depends_on=[], extra_hints="",
    )
    assert "Additional hints" not in p


# -- execute() with mocked llm_driver ----------------------------


def _ctx(bus: Bus) -> SimpleNamespace:
    return SimpleNamespace(bus=bus)


def _register_llm(bus: Bus, response: dict):
    seen: list[dict] = []

    async def handler(msg):
        seen.append(dict(msg.payload) if isinstance(msg.payload, dict) else {})
        return response

    bus.register("llm_driver", handler)
    return seen


_GOOD_AGENT_MD = dedent("""\
    ---
    driver: python
    run_mode: one_shot
    scope: user
    depends_on: [llm_driver]
    ready_timeout: 30
    ---
    # weather_bot

    Summarizes the morning weather report.

    ## Operations

    - `bus.request("weather_bot", {op: "summarize"})` → `{ok, summary}`

    ## Why this is an agent

    Holds an HTTP client cache for the weather API; lifetime > one request.
""")


async def test_execute_happy_path_parses_frontmatter_and_body():
    bus = Bus()
    seen = _register_llm(bus, {
        "ok": True, "content": _GOOD_AGENT_MD,
        "stop_reason": "complete", "usage": {"prompt_tokens": 100, "completion_tokens": 80},
    })
    r = await execute({
        "name": "weather_bot",
        "description": "summarize the morning weather report",
        "run_mode": "one_shot",
        "driver": "python",
        "depends_on": ["llm_driver"],
    }, ctx=_ctx(bus))

    assert r["ok"] is True
    assert r["frontmatter"]["driver"] == "python"
    assert r["frontmatter"]["run_mode"] == "one_shot"
    assert "# weather_bot" in r["body"]
    assert r["warnings"] == []
    # llm_driver call shape
    assert seen[0]["op"] == "run_turn"
    assert seen[0]["strip_thinking_blocks"] is True


async def test_execute_rejects_invalid_name():
    bus = Bus()
    r = await execute({"name": "BadName", "description": "x"}, ctx=_ctx(bus))
    assert r["ok"] is False
    assert "snake_case" in r["error"]


async def test_execute_rejects_invalid_run_mode():
    bus = Bus()
    r = await execute({"name": "x", "description": "y", "run_mode": "loopy"},
                      ctx=_ctx(bus))
    assert r["ok"] is False
    assert "run_mode" in r["error"]


async def test_execute_handles_fenced_output():
    bus = Bus()
    fenced = "```markdown\n" + _GOOD_AGENT_MD + "```"
    _register_llm(bus, {"ok": True, "content": fenced,
                         "stop_reason": "complete", "usage": {}})
    r = await execute({
        "name": "weather_bot", "description": "x",
        "run_mode": "one_shot", "driver": "python",
        "depends_on": ["llm_driver"],
    }, ctx=_ctx(bus))
    assert r["ok"] is True


async def test_execute_warns_on_field_mismatch():
    """LLM returns frontmatter that disagrees with the spec — warn but accept."""
    bus = Bus()
    drift = _GOOD_AGENT_MD.replace("driver: python", "driver: hybrid")
    _register_llm(bus, {"ok": True, "content": drift,
                         "stop_reason": "complete", "usage": {}})
    r = await execute({
        "name": "weather_bot", "description": "x",
        "run_mode": "one_shot", "driver": "python",
        "depends_on": ["llm_driver"],
    }, ctx=_ctx(bus))
    assert r["ok"] is True
    assert any("driver" in w for w in r["warnings"])


async def test_execute_rejects_non_frontmatter_output():
    bus = Bus()
    _register_llm(bus, {"ok": True,
                         "content": "Sure! Here's your agent:\n\nbody",
                         "stop_reason": "complete", "usage": {}})
    r = await execute({"name": "x", "description": "y"}, ctx=_ctx(bus))
    assert r["ok"] is False
    assert "frontmatter" in r["error"]


async def test_execute_propagates_llm_failure():
    bus = Bus()
    _register_llm(bus, {"ok": False, "error": "auth_failed"})
    r = await execute({"name": "x", "description": "y"}, ctx=_ctx(bus))
    assert r["ok"] is False
    assert "auth_failed" in r["error"]
