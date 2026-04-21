"""Dashboard plugin — live refresh + auto-exit on user message/other command."""
from __future__ import annotations

import asyncio

import pytest

from yuxu.bundled.dashboard.handler import Dashboard
from yuxu.bundled.gateway.adapters.base import PlatformAdapter
from yuxu.bundled.gateway.handler import GatewayManager
from yuxu.bundled.gateway.session import (
    InboundMessage,
    SendResult,
    SessionSource,
)
from yuxu.core.bus import Bus

pytestmark = pytest.mark.asyncio


class RecordingAdapter(PlatformAdapter):
    platform = "record"
    supports_edit = True

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict]] = []

    async def connect(self) -> None: pass
    async def disconnect(self) -> None: pass

    async def send(self, source, text, *, reply_to_message_id=None) -> SendResult:
        self.calls.append(("send", {"text": text}))
        return SendResult(ok=True, message_id=f"m-{len(self.calls)}")

    async def edit(self, source, message_id, text, *, finalize=False) -> SendResult:
        self.calls.append(("edit", {"message_id": message_id, "text": text,
                                     "finalize": finalize}))
        return SendResult(ok=True, message_id=message_id)


class _FakeLoader:
    """Minimal stand-in so Dashboard's snapshot has something to render."""
    def __init__(self, states: dict[str, str]) -> None:
        self._states = states

    def get_state(self, name=None):
        if name is not None:
            return {"name": name, "status": self._states.get(name, "idle")}
        return dict(self._states)


class _FakeCtx:
    """Minimal AgentContext shim just for Dashboard tests."""
    def __init__(self, bus: Bus, gm: GatewayManager,
                 states: dict[str, str] | None = None) -> None:
        self.bus = bus
        self._gm = gm
        self.loader = _FakeLoader(states or {"gateway": "ready",
                                              "dashboard": "ready"})

    def get_agent(self, name: str):
        if name == "gateway":
            return self._gm
        return None


async def _yield(n: int = 6) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


async def _setup() -> tuple[Bus, GatewayManager, RecordingAdapter, Dashboard]:
    bus = Bus()
    gm = GatewayManager(bus)
    adapter = RecordingAdapter()
    gm.register_adapter(adapter)
    await gm.start()
    # Register gateway bus handler so bus.request("gateway", ...) works
    # (register_command goes through it during real boot; dashboard tests
    # drive /dashboard directly so registry state doesn't matter here).
    ctx = _FakeCtx(bus, gm)
    dash = Dashboard(ctx)
    dash._refresh_seconds = 0.05    # keep tests fast
    dash.install()
    # Pretend the /dashboard command was registered.
    gm.commands["/dashboard"] = {"agent": "dashboard",
                                 "help": "Open a live dashboard."}
    return bus, gm, adapter, dash


async def test_slash_dashboard_opens_a_draft_and_starts_refresh_loop():
    bus, gm, adapter, dash = await _setup()
    try:
        await gm._on_inbound(InboundMessage(
            source=SessionSource(platform="record", chat_id="c1", user_id="u1"),
            text="/dashboard",
        ))
        await _yield(10)
        # Draft "open" should have triggered a send on the adapter.
        assert any(c[0] == "send" for c in adapter.calls)
        assert "record:c1:default" in dash._active
        # Give the refresh loop a moment to tick at least once.
        await asyncio.sleep(0.12)
        # Expect at least one edit since open (refresh loop runs flush()).
        assert any(c[0] == "edit" for c in adapter.calls)
    finally:
        await dash.shutdown()
        await gm.stop()


async def test_dashboard_exits_when_user_sends_a_plain_message():
    bus, gm, adapter, dash = await _setup()
    try:
        src = SessionSource(platform="record", chat_id="c1", user_id="u1")
        await gm._on_inbound(InboundMessage(source=src, text="/dashboard"))
        await _yield(8)
        assert "record:c1:default" in dash._active

        # Plain message from same session → should exit dashboard.
        await gm._on_inbound(InboundMessage(source=src, text="hi there"))
        await _yield(10)
        assert "record:c1:default" not in dash._active
        # Finalize edit should carry the 'Exited' marker in the footer.
        edits = [c for c in adapter.calls if c[0] == "edit"]
        final = [c for c in edits if c[1].get("finalize")]
        assert final, "expected a finalize edit when dashboard exits"
        assert "📴 Exited" in final[-1][1]["text"] or "frozen" in final[-1][1]["text"]
    finally:
        await dash.shutdown()
        await gm.stop()


async def test_dashboard_exits_when_other_slash_command_arrives():
    bus, gm, adapter, dash = await _setup()
    try:
        gm.commands["/other"] = {"agent": "other"}
        src = SessionSource(platform="record", chat_id="c2", user_id="u2")
        await gm._on_inbound(InboundMessage(source=src, text="/dashboard"))
        await _yield(8)
        assert "record:c2:default" in dash._active

        await gm._on_inbound(InboundMessage(source=src, text="/other"))
        await _yield(10)
        assert "record:c2:default" not in dash._active
    finally:
        await dash.shutdown()
        await gm.stop()


async def test_re_invoking_dashboard_restarts_cleanly():
    bus, gm, adapter, dash = await _setup()
    try:
        src = SessionSource(platform="record", chat_id="c3", user_id="u3")
        await gm._on_inbound(InboundMessage(source=src, text="/dashboard"))
        await _yield(8)
        first_entry = dash._active["record:c3:default"]
        first_draft_id = first_entry["draft"].id

        # Second /dashboard should close the first and open a fresh one.
        await gm._on_inbound(InboundMessage(source=src, text="/dashboard"))
        await _yield(10)
        assert "record:c3:default" in dash._active
        second_entry = dash._active["record:c3:default"]
        assert second_entry["draft"].id != first_draft_id
    finally:
        await dash.shutdown()
        await gm.stop()


async def test_shutdown_stops_all_active_dashboards():
    bus, gm, adapter, dash = await _setup()
    try:
        for chat in ("a", "b", "c"):
            src = SessionSource(platform="record", chat_id=chat, user_id="u")
            await gm._on_inbound(InboundMessage(source=src, text="/dashboard"))
        await _yield(10)
        assert len(dash._active) == 3

        await dash.shutdown()
        assert dash._active == {}
    finally:
        await gm.stop()


async def test_snapshot_renders_loader_states_in_buckets():
    bus = Bus()
    gm = GatewayManager(bus)
    adapter = RecordingAdapter()
    gm.register_adapter(adapter)
    await gm.start()
    try:
        ctx = _FakeCtx(bus, gm, states={
            "gateway": "ready",
            "dashboard": "ready",
            "planner": "loading",
            "broken": "failed",
        })
        dash = Dashboard(ctx)
        snap = dash._collect_snapshot()
        assert "gateway" in snap and "dashboard" in snap
        assert "[ready]" in snap or "ready" in snap
        assert "[failed]" in snap
        assert "broken" in snap
    finally:
        await gm.stop()
