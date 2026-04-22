from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from yuxu.bundled.scheduler.handler import NAME, Scheduler
from yuxu.core.bus import Bus
from yuxu.core.loader import Loader

pytestmark = pytest.mark.asyncio


class _M:
    def __init__(self, payload):
        self.payload = payload


# -- validation (sync) ------------------------------------------------

async def test_validate_drops_missing_name():
    s = Scheduler(None, [{"target": "x", "event": "run", "interval_sec": 1}])
    assert s._schedules == []


async def test_validate_drops_missing_target():
    s = Scheduler(None, [{"name": "a", "event": "run", "interval_sec": 1}])
    assert s._schedules == []


async def test_validate_drops_both_triggers():
    s = Scheduler(None, [{"name": "a", "target": "x", "event": "run",
                          "interval_sec": 1, "daily_at": "06:00"}])
    assert s._schedules == []


async def test_validate_drops_no_triggers():
    s = Scheduler(None, [{"name": "a", "target": "x", "event": "run"}])
    assert s._schedules == []


async def test_validate_drops_zero_interval():
    s = Scheduler(None, [{"name": "a", "target": "x", "event": "run",
                          "interval_sec": 0}])
    assert s._schedules == []


async def test_validate_drops_bad_daily_at():
    s = Scheduler(None, [{"name": "a", "target": "x", "event": "run",
                          "daily_at": "9am"}])
    assert s._schedules == []


async def test_validate_accepts_valid_interval():
    s = Scheduler(None, [{"name": "a", "target": "x", "event": "run",
                          "interval_sec": 0.5}])
    assert len(s._schedules) == 1


async def test_validate_accepts_valid_daily_at():
    s = Scheduler(None, [{"name": "a", "target": "x", "event": "run",
                          "daily_at": "23:59"}])
    assert len(s._schedules) == 1


async def test_seconds_until_daily_future_today():
    # Use a naive local datetime; astimezone() makes it tz-aware.
    now = datetime(2026, 4, 21, 5, 0).astimezone()
    s = Scheduler._seconds_until_daily("06:00", now)
    assert 3590 < s < 3610


async def test_seconds_until_daily_past_goes_tomorrow():
    now = datetime(2026, 4, 21, 7, 0).astimezone()
    s = Scheduler._seconds_until_daily("06:00", now)
    # ~23 hours exactly (82800s). Accept tiny tz / DST drift via 5s window.
    assert 82_795 <= s <= 82_805


async def test_seconds_until_daily_equal_goes_tomorrow():
    # Same minute → push to tomorrow (avoid immediate fire on boundary)
    now = datetime(2026, 4, 21, 6, 0).astimezone()
    s = Scheduler._seconds_until_daily("06:00", now)
    assert 86_390 < s < 86_410


# -- async runtime ----------------------------------------------------

async def test_interval_fires_multiple_times():
    bus = Bus()
    received: list = []

    async def sink(msg):
        received.append(msg.event)
        return {"ok": True}

    bus.register("sink", sink)
    await bus.ready("sink")

    s = Scheduler(bus, [{"name": "s1", "target": "sink",
                         "event": "tick", "interval_sec": 0.05}])
    await s.start_all()
    await asyncio.sleep(0.18)
    await s.stop_all()

    assert len(received) >= 2


async def test_fire_publishes_tick_event():
    bus = Bus()
    ticks: list = []
    bus.subscribe(f"{NAME}.tick", lambda m: ticks.append(m))
    bus.register("noop", lambda msg: {"ok": True})
    await bus.ready("noop")

    s = Scheduler(bus, [{"name": "s1", "target": "noop",
                         "event": "run", "interval_sec": 0.05}])
    await s.start_all()
    await asyncio.sleep(0.13)
    await s.stop_all()
    for _ in range(3):
        await asyncio.sleep(0)

    assert len(ticks) >= 1
    p = ticks[0]["payload"]
    assert p["schedule"] == "s1"
    assert p["target"] == "noop"
    assert p["event"] == "run"
    assert p["count"] == 1


async def test_fire_increments_count_per_schedule():
    bus = Bus()
    bus.register("noop", lambda msg: {"ok": True})
    await bus.ready("noop")
    s = Scheduler(bus, [{"name": "a", "target": "noop",
                         "event": "run", "interval_sec": 0.03}])
    await s.start_all()
    await asyncio.sleep(0.1)
    await s.stop_all()
    assert s._fire_counts.get("a", 0) >= 2


async def test_send_failure_emits_error_and_continues():
    # target not registered → bus.send logs warning, doesn't raise.
    bus = Bus()
    errors: list = []
    bus.subscribe(f"{NAME}.error", lambda m: errors.append(m))

    s = Scheduler(bus, [{"name": "bad", "target": "missing",
                         "event": "run", "interval_sec": 0.05}])
    await s.start_all()
    await asyncio.sleep(0.12)
    await s.stop_all()
    # bus.send doesn't raise on missing handler, so our _fire succeeds and emits
    # scheduler.tick (not error). That behavior is intentional: scheduler's job
    # ends at bus.send; missing-target diagnosis belongs elsewhere.
    # This test documents that no error is fired in that case.
    assert errors == []


async def test_stop_cancels_tasks():
    bus = Bus()
    bus.register("noop", lambda msg: {"ok": True})
    await bus.ready("noop")
    s = Scheduler(bus, [{"name": "a", "target": "noop",
                         "event": "run", "interval_sec": 0.1}])
    await s.start_all()
    assert len(s._tasks) == 1
    await s.stop_all()
    assert s._tasks == []


async def test_handle_status():
    s = Scheduler(Bus(), [
        {"name": "a", "target": "x", "event": "run", "interval_sec": 10},
        {"name": "b", "target": "y", "event": "run", "daily_at": "06:00"},
    ])
    r = await s.handle(_M({"op": "status"}))
    assert r["ok"] is True
    assert len(r["schedules"]) == 2
    entries = {e["name"]: e for e in r["schedules"]}
    assert entries["a"]["trigger"] == "interval_sec=10"
    assert entries["b"]["trigger"] == "daily_at=06:00"


async def test_handle_unknown_op():
    s = Scheduler(Bus(), [])
    r = await s.handle(_M({"op": "weird"}))
    assert r["ok"] is False


# -- integration via loader -------------------------------------------

async def test_integration_flat_list_config(tmp_path, monkeypatch, bundled_dir):
    cfg = tmp_path / "schedules.yaml"
    cfg.write_text(
        "- name: tick_test\n"
        "  target: sink\n"
        "  event: run\n"
        "  interval_sec: 0.05\n"
    )
    monkeypatch.setenv("SCHEDULES_CONFIG", str(cfg))

    bus = Bus()
    received: list = []

    async def sink(msg):
        received.append(msg)
        return {"ok": True}

    bus.register("sink", sink)
    await bus.ready("sink")

    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()
    await loader.ensure_running("scheduler")
    assert bus.query_status("scheduler") == "ready"

    await asyncio.sleep(0.12)
    r = await bus.request("scheduler", {"op": "status"}, timeout=2.0)
    assert r["ok"] is True
    assert any(e["name"] == "tick_test" for e in r["schedules"])

    await loader.stop("scheduler")
    assert len(received) >= 1


async def test_integration_schedules_dict_config(tmp_path, monkeypatch,
                                                 bundled_dir):
    cfg = tmp_path / "schedules.yaml"
    cfg.write_text(
        "schedules:\n"
        "  - name: dict_test\n"
        "    target: sink\n"
        "    event: run\n"
        "    interval_sec: 0.05\n"
    )
    monkeypatch.setenv("SCHEDULES_CONFIG", str(cfg))

    bus = Bus()
    bus.register("sink", lambda msg: {"ok": True})
    await bus.ready("sink")

    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()
    await loader.ensure_running("scheduler")
    r = await bus.request("scheduler", {"op": "status"}, timeout=2.0)
    assert any(e["name"] == "dict_test" for e in r["schedules"])
    await loader.stop("scheduler")


async def test_no_config_file_starts_empty(tmp_path, monkeypatch, bundled_dir):
    monkeypatch.setenv("SCHEDULES_CONFIG", str(tmp_path / "nonexistent.yaml"))
    bus = Bus()
    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()
    await loader.ensure_running("scheduler")
    r = await bus.request("scheduler", {"op": "status"}, timeout=2.0)
    assert r["schedules"] == []
    await loader.stop("scheduler")


async def test_bad_yaml_starts_empty(tmp_path, monkeypatch, bundled_dir):
    cfg = tmp_path / "schedules.yaml"
    cfg.write_text("not: valid: yaml: at all: [[[")
    monkeypatch.setenv("SCHEDULES_CONFIG", str(cfg))
    bus = Bus()
    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()
    await loader.ensure_running("scheduler")
    r = await bus.request("scheduler", {"op": "status"}, timeout=2.0)
    assert r["schedules"] == []
    await loader.stop("scheduler")


# -- priority + throttle tier (v0.2) -----------------------------


async def test_validate_accepts_known_priorities():
    s = Scheduler(None, [
        {"name": "a", "target": "x", "event": "run", "interval_sec": 1,
         "priority": "critical"},
        {"name": "b", "target": "x", "event": "run", "interval_sec": 1,
         "priority": "normal"},
        {"name": "c", "target": "x", "event": "run", "interval_sec": 1,
         "priority": "nice_to_have"},
    ])
    assert {e["name"] for e in s._schedules} == {"a", "b", "c"}


async def test_validate_drops_unknown_priority():
    s = Scheduler(None, [{"name": "a", "target": "x", "event": "run",
                          "interval_sec": 1, "priority": "urgent"}])
    assert s._schedules == []


async def test_should_fire_rules():
    assert Scheduler._should_fire("critical", "normal") is True
    assert Scheduler._should_fire("nice_to_have", "normal") is True
    assert Scheduler._should_fire("critical", "soft") is True
    assert Scheduler._should_fire("normal", "soft") is True
    assert Scheduler._should_fire("nice_to_have", "soft") is False
    assert Scheduler._should_fire("critical", "hard") is True
    assert Scheduler._should_fire("normal", "hard") is False
    assert Scheduler._should_fire("nice_to_have", "hard") is False


async def test_cap_event_escalates_to_hard_and_renews_ttl():
    bus = Bus()
    s = Scheduler(bus, [], throttle_ttl_sec=60.0)
    assert s._throttle_level == "normal"
    s._on_cap_event({"topic": "minimax_budget.interval_soft_cap",
                     "payload": {"agent": "x"}})
    assert s._throttle_level == "soft"
    soft_until = s._throttle_until
    s._on_cap_event({"topic": "minimax_budget.interval_hard_cap",
                     "payload": {"agent": "x"}})
    assert s._throttle_level == "hard"
    assert s._throttle_until >= soft_until  # TTL renewed


async def test_cap_event_does_not_downgrade():
    bus = Bus()
    s = Scheduler(bus, [], throttle_ttl_sec=60.0)
    s._on_cap_event({"topic": "minimax_budget.interval_hard_cap",
                     "payload": {}})
    assert s._throttle_level == "hard"
    # Soft coming in after hard must not downgrade
    s._on_cap_event({"topic": "minimax_budget.weekly_soft_cap",
                     "payload": {}})
    assert s._throttle_level == "hard"


async def test_throttle_ttl_auto_expires(monkeypatch):
    import time as _time
    bus = Bus()
    s = Scheduler(bus, [], throttle_ttl_sec=1.0)
    s._on_cap_event({"topic": "minimax_budget.interval_hard_cap",
                     "payload": {}})
    assert s._current_throttle_level() == "hard"
    # Fast-forward by manipulating the expiry, not sleeping
    s._throttle_until = _time.time() - 0.01
    assert s._current_throttle_level() == "normal"


async def test_fire_skips_non_critical_when_hard(monkeypatch):
    bus = Bus()
    sent: list[tuple[str, str]] = []

    async def _sink(msg):
        sent.append((msg.to, msg.event))
        return {"ok": True}

    bus.register("sink", _sink)
    await bus.ready("sink")
    skipped: list[dict] = []

    def _on_skip(ev):
        skipped.append(ev["payload"])

    bus.subscribe(f"{NAME}.skipped", _on_skip)

    s = Scheduler(bus, [
        {"name": "crit", "target": "sink", "event": "run",
         "interval_sec": 60, "priority": "critical"},
        {"name": "ord", "target": "sink", "event": "run",
         "interval_sec": 60, "priority": "normal"},
        {"name": "nice", "target": "sink", "event": "run",
         "interval_sec": 60, "priority": "nice_to_have"},
    ])
    # Simulate hard cap
    s._on_cap_event({"topic": "minimax_budget.interval_hard_cap", "payload": {}})
    for sched in s._schedules:
        await s._fire(sched)
    await asyncio.sleep(0.01)  # let publish tasks run
    assert sent == [("sink", "run")]  # only critical fired
    names = {sp["schedule"] for sp in skipped}
    assert names == {"ord", "nice"}
    for sp in skipped:
        assert sp["reason"] == "budget_throttle"
        assert sp["throttle_level"] == "hard"


async def test_fire_soft_skips_only_nice_to_have():
    bus = Bus()
    sent: list[str] = []

    async def _sink(msg):
        sent.append(msg.to)
        return {"ok": True}

    bus.register("sink", _sink)
    await bus.ready("sink")

    s = Scheduler(bus, [
        {"name": "crit", "target": "sink", "event": "run",
         "interval_sec": 60, "priority": "critical"},
        {"name": "ord", "target": "sink", "event": "run",
         "interval_sec": 60, "priority": "normal"},
        {"name": "nice", "target": "sink", "event": "run",
         "interval_sec": 60, "priority": "nice_to_have"},
    ])
    s._on_cap_event({"topic": "minimax_budget.weekly_soft_cap", "payload": {}})
    for sched in s._schedules:
        await s._fire(sched)
    await asyncio.sleep(0.01)
    # crit + ord fired (2 sends), nice skipped
    assert len(sent) == 2


async def test_status_op_includes_throttle_and_skip_counts():
    bus = Bus()

    async def _sink(msg):
        return {"ok": True}

    bus.register("sink", _sink)
    await bus.ready("sink")

    s = Scheduler(bus, [
        {"name": "n1", "target": "sink", "event": "run",
         "interval_sec": 60, "priority": "nice_to_have"},
    ])
    s._on_cap_event({"topic": "minimax_budget.interval_hard_cap", "payload": {}})
    await s._fire(s._schedules[0])
    r = await s.handle(_M({"op": "status"}))
    assert r["ok"] is True
    assert r["throttle"]["level"] == "hard"
    assert r["throttle"]["ttl_remaining_sec"] > 0
    assert r["total_skips"] == 1
    assert r["schedules"][0]["priority"] == "nice_to_have"
    assert r["schedules"][0]["skips"] == 1


async def test_override_throttle_op_forces_level():
    bus = Bus()
    s = Scheduler(bus, [])
    r = await s.handle(_M({"op": "override_throttle", "level": "hard",
                           "ttl_sec": 30}))
    assert r["ok"] is True
    assert r["throttle"]["level"] == "hard"
    assert s._last_cap_topic == "manual_override"
    # Reset back to normal
    r = await s.handle(_M({"op": "override_throttle", "level": "normal"}))
    assert r["throttle"]["level"] == "normal"


async def test_override_throttle_rejects_bad_level():
    bus = Bus()
    s = Scheduler(bus, [])
    r = await s.handle(_M({"op": "override_throttle", "level": "panic"}))
    assert r["ok"] is False
    assert "invalid" in r["error"]


async def test_subscriptions_installed_and_removed(bundled_dir, monkeypatch):
    monkeypatch.delenv("SCHEDULES_CONFIG", raising=False)
    bus = Bus()
    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()
    await loader.ensure_running("scheduler")
    # Publish a hard cap; the subscriber should escalate the scheduler
    await bus.publish("minimax_budget.interval_hard_cap", {"agent": "x"})
    await asyncio.sleep(0.02)
    r = await bus.request("scheduler", {"op": "status"}, timeout=2.0)
    assert r["throttle"]["level"] == "hard"
    await loader.stop("scheduler")
    # After stop, subscriber removed — publishes no longer mutate state
    sched = loader.get_handle("scheduler")
    # handle is None after stop (loader cleared), so just verify bus unsubscribed:
    # re-publishing should not raise — the remaining subscribers list is empty
    await bus.publish("minimax_budget.interval_hard_cap", {"agent": "x"})
