"""help_plugin — /help lists registered slash commands via the gateway registry."""
from __future__ import annotations

import asyncio

import pytest

from yuxu.bundled.gateway.adapters.base import PlatformAdapter
from yuxu.bundled.gateway.handler import GatewayManager
from yuxu.bundled.gateway.session import (
    InboundMessage,
    SendResult,
    SessionSource,
)
from yuxu.bundled.help_plugin.handler import HelpPlugin
from yuxu.core.bus import Bus

pytestmark = pytest.mark.asyncio


class RecordingAdapter(PlatformAdapter):
    platform = "record"
    supports_edit = True

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []

    async def connect(self) -> None: pass
    async def disconnect(self) -> None: pass

    async def send(self, source, text, *, reply_to_message_id=None, parse_mode=None) -> SendResult:
        self.sent.append(text)
        return SendResult(ok=True, message_id=f"m-{len(self.sent)}")

    async def edit(self, source, message_id, text, *, finalize=False) -> SendResult:
        return SendResult(ok=True, message_id=message_id)


class _FakeCtx:
    def __init__(self, bus: Bus) -> None:
        self.bus = bus


async def _yield(n: int = 6) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


async def _setup(commands: dict[str, dict]):
    bus = Bus()
    gm = GatewayManager(bus)
    adapter = RecordingAdapter()
    gm.register_adapter(adapter)
    await gm.start()
    gm.commands.update(commands)
    # Register the gateway bus handle so bus.request("gateway", ...) resolves.
    bus.register("gateway", gm.handle)
    plugin = HelpPlugin(_FakeCtx(bus))
    plugin.install()
    # Drive a real session so session_key resolves for _reply.
    await gm._on_inbound(InboundMessage(
        source=SessionSource(platform="record", chat_id="c", user_id="u"),
        text="ping",
    ))
    await _yield()
    return bus, gm, adapter, plugin


def test_format_empty_registry():
    plugin = HelpPlugin(_FakeCtx(Bus()))
    assert plugin._format({}) == "no commands registered"


def test_format_lists_commands_alphabetical_with_help_text():
    plugin = HelpPlugin(_FakeCtx(Bus()))
    out = plugin._format({
        "/dashboard": {"agent": "dashboard", "help": "live view"},
        "/help": {"agent": "help_plugin", "help": "list commands"},
    })
    assert "**Available commands**" in out
    # alphabetical order: /dashboard appears before /help
    assert out.index("/dashboard") < out.index("/help")
    assert "live view" in out and "list commands" in out
    assert "/help /cmd" in out  # tip line


def test_format_single_command_details_via_selector():
    plugin = HelpPlugin(_FakeCtx(Bus()))
    out = plugin._format(
        {"/dashboard": {"agent": "dashboard", "help": "live view"}},
        selector="/dashboard",
    )
    assert "**/dashboard**" in out
    assert "live view" in out
    assert "handled by: dashboard" in out


def test_format_unknown_selector_says_unknown():
    plugin = HelpPlugin(_FakeCtx(Bus()))
    out = plugin._format(
        {"/dashboard": {"agent": "dashboard"}},
        selector="/ghost",
    )
    assert "unknown command" in out
    assert "/ghost" in out


async def test_help_command_replies_with_command_list():
    bus, gm, adapter, plugin = await _setup({
        "/dashboard": {"agent": "dashboard", "help": "live view"},
        "/help": {"agent": "help_plugin", "help": "list commands"},
    })
    try:
        await gm._on_inbound(InboundMessage(
            source=SessionSource(platform="record", chat_id="c", user_id="u"),
            text="/help",
        ))
        await _yield(20)
        # 'ping' already triggered a user_message (no send); /help reply is
        # the only send we care about here.
        assert any("/dashboard" in s and "/help" in s for s in adapter.sent), \
            adapter.sent
    finally:
        await gm.stop()


async def test_help_command_with_selector_replies_with_detail():
    bus, gm, adapter, plugin = await _setup({
        "/dashboard": {"agent": "dashboard", "help": "live view"},
        "/help": {"agent": "help_plugin"},
    })
    try:
        await gm._on_inbound(InboundMessage(
            source=SessionSource(platform="record", chat_id="c", user_id="u"),
            text="/help /dashboard",
        ))
        await _yield(20)
        detail_replies = [s for s in adapter.sent if "/dashboard" in s]
        assert detail_replies, adapter.sent
        assert any("live view" in s for s in detail_replies)
    finally:
        await gm.stop()


async def test_help_ignores_other_commands():
    bus, gm, adapter, plugin = await _setup({
        "/help": {"agent": "help_plugin"},
        "/dashboard": {"agent": "dashboard"},
    })
    try:
        await gm._on_inbound(InboundMessage(
            source=SessionSource(platform="record", chat_id="c", user_id="u"),
            text="/dashboard",
        ))
        await _yield(20)
        # help_plugin should NOT have sent anything for /dashboard
        for s in adapter.sent:
            assert "**Available commands**" not in s
            assert "**/dashboard**" not in s
    finally:
        await gm.stop()
