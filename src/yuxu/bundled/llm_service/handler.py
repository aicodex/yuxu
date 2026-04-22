"""LLMService — OpenAI-compatible chat completions HTTP client.

Takes a `rate_limiter` callable (pool, tokens) -> async context manager,
typically `rate_limit_service.acquire`. Returns a normalized response dict.
No streaming, no retries in MVP.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable, Optional

import httpx

log = logging.getLogger(__name__)

# Match <think>...</think>, <thinking>...</thinking>, with optional attributes,
# case-insensitive, dotall (spans newlines). Also greedy-tolerant: strips a
# leading unclosed <think> opener as a last resort (some providers emit it
# without a closing tag when truncated).
_THINK_BLOCK_RE = re.compile(
    r"<think(?:ing)?\b[^>]*>.*?</think(?:ing)?>",
    re.DOTALL | re.IGNORECASE,
)
_THINK_OPEN_RE = re.compile(r"<think(?:ing)?\b[^>]*>", re.IGNORECASE)


def _strip_thinking_blocks(content: Optional[str]) -> Optional[str]:
    """Remove <think>…</think> / <thinking>…</thinking> spans from LLM output.

    Some providers (notably MiniMax) leak thinking blocks even when the
    system prompt forbids them. Strip them at the service boundary so
    callers see clean content.
    """
    if not content:
        return content
    cleaned = _THINK_BLOCK_RE.sub("", content)
    # If a stray opener remains (truncated mid-thinking), drop everything from
    # that point to a following blank line, which is conservative but avoids
    # leaking partial reasoning.
    m = _THINK_OPEN_RE.search(cleaned)
    if m:
        cleaned = cleaned[: m.start()]
    return cleaned.strip()


class LLMServiceError(Exception):
    pass


class LLMService:
    DEFAULT_TIMEOUT = 60.0
    COMPLETION_TOPIC = "llm_service.request_completed"

    def __init__(self, rate_limiter: Callable, *,
                 default_timeout: Optional[float] = None,
                 client: Optional[httpx.AsyncClient] = None,
                 bus: Any = None) -> None:
        self.rate_limiter = rate_limiter  # (pool, tokens=1) -> async ctx manager
        self.default_timeout = default_timeout or self.DEFAULT_TIMEOUT
        self._client = client
        self._owned_client = client is None
        # Optional bus for observability. When set, handle() publishes
        # `llm_service.request_completed` on every successful call so
        # trackers (minimax_budget, resource_guardian, ...) can attribute
        # usage per agent without hooking chat() directly.
        self.bus = bus

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
            self._owned_client = True
        return self._client

    async def chat(self, *, pool: str, model: str, messages: list[dict],
                   tools: Optional[list[dict]] = None,
                   temperature: Optional[float] = None,
                   json_mode: bool = False,
                   extra_body: Optional[dict] = None,
                   timeout: Optional[float] = None,
                   strip_thinking_blocks: bool = False) -> dict:
        async with self.rate_limiter(pool) as ctx:
            if not isinstance(ctx, dict):
                raise LLMServiceError(
                    f"rate limiter returned no account for pool {pool!r}; "
                    "ensure rate_limit_service is configured for this pool"
                )
            extra = ctx.get("extra") or {}
            api_key = extra.get("api_key")
            base_url = extra.get("base_url", "")
            if not api_key:
                raise LLMServiceError(
                    f"account {ctx.get('account')!r} in pool {pool!r} has no api_key"
                )
            if not base_url:
                raise LLMServiceError(
                    f"account {ctx.get('account')!r} in pool {pool!r} has no base_url"
                )
            url = base_url.rstrip("/") + "/chat/completions"
            body: dict[str, Any] = {"model": model, "messages": messages}
            if temperature is not None:
                body["temperature"] = temperature
            if tools:
                body["tools"] = tools
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            if extra_body:
                body.update(extra_body)
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            client = self._get_client()
            t0 = time.perf_counter()
            try:
                resp = await client.post(url, json=body, headers=headers,
                                         timeout=timeout or self.default_timeout)
            except httpx.RequestError as e:
                raise LLMServiceError(f"request error: {e}") from e
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            if resp.status_code >= 400:
                text = resp.text
                raise LLMServiceError(f"HTTP {resp.status_code}: {text[:300]}")
            try:
                api_resp = resp.json()
            except json.JSONDecodeError as e:
                raise LLMServiceError(f"non-JSON response: {e}") from e
            normalized = self._normalize(api_resp)
            if strip_thinking_blocks:
                normalized["content"] = _strip_thinking_blocks(
                    normalized.get("content")
                )
            # Enrich with client-measured timing. output_tps is a LOWER BOUND
            # since elapsed includes network + prompt processing (non-streaming
            # can't separate TTFT from stream time). True TPS needs streaming.
            normalized["elapsed_ms"] = round(elapsed_ms, 2)
            usage = normalized.get("usage") or {}
            completion_tokens = int(usage.get("completion_tokens") or 0)
            if elapsed_ms > 0 and completion_tokens > 0:
                normalized["output_tps"] = round(
                    completion_tokens / (elapsed_ms / 1000.0), 2,
                )
            else:
                normalized["output_tps"] = None
            return normalized

    def _normalize(self, api_resp: dict) -> dict:
        choices = api_resp.get("choices") or []
        if not choices:
            return {
                "content": None,
                "tool_calls": [],
                "stop_reason": "end_turn",
                "usage": api_resp.get("usage") or {},
            }
        choice = choices[0]
        msg = choice.get("message") or {}
        finish = choice.get("finish_reason") or "stop"
        tool_calls: list[dict] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append({
                "id": tc.get("id"),
                "name": fn.get("name"),
                "input": args,
            })
        stop_reason = "tool_use" if tool_calls else (
            "end_turn" if finish in ("stop", "end_turn") else finish
        )
        return {
            "content": msg.get("content"),
            "tool_calls": tool_calls,
            "stop_reason": stop_reason,
            "usage": api_resp.get("usage") or {},
        }

    async def close(self) -> None:
        if self._client is not None and self._owned_client:
            await self._client.aclose()
            self._client = None

    async def handle(self, msg) -> dict:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        op = payload.get("op", "chat")
        if op != "chat":
            return {"ok": False, "error": f"unknown op: {op!r}"}
        required = ("pool", "model", "messages")
        missing = [k for k in required if k not in payload]
        if missing:
            return {"ok": False, "error": f"missing fields: {missing}"}
        try:
            result = await self.chat(
                pool=payload["pool"],
                model=payload["model"],
                messages=payload["messages"],
                tools=payload.get("tools"),
                temperature=payload.get("temperature"),
                json_mode=payload.get("json_mode", False),
                extra_body=payload.get("extra_body"),
                timeout=payload.get("timeout"),
                strip_thinking_blocks=payload.get("strip_thinking_blocks", False),
            )
        except LLMServiceError as e:
            return {"ok": False, "error": str(e)}
        except KeyError as e:
            return {"ok": False, "error": f"unknown pool: {e.args[0]}"}

        # Fire-and-forget observability event. Best-effort: any failure in the
        # publish path must not break the actual LLM reply.
        if self.bus is not None:
            try:
                await self.bus.publish(self.COMPLETION_TOPIC, {
                    "agent": getattr(msg, "sender", None) or "unknown",
                    "pool": payload["pool"],
                    "model": payload["model"],
                    "usage": result.get("usage") or {},
                    "stop_reason": result.get("stop_reason"),
                    "elapsed_ms": result.get("elapsed_ms"),
                    "output_tps": result.get("output_tps"),
                })
            except Exception:
                log.exception("llm_service: failed to publish %s",
                              self.COMPLETION_TOPIC)
        return {"ok": True, **result}
