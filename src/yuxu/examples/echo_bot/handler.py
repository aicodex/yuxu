"""EchoBot — listens on gateway.user_message, replies with a streaming draft.

No real LLM — mocks the `thinking → content` phases so you can verify the
gateway UX end-to-end in one terminal without any API keys.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)

THINKING_CHUNKS = [
    "Received user input. ",
    "In mock mode I don't actually plan, ",
    "just echoing it back with a friendly tone.",
]

TYPING_DELAY = 0.2       # 秒 per 流式 chunk, 够短避免 demo 乏味
CHUNK_FLUSH_EACH = True  # True → 每 append 一次 flush 一次（看刷新效果）


class EchoBot:
    def __init__(self, ctx) -> None:
        self.ctx = ctx

    def install(self) -> None:
        self.ctx.bus.subscribe("gateway.user_message", self._on_user_message)

    async def _on_user_message(self, event: dict) -> None:
        payload = event.get("payload") if isinstance(event, dict) else None
        if not isinstance(payload, dict):
            return
        session_key = payload.get("session_key", "")
        source = payload.get("source") or {}
        text = str(payload.get("text", "")).strip()
        if not text:
            return

        gw = self.ctx.get_agent("gateway")
        if gw is None:
            log.warning("echo_bot: gateway handle not available; dropping")
            return

        user_label = source.get("user_id") or "user"
        try:
            async with gw.open_draft(
                session_key=session_key,
                quote_user=str(user_label),
                quote_text=text,
                footer_meta=[("Agent", "echo_bot"), ("Mode", "mock")],
                throttle_seconds=0.05,
            ) as draft:
                for chunk in THINKING_CHUNKS:
                    draft.append_thinking(chunk)
                    if CHUNK_FLUSH_EACH:
                        await draft.flush()
                    await asyncio.sleep(TYPING_DELAY)
                for chunk in (
                    "You said: ",
                    f'"{text}"',
                    f". Hi {user_label}! 👋",
                ):
                    draft.append_content(chunk)
                    if CHUNK_FLUSH_EACH:
                        await draft.flush()
                    await asyncio.sleep(TYPING_DELAY)
        except Exception:
            log.exception("echo_bot: draft flow failed")
