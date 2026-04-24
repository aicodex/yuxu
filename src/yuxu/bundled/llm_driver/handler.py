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

from yuxu.core import session_log

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
    # v0.2 retry knobs. Kept as class attrs so tests (and users) can override.
    MAX_RETRIES_ON_PROVIDER_RATE_LIMIT = 3
    RETRY_BACKOFF_BASE_SEC = 1.0
    RETRY_BACKOFF_MAX_SEC = 30.0

    def __init__(self, bus, loader=None) -> None:
        self.bus = bus
        self.loader = loader

    async def _log_message(self, sender: Optional[str], entry: dict) -> None:
        """Append a message line to sender's session transcript. No-op if
        loader isn't wired (e.g. unit tests) or sender is unknown."""
        if not sender or self.loader is None:
            return
        spec = self.loader.specs.get(sender)
        if spec is None:
            return
        try:
            await session_log.append(spec.path, sender, {"event": "message", **entry})
        except Exception:
            log.exception("llm_driver: session_log.append(%s) raised", sender)

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
        agent: Optional[str] = None,
        cost_hint: Optional[float] = None,
        thinking: Any = None,
        max_tokens: Optional[int] = None,
        attachments: Optional[list[str]] = None,
    ) -> dict:
        dispatch = tool_dispatch or {}
        total_prompt = 0
        total_completion = 0
        total_elapsed_ms = 0.0
        total_retries = 0
        final_content: Optional[str] = None
        final_reasoning: Optional[str] = None
        stop_reason = "max_iter"
        error_msg: Optional[str] = None
        iterations_done = 0

        # Log the input messages (treat this run_turn call as one turn). Each
        # subsequent run_turn call from the same sender will append its own
        # input + outputs to the same JSONL; lifecycle lines separate runs.
        for m in messages:
            await self._log_message(agent, m)

        for it in range(max_iterations):
            iterations_done = it + 1
            api_messages: list[dict] = [{"role": "system", "content": system_prompt}]
            if attachments:
                for att in attachments:
                    api_messages.append({
                        "role": "user",
                        "content": f"<system-reminder>\n{att}\n</system-reminder>",
                    })
            api_messages.extend(messages)
            base_req: dict[str, Any] = {
                "pool": pool,
                "model": model,
                "messages": api_messages,
            }
            if tools:
                base_req["tools"] = [_to_openai_tool(t) for t in tools]
            if temperature is not None:
                base_req["temperature"] = temperature
            if json_mode:
                base_req["json_mode"] = True
            if strip_thinking_blocks:
                base_req["strip_thinking_blocks"] = True
            if agent is not None:
                base_req["agent"] = agent
            if cost_hint is not None:
                base_req["cost_hint"] = cost_hint
            if thinking is not None:
                base_req["thinking"] = thinking
            if max_tokens is not None:
                base_req["max_tokens"] = max_tokens

            resp, retries, fatal_error = await self._call_with_retry(
                base_req, llm_timeout, agent,
            )
            total_retries += retries
            if fatal_error is not None:
                stop_reason = "error"
                error_msg = fatal_error
                break

            if not resp.get("ok", True):  # non-retryable failure
                stop_reason = "error"
                error_msg = f"llm_service: {resp.get('error')}"
                break

            final_content = resp.get("content")
            # Keep the most recent non-empty reasoning for callers that want
            # a quick summary without reading the full transcript.
            iter_reasoning = resp.get("reasoning")
            if iter_reasoning:
                final_reasoning = iter_reasoning
            usage = resp.get("usage") or {}
            total_prompt += usage.get("prompt_tokens") or 0
            total_completion += usage.get("completion_tokens") or 0
            total_elapsed_ms += float(resp.get("elapsed_ms") or 0.0)

            # Log reasoning (Anthropic thinking blocks / DeepSeek
            # reasoning_content) BEFORE the assistant message so the
            # transcript reads in temporal order: user -> reasoning ->
            # assistant -> tool. Reasoning is NOT appended to `messages`
            # because most providers either bury it in content (MiniMax
            # OpenAI-path) or need it only for interleaved-thinking
            # multi-turn — which yuxu doesn't do yet.
            reasoning = resp.get("reasoning")
            if reasoning:
                await self._log_message(agent, {
                    "role": "assistant", "kind": "reasoning",
                    "content": reasoning, "iteration": it + 1,
                })

            asst_msg = _assistant_message(resp)
            messages.append(asst_msg)
            await self._log_message(agent, {**asst_msg, "iteration": it + 1})

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
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tc.get("id"),
                    "content": content,
                }
                messages.append(tool_msg)
                await self._log_message(agent, {**tool_msg, "tool_name": name,
                                                 "iteration": it + 1})

        # Aggregate throughput across iterations. elapsed_ms sums only the
        # llm_service call time (excludes tool dispatch + local work), so
        # output_tps reflects model throughput, not wall clock.
        output_tps: Optional[float] = None
        if total_elapsed_ms > 0 and total_completion > 0:
            output_tps = round(total_completion / (total_elapsed_ms / 1000.0), 2)
        return {
            "ok": stop_reason == "complete",
            "content": final_content,
            "reasoning": final_reasoning,
            "iterations": iterations_done,
            "stop_reason": stop_reason,
            "usage": {"prompt_tokens": total_prompt, "completion_tokens": total_completion},
            "elapsed_ms": round(total_elapsed_ms, 2),
            "output_tps": output_tps,
            "retries": total_retries,
            "error": error_msg,
        }

    async def _call_with_retry(
        self, base_req: dict, llm_timeout: float, sender: Optional[str],
    ) -> tuple[dict, int, Optional[str]]:
        """Call llm_service with provider-rate-limit retry + priority lane.

        Returns (response_dict, retries_used, fatal_error_msg). On non-retryable
        transport failure, response_dict is the last attempt and fatal_error_msg
        is set. On retryable rate-limit exhaustion, the final 429/1002 response
        is returned as-is (ok=False), no fatal error.
        """
        attempts = self.MAX_RETRIES_ON_PROVIDER_RATE_LIMIT + 1
        retries = 0
        last_resp: Optional[dict] = None
        for attempt in range(attempts):
            req = dict(base_req)
            if attempt > 0:
                req["priority"] = "retry"
            try:
                resp = await self.bus.request(
                    "llm_service", req, timeout=llm_timeout,
                    sender=sender,
                )
            except Exception as e:
                log.exception("llm_driver: bus.request to llm_service failed")
                return {}, retries, f"llm_service: {e}"
            last_resp = resp
            if resp.get("ok"):
                return resp, retries, None
            if resp.get("error_kind") != "provider_rate_limit":
                # Non-retryable failure — bubble it up unchanged.
                return resp, retries, None
            # Retryable. If we've hit the limit, give up and return the 429 resp.
            if attempt >= self.MAX_RETRIES_ON_PROVIDER_RATE_LIMIT:
                log.warning("llm_driver: exhausted %d retries on %s",
                            attempt, resp.get("error_code"))
                return resp, retries, None
            retries += 1
            # Backoff: exp unless provider gave Retry-After, then take the max.
            backoff = min(
                self.RETRY_BACKOFF_MAX_SEC,
                self.RETRY_BACKOFF_BASE_SEC * (2 ** attempt),
            )
            ra = resp.get("retry_after_sec")
            if ra is not None:
                try:
                    backoff = max(backoff, float(ra))
                except (TypeError, ValueError):
                    pass
            log.info(
                "llm_driver: %s on attempt %d, sleeping %.1fs before retry",
                resp.get("error_code"), attempt + 1, backoff,
            )
            await asyncio.sleep(backoff)
        return last_resp or {}, retries, None

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
        # Preserve the ORIGINAL caller identity across the driver→service hop
        # so rate_limit_service attributes to the real agent (e.g.
        # "reflection_agent"), not to llm_driver itself.
        original_sender = getattr(msg, "sender", None)
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
            agent=payload.get("agent") or original_sender,
            cost_hint=payload.get("cost_hint"),
            thinking=payload.get("thinking"),
            max_tokens=payload.get("max_tokens"),
            attachments=payload.get("attachments"),
        )
        result["messages"] = messages
        return result
