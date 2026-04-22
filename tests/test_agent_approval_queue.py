from __future__ import annotations

import asyncio

import pytest

from yuxu.bundled.approval_queue.handler import ApprovalQueue, NAME, NAMESPACE, STATE_KEY
from yuxu.bundled.checkpoint_store.handler import CheckpointStore
from yuxu.core.bus import Bus
from yuxu.core.loader import Loader

pytestmark = pytest.mark.asyncio


async def _setup_bus_with_store(tmp_path):
    bus = Bus()
    store = CheckpointStore(tmp_path)
    bus.register("checkpoint_store", store.handle)
    await bus.ready("checkpoint_store")
    return bus, store


class _M:
    def __init__(self, payload, sender=None):
        self.payload = payload
        self.sender = sender


async def test_enqueue_creates_pending(tmp_path):
    bus, _ = await _setup_bus_with_store(tmp_path)
    q = ApprovalQueue(bus)
    r = await q.enqueue(action="delete_memory", detail={"key": "x"},
                        requester="memory_curator")
    assert r["ok"] is True
    assert r["status"] == "pending"
    assert isinstance(r["approval_id"], str) and len(r["approval_id"]) > 0


async def test_enqueue_rejects_empty_action(tmp_path):
    bus, _ = await _setup_bus_with_store(tmp_path)
    q = ApprovalQueue(bus)
    r = await q.enqueue(action="", detail={}, requester="x")
    assert r["ok"] is False


async def test_enqueue_publishes_pending_event(tmp_path):
    bus, _ = await _setup_bus_with_store(tmp_path)
    events = []
    bus.subscribe(f"{NAME}.pending", lambda m: events.append(m))
    q = ApprovalQueue(bus)
    await q.enqueue(action="send_external", detail={"target": "tg:chat"},
                    requester="gateway")
    # let publish tasks drain
    for _ in range(3):
        await asyncio.sleep(0)
    assert len(events) == 1
    assert events[0]["topic"] == f"{NAME}.pending"
    assert events[0]["payload"]["action"] == "send_external"
    assert events[0]["payload"]["requester"] == "gateway"


async def test_approve_publishes_decided_and_approved(tmp_path):
    bus, _ = await _setup_bus_with_store(tmp_path)
    decided = []
    approved = []
    bus.subscribe(f"{NAME}.decided", lambda m: decided.append(m))
    bus.subscribe(f"{NAME}.approved", lambda m: approved.append(m))
    q = ApprovalQueue(bus)
    r = await q.enqueue(action="x", detail=None, requester="r")
    aid = r["approval_id"]
    r2 = await q.approve(aid, reason="ok")
    assert r2["ok"] is True
    assert r2["status"] == "approved"
    for _ in range(3):
        await asyncio.sleep(0)
    assert len(approved) == 1
    assert len(decided) == 1
    assert decided[0]["payload"]["decision"] == "approved"
    assert decided[0]["payload"]["approval_id"] == aid


async def test_reject_publishes_decided_and_rejected(tmp_path):
    bus, _ = await _setup_bus_with_store(tmp_path)
    rejected = []
    decided = []
    bus.subscribe(f"{NAME}.rejected", lambda m: rejected.append(m))
    bus.subscribe(f"{NAME}.decided", lambda m: decided.append(m))
    q = ApprovalQueue(bus)
    r = await q.enqueue(action="x", detail=None, requester="r")
    await q.reject(r["approval_id"], reason="no")
    for _ in range(3):
        await asyncio.sleep(0)
    assert len(rejected) == 1
    assert decided[0]["payload"]["decision"] == "rejected"


async def test_decide_twice_rejected(tmp_path):
    bus, _ = await _setup_bus_with_store(tmp_path)
    q = ApprovalQueue(bus)
    r = await q.enqueue(action="x", detail=None, requester="r")
    aid = r["approval_id"]
    await q.approve(aid)
    r2 = await q.approve(aid)
    assert r2["ok"] is False
    r3 = await q.reject(aid)
    assert r3["ok"] is False


async def test_list_filter_by_status(tmp_path):
    bus, _ = await _setup_bus_with_store(tmp_path)
    q = ApprovalQueue(bus)
    r1 = await q.enqueue(action="a", detail=None, requester="r")
    r2 = await q.enqueue(action="b", detail=None, requester="r")
    r3 = await q.enqueue(action="c", detail=None, requester="r")
    await q.approve(r1["approval_id"])
    await q.reject(r2["approval_id"])
    # r3 remains pending
    pending = q._list("pending")
    approved = q._list("approved")
    assert len(pending) == 1 and pending[0]["approval_id"] == r3["approval_id"]
    assert len(approved) == 1 and approved[0]["approval_id"] == r1["approval_id"]


async def test_persistence_roundtrip(tmp_path):
    bus, _ = await _setup_bus_with_store(tmp_path)
    q1 = ApprovalQueue(bus)
    r = await q1.enqueue(action="delete_memory", detail={"k": "v"},
                         requester="x")
    aid = r["approval_id"]
    # New instance, same store; state should load
    q2 = ApprovalQueue(bus)
    await q2.load_state()
    item = q2._queue.get(aid)
    assert item is not None
    assert item["action"] == "delete_memory"
    assert item["status"] == "pending"


async def test_handle_status_empty(tmp_path):
    bus, _ = await _setup_bus_with_store(tmp_path)
    q = ApprovalQueue(bus)
    r = await q.handle(_M({"op": "status"}))
    assert r["ok"] is True
    assert r["pending_count"] == 0
    assert r["total"] == 0


async def test_handle_enqueue_uses_sender_when_no_requester(tmp_path):
    bus, _ = await _setup_bus_with_store(tmp_path)
    q = ApprovalQueue(bus)
    r = await q.handle(_M({"op": "enqueue", "action": "x"}, sender="caller"))
    assert r["ok"] is True
    item = list(q._queue.values())[0]
    assert item["requester"] == "caller"


async def test_handle_unknown_op(tmp_path):
    bus, _ = await _setup_bus_with_store(tmp_path)
    q = ApprovalQueue(bus)
    r = await q.handle(_M({"op": "nonsense"}))
    assert r["ok"] is False


async def test_handle_approve_missing_id(tmp_path):
    bus, _ = await _setup_bus_with_store(tmp_path)
    q = ApprovalQueue(bus)
    r = await q.handle(_M({"op": "approve"}))
    assert r["ok"] is False


# -- integration through loader ----------------------------------

async def test_integration_via_loader(tmp_path, monkeypatch, bundled_dir):
    monkeypatch.setenv("CHECKPOINT_ROOT", str(tmp_path))
    bus = Bus()
    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()
    await loader.ensure_running("approval_queue")
    assert bus.query_status("checkpoint_store") == "ready"
    assert bus.query_status("approval_queue") == "ready"

    r = await bus.request(
        "approval_queue",
        {"op": "enqueue", "action": "delete_memory",
         "detail": {"key": "foo"}, "requester": "test"},
        timeout=5.0,
    )
    assert r["ok"] is True
    aid = r["approval_id"]

    r2 = await bus.request(
        "approval_queue",
        {"op": "list", "status": "pending"},
        timeout=5.0,
    )
    assert r2["ok"] is True
    assert len(r2["items"]) == 1
    assert r2["items"][0]["approval_id"] == aid

    # approve and confirm decided event fires
    decided = []
    bus.subscribe("approval_queue.decided", lambda m: decided.append(m))
    r3 = await bus.request(
        "approval_queue",
        {"op": "approve", "approval_id": aid, "reason": "lgtm"},
        timeout=5.0,
    )
    assert r3["ok"] is True
    for _ in range(3):
        await asyncio.sleep(0)
    assert len(decided) == 1
    assert decided[0]["payload"]["decision"] == "approved"
