"""P1 integration: blank business agent -> llm_driver -> llm_service
-> rate_limit_service -> (mocked) HTTP.

Verifies the dep chain resolves via `ensure_running` and one end-to-end turn
completes against a fake OpenAI-compatible endpoint.
"""
from __future__ import annotations

import textwrap

import httpx
import pytest

from yuxu.core.main import boot

pytestmark = pytest.mark.asyncio


def _write_rate_config(tmp_path, monkeypatch):
    cfg = tmp_path / "rate.yaml"
    cfg.write_text(
        "minimax:\n"
        "  max_concurrent: 2\n"
        "  rpm: 60\n"
        "  accounts:\n"
        "    - id: k1\n"
        "      api_key: test-key\n"
        "      base_url: http://mock/v1\n"
    )
    monkeypatch.setenv("RATE_LIMITS_CONFIG", str(cfg))


def _write_chat_bot_agent(tmp_path):
    user_dir = tmp_path / "config_agents"
    user_dir.mkdir()
    agent_dir = user_dir / "chat_bot"
    agent_dir.mkdir()
    (agent_dir / "AGENT.md").write_text(
        "---\n"
        "driver: python\n"
        "run_mode: persistent\n"
        "scope: user\n"
        "depends_on: [llm_driver]\n"
        "---\n"
        "# chat_bot (test fixture)\n"
    )
    (agent_dir / "__init__.py").write_text(textwrap.dedent("""
        async def start(ctx):
            async def handler(msg):
                payload = msg.payload if isinstance(msg.payload, dict) else {}
                return await ctx.bus.request("llm_driver", {
                    "system_prompt": "be brief",
                    "messages": [{"role": "user", "content": payload.get("text", "")}],
                    "pool": "minimax",
                    "model": "test-model",
                }, timeout=10.0)
            ctx.bus.register("chat_bot", handler)
            await ctx.ready()
    """))
    return user_dir


async def test_end_to_end_blank_agent(tmp_path, monkeypatch, bundled_dir):
    _write_rate_config(tmp_path, monkeypatch)
    user_dir = _write_chat_bot_agent(tmp_path)

    transport_log: list[str] = []

    def route(req: httpx.Request):
        transport_log.append(str(req.url))
        assert req.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
        })

    bus, loader = await boot(
        dirs=[bundled_dir, str(user_dir)],
        autostart_persistent=True,
    )

    # whole chain came up via dependency resolution
    for name in ("rate_limit_service", "llm_service", "llm_driver", "chat_bot"):
        assert bus.query_status(name) == "ready", f"{name} not ready"

    # inject mock transport into the running llm_service
    llm_service = loader.get_handle("llm_service")
    llm_service._client = httpx.AsyncClient(transport=httpx.MockTransport(route))
    llm_service._owned_client = True

    result = await bus.request("chat_bot", {"text": "ping"}, timeout=10.0)
    assert result["ok"] is True
    assert result["content"] == "pong"
    assert result["stop_reason"] == "complete"
    assert result["usage"]["prompt_tokens"] == 5
    assert transport_log == ["http://mock/v1/chat/completions"]


async def test_end_to_end_with_tool_call(tmp_path, monkeypatch, bundled_dir):
    """Same pipeline, but with a tool-using business agent."""
    _write_rate_config(tmp_path, monkeypatch)

    user_dir = tmp_path / "config_agents"
    user_dir.mkdir()

    # tool agent: any request with op=execute returns a fixed output
    tool_dir = user_dir / "clock_tool"
    tool_dir.mkdir()
    (tool_dir / "AGENT.md").write_text(
        "---\ndriver: python\nrun_mode: persistent\nscope: user\n---\n"
    )
    (tool_dir / "__init__.py").write_text(textwrap.dedent("""
        async def start(ctx):
            async def handler(msg):
                return {"output": {"now": "2026-04-21T00:00:00Z"}}
            ctx.bus.register("clock_tool", handler)
            await ctx.ready()
    """))

    # business agent: uses llm_driver with a tool
    bot_dir = user_dir / "tool_bot"
    bot_dir.mkdir()
    (bot_dir / "AGENT.md").write_text(
        "---\n"
        "driver: python\n"
        "run_mode: persistent\n"
        "scope: user\n"
        "depends_on: [llm_driver, clock_tool]\n"
        "---\n"
    )
    (bot_dir / "__init__.py").write_text(textwrap.dedent("""
        async def start(ctx):
            async def handler(msg):
                return await ctx.bus.request("llm_driver", {
                    "system_prompt": "use tools",
                    "messages": [{"role": "user", "content": "time?"}],
                    "pool": "minimax",
                    "model": "test-model",
                    "tools": [{"name": "now", "description": "get time",
                               "parameters": {"type": "object"}}],
                    "tool_dispatch": {"now": "clock_tool"},
                }, timeout=10.0)
            ctx.bus.register("tool_bot", handler)
            await ctx.ready()
    """))

    # LLM: first round issues a tool_call; second returns final text.
    call_count = [0]

    def route(req):
        call_count[0] += 1
        if call_count[0] == 1:
            return httpx.Response(200, json={
                "choices": [{
                    "message": {
                        "content": None,
                        "tool_calls": [{
                            "id": "c1", "type": "function",
                            "function": {"name": "now", "arguments": "{}"},
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            })
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "it is 2026-04-21"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 4},
        })

    bus, loader = await boot(
        dirs=[bundled_dir, str(user_dir)],
        autostart_persistent=True,
    )
    llm_service = loader.get_handle("llm_service")
    llm_service._client = httpx.AsyncClient(transport=httpx.MockTransport(route))
    llm_service._owned_client = True

    r = await bus.request("tool_bot", {}, timeout=10.0)
    assert r["ok"] is True
    assert r["content"] == "it is 2026-04-21"
    assert r["iterations"] == 2
    assert call_count[0] == 2
    assert r["usage"]["prompt_tokens"] == 8  # accumulated
