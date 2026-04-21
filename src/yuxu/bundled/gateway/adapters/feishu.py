"""Feishu / Lark adapter — outbound-only for now.

Ships:
- Tenant-access-token mgmt (app_id + app_secret → token, auto-refresh ~5min
  before expiry)
- Send plain text (msg_type=text)
- Send / edit interactive card (msg_type=interactive) — drives the
  draft-rendering pipeline (quote / 💭thinking / content / footer)
- Reply-to via the /messages/{msg_id}/reply endpoint

Deferred:
- Inbound event webhook (Feishu requires HTTPS public endpoint; the
  embedded HTTP server is a separate adapter layer)
- Stream-card API (we use message PATCH for edit; for long responses
  that's enough and keeps the adapter simple)

Environment variables (wired in gateway/__init__.py):
    FEISHU_APP_ID        required
    FEISHU_APP_SECRET    required
    FEISHU_API_BASE      default https://open.feishu.cn
                         (use https://open.larksuite.com for international Lark)
    FEISHU_RECEIVE_ID_TYPE  default chat_id (also: open_id, user_id, email, union_id)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

import httpx

from ..draft import DraftMessage, combine_draft_markdown
from ..session import SendResult, SessionSource
from .base import PlatformAdapter

log = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://open.feishu.cn"
TOKEN_REFRESH_MARGIN = 300.0      # refresh 5 min before nominal expiry
TOKEN_RETRY_BACKOFF = 30.0


class FeishuAdapter(PlatformAdapter):
    platform = "feishu"
    supports_edit = True

    def __init__(self, app_id: str, app_secret: str, *,
                 api_base: str = DEFAULT_API_BASE,
                 default_receive_id_type: str = "chat_id",
                 http_client: Optional[httpx.AsyncClient] = None) -> None:
        super().__init__()
        if not app_id or not app_secret:
            raise ValueError("FeishuAdapter requires app_id and app_secret")
        self._app_id = app_id
        self._app_secret = app_secret
        self._api_base = api_base.rstrip("/")
        self._receive_id_type = default_receive_id_type
        self._client = http_client
        self._owned_client = http_client is None
        self._token: Optional[str] = None
        self._token_exp: float = 0.0
        self._refresh_task: Optional[asyncio.Task] = None
        self._stopping = False

    # ---- lifecycle --------------------------------------------

    async def connect(self) -> None:
        self._stopping = False
        self._client = self._client or httpx.AsyncClient()
        await self._refresh_token()
        self._refresh_task = asyncio.create_task(
            self._refresh_loop(), name="gateway.feishu.refresh",
        )

    async def disconnect(self) -> None:
        self._stopping = True
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_task.cancel()
            try:
                await asyncio.wait_for(self._refresh_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        if self._client is not None and self._owned_client:
            await self._client.aclose()
        self._client = None

    # ---- token management -------------------------------------

    async def _refresh_token(self) -> None:
        assert self._client is not None
        url = f"{self._api_base}/open-apis/auth/v3/tenant_access_token/internal"
        body = {"app_id": self._app_id, "app_secret": self._app_secret}
        resp = await self._client.post(url, json=body, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"feishu auth failed: {data}")
        self._token = data["tenant_access_token"]
        self._token_exp = time.time() + float(data.get("expire", 7200))

    async def _refresh_loop(self) -> None:
        while not self._stopping:
            try:
                sleep_for = max(60.0, (self._token_exp - time.time()) - TOKEN_REFRESH_MARGIN)
                await asyncio.sleep(sleep_for)
                if self._stopping:
                    return
                await self._refresh_token()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("feishu: token refresh failed; backing off")
                await asyncio.sleep(TOKEN_RETRY_BACKOFF)

    async def _ensure_token(self) -> None:
        if self._token is None or time.time() >= self._token_exp - 60.0:
            await self._refresh_token()

    def _auth_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    # ---- outbound ---------------------------------------------

    async def send(self, source: SessionSource, text: str, *,
                   reply_to_message_id: Optional[str] = None) -> SendResult:
        content = json.dumps({"text": text}, ensure_ascii=False)
        if reply_to_message_id:
            return await self._reply(reply_to_message_id, "text", content)
        return await self._send_raw(source, "text", content)

    async def edit(self, source: SessionSource, message_id: str, text: str, *,
                   finalize: bool = False) -> SendResult:
        content = json.dumps({"text": text}, ensure_ascii=False)
        return await self._patch_message(message_id, content)

    async def render_draft(self, source: SessionSource, draft: DraftMessage, *,
                           message_id: Optional[str],
                           finalize: bool) -> SendResult:
        if draft.is_empty() and not finalize:
            return SendResult(ok=True, message_id=message_id)
        card = _build_feishu_card(draft)
        content = json.dumps(card, ensure_ascii=False)
        if message_id is None:
            return await self._send_raw(source, "interactive", content)
        return await self._patch_message(message_id, content)

    # ---- HTTP internals ---------------------------------------

    async def _send_raw(self, source: SessionSource, msg_type: str,
                        content: str) -> SendResult:
        try:
            await self._ensure_token()
        except Exception as e:
            return SendResult(ok=False, error=f"auth: {e}")
        assert self._client is not None
        url = f"{self._api_base}/open-apis/im/v1/messages"
        params = {"receive_id_type": self._receive_id_type}
        body = {
            "receive_id": source.chat_id,
            "msg_type": msg_type,
            "content": content,
        }
        try:
            resp = await self._client.post(url, params=params, json=body,
                                            headers=self._auth_headers(),
                                            timeout=30.0)
        except httpx.RequestError as e:
            return SendResult(ok=False, error=f"request: {e}")
        return _parse_send_response(resp)

    async def _reply(self, parent_id: str, msg_type: str,
                     content: str) -> SendResult:
        try:
            await self._ensure_token()
        except Exception as e:
            return SendResult(ok=False, error=f"auth: {e}")
        assert self._client is not None
        url = f"{self._api_base}/open-apis/im/v1/messages/{parent_id}/reply"
        body = {"msg_type": msg_type, "content": content}
        try:
            resp = await self._client.post(url, json=body,
                                            headers=self._auth_headers(),
                                            timeout=30.0)
        except httpx.RequestError as e:
            return SendResult(ok=False, error=f"request: {e}")
        return _parse_send_response(resp)

    async def _patch_message(self, message_id: str, content: str) -> SendResult:
        try:
            await self._ensure_token()
        except Exception as e:
            return SendResult(ok=False, error=f"auth: {e}")
        assert self._client is not None
        url = f"{self._api_base}/open-apis/im/v1/messages/{message_id}"
        body = {"content": content}
        try:
            resp = await self._client.patch(url, json=body,
                                              headers=self._auth_headers(),
                                              timeout=30.0)
        except httpx.RequestError as e:
            return SendResult(ok=False, error=f"request: {e}")
        if resp.status_code >= 400:
            return SendResult(ok=False,
                              error=f"HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
        except Exception as e:
            return SendResult(ok=False, error=f"decode: {e}")
        if data.get("code") != 0:
            return SendResult(ok=False, error=str(data.get("msg") or data))
        return SendResult(ok=True, message_id=message_id)


def _parse_send_response(resp: httpx.Response) -> SendResult:
    if resp.status_code >= 400:
        return SendResult(ok=False,
                          error=f"HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        data = resp.json()
    except Exception as e:
        return SendResult(ok=False, error=f"decode: {e}")
    if data.get("code") != 0:
        return SendResult(ok=False, error=str(data.get("msg") or data))
    msg_id = (data.get("data") or {}).get("message_id")
    return SendResult(ok=True, message_id=msg_id)


def _build_feishu_card(draft: DraftMessage) -> dict:
    """Feishu interactive card Schema 2.0.

    Layout mirrors the OpenClaw feishu-extension pattern:
        [markdown block: quote + 💭thinking + content]
        [hr + grey markdown footer]
    """
    body_text = combine_draft_markdown(draft, include_footer=False)
    if not body_text:
        body_text = " "   # Feishu rejects empty markdown content
    elements: list[dict] = [{"tag": "markdown", "content": body_text}]
    if draft.footer_meta:
        elements.append({"tag": "hr"})
        meta_line = " | ".join(f"{k}: {v}" for k, v in draft.footer_meta)
        elements.append({
            "tag": "markdown",
            "content": f"<font color='grey'>{meta_line}</font>",
        })
    return {
        "schema": "2.0",
        "config": {"width_mode": "fill"},
        "body": {"elements": elements},
    }
