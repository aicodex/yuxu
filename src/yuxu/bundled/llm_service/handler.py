"""LLMService — OpenAI-compatible chat completions HTTP client.

Takes a `rate_limiter` callable (pool, tokens) -> async context manager,
typically `rate_limit_service.acquire`. Returns a normalized response dict.
No streaming, no retries in MVP.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional

import httpx

log = logging.getLogger(__name__)


class LLMServiceError(Exception):
    pass


class LLMService:
    DEFAULT_TIMEOUT = 60.0

    def __init__(self, rate_limiter: Callable, *,
                 default_timeout: Optional[float] = None,
                 client: Optional[httpx.AsyncClient] = None) -> None:
        self.rate_limiter = rate_limiter  # (pool, tokens=1) -> async ctx manager
        self.default_timeout = default_timeout or self.DEFAULT_TIMEOUT
        self._client = client
        self._owned_client = client is None

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
                   timeout: Optional[float] = None) -> dict:
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
            try:
                resp = await client.post(url, json=body, headers=headers,
                                         timeout=timeout or self.default_timeout)
            except httpx.RequestError as e:
                raise LLMServiceError(f"request error: {e}") from e
            if resp.status_code >= 400:
                text = resp.text
                raise LLMServiceError(f"HTTP {resp.status_code}: {text[:300]}")
            try:
                api_resp = resp.json()
            except json.JSONDecodeError as e:
                raise LLMServiceError(f"non-JSON response: {e}") from e
            return self._normalize(api_resp)

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
            )
            return {"ok": True, **result}
        except LLMServiceError as e:
            return {"ok": False, "error": str(e)}
        except KeyError as e:
            return {"ok": False, "error": f"unknown pool: {e.args[0]}"}
