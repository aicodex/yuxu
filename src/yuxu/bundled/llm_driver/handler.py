"""LlmDriver — iterative LLM + tool-call loop built on the bus.

Port of Stage 1 `run_turn` adapted to:
- Async HTTP via llm_service (bus.request)
- Async tools via bus.request to per-tool addresses
- No Python-object coupling: everything flows as JSON over the bus
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

DEFAULT_MAX_ITERATIONS = 32
DEFAULT_MAX_OUTPUT_BYTES = 50_000
DEFAULT_TOOL_TIMEOUT = 60.0
DEFAULT_LLM_TIMEOUT = 180.0
DEFAULT_MAX_TOTAL_TOKENS: Optional[int] = None  # off by default; caller opts in


def _cap(s: str, max_bytes: int) -> str:
    b = s.encode("utf-8", errors="replace")
    if len(b) <= max_bytes:
        return s
    truncated = b[:max_bytes].decode("utf-8", errors="ignore")
    return truncated + f"\n...[truncated {len(b) - max_bytes} bytes]"


def _tool_result_content(result: Any) -> str:
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(result)


def _to_openai_tool(t: dict) -> dict:
    """Accept raw function schema OR pre-wrapped {type, function} shape."""
    if "type" in t and "function" in t:
        return t
    return {"type": "function", "function": t}


def _assistant_message(resp: dict) -> dict:
    if resp.get("tool_calls"):
        return {
            "role": "assistant",
            "content": resp.get("content"),
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["input"], ensure_ascii=False),
                    },
                }
                for tc in resp["tool_calls"]
            ],
        }
    return {"role": "assistant", "content": resp.get("content")}


def _extract_tool_output(raw: Any) -> Any:
    """Normalize a tool's bus reply into something to hand back to the LLM."""
    if isinstance(raw, dict):
        if "output" in raw:
            return raw["output"]
        if raw.get("ok") is False:
            return {"error": raw.get("error", "tool failed")}
    return raw


class LlmDriver:
    def __init__(self, bus) -> None:
        self.bus = bus

    async def run_turn(
        self,
        *,
        system_prompt: str,
        messages: list[dict],
        pool: str,
        model: str,
        tools: Optional[list[dict]] = None,
        tool_dispatch: Optional[dict[str, str]] = None,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        tool_timeout: float = DEFAULT_TOOL_TIMEOUT,
        llm_timeout: float = DEFAULT_LLM_TIMEOUT,
        temperature: Optional[float] = None,
        json_mode: bool = False,
        strip_thinking_blocks: bool = False,
        max_total_tokens: Optional[int] = DEFAULT_MAX_TOTAL_TOKENS,
    ) -> dict:
        dispatch = tool_dispatch or {}
        total_prompt = 0
        total_completion = 0
        final_content: Optional[str] = None
        stop_reason = "max_iter"
        error_msg: Optional[str] = None
        iterations_done = 0

        for it in range(max_iterations):
            iterations_done = it + 1
            api_messages = [{"role": "system", "content": system_prompt}, *messages]
            llm_req: dict[str, Any] = {
                "pool": pool,
                "model": model,
                "messages": api_messages,
            }
            if tools:
                llm_req["tools"] = [_to_openai_tool(t) for t in tools]
            if temperature is not None:
                llm_req["temperature"] = temperature
            if json_mode:
                llm_req["json_mode"] = True
            if strip_thinking_blocks:
                llm_req["strip_thinking_blocks"] = True

            try:
                resp = await self.bus.request("llm_service", llm_req, timeout=llm_timeout)
            except Exception as e:
                log.exception("llm_driver: llm_service failed at iter %d", it)
                stop_reason = "error"
                error_msg = f"llm_service: {e}"
                break

            if not resp.get("ok", True):  # llm_service may return ok:false
                stop_reason = "error"
                error_msg = f"llm_service: {resp.get('error')}"
                break

            final_content = resp.get("content")
            usage = resp.get("usage") or {}
            total_prompt += usage.get("prompt_tokens") or 0
            total_completion += usage.get("completion_tokens") or 0

            messages.append(_assistant_message(resp))

            tool_calls = resp.get("tool_calls") or []
            if not tool_calls:
                stop_reason = "complete"
                break

            if max_total_tokens is not None and (total_prompt + total_completion) >= max_total_tokens:
                stop_reason = "token_budget"
                error_msg = (
                    f"token budget exceeded: {total_prompt + total_completion} "
                    f"tokens >= {max_total_tokens}; aborting before next iteration"
                )
                break

            for tc in tool_calls:
                name = tc["name"]
                addr = dispatch.get(name, name)
                try:
                    raw = await self.bus.request(
                        addr,
                        {"op": "execute", "input": tc.get("input", {})},
                        timeout=tool_timeout,
                    )
                    body = _extract_tool_output(raw)
                except asyncio.TimeoutError:
                    body = {"error": f"tool {name} timed out after {tool_timeout}s"}
                except Exception as e:
                    body = {"error": f"tool {name}: {e}"}
                content = _cap(_tool_result_content(body), max_output_bytes)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id"),
                    "content": content,
                })

        return {
            "ok": stop_reason == "complete",
            "content": final_content,
            "iterations": iterations_done,
            "stop_reason": stop_reason,
            "usage": {"prompt_tokens": total_prompt, "completion_tokens": total_completion},
            "error": error_msg,
        }

    async def handle(self, msg) -> dict:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        op = payload.get("op", "run_turn")
        if op != "run_turn":
            return {"ok": False, "error": f"unknown op: {op!r}"}
        required = ("system_prompt", "messages", "pool", "model")
        missing = [k for k in required if k not in payload]
        if missing:
            return {"ok": False, "error": f"missing fields: {missing}"}
        messages = list(payload["messages"])  # don't mutate caller's list
        result = await self.run_turn(
            system_prompt=payload["system_prompt"],
            messages=messages,
            pool=payload["pool"],
            model=payload["model"],
            tools=payload.get("tools"),
            tool_dispatch=payload.get("tool_dispatch"),
            max_iterations=payload.get("max_iterations", DEFAULT_MAX_ITERATIONS),
            max_output_bytes=payload.get("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES),
            tool_timeout=payload.get("tool_timeout", DEFAULT_TOOL_TIMEOUT),
            llm_timeout=payload.get("llm_timeout", DEFAULT_LLM_TIMEOUT),
            temperature=payload.get("temperature"),
            json_mode=payload.get("json_mode", False),
            strip_thinking_blocks=payload.get("strip_thinking_blocks", False),
            max_total_tokens=payload.get("max_total_tokens", DEFAULT_MAX_TOTAL_TOKENS),
        )
        result["messages"] = messages
        return result
