"""Tests for Telegram webhook mode.

Covers:
  - TelegramWebhook server: secret-token gate, bad JSON, dispatch
  - TelegramAdapter in webhook mode: setWebhook/deleteWebhook calls, inbound
    update → _deliver, mode exclusivity (no long-poll when webhook active)
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from yuxu.bundled.gateway.adapters.telegram import TelegramAdapter
from yuxu.bundled.gateway.adapters.telegram_webhook import TelegramWebhook
from yuxu.bundled.gateway.session import InboundMessage

pytestmark = pytest.mark.asyncio


# -- webhook server standalone ---------------------------------


async def _run_webhook_and_post(wh: TelegramWebhook, *,
                                  body: bytes,
                                  headers: dict | None = None,
                                  method: str = "POST") -> tuple[int, dict]:
    wh.host = "127.0.0.1"
    wh.port = 0
    await wh.start()
    try:
        sock = wh._site._server.sockets[0]
        port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}{wh.path}"
        async with httpx.AsyncClient() as c:
            if method == "GET":
                resp = await c.get(url, headers=headers or {}, timeout=5.0)
            else:
                resp = await c.post(url, content=body,
                                      headers=headers or {"Content-Type": "application/json"},
                                      timeout=5.0)
        return resp.status_code, resp.json()
    finally:
        await wh.stop()


async def test_webhook_accepts_valid_update():
    received = []

    async def on_update(u): received.append(u)

    wh = TelegramWebhook(on_update=on_update)
    body = json.dumps({"update_id": 1, "message": {"message_id": 1}}).encode()
    status, data = await _run_webhook_and_post(wh, body=body)
    assert status == 200
    assert data == {"ok": True}
    assert received[0]["update_id"] == 1


async def test_webhook_secret_token_required_when_set():
    received = []
    wh = TelegramWebhook(secret_token="s3cret",
                          on_update=lambda u: received.append(u) or asyncio.sleep(0))
    body = json.dumps({"update_id": 2}).encode()
    # missing
    status, _ = await _run_webhook_and_post(wh, body=body)
    assert status == 403
    # wrong
    status, _ = await _run_webhook_and_post(wh, body=body, headers={
        "Content-Type": "application/json",
        "X-Telegram-Bot-Api-Secret-Token": "wrong",
    })
    assert status == 403


async def test_webhook_secret_token_ok():
    received = []

    async def on_update(u): received.append(u)

    wh = TelegramWebhook(secret_token="s3cret", on_update=on_update)
    body = json.dumps({"update_id": 3}).encode()
    status, _ = await _run_webhook_and_post(wh, body=body, headers={
        "Content-Type": "application/json",
        "X-Telegram-Bot-Api-Secret-Token": "s3cret",
    })
    assert status == 200
    assert received[0]["update_id"] == 3


async def test_webhook_bad_json():
    wh = TelegramWebhook()
    status, _ = await _run_webhook_and_post(wh, body=b"{not json")
    assert status == 400


async def test_webhook_non_object_rejected():
    wh = TelegramWebhook()
    status, _ = await _run_webhook_and_post(wh, body=json.dumps([1, 2]).encode())
    assert status == 400


async def test_webhook_health_endpoint():
    wh = TelegramWebhook()
    wh.host = "127.0.0.1"
    wh.port = 0
    await wh.start()
    try:
        sock = wh._site._server.sockets[0]
        port = sock.getsockname()[1]
        async with httpx.AsyncClient() as c:
            resp = await c.get(f"http://127.0.0.1:{port}{wh.path}/health")
        assert resp.status_code == 200
    finally:
        await wh.stop()


async def test_webhook_handler_exception_still_returns_200():
    """Telegram retries forever on non-200. Handler crashes must not leak."""
    async def bad(u): raise RuntimeError("boom")
    wh = TelegramWebhook(on_update=bad)
    status, data = await _run_webhook_and_post(
        wh, body=json.dumps({"update_id": 99}).encode(),
    )
    assert status == 200
    assert data == {"ok": True}


# -- TelegramAdapter in webhook mode ---------------------------


def _tg_routes():
    """MockTransport: record setWebhook / deleteWebhook, echo send."""
    calls: list[tuple[str, dict]] = []

    def route(req: httpx.Request):
        path = req.url.path
        body = json.loads(req.content) if req.content else {}
        if path.endswith("/setWebhook"):
            calls.append(("setWebhook", body))
            return httpx.Response(200, json={"ok": True, "result": True})
        if path.endswith("/deleteWebhook"):
            calls.append(("deleteWebhook", body))
            return httpx.Response(200, json={"ok": True, "result": True})
        if path.endswith("/sendMessage"):
            return httpx.Response(200, json={"ok": True,
                                              "result": {"message_id": 1}})
        if path.endswith("/getUpdates"):
            # If long-poll is active, we'd hit this. Tests assert it's NOT
            # hit in webhook mode.
            return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(404)
    return route, calls


async def test_adapter_webhook_mode_calls_setWebhook_on_connect():
    route, calls = _tg_routes()
    adapter = TelegramAdapter(
        bot_token="ABC",
        webhook_host="127.0.0.1", webhook_port=0,
        webhook_public_url="https://example.com/tg/webhook",
        webhook_secret_token="sec",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(route)),
    )
    adapter._owned_client = True
    await adapter.connect()
    try:
        assert adapter._webhook is not None          # server up
        assert adapter._poll_task is None             # no long-poll
        sw = [c for c in calls if c[0] == "setWebhook"]
        assert len(sw) == 1
        assert sw[0][1]["url"] == "https://example.com/tg/webhook"
        assert sw[0][1]["secret_token"] == "sec"
    finally:
        await adapter.disconnect()
        assert [c[0] for c in calls if c[0] == "deleteWebhook"] == ["deleteWebhook"]


async def test_adapter_webhook_delivers_inbound_update():
    route, _ = _tg_routes()
    delivered: list[InboundMessage] = []

    async def capture(msg): delivered.append(msg)

    adapter = TelegramAdapter(
        bot_token="ABC",
        webhook_host="127.0.0.1", webhook_port=0,
        webhook_public_url="https://example.com/tg",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(route)),
    )
    adapter._owned_client = True
    adapter.bind_inbound(capture)
    await adapter.connect()
    try:
        sock = adapter._webhook._site._server.sockets[0]
        port = sock.getsockname()[1]
        # Post a Telegram update to our webhook
        async with httpx.AsyncClient() as c:
            await c.post(
                f"http://127.0.0.1:{port}{adapter._webhook.path}",
                json={
                    "update_id": 7,
                    "message": {
                        "message_id": 50, "date": 0,
                        "from": {"id": 42, "is_bot": False, "first_name": "A"},
                        "chat": {"id": 999, "type": "private"},
                        "text": "hello via webhook",
                    },
                },
                timeout=5.0,
            )
        for _ in range(30):
            await asyncio.sleep(0.02)
            if delivered:
                break
        assert len(delivered) == 1
        msg = delivered[0]
        assert msg.text == "hello via webhook"
        assert msg.source.platform == "telegram"
        assert msg.source.chat_id == "999"
    finally:
        await adapter.disconnect()


async def test_adapter_webhook_no_public_url_skips_setWebhook():
    route, calls = _tg_routes()
    adapter = TelegramAdapter(
        bot_token="ABC",
        webhook_host="127.0.0.1", webhook_port=0,
        # NO webhook_public_url — tests "server up, registration skipped"
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(route)),
    )
    adapter._owned_client = True
    await adapter.connect()
    try:
        assert adapter._webhook is not None
        assert [c for c in calls if c[0] == "setWebhook"] == []
    finally:
        await adapter.disconnect()
        # no delete either (nothing to unregister)
        assert [c for c in calls if c[0] == "deleteWebhook"] == []


async def test_adapter_longpoll_mode_when_no_webhook_config():
    route, _ = _tg_routes()
    adapter = TelegramAdapter(
        bot_token="ABC",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(route)),
    )
    adapter._owned_client = True
    await adapter.connect()
    try:
        assert adapter._webhook is None
        assert adapter._poll_task is not None
    finally:
        await adapter.disconnect()


# -- config env wiring -----------------------------------------


async def test_build_adapters_reads_telegram_webhook_env(monkeypatch):
    from yuxu.bundled.gateway import _build_adapters
    monkeypatch.setenv("GATEWAY_CONSOLE_ENABLED", "false")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "ABC")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_HOST", "0.0.0.0")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_PORT", "8443")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_PUBLIC_URL", "https://x.example/tg")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET_TOKEN", "sectok")

    adapters = _build_adapters()
    tg = [a for a in adapters if a.platform == "telegram"]
    assert len(tg) == 1
    a = tg[0]
    assert a._webhook_mode is True
    assert a._webhook_host == "0.0.0.0"
    assert a._webhook_port == 8443
    assert a._webhook_public_url == "https://x.example/tg"
    assert a._webhook_secret == "sectok"
