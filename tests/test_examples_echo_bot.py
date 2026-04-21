"""End-to-end smoke for examples/echo_bot.

Exercises the full path:
    inbound InboundMessage (synthetic) → gateway.user_message publish
    → echo_bot subscription → gw.open_draft → adapter.render_draft (finalize)

Uses the existing ConsoleAdapter (with stdout suppressed via capsys)
because it implements the finalize-only rendering path we care about.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from yuxu.bundled.gateway.adapters.console import ConsoleAdapter
from yuxu.bundled.gateway.handler import GatewayManager
from yuxu.bundled.gateway.session import InboundMessage, SessionSource
from yuxu.core.bus import Bus
from yuxu.core.loader import Loader

pytestmark = pytest.mark.asyncio


async def _wait_until(predicate, timeout=3.0, step=0.02):
    import time
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if predicate():
            return True
        await asyncio.sleep(step)
    return False


async def test_echo_bot_renders_full_card_on_console(bundled_dir, tmp_path,
                                                      monkeypatch):
    # Run with a temp YUXU_HOME so we don't touch user's real ~/.yuxu
    monkeypatch.setenv("YUXU_HOME", str(tmp_path / ".yuxu"))
    # Disable Telegram to keep the test hermetic
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    bus = Bus()
    # Bundled agents + the example echo_bot (which ships inside yuxu.examples)
    import yuxu.examples
    examples_dir = str(Path(yuxu.examples.__file__).parent)
    loader = Loader(bus, dirs=[bundled_dir, examples_dir])
    await loader.scan()

    # echo_bot appears as a scanned spec
    assert "echo_bot" in loader.specs
    assert "gateway" in loader.specs

    # Bring up gateway first (we'll inject a RecordingAdapter to observe
    # outbound without needing a real terminal)
    await loader.ensure_running("gateway")
    gw: GatewayManager = loader.get_handle("gateway")

    # Swap console adapter with a recording one so we can assert on output.
    for name in list(gw.adapters.keys()):
        adapter = gw.adapters.pop(name)
        await adapter.disconnect()

    class RecordingConsole(ConsoleAdapter):
        platform = "console"

    recorder = RecordingConsole(user_id="tester", read_stdin=False)
    gw.register_adapter(recorder)
    await recorder.connect()

    # Start echo_bot
    await loader.ensure_running("echo_bot")
    assert bus.query_status("echo_bot") == "ready"

    # Simulate an inbound user message (as if console read "hello")
    inbound = InboundMessage(
        source=SessionSource(
            platform="console", chat_id="tester", user_id="tester",
            chat_type="dm",
        ),
        text="hello",
    )
    await recorder._deliver(inbound)

    ok = await _wait_until(lambda: len(recorder.outbox) >= 1, timeout=5.0)
    assert ok, "echo_bot never produced a finalized card"

    # The finalized draft should include quote, thinking, content, footer.
    card = recorder.outbox[0]
    assert card["finalize"] is True
    draft = card["draft"]
    assert draft["quote_text"] == "hello"
    assert draft["quote_user"] == "tester"
    assert "Received user input" in draft["thinking"]
    assert "You said: " in draft["content"]
    assert "hello" in draft["content"]
    footer_keys = [k for k, _ in draft["footer_meta"]]
    assert "Agent" in footer_keys


async def test_echo_bot_ignores_cancel(bundled_dir, tmp_path, monkeypatch):
    """/stop should emit gateway.user_cancel, not trigger echo_bot."""
    monkeypatch.setenv("YUXU_HOME", str(tmp_path / ".yuxu"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    bus = Bus()
    import yuxu.examples
    examples_dir = str(Path(yuxu.examples.__file__).parent)
    loader = Loader(bus, dirs=[bundled_dir, examples_dir])
    await loader.scan()

    await loader.ensure_running("gateway")
    gw: GatewayManager = loader.get_handle("gateway")
    for name in list(gw.adapters.keys()):
        adapter = gw.adapters.pop(name)
        await adapter.disconnect()
    recorder = ConsoleAdapter(user_id="tester", read_stdin=False)
    gw.register_adapter(recorder)
    await recorder.connect()

    await loader.ensure_running("echo_bot")

    # Observe gateway.user_cancel fanning out
    cancel_events = []
    bus.subscribe("gateway.user_cancel", lambda ev: cancel_events.append(ev["payload"]))

    await recorder._deliver(InboundMessage(
        source=SessionSource(platform="console", chat_id="tester",
                              user_id="tester", chat_type="dm"),
        text="/stop",
    ))
    await asyncio.sleep(0.3)

    # echo_bot produced nothing
    assert recorder.outbox == []
    # cancel did fan out
    assert len(cancel_events) == 1
    assert cancel_events[0]["session_key"].startswith("console:tester:")
