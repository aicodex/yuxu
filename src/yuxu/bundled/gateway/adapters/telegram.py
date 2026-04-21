"""Telegram adapter — httpx-based Bot API client (long-polling).

Minimal: sendMessage + getUpdates long-poll. No buttons, no attachments,
no streaming edits yet — add when a business need drives it.

Configuration (env vars, read by the gateway agent __init__):
    TELEGRAM_BOT_TOKEN          required, from @BotFather
    TELEGRAM_ALLOWED_USER_IDS   optional, comma-separated numeric ids
    TELEGRAM_API_BASE           optional, default https://api.telegram.org
    TELEGRAM_POLL_TIMEOUT       optional, seconds (default 25)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

from ..session import InboundMessage, SendResult, SessionSource
from .base import PlatformAdapter

log = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.telegram.org"
DEFAULT_POLL_TIMEOUT = 25


class TelegramAdapter(PlatformAdapter):
    platform = "telegram"
    supports_edit = True

    def __init__(self, bot_token: str, *,
                 allowed_user_ids: Optional[set[int]] = None,
                 api_base: str = DEFAULT_API_BASE,
                 poll_timeout: int = DEFAULT_POLL_TIMEOUT,
                 http_client: Optional[httpx.AsyncClient] = None) -> None:
        super().__init__()
        if not bot_token:
            raise ValueError("TelegramAdapter requires a bot_token")
        self._token = bot_token
        self._allowed = set(allowed_user_ids) if allowed_user_ids else None
        self._api_base = api_base.rstrip("/")
        self._poll_timeout = poll_timeout
        self._client = http_client
        self._owned_client = http_client is None
        self._offset = 0
        self._poll_task: Optional[asyncio.Task] = None
        self._stopping = False

    # ---- lifecycle ----

    async def connect(self) -> None:
        self._stopping = False
        self._client = self._client or httpx.AsyncClient()
        self._poll_task = asyncio.create_task(self._poll_loop(),
                                              name="gateway.telegram.poll")

    async def disconnect(self) -> None:
        self._stopping = True
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await asyncio.wait_for(self._poll_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        if self._client is not None and self._owned_client:
            await self._client.aclose()
        self._client = None

    # ---- outbound ----

    async def send(self, source: SessionSource, text: str, *,
                   reply_to_message_id: Optional[str] = None,
                   parse_mode: Optional[str] = None) -> SendResult:
        body: dict = {"chat_id": source.chat_id, "text": text}
        if parse_mode:
            body["parse_mode"] = parse_mode
        if reply_to_message_id is not None:
            body["reply_to_message_id"] = int(reply_to_message_id)
        try:
            data = await self._post("sendMessage", body)
        except Exception as e:
            return SendResult(ok=False, error=str(e))
        if not data.get("ok"):
            return SendResult(ok=False, error=str(data.get("description")))
        msg_id = data.get("result", {}).get("message_id")
        return SendResult(ok=True,
                          message_id=str(msg_id) if msg_id is not None else None)

    async def edit(self, source: SessionSource, message_id: str, text: str, *,
                   finalize: bool = False,
                   parse_mode: Optional[str] = None) -> SendResult:
        body: dict = {
            "chat_id": source.chat_id,
            "message_id": int(message_id),
            "text": text,
        }
        if parse_mode:
            body["parse_mode"] = parse_mode
        try:
            data = await self._post("editMessageText", body)
        except Exception as e:
            return SendResult(ok=False, error=str(e))
        if not data.get("ok"):
            desc = str(data.get("description") or "")
            # Benign: same-content edits get "message is not modified".
            if "not modified" in desc:
                return SendResult(ok=True, message_id=message_id)
            return SendResult(ok=False, error=desc)
        return SendResult(ok=True, message_id=message_id)

    async def render_draft(self, source: SessionSource, draft, *,
                           message_id: Optional[str],
                           finalize: bool) -> SendResult:
        if draft.is_empty():
            return SendResult(ok=True, message_id=message_id)
        html = _render_draft_telegram_html(draft)
        if message_id is None:
            return await self.send(source, html, parse_mode="HTML")
        return await self.edit(source, message_id, html, finalize=finalize,
                               parse_mode="HTML")

    # ---- internal ----

    async def _post(self, method: str, body: dict) -> dict:
        assert self._client is not None
        url = f"{self._api_base}/bot{self._token}/{method}"
        resp = await self._client.post(url, json=body, timeout=30.0)
        resp.raise_for_status()
        return resp.json()

    async def _poll_loop(self) -> None:
        while not self._stopping:
            try:
                data = await self._get_updates()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("telegram: getUpdates failed; retrying")
                await asyncio.sleep(3.0)
                continue
            if not data.get("ok"):
                log.warning("telegram: getUpdates returned ok=false: %s",
                            data.get("description"))
                await asyncio.sleep(3.0)
                continue
            for update in data.get("result") or []:
                self._offset = max(self._offset, int(update["update_id"]) + 1)
                await self._dispatch_update(update)
            # Cooperative yield so cancel-during-test and tight-loop tests
            # don't starve the event loop when the transport returns instantly.
            await asyncio.sleep(0)

    async def _get_updates(self) -> dict:
        assert self._client is not None
        url = f"{self._api_base}/bot{self._token}/getUpdates"
        params = {"timeout": self._poll_timeout, "offset": self._offset}
        resp = await self._client.get(
            url, params=params, timeout=self._poll_timeout + 5
        )
        resp.raise_for_status()
        return resp.json()

    async def _dispatch_update(self, update: dict) -> None:
        msg = update.get("message") or update.get("edited_message")
        if not msg or "text" not in msg:
            return  # stickers/photos/etc. — ignored for MVP
        from_user = msg.get("from") or {}
        user_id = str(from_user.get("id")) if "id" in from_user else None
        if self._allowed is not None and from_user.get("id") not in self._allowed:
            log.info("telegram: ignoring disallowed user %s", user_id)
            return
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id"))
        chat_type = chat.get("type", "private")
        thread_id = msg.get("message_thread_id")
        reply_to = msg.get("reply_to_message", {}).get("message_id")
        inbound = InboundMessage(
            source=SessionSource(
                platform=self.platform,
                chat_id=chat_id,
                user_id=user_id,
                thread_id=str(thread_id) if thread_id else None,
                chat_type=_telegram_chat_type(chat_type),
            ),
            text=msg["text"],
            reply_to_message_id=str(reply_to) if reply_to is not None else None,
            raw=update,
        )
        await self._deliver(inbound)


def _telegram_chat_type(t: str) -> str:
    if t == "private":
        return "dm"
    if t in ("group", "supergroup"):
        return "group"
    if t == "channel":
        return "channel"
    return t


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_draft_telegram_html(draft) -> str:
    """Render a DraftMessage to Telegram-flavored HTML.

    Telegram Bot API (>= 7.0) supports <blockquote>, <b>, <i>, <code>, <pre>,
    <a href>, <s>, <u>, <tg-spoiler>. We only use the first four.
    """
    parts: list[str] = []

    if draft.quote_user and draft.quote_text:
        header = f"回复 {_html_escape(draft.quote_user)}: {_html_escape(draft.quote_text.splitlines()[0] if draft.quote_text else '')}"
        rest = "\n".join(_html_escape(line) for line in draft.quote_text.splitlines()[1:])
        body = header + ("\n" + rest if rest else "")
        parts.append(f"<blockquote>{body}</blockquote>")

    if draft.thinking:
        parts.append("💭 <b>Thinking</b>")
        parts.append(f"<blockquote>{_html_escape(draft.thinking)}</blockquote>")

    if draft.content:
        parts.append(_html_escape(draft.content))

    if draft.footer_meta:
        parts.append("")
        parts.append("――――――――――――")
        meta = " | ".join(f"{k}: {v}" for k, v in draft.footer_meta)
        parts.append(f"<i>{_html_escape(meta)}</i>")

    return "\n".join(parts)
