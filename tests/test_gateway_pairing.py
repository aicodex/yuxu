"""PairingRegistry + gateway pairing gate + bus ops + CLI."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from yuxu.bundled.gateway.handler import GatewayManager
from yuxu.bundled.gateway.pairing import PairingRegistry
from yuxu.bundled.gateway.session import InboundMessage, SessionSource
from yuxu.core.bus import Bus
from yuxu.cli.app import main as cli_main

pytestmark = pytest.mark.asyncio


# -- PairingRegistry unit tests --------------------------------


def test_registry_empty_when_file_missing(tmp_path):
    reg = PairingRegistry(tmp_path / "missing.yaml")
    assert reg.list_allowed() == []
    assert reg.list_pending() == []
    assert not reg.is_allowed("feishu", "ou_x")


def test_registry_add_pending_and_approve(tmp_path):
    path = tmp_path / "p.yaml"
    reg = PairingRegistry(path)
    reg.add_pending("feishu", "ou_alice", first_message="hi", chat_id="oc_1")
    assert len(reg.list_pending("feishu")) == 1
    assert not reg.is_allowed("feishu", "ou_alice")

    reg.approve_pending("feishu", "ou_alice", note="alice the tester")
    assert reg.is_allowed("feishu", "ou_alice")
    assert reg.list_pending("feishu") == []
    # persisted to disk
    assert path.exists()
    data = path.read_text()
    assert "ou_alice" in data
    assert "alice the tester" in data


def test_registry_reload_persists(tmp_path):
    path = tmp_path / "p.yaml"
    reg = PairingRegistry(path)
    reg.allow("telegram", "123", note="alice")
    reg.add_pending("feishu", "ou_pending", first_message="hey")

    reg2 = PairingRegistry(path)
    assert reg2.is_allowed("telegram", "123")
    assert len(reg2.list_pending("feishu")) == 1


def test_registry_revoke(tmp_path):
    reg = PairingRegistry(tmp_path / "p.yaml")
    reg.allow("feishu", "ou_x")
    assert reg.is_allowed("feishu", "ou_x")
    assert reg.revoke_allowed("feishu", "ou_x") is True
    assert not reg.is_allowed("feishu", "ou_x")
    # second revoke is a no-op
    assert reg.revoke_allowed("feishu", "ou_x") is False


def test_registry_reject_pending(tmp_path):
    reg = PairingRegistry(tmp_path / "p.yaml")
    reg.add_pending("feishu", "ou_x", first_message="spam")
    assert reg.reject_pending("feishu", "ou_x") is True
    assert reg.reject_pending("feishu", "ou_x") is False


def test_registry_add_pending_idempotent(tmp_path):
    reg = PairingRegistry(tmp_path / "p.yaml")
    e1 = reg.add_pending("feishu", "ou_x", first_message="first")
    e2 = reg.add_pending("feishu", "ou_x", first_message="second try")
    assert e1 is e2
    assert e2.first_message == "first"   # preserves earliest


def test_registry_list_filters_by_platform(tmp_path):
    reg = PairingRegistry(tmp_path / "p.yaml")
    reg.allow("feishu", "ou_1")
    reg.allow("telegram", "t1")
    reg.add_pending("feishu", "ou_2")
    assert {e.user_id for e in reg.list_allowed("feishu")} == {"ou_1"}
    assert {e.user_id for e in reg.list_allowed("telegram")} == {"t1"}
    assert {e.user_id for e in reg.list_allowed()} == {"ou_1", "t1"}


def test_registry_handles_garbled_yaml(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("{not yaml")
    reg = PairingRegistry(path)
    assert reg.list_allowed() == []
    # still usable
    reg.allow("feishu", "ou_x")
    reg2 = PairingRegistry(path)
    assert reg2.is_allowed("feishu", "ou_x")


# -- gateway pairing gate --------------------------------------


async def test_gateway_no_gate_without_pairing():
    bus = Bus()
    events: list[dict] = []
    bus.subscribe("gateway.user_message", lambda ev: events.append(ev["payload"]))
    gm = GatewayManager(bus)   # no pairing → no gate
    msg = InboundMessage(
        source=SessionSource(platform="feishu", chat_id="oc", user_id="ou_new"),
        text="hello",
    )
    await gm._on_inbound(msg)
    await asyncio.sleep(0)
    assert len(events) == 1


async def test_gateway_blocks_unknown_when_pairing_required(tmp_path):
    bus = Bus()
    reg = PairingRegistry(tmp_path / "p.yaml")
    msg_events, pending_events = [], []
    bus.subscribe("gateway.user_message", lambda ev: msg_events.append(ev["payload"]))
    bus.subscribe("gateway.pairing_requested",
                   lambda ev: pending_events.append(ev["payload"]))

    gm = GatewayManager(bus, pairing=reg, pairing_required_platforms={"feishu"})
    msg = InboundMessage(
        source=SessionSource(platform="feishu", chat_id="oc", user_id="ou_unknown"),
        text="hello",
    )
    await gm._on_inbound(msg)
    await asyncio.sleep(0); await asyncio.sleep(0)

    assert msg_events == []                         # blocked
    assert len(pending_events) == 1
    assert pending_events[0]["user_id"] == "ou_unknown"
    # pending now recorded
    assert len(reg.list_pending("feishu")) == 1


async def test_gateway_passes_when_user_preallowed(tmp_path):
    bus = Bus()
    reg = PairingRegistry(tmp_path / "p.yaml")
    reg.allow("feishu", "ou_alice")
    msg_events: list[dict] = []
    bus.subscribe("gateway.user_message", lambda ev: msg_events.append(ev["payload"]))

    gm = GatewayManager(bus, pairing=reg, pairing_required_platforms={"feishu"})
    await gm._on_inbound(InboundMessage(
        source=SessionSource(platform="feishu", chat_id="oc", user_id="ou_alice"),
        text="hi",
    ))
    await asyncio.sleep(0)
    assert len(msg_events) == 1


async def test_gateway_pairing_only_gates_listed_platforms(tmp_path):
    bus = Bus()
    reg = PairingRegistry(tmp_path / "p.yaml")
    msg_events: list[dict] = []
    bus.subscribe("gateway.user_message", lambda ev: msg_events.append(ev["payload"]))
    gm = GatewayManager(bus, pairing=reg, pairing_required_platforms={"feishu"})
    # Telegram isn't required — falls through
    await gm._on_inbound(InboundMessage(
        source=SessionSource(platform="telegram", chat_id="123", user_id="456"),
        text="hi",
    ))
    await asyncio.sleep(0)
    assert len(msg_events) == 1


async def test_gateway_pairing_cancel_still_bypasses(tmp_path):
    """/stop should always emit user_cancel, even for unpaired users."""
    bus = Bus()
    reg = PairingRegistry(tmp_path / "p.yaml")
    cancel_events = []
    bus.subscribe("gateway.user_cancel",
                   lambda ev: cancel_events.append(ev["payload"]))
    gm = GatewayManager(bus, pairing=reg, pairing_required_platforms={"feishu"})
    await gm._on_inbound(InboundMessage(
        source=SessionSource(platform="feishu", chat_id="c", user_id="ou_new"),
        text="/stop",
    ))
    await asyncio.sleep(0)
    assert len(cancel_events) == 1


async def test_gateway_pairing_anonymous_inbound_blocked(tmp_path):
    bus = Bus()
    reg = PairingRegistry(tmp_path / "p.yaml")
    msg_events: list[dict] = []
    bus.subscribe("gateway.user_message", lambda ev: msg_events.append(ev["payload"]))
    gm = GatewayManager(bus, pairing=reg, pairing_required_platforms={"feishu"})
    # No user_id (anonymous) on a pairing-required platform → blocked.
    await gm._on_inbound(InboundMessage(
        source=SessionSource(platform="feishu", chat_id="c", user_id=None),
        text="hi",
    ))
    await asyncio.sleep(0)
    assert msg_events == []


# -- bus ops ---------------------------------------------------


async def test_bus_op_pair_list(tmp_path):
    bus = Bus()
    reg = PairingRegistry(tmp_path / "p.yaml")
    reg.allow("feishu", "ou_a", note="Alice")
    reg.add_pending("feishu", "ou_b", first_message="hi")
    gm = GatewayManager(bus, pairing=reg, pairing_required_platforms={"feishu"})

    class _M: payload = {"op": "pair_list"}
    r = await gm.handle(_M())
    assert r["ok"] is True
    allowed_ids = [e["user_id"] for e in r["allowed"]]
    pending_ids = [e["user_id"] for e in r["pending"]]
    assert "ou_a" in allowed_ids
    assert "ou_b" in pending_ids
    assert "feishu" in r["required_platforms"]


async def test_bus_op_pair_approve_reject_revoke(tmp_path):
    bus = Bus()
    reg = PairingRegistry(tmp_path / "p.yaml")
    reg.add_pending("feishu", "ou_a", first_message="hi")
    gm = GatewayManager(bus, pairing=reg)

    class _A: payload = {"op": "pair_approve", "platform": "feishu",
                          "user_id": "ou_a", "note": "test"}
    r = await gm.handle(_A())
    assert r["ok"] is True
    assert reg.is_allowed("feishu", "ou_a")

    class _V: payload = {"op": "pair_revoke", "platform": "feishu",
                          "user_id": "ou_a"}
    r = await gm.handle(_V())
    assert r["ok"] is True and r["removed"] is True

    reg.add_pending("feishu", "ou_b")
    class _R: payload = {"op": "pair_reject", "platform": "feishu",
                          "user_id": "ou_b"}
    r = await gm.handle(_R())
    assert r["ok"] is True


async def test_bus_ops_error_when_pairing_disabled():
    gm = GatewayManager(Bus())   # no pairing
    class _M: payload = {"op": "pair_list"}
    r = await gm.handle(_M())
    assert r["ok"] is False
    assert "not enabled" in r["error"]


# -- CLI -------------------------------------------------------


def _init_project(tmp_path) -> Path:
    proj = tmp_path / "proj"
    cli_main(["init", str(proj)])
    return proj


def test_cli_pair_approve_then_list(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("YUXU_HOME", str(tmp_path / ".yuxu"))
    proj = _init_project(tmp_path)

    rc = cli_main(["pair", "approve", "feishu", "ou_cli_user",
                    "--project", str(proj), "--note", "pre-provisioned"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "approved" in out

    rc = cli_main(["pair", "list", "--project", str(proj)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ou_cli_user" in out
    assert "pre-provisioned" in out


def test_cli_pair_reject_no_record(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("YUXU_HOME", str(tmp_path / ".yuxu"))
    proj = _init_project(tmp_path)
    rc = cli_main(["pair", "reject", "feishu", "ou_ghost",
                    "--project", str(proj)])
    assert rc == 0   # idempotent
    out = capsys.readouterr().out
    assert "no pending" in out


def test_cli_pair_revoke(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("YUXU_HOME", str(tmp_path / ".yuxu"))
    proj = _init_project(tmp_path)
    cli_main(["pair", "approve", "feishu", "ou_x", "--project", str(proj)])
    capsys.readouterr()
    rc = cli_main(["pair", "revoke", "feishu", "ou_x", "--project", str(proj)])
    assert rc == 0
    assert "revoked" in capsys.readouterr().out


def test_cli_pair_outside_project(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("YUXU_HOME", str(tmp_path / ".yuxu"))
    rc = cli_main(["pair", "list", "--project", str(tmp_path / "nope")])
    assert rc == 1
    assert "not a yuxu project" in capsys.readouterr().err
