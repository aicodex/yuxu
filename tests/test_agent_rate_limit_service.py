from __future__ import annotations

import asyncio
import time

import pytest

from yuxu.bundled.rate_limit_service.handler import RateLimitService
from yuxu.core.bus import Bus
from yuxu.core.loader import Loader

pytestmark = pytest.mark.asyncio


# -- basic --------------------------------------------------------

async def test_acquire_and_release():
    svc = RateLimitService({
        "p": {"max_concurrent": 2, "accounts": [{"id": "k1", "api_key": "secret"}]}
    })
    async with svc.acquire("p") as ctx:
        assert ctx["pool"] == "p"
        assert ctx["account"] == "k1"
        assert ctx["extra"]["api_key"] == "secret"
        assert svc.pools["p"].accounts[0].concurrent == 1
    assert svc.pools["p"].accounts[0].concurrent == 0


async def test_unknown_pool_raises():
    svc = RateLimitService({})
    with pytest.raises(KeyError):
        async with svc.acquire("ghost"):
            pass


async def test_concurrent_cap_queues_second_caller():
    svc = RateLimitService({"p": {"max_concurrent": 1, "accounts": [{"id": "k1"}]}})
    order = []

    async def worker(tag, hold):
        async with svc.acquire("p"):
            order.append(f"{tag}:in")
            await asyncio.sleep(hold)
            order.append(f"{tag}:out")

    await asyncio.gather(worker("A", 0.05), worker("B", 0.01))
    # A enters first; B must wait for A to release
    assert order[0].endswith("in")
    assert order.index("A:out") < order.index("B:in")


async def test_concurrent_cap_with_two_accounts_parallel():
    svc = RateLimitService({
        "p": {"max_concurrent": 1, "accounts": [{"id": "k1"}, {"id": "k2"}]}
    })
    ids = []

    async def worker():
        async with svc.acquire("p") as ctx:
            ids.append(ctx["account"])
            await asyncio.sleep(0.02)

    await asyncio.gather(worker(), worker())
    # least_load spreads load: both accounts used
    assert set(ids) == {"k1", "k2"}


async def test_least_load_strategy_picks_idlest():
    svc = RateLimitService({
        "p": {"max_concurrent": 10, "accounts": [{"id": "k1"}, {"id": "k2"}]}
    })
    k1, k2 = svc.pools["p"].accounts
    k1.concurrent = 3
    k2.concurrent = 1
    async with svc.acquire("p") as ctx:
        assert ctx["account"] == "k2"


async def test_round_robin_strategy():
    svc = RateLimitService({
        "p": {
            "max_concurrent": 5,
            "strategy": "round_robin",
            "accounts": [{"id": "k1"}, {"id": "k2"}, {"id": "k3"}],
        }
    })
    seen = []
    for _ in range(6):
        async with svc.acquire("p") as ctx:
            seen.append(ctx["account"])
    assert seen == ["k1", "k2", "k3", "k1", "k2", "k3"]


async def test_rpm_enforced_and_waits(monkeypatch):
    # Shrink the RPM window so the test runs in real time without hacks.
    import yuxu.bundled.rate_limit_service.handler as hmod
    monkeypatch.setattr(hmod.RateLimitService, "RPM_WINDOW_SEC", 0.2)

    svc = RateLimitService({"p": {"rpm": 2, "accounts": [{"id": "k1"}]}})
    async with svc.acquire("p", timeout=1.0):
        pass
    async with svc.acquire("p", timeout=1.0):
        pass
    # Third call inside the 0.2s window must wait until oldest rolls off.
    t0 = time.monotonic()
    async with svc.acquire("p", timeout=2.0):
        pass
    elapsed = time.monotonic() - t0
    assert elapsed > 0.1, f"expected RPM gating to wait, elapsed={elapsed:.3f}"
    assert elapsed < 1.0


async def test_rpm_timeout_when_window_never_drains(monkeypatch):
    import yuxu.bundled.rate_limit_service.handler as hmod
    monkeypatch.setattr(hmod.RateLimitService, "RPM_WINDOW_SEC", 10.0)

    svc = RateLimitService({"p": {"rpm": 1, "accounts": [{"id": "k1"}]}})
    async with svc.acquire("p"):
        pass
    # Window is 10s, rpm 1 -> 2nd call must wait; with 0.1 timeout it raises.
    with pytest.raises(asyncio.TimeoutError):
        async with svc.acquire("p", timeout=0.1):
            pass


async def test_acquire_timeout_raises():
    svc = RateLimitService({"p": {"max_concurrent": 1, "accounts": [{"id": "k1"}]}})

    async def hold():
        async with svc.acquire("p"):
            await asyncio.sleep(0.5)

    holder = asyncio.create_task(hold())
    await asyncio.sleep(0.02)  # let holder acquire
    with pytest.raises(asyncio.TimeoutError):
        async with svc.acquire("p", timeout=0.05):
            pass
    await holder


async def test_release_on_exception():
    svc = RateLimitService({"p": {"max_concurrent": 1, "accounts": [{"id": "k1"}]}})
    try:
        async with svc.acquire("p"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert svc.pools["p"].accounts[0].concurrent == 0
    # Can still acquire after exception
    async with svc.acquire("p"):
        pass


async def test_snapshot_and_handle_status():
    svc = RateLimitService({
        "p": {"max_concurrent": 3, "rpm": 10, "accounts": [{"id": "k1"}]}
    })
    async with svc.acquire("p"):
        snap = svc.snapshot()
        assert snap["p"]["accounts"][0]["concurrent"] == 1
    # via handle()
    class M: payload = {"op": "status"}
    r = await svc.handle(M())
    assert r["ok"] is True
    assert "p" in r["pools"]


async def test_handle_unknown_op():
    svc = RateLimitService({})
    class M: payload = {"op": "reload"}
    r = await svc.handle(M())
    assert r["ok"] is False


async def test_bad_config_ignored():
    svc = RateLimitService({
        "p": "not a dict",
        "q": {"accounts": [{"no_id": "x"}]},
        "good": {"accounts": [{"id": "k1"}]},
    })
    assert "p" not in svc.pools
    assert "q" not in svc.pools
    assert "good" in svc.pools


# -- bus integration ---------------------------------------------

async def test_bus_integration_yaml(tmp_path, monkeypatch, bundled_dir):
    cfg = tmp_path / "rate.yaml"
    cfg.write_text(
        "minimax:\n"
        "  max_concurrent: 2\n"
        "  accounts:\n"
        "    - id: key1\n"
        "      api_key: k1v\n"
    )
    monkeypatch.setenv("RATE_LIMITS_CONFIG", str(cfg))
    bus = Bus()
    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()
    await loader.ensure_running("rate_limit_service")
    assert bus.query_status("rate_limit_service") == "ready"

    rls = loader.get_handle("rate_limit_service")
    async with rls.acquire("minimax") as lease:
        assert lease["account"] == "key1"
        assert lease["extra"]["api_key"] == "k1v"

    r = await bus.request("rate_limit_service", {"op": "status"}, timeout=1.0)
    assert r["ok"] is True
    assert "minimax" in r["pools"]


async def test_rate_limit_unknown_pool_raises(tmp_path, monkeypatch, bundled_dir):
    cfg = tmp_path / "rate.yaml"
    cfg.write_text("p:\n  accounts:\n    - id: default\n")
    monkeypatch.setenv("RATE_LIMITS_CONFIG", str(cfg))
    bus = Bus()
    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()
    await loader.ensure_running("rate_limit_service")
    rls = loader.get_handle("rate_limit_service")
    with pytest.raises(KeyError):
        async with rls.acquire("ghost"):
            pass
