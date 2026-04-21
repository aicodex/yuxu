"""Pairing hot reload — CLI writes pairings.yaml, running gateway picks it up."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from yuxu.bundled.gateway.adapters.base import PlatformAdapter
from yuxu.bundled.gateway.handler import GatewayManager
from yuxu.bundled.gateway.pairing import PairingRegistry
from yuxu.bundled.gateway.session import (
    InboundMessage,
    SendResult,
    SessionSource,
)
from yuxu.core.bus import Bus

pytestmark = pytest.mark.asyncio


class _Adapter(PlatformAdapter):
    platform = "feishu"
    supports_edit = False

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []

    async def connect(self) -> None: pass
    async def disconnect(self) -> None: pass

    async def send(self, source, text, *, reply_to_message_id=None) -> SendResult:
        self.sent.append(text)
        return SendResult(ok=True, message_id="m1")


async def _yield(n: int = 4) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


def test_reload_if_changed_returns_false_when_unchanged(tmp_path):
    path = tmp_path / "pairings.yaml"
    reg = PairingRegistry(path)
    assert reg.reload_if_changed() is False
    reg.allow("feishu", "ou_a")
    # save() updated _last_mtime → watcher shouldn't re-read
    assert reg.reload_if_changed() is False


def test_reload_if_changed_detects_external_write(tmp_path):
    path = tmp_path / "pairings.yaml"
    reg = PairingRegistry(path)
    assert not reg.is_allowed("feishu", "ou_b")

    # Simulate CLI process writing a new entry to disk.
    other = PairingRegistry(path)
    other.allow("feishu", "ou_b", note="via cli")
    # Force mtime to move forward on fast filesystems.
    import os, time
    os.utime(path, (time.time() + 1, time.time() + 1))

    assert reg.reload_if_changed() is True
    assert reg.is_allowed("feishu", "ou_b")


async def test_gateway_watcher_picks_up_cli_approval(tmp_path):
    """Running gateway sees an external approve via its pairing watch loop."""
    pairings_path = tmp_path / "pairings.yaml"
    reg = PairingRegistry(pairings_path)
    bus = Bus()
    gm = GatewayManager(
        bus, pairing=reg,
        pairing_required_platforms={"feishu"},
        pairing_poll_seconds=0.05,   # fast for tests
    )
    adapter = _Adapter()
    gm.register_adapter(adapter)

    reload_events: list[dict] = []
    bus.subscribe("gateway.pairings_reloaded",
                  lambda ev: reload_events.append(ev["payload"]))

    await gm.start()
    try:
        # Unpaired user → pending + replied with hint
        src = SessionSource(platform="feishu", chat_id="c1", user_id="ou_x")
        await gm._on_inbound(InboundMessage(source=src, text="hello"))
        await _yield(4)
        assert len(adapter.sent) == 1
        assert "yuxu pair approve" in adapter.sent[0]

        # CLI-side: a separate registry process approves.
        cli = PairingRegistry(pairings_path)
        cli.approve_pending("feishu", "ou_x", note="admin")
        import os, time
        os.utime(pairings_path, (time.time() + 1, time.time() + 1))

        # Wait for the watcher to pick it up.
        for _ in range(40):
            if reg.is_allowed("feishu", "ou_x"):
                break
            await asyncio.sleep(0.05)
        assert reg.is_allowed("feishu", "ou_x"), (
            "watcher never reloaded pairings"
        )
        assert any(e["path"] == str(pairings_path) for e in reload_events)

        # Now the same user's message goes through as a normal user_message.
        user_events: list[dict] = []
        bus.subscribe("gateway.user_message",
                      lambda ev: user_events.append(ev["payload"]))
        await gm._on_inbound(InboundMessage(source=src, text="hello again"))
        await _yield(4)
        assert len(user_events) == 1
        assert user_events[0]["text"] == "hello again"
    finally:
        await gm.stop()


async def test_watcher_stops_cleanly_on_gateway_stop(tmp_path):
    reg = PairingRegistry(tmp_path / "pairings.yaml")
    bus = Bus()
    gm = GatewayManager(bus, pairing=reg, pairing_poll_seconds=0.05)
    await gm.start()
    assert gm._pairing_watch_task is not None
    assert not gm._pairing_watch_task.done()
    await gm.stop()
    assert gm._pairing_watch_task is None


async def test_no_watcher_when_no_pairing(tmp_path):
    bus = Bus()
    gm = GatewayManager(bus, pairing=None, pairing_poll_seconds=0.05)
    await gm.start()
    try:
        assert gm._pairing_watch_task is None
    finally:
        await gm.stop()
