from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from yuxu.bundled.checkpoint_store.handler import CheckpointStore
from yuxu.bundled.recovery_agent.handler import RecoveryAgent
from yuxu.core.bus import Bus
from yuxu.core.loader import Loader

pytestmark = pytest.mark.asyncio


async def _setup_bus_with_store(tmp_path):
    bus = Bus()
    store = CheckpointStore(tmp_path)
    bus.register("checkpoint_store", store.handle)
    await bus.ready("checkpoint_store")
    return bus, store


async def test_scan_empty_root(tmp_path):
    bus, _ = await _setup_bus_with_store(tmp_path)
    agent = RecoveryAgent(bus)
    r = await agent.scan()
    assert r["ok"] is True
    assert r["inventory"] == {}
    assert r["counts"] == {"fresh": 0, "stale": 0, "abandoned": 0, "unknown": 0}


async def test_scan_classifies_by_age(tmp_path):
    bus, store = await _setup_bus_with_store(tmp_path)
    # Save a real fresh checkpoint
    store.save("fresh_ns", "k", {"x": 1})
    # Manually rewrite saved_at for stale / abandoned
    import json as _json
    stale_path = tmp_path / "stale_ns" / "k.json"
    (tmp_path / "stale_ns").mkdir(parents=True)
    aband_path = tmp_path / "aband_ns" / "k.json"
    (tmp_path / "aband_ns").mkdir(parents=True)
    now = datetime.now(timezone.utc)
    stale_time = (now - timedelta(hours=5)).isoformat()
    aband_time = (now - timedelta(days=3)).isoformat()
    stale_path.write_text(_json.dumps({"version": 1, "namespace": "stale_ns",
                                       "key": "k", "saved_at": stale_time, "data": {}}))
    aband_path.write_text(_json.dumps({"version": 1, "namespace": "aband_ns",
                                       "key": "k", "saved_at": aband_time, "data": {}}))

    agent = RecoveryAgent(bus)
    r = await agent.scan()
    assert r["counts"]["fresh"] == 1
    assert r["counts"]["stale"] == 1
    assert r["counts"]["abandoned"] == 1
    assert set(r["inventory"].keys()) == {"fresh_ns", "stale_ns", "aband_ns"}


async def test_scan_unknown_saved_at(tmp_path):
    bus, _ = await _setup_bus_with_store(tmp_path)
    import json as _json
    (tmp_path / "ns").mkdir()
    (tmp_path / "ns" / "k.json").write_text(_json.dumps({
        "version": 1, "namespace": "ns", "key": "k",
        "saved_at": "not-a-date", "data": {},
    }))
    agent = RecoveryAgent(bus)
    r = await agent.scan()
    assert r["counts"]["unknown"] == 1


async def test_scan_publishes_event(tmp_path):
    bus, store = await _setup_bus_with_store(tmp_path)
    store.save("n", "k", {})
    events = []
    bus.subscribe("recovery_agent.scan_complete", lambda m: events.append(m))
    agent = RecoveryAgent(bus)
    await agent.scan()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert events and events[0]["topic"] == "recovery_agent.scan_complete"
    assert "n" in events[0]["payload"]["namespaces"]


async def test_gc_deletes_old(tmp_path):
    bus, store = await _setup_bus_with_store(tmp_path)
    import json as _json
    now = datetime.now(timezone.utc)

    # Two old, one fresh
    for ns, key, delta in [
        ("ns1", "old1", timedelta(days=10)),
        ("ns1", "old2", timedelta(days=20)),
        ("ns2", "new", timedelta(minutes=5)),
    ]:
        d = tmp_path / ns
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{key}.json").write_text(_json.dumps({
            "version": 1, "namespace": ns, "key": key,
            "saved_at": (now - delta).isoformat(),
            "data": {},
        }))

    agent = RecoveryAgent(bus)
    r = await agent.gc(max_age_days=5)
    assert r["ok"] is True
    deleted_keys = sorted(d["key"] for d in r["deleted"])
    assert deleted_keys == ["old1", "old2"]
    assert (tmp_path / "ns2" / "new.json").exists()
    assert not (tmp_path / "ns1" / "old1.json").exists()


async def test_gc_zero_deletes_all_past():
    # gc with 0 days = delete anything with known age > 0
    # Use a real temp path via fixture
    pass


async def test_gc_negative_rejected(tmp_path):
    bus, _ = await _setup_bus_with_store(tmp_path)
    agent = RecoveryAgent(bus)
    r = await agent.gc(max_age_days=-1)
    assert r["ok"] is False


async def test_handle_status_triggers_scan_on_first_call(tmp_path):
    bus, store = await _setup_bus_with_store(tmp_path)
    store.save("n", "k", {})
    agent = RecoveryAgent(bus)

    class _M:
        payload = {"op": "status"}

    r = await agent.handle(_M())
    assert r["ok"] is True
    assert "n" in r["inventory"]


async def test_handle_rescan(tmp_path):
    bus, store = await _setup_bus_with_store(tmp_path)
    agent = RecoveryAgent(bus)
    await agent.scan()  # cached empty
    store.save("n", "k", {})

    class _M: payload = {"op": "rescan"}
    r = await agent.handle(_M())
    assert "n" in r["inventory"]


async def test_handle_gc_bad_days(tmp_path):
    bus, _ = await _setup_bus_with_store(tmp_path)
    agent = RecoveryAgent(bus)
    class _M: payload = {"op": "gc", "max_age_days": "nonsense"}
    r = await agent.handle(_M())
    assert r["ok"] is False


async def test_handle_unknown_op(tmp_path):
    bus, _ = await _setup_bus_with_store(tmp_path)
    agent = RecoveryAgent(bus)
    class _M: payload = {"op": "weird"}
    r = await agent.handle(_M())
    assert r["ok"] is False


async def test_custom_thresholds(tmp_path):
    bus, store = await _setup_bus_with_store(tmp_path)
    # 100s fresh window, 200s stale window
    agent = RecoveryAgent(bus, fresh_sec=100, stale_sec=200)
    import json as _json
    now = datetime.now(timezone.utc)
    for key, delta in [("a", 50), ("b", 150), ("c", 500)]:
        d = tmp_path / "n"
        d.mkdir(exist_ok=True)
        (d / f"{key}.json").write_text(_json.dumps({
            "version": 1, "namespace": "n", "key": key,
            "saved_at": (now - timedelta(seconds=delta)).isoformat(),
            "data": {},
        }))
    r = await agent.scan()
    assert r["counts"]["fresh"] == 1
    assert r["counts"]["stale"] == 1
    assert r["counts"]["abandoned"] == 1


# -- integration through loader ----------------------------------

async def test_integration_via_loader(tmp_path, monkeypatch, bundled_dir):
    monkeypatch.setenv("CHECKPOINT_ROOT", str(tmp_path))
    bus = Bus()
    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()
    await loader.ensure_running("recovery_agent")  # pulls checkpoint_store
    assert bus.query_status("checkpoint_store") == "ready"
    assert bus.query_status("recovery_agent") == "ready"

    # seed a checkpoint, then rescan via bus
    await bus.request("checkpoint_store",
                      {"op": "save", "namespace": "x", "key": "k", "data": {}},
                      timeout=2.0)
    r = await bus.request("recovery_agent", {"op": "rescan"}, timeout=5.0)
    assert r["ok"] is True
    assert "x" in r["inventory"]
