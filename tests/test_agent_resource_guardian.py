from __future__ import annotations

import asyncio

import pytest

from yuxu.bundled.resource_guardian.handler import ResourceGuardian
from yuxu.core.bus import Bus
from yuxu.core.loader import Loader

pytestmark = pytest.mark.asyncio


async def _yield():
    for _ in range(4):
        await asyncio.sleep(0)


async def test_error_count_tracked():
    bus = Bus()
    g = ResourceGuardian(bus, error_threshold=100)
    g.install()
    for _ in range(3):
        await bus.publish("theme_rank.error", {"msg": "oops"})
    await _yield()
    rep = g.report()
    assert rep["theme_rank"]["error"] == 3


async def test_throttle_count_tracked():
    bus = Bus()
    g = ResourceGuardian(bus, throttle_threshold=100)
    g.install()
    for _ in range(4):
        await bus.publish("_meta.ratelimit.throttled", {"agent": "llm_service"})
    await _yield()
    rep = g.report()
    assert rep["llm_service"]["throttled"] == 4


async def test_warning_emitted_on_threshold():
    bus = Bus()
    g = ResourceGuardian(bus, error_threshold=3)
    g.install()

    warnings = []
    bus.subscribe("alpha.resource_warning",
                  lambda ev: warnings.append(ev["payload"]))

    for _ in range(3):
        await bus.publish("alpha.error", {})
    await _yield()
    assert len(warnings) == 1
    assert warnings[0]["kind"] == "error"
    assert warnings[0]["count"] == 3


async def test_warning_suppressed_same_window():
    bus = Bus()
    g = ResourceGuardian(bus, window_sec=60, error_threshold=2)
    g.install()
    warnings = []
    bus.subscribe("alpha.resource_warning",
                  lambda ev: warnings.append(ev["payload"]))
    for _ in range(10):
        await bus.publish("alpha.error", {})
    await _yield()
    assert len(warnings) == 1  # only once per window


async def test_per_agent_counts_independent():
    bus = Bus()
    g = ResourceGuardian(bus, error_threshold=100)
    g.install()
    await bus.publish("a.error", {})
    await bus.publish("a.error", {})
    await bus.publish("b.error", {})
    await _yield()
    rep = g.report()
    assert rep["a"]["error"] == 2
    assert rep["b"]["error"] == 1


async def test_resource_warning_events_not_counted():
    bus = Bus()
    g = ResourceGuardian(bus, error_threshold=100)
    g.install()
    # Someone published a resource_warning (matches *.error pattern? no, .error is suffix)
    # But what if someone publishes foo.error.something? We match via suffix, which
    # should only fire on exact ".error". Verify:
    await bus.publish("x.error.extra", {})
    await _yield()
    rep = g.report()
    # topic is "x.error.extra" -> "*.error" pattern does NOT match (fnmatch needs exact)
    # Our handler checks suffix ".error"; "x.error.extra" does NOT endswith ".error"
    assert rep == {}


async def test_report_via_handle():
    bus = Bus()
    g = ResourceGuardian(bus, error_threshold=100)
    g.install()
    await bus.publish("q.error", {})
    await _yield()
    class _M: payload = {"op": "report"}
    r = await g.handle(_M())
    assert r["ok"] is True
    assert r["per_agent"]["q"]["error"] == 1


async def test_reset():
    bus = Bus()
    g = ResourceGuardian(bus, error_threshold=100)
    g.install()
    await bus.publish("x.error", {})
    await _yield()
    assert g.report() != {}
    class _M: payload = {"op": "reset"}
    r = await g.handle(_M())
    assert r["ok"] is True
    assert g.report() == {}


async def test_unknown_op():
    bus = Bus()
    g = ResourceGuardian(bus)
    class _M: payload = {"op": "foo"}
    r = await g.handle(_M())
    assert r["ok"] is False


async def test_window_prune(monkeypatch):
    bus = Bus()
    # 0.1s window
    g = ResourceGuardian(bus, window_sec=0.1, error_threshold=100)
    g.install()
    await bus.publish("z.error", {})
    await _yield()
    assert g.report()["z"]["error"] == 1
    await asyncio.sleep(0.15)
    # Next report: old event pruned
    assert g.report().get("z", {}).get("error", 0) == 0


async def test_integration_via_loader(bundled_dir):
    bus = Bus()
    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()
    await loader.ensure_running("resource_guardian")
    assert bus.query_status("resource_guardian") == "ready"

    r = await bus.request("resource_guardian", {"op": "report"}, timeout=2.0)
    assert r["ok"] is True
    assert r["per_agent"] == {}
