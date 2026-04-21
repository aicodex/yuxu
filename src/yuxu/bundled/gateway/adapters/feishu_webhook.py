"""Feishu event-subscription HTTP server (aiohttp).

Runs inside the FeishuAdapter when webhook_host/webhook_port are set.
Exposes a single POST endpoint; verifies, decrypts, and dispatches events
to the registered callback. The URL challenge handshake is answered
automatically.

For HTTPS termination you put a reverse proxy in front (nginx / caddy /
tailscale funnel). The adapter itself only listens HTTP — Feishu's open
platform requires the externally visible URL be HTTPS.
"""
from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable, Optional

from aiohttp import web

from .feishu_events import (
    VerifyError,
    is_url_verification,
    unwrap_event,
    url_verification_response,
    verify_signature,
    verify_token,
)

log = logging.getLogger(__name__)

EventHandler = Callable[[dict], Awaitable[None]]


class FeishuWebhook:
    """aiohttp-backed webhook listener.

    Verification order on each POST:
        1. If signature headers present AND encrypt_key set → HMAC check
        2. JSON parse
        3. If `encrypt` field present → decrypt (requires encrypt_key)
        4. If url_verification → respond with {challenge}
        5. If verification_token configured → check event.token (v1) or header.token (v2)
        6. Dispatch to on_event(event) — any exception is logged, not
           propagated (Feishu just needs HTTP 200 to stop retries)
    """

    def __init__(self, *, host: str = "0.0.0.0", port: int = 8080,
                 path: str = "/feishu/webhook",
                 verification_token: str = "",
                 encrypt_key: str = "",
                 on_event: Optional[EventHandler] = None) -> None:
        self.host = host
        self.port = port
        self.path = path if path.startswith("/") else "/" + path
        self.verification_token = verification_token
        self.encrypt_key = encrypt_key
        self.on_event = on_event
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.BaseSite] = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post(self.path, self._handle_post)
        # liveness probe — easy way to confirm the server is up
        app.router.add_get(self.path + "/health", self._handle_health)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        log.info("feishu webhook listening http://%s:%d%s",
                 self.host, self.port, self.path)

    async def stop(self) -> None:
        if self._site is not None:
            try:
                await self._site.stop()
            except Exception:
                log.exception("feishu webhook site stop raised")
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                log.exception("feishu webhook runner cleanup raised")
        self._site = None
        self._runner = None

    # ---- handlers -------------------------------------------

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def _handle_post(self, request: web.Request) -> web.Response:
        body = await request.read()

        # Optional HMAC check (Feishu sets these when encrypt_key is active).
        sig = request.headers.get("X-Lark-Signature")
        if sig and self.encrypt_key:
            ts = request.headers.get("X-Lark-Request-Timestamp", "")
            nonce = request.headers.get("X-Lark-Request-Nonce", "")
            try:
                verify_signature(timestamp=ts, nonce=nonce, body=body,
                                 encrypt_key=self.encrypt_key, signature=sig)
            except VerifyError as e:
                log.warning("feishu webhook: signature rejected: %s", e)
                return web.json_response({"error": "bad signature"}, status=403)

        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            return web.json_response({"error": f"bad json: {e}"}, status=400)

        # Decrypt if encrypted
        try:
            event = unwrap_event(data, self.encrypt_key or None)
        except VerifyError as e:
            log.warning("feishu webhook: decrypt failed: %s", e)
            return web.json_response({"error": str(e)}, status=403)

        # Handshake first — Feishu sends url_verification when setting up.
        if is_url_verification(event):
            return web.json_response(url_verification_response(event))

        # Plaintext token check (v1 events carry `token`; v2 carries
        # `header.token`). Encrypted events still may include a token;
        # we verify it when configured.
        if self.verification_token:
            try:
                verify_token(event, self.verification_token)
            except VerifyError as e:
                log.warning("feishu webhook: token rejected: %s", e)
                return web.json_response({"error": "bad token"}, status=403)

        if self.on_event is not None:
            try:
                await self.on_event(event)
            except Exception:
                log.exception("feishu webhook: on_event handler raised")

        return web.json_response({"ok": True})
