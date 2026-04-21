"""Gateway command registry + routing."""
from __future__ import annotations

import asyncio

import pytest

from yuxu.bundled.gateway.handler import GatewayManager
from yuxu.bundled.gateway.session import InboundMessage, SessionSource
from yuxu.core.bus import Bus

pytestmark = pytest.mark.asyncio


async def _yield(n=4):
    for _ in range(n):
        await asyncio.sleep(0)


async def test_register_list_unregister_commands():
    gm = GatewayManager(Bus())
    class _M: payload = {"op": "register_command",
                          "command": "/dashboard",
                          "agent": "dashboard",
                          "help": "live view"}
    r = await gm.handle(_M())
    assert r["ok"] is True

    class _L: payload = {"op": "list_commands"}
    r = await gm.handle(_L())
    assert r["ok"] is True
    assert "/dashboard" in r["commands"]
    assert r["commands"]["/dashboard"]["help"] == "live view"

    class _U: payload = {"op": "unregister_command", "command": "/dashboard"}
    r = await gm.handle(_U())
    assert r["ok"] is True
    r = await gm.handle(_L())
    assert r["commands"] == {}


async def test_register_command_validation():
    gm = GatewayManager(Bus())
    class _M: payload = {"op": "register_command", "command": "dashboard"}
    r = await gm.handle(_M())
    assert r["ok"] is False
    assert "start with '/'" in r["error"]

    class _M2: payload = {"op": "register_command", "command": "/two words"}
    r = await gm.handle(_M2())
    assert r["ok"] is False


async def test_slash_command_routes_to_command_invoked():
    bus = Bus()
    gm = GatewayManager(bus)
    gm.commands["/dashboard"] = {"agent": "dashboard", "help": "..."}
    user_events, command_events = [], []
    bus.subscribe("gateway.user_message",
                  lambda ev: user_events.append(ev["payload"]))
    bus.subscribe("gateway.command_invoked",
                  lambda ev: command_events.append(ev["payload"]))

    await gm._on_inbound(InboundMessage(
        source=SessionSource(platform="console", chat_id="c", user_id="u"),
        text="/dashboard",
    ))
    await _yield()
    assert len(command_events) == 1
    assert command_events[0]["command"] == "/dashboard"
    assert command_events[0]["handler_agent"] == "dashboard"
    assert user_events == []


async def test_slash_command_with_args():
    bus = Bus()
    gm = GatewayManager(bus)
    gm.commands["/help"] = {"agent": "help_plugin"}
    events = []
    bus.subscribe("gateway.command_invoked",
                  lambda ev: events.append(ev["payload"]))
    await gm._on_inbound(InboundMessage(
        source=SessionSource(platform="console", chat_id="c", user_id="u"),
        text="/help /dashboard",
    ))
    await _yield()
    assert events[0]["command"] == "/help"
    assert events[0]["args"] == "/dashboard"


async def test_unknown_slash_falls_through_to_user_message():
    bus = Bus()
    gm = GatewayManager(bus)
    user_events, command_events = [], []
    bus.subscribe("gateway.user_message",
                  lambda ev: user_events.append(ev["payload"]))
    bus.subscribe("gateway.command_invoked",
                  lambda ev: command_events.append(ev["payload"]))
    await gm._on_inbound(InboundMessage(
        source=SessionSource(platform="console", chat_id="c", user_id="u"),
        text="/notacommand",
    ))
    await _yield()
    assert command_events == []
    assert len(user_events) == 1


async def test_slash_cancel_still_cancels_even_with_registry():
    """/stop should route to user_cancel regardless of the command registry."""
    bus = Bus()
    gm = GatewayManager(bus)
    gm.commands["/stop"] = {"agent": "imaginary"}   # shouldn't shadow cancel
    cancel_events, command_events = [], []
    bus.subscribe("gateway.user_cancel",
                  lambda ev: cancel_events.append(ev["payload"]))
    bus.subscribe("gateway.command_invoked",
                  lambda ev: command_events.append(ev["payload"]))
    await gm._on_inbound(InboundMessage(
        source=SessionSource(platform="console", chat_id="c", user_id="u"),
        text="/stop",
    ))
    await _yield()
    assert len(cancel_events) == 1
    assert command_events == []


async def test_gateway_starts_via_loader_has_dashboard_and_help(bundled_dir):
    """Full-boot: dashboard + help_plugin register themselves on start."""
    from yuxu.core.loader import Loader
    bus = Bus()
    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()
    await loader.ensure_running("dashboard")
    await loader.ensure_running("help_plugin")

    gm = loader.get_handle("gateway")
    assert gm is not None
    # Both commands should be registered now.
    assert "/dashboard" in gm.commands
    assert "/help" in gm.commands
