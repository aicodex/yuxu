from __future__ import annotations

import asyncio
from typing import Optional

import httpx
import pytest

from yuxu.bundled.gateway.adapters.base import PlatformAdapter
from yuxu.bundled.gateway.adapters.console import ConsoleAdapter
from yuxu.bundled.gateway.adapters.telegram import TelegramAdapter
from yuxu.bundled.gateway.handler import GatewayManager
from yuxu.bundled.gateway.session import (
    InboundMessage,
    SendResult,
    SessionEntry,
    SessionSource,
)
from yuxu.core.bus import Bus
from yuxu.core.loader import Loader

pytestmark = pytest.mark.asyncio


# -- helpers ---------------------------------------------------


class RecordingAdapter(PlatformAdapter):
    platform = "record"

    def __init__(self) -> None:
        super().__init__()
        self.connected = False
        self.disconnected = False
        self.outbox: list[tuple[SessionSource, str, Optional[str]]] = []
        self.next_send_result = SendResult(ok=True, message_id="rec-1")

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def send(self, source, text, *, reply_to_message_id=None, parse_mode=None) -> SendResult:
        self.outbox.append((source, text, reply_to_message_id))
        return self.next_send_result

    # test helper
    async def inject(self, text: str, chat_id: str = "u1",
                     user_id: str = "u1", thread_id: Optional[str] = None) -> None:
        await self._deliver(InboundMessage(
            source=SessionSource(
                platform=self.platform, chat_id=chat_id, user_id=user_id,
                thread_id=thread_id, chat_type="dm",
            ),
            text=text,
        ))


async def _yield(n: int = 4) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


# -- dataclasses ----------------------------------------------


async def test_session_source_key_no_thread():
    s = SessionSource(platform="tg", chat_id="100", user_id="7")
    assert s.session_key == "tg:100:default"


async def test_session_source_key_with_thread():
    s = SessionSource(platform="tg", chat_id="100", user_id="7", thread_id="abc")
    assert s.session_key == "tg:100:abc"


async def test_inbound_message_as_dict_round_trip():
    msg = InboundMessage(
        source=SessionSource(platform="tg", chat_id="42"),
        text="hi",
    )
    d = msg.as_dict()
    assert d["session_key"] == "tg:42:default"
    assert d["source"]["platform"] == "tg"
    assert d["text"] == "hi"


# -- GatewayManager: inbound routing --------------------------


async def test_inbound_publishes_gateway_user_message():
    bus = Bus()
    captured: list[dict] = []
    bus.subscribe("gateway.user_message",
                  lambda ev: captured.append(ev["payload"]))
    gm = GatewayManager(bus)
    adapter = RecordingAdapter()
    gm.register_adapter(adapter)
    await gm.start()
    try:
        await adapter.inject("hello world")
        await _yield()
        assert len(captured) == 1
        assert captured[0]["text"] == "hello world"
        assert captured[0]["source"]["platform"] == "record"
    finally:
        await gm.stop()


async def test_inbound_slash_stop_emits_user_cancel_instead():
    bus = Bus()
    msg_events, cancel_events = [], []
    bus.subscribe("gateway.user_message", lambda e: msg_events.append(e["payload"]))
    bus.subscribe("gateway.user_cancel", lambda e: cancel_events.append(e["payload"]))
    gm = GatewayManager(bus)
    adapter = RecordingAdapter()
    gm.register_adapter(adapter)
    await gm.start()
    try:
        await adapter.inject("/stop")
        await _yield()
        assert cancel_events and not msg_events
        assert cancel_events[0]["session_key"].startswith("record:")
    finally:
        await gm.stop()


async def test_inbound_registers_session_entry():
    bus = Bus()
    gm = GatewayManager(bus)
    adapter = RecordingAdapter()
    gm.register_adapter(adapter)
    await gm.start()
    try:
        await adapter.inject("hi", chat_id="A")
        await _yield()
        await adapter.inject("hi again", chat_id="A")
        await _yield()
        assert len(gm.sessions) == 1
        entry = next(iter(gm.sessions.values()))
        assert entry.source.chat_id == "A"
        assert entry.last_inbound_ts is not None
    finally:
        await gm.stop()


# -- GatewayManager: outbound routing -------------------------


async def test_outbound_via_reply_topic():
    bus = Bus()
    gm = GatewayManager(bus)
    adapter = RecordingAdapter()
    gm.register_adapter(adapter)
    await gm.start()
    try:
        # seed a session by injecting first
        await adapter.inject("hi", chat_id="A")
        await _yield()
        key = f"record:A:default"
        await bus.publish("gateway.reply",
                          {"session_key": key, "text": "hello back"})
        await _yield()
        assert len(adapter.outbox) == 1
        sent_source, sent_text, reply_to = adapter.outbox[0]
        assert sent_source.chat_id == "A"
        assert sent_text == "hello back"
        assert reply_to is None
    finally:
        await gm.stop()


async def test_outbound_via_explicit_source():
    """reply with `source` dict, no prior session needed."""
    bus = Bus()
    gm = GatewayManager(bus)
    adapter = RecordingAdapter()
    gm.register_adapter(adapter)
    await gm.start()
    try:
        await bus.publish("gateway.reply", {
            "source": {"platform": "record", "chat_id": "X", "user_id": "u"},
            "text": "unsolicited",
        })
        await _yield()
        assert len(adapter.outbox) == 1
        assert adapter.outbox[0][0].chat_id == "X"
    finally:
        await gm.stop()


async def test_outbound_unknown_session_key_drops_with_error():
    bus = Bus()
    gm = GatewayManager(bus)
    adapter = RecordingAdapter()
    gm.register_adapter(adapter)
    await gm.start()
    try:
        await bus.publish("gateway.reply",
                          {"session_key": "ghost:0:default", "text": "x"})
        await _yield()
        assert adapter.outbox == []
    finally:
        await gm.stop()


async def test_outbound_unknown_platform_reported_as_error():
    bus = Bus()
    gm = GatewayManager(bus)
    adapter = RecordingAdapter()
    gm.register_adapter(adapter)
    await gm.start()
    try:
        class _Msg: payload = {"op": "send",
                               "source": {"platform": "feishu", "chat_id": "c"},
                               "text": "hi"}
        r = await gm.handle(_Msg())
        assert r["ok"] is False
        assert "no adapter" in r["error"]
    finally:
        await gm.stop()


# -- handle() op surface --------------------------------------


async def test_op_send_returns_message_id():
    bus = Bus()
    gm = GatewayManager(bus)
    adapter = RecordingAdapter()
    gm.register_adapter(adapter)
    await gm.start()
    try:
        class _Msg: payload = {"op": "send",
                               "source": {"platform": "record", "chat_id": "Z"},
                               "text": "ok"}
        r = await gm.handle(_Msg())
        assert r == {"ok": True, "message_id": "rec-1", "error": None}
    finally:
        await gm.stop()


async def test_op_sessions_returns_current_list():
    bus = Bus()
    gm = GatewayManager(bus)
    adapter = RecordingAdapter()
    gm.register_adapter(adapter)
    await gm.start()
    try:
        await adapter.inject("hi", chat_id="A")
        await adapter.inject("hi", chat_id="B")
        await _yield()
        class _Msg: payload = {"op": "sessions"}
        r = await gm.handle(_Msg())
        assert r["ok"] is True
        chat_ids = sorted(s["source"]["chat_id"] for s in r["sessions"])
        assert chat_ids == ["A", "B"]
    finally:
        await gm.stop()


async def test_op_unknown():
    bus = Bus()
    gm = GatewayManager(bus)
    class _Msg: payload = {"op": "weird"}
    r = await gm.handle(_Msg())
    assert r["ok"] is False


# -- adapter registration rules -------------------------------


async def test_register_duplicate_platform_raises():
    gm = GatewayManager(Bus())
    gm.register_adapter(RecordingAdapter())
    with pytest.raises(ValueError):
        gm.register_adapter(RecordingAdapter())


# -- ConsoleAdapter -------------------------------------------


async def test_console_adapter_inbound_via_queue():
    bus = Bus()
    gm = GatewayManager(bus)
    console = ConsoleAdapter(user_id="dev")
    gm.register_adapter(console)
    captured = []
    bus.subscribe("gateway.user_message", lambda e: captured.append(e["payload"]))
    await gm.start()
    try:
        await console.push_input("hello")
        for _ in range(30):
            await asyncio.sleep(0.01)
            if captured:
                break
        assert captured and captured[0]["text"] == "hello"
        assert captured[0]["source"]["platform"] == "console"
    finally:
        await gm.stop()


async def test_console_adapter_outbound_records_outbox(capsys):
    source = SessionSource(platform="console", chat_id="dev", user_id="dev")
    adapter = ConsoleAdapter()
    r = await adapter.send(source, "hi there")
    assert r.ok is True
    assert adapter.outbox[0]["text"] == "hi there"


# -- TelegramAdapter (mocked httpx) ---------------------------


def _tg_route(handler):
    return httpx.MockTransport(handler)


async def test_telegram_adapter_connect_requires_token():
    with pytest.raises(ValueError):
        TelegramAdapter(bot_token="")


async def test_telegram_send_calls_api_shape():
    captured = {}

    def route(req: httpx.Request):
        captured["url"] = str(req.url)
        import json
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={
            "ok": True,
            "result": {"message_id": 42},
        })

    adapter = TelegramAdapter(
        bot_token="ABC",
        http_client=httpx.AsyncClient(transport=_tg_route(route)),
    )
    adapter._owned_client = True  # ensure close on disconnect
    src = SessionSource(platform="telegram", chat_id="999", user_id="7")
    r = await adapter.send(src, "hi")
    assert r.ok is True
    assert r.message_id == "42"
    assert "sendMessage" in captured["url"]
    assert captured["body"] == {"chat_id": "999", "text": "hi"}
    await adapter.disconnect()


async def test_telegram_poll_dispatches_inbound_message():
    updates = [{
        "update_id": 100,
        "message": {
            "message_id": 1, "date": 0,
            "from": {"id": 42, "is_bot": False, "first_name": "Alice"},
            "chat": {"id": 999, "type": "private"},
            "text": "hello yuxu",
        },
    }]
    first_call = {"done": False}

    def route(req: httpx.Request):
        if "getUpdates" in str(req.url) and not first_call["done"]:
            first_call["done"] = True
            return httpx.Response(200, json={"ok": True, "result": updates})
        # subsequent getUpdates return empty (long-poll simulation)
        return httpx.Response(200, json={"ok": True, "result": []})

    adapter = TelegramAdapter(
        bot_token="ABC",
        poll_timeout=1,
        http_client=httpx.AsyncClient(transport=_tg_route(route)),
    )
    adapter._owned_client = True
    received: list[InboundMessage] = []

    async def on_inbound(m):
        received.append(m)

    adapter.bind_inbound(on_inbound)
    await adapter.connect()
    try:
        for _ in range(30):
            await asyncio.sleep(0.02)
            if received:
                break
        assert received and received[0].text == "hello yuxu"
        assert received[0].source.user_id == "42"
        assert received[0].source.chat_type == "dm"
    finally:
        await adapter.disconnect()


async def test_telegram_allowlist_filters_non_allowed():
    def route(req):
        return httpx.Response(200, json={"ok": True, "result": [{
            "update_id": 1,
            "message": {
                "message_id": 1, "date": 0,
                "from": {"id": 111, "is_bot": False, "first_name": "X"},
                "chat": {"id": 999, "type": "private"},
                "text": "knock knock",
            },
        }]})

    adapter = TelegramAdapter(
        bot_token="ABC", allowed_user_ids={42},
        poll_timeout=1,
        http_client=httpx.AsyncClient(transport=_tg_route(route)),
    )
    adapter._owned_client = True
    received: list[InboundMessage] = []

    async def on_inbound(m):
        received.append(m)

    adapter.bind_inbound(on_inbound)
    await adapter.connect()
    try:
        for _ in range(10):
            await asyncio.sleep(0.02)
        assert received == []  # filtered
    finally:
        await adapter.disconnect()


# -- full boot via Loader -------------------------------------


async def test_gateway_starts_via_loader(bundled_dir, monkeypatch):
    monkeypatch.setenv("GATEWAY_CONSOLE_ENABLED", "false")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    bus = Bus()
    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()
    await loader.ensure_running("gateway")
    assert bus.query_status("gateway") == "ready"
    gm = loader.get_handle("gateway")
    assert gm is not None
    # no adapters configured
    assert gm.adapters == {}
    await loader.stop("gateway")
