"""Telegram webhook receiver.

Reverse-proxy Telegram's POST-based update push:
    POST {webhook_path}   Content-Type: application/json
    Body: a single Update object (same schema as getUpdates result items)

If `secret_token` is configured, Telegram sends it in
`X-Telegram-Bot-Api-Secret-Token`; we reject anything else with 403.

For HTTPS termination you put a reverse proxy in front (nginx / caddy /
tailscale funnel / ngrok). Telegram requires HTTPS on port 443/80/88/8443
for the **external** URL; the internal port bound here can be anything
you route to.
"""
from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable, Optional

from aiohttp import web

log = logging.getLogger(__name__)

UpdateHandler = Callable[[dict], Awaitable[None]]


class TelegramWebhook:
    def __init__(self, *, host: str = "0.0.0.0", port: int = 8443,
                 path: str = "/telegram/webhook",
                 secret_token: str = "",
                 on_update: Optional[UpdateHandler] = None) -> None:
        self.host = host
        self.port = port
        self.path = path if path.startswith("/") else "/" + path
        self.secret_token = secret_token
        self.on_update = on_update
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.BaseSite] = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post(self.path, self._handle_post)
        app.router.add_get(self.path + "/health", self._handle_health)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        log.info("telegram webhook listening http://%s:%d%s",
                 self.host, self.port, self.path)

    async def stop(self) -> None:
        if self._site is not None:
            try:
                await self._site.stop()
            except Exception:
                log.exception("telegram webhook site stop raised")
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                log.exception("telegram webhook runner cleanup raised")
        self._site = None
        self._runner = None

    # ---- handlers -------------------------------------------

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def _handle_post(self, request: web.Request) -> web.Response:
        if self.secret_token:
            got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if got != self.secret_token:
                log.warning("telegram webhook: secret-token mismatch")
                return web.json_response({"error": "bad secret"}, status=403)
        try:
            body = await request.read()
            update = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            return web.json_response({"error": f"bad json: {e}"}, status=400)

        if not isinstance(update, dict):
            return web.json_response({"error": "expected object"}, status=400)

        if self.on_update is not None:
            try:
                await self.on_update(update)
            except Exception:
                log.exception("telegram webhook: on_update raised")

        return web.json_response({"ok": True})
