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


# -- v0.2 weighted admission (DWRR) + priority lane + deficit ---


async def test_weights_parsed_from_config():
    svc = RateLimitService({
        "p": {"accounts": [{"id": "a"}],
              "weights": {"agent_a": 4, "agent_b": 2}},
    })
    pool = svc.pools["p"]
    assert pool.weights == {"agent_a": 4, "agent_b": 2}


async def test_weights_reject_non_positive():
    svc = RateLimitService({
        "p": {"accounts": [{"id": "a"}],
              "weights": {"x": 0, "y": -1, "z": 3}},
    })
    assert svc.pools["p"].weights == {"z": 3}


async def test_actual_cost_debits_consumed_on_success():
    svc = RateLimitService({"p": {"accounts": [{"id": "a"}]}})
    async with svc.acquire("p", agent="bob", cost_hint=100) as h:
        h["actual_cost"] = 150.0
    # `consumed` is the invariant counter (monotonic in successful calls,
    # decoupled from DWRR refill churn)
    assert svc.pools["p"].consumed["bob"] == 150.0


async def test_no_actual_cost_means_not_consumed():
    """Simulates a failed call: caller never sets actual_cost. The call
    took a slot but didn't burn a "successful" cost."""
    svc = RateLimitService({"p": {"accounts": [{"id": "a"}]}})
    async with svc.acquire("p", agent="bob", cost_hint=100):
        pass  # no h["actual_cost"]
    assert svc.pools["p"].consumed.get("bob", 0.0) == 0.0


async def test_retry_priority_still_debits_on_success():
    """A successful retry does consume the logical-call's tokens. Only
    FAILED attempts (no actual_cost set) skip debit, regardless of priority.
    """
    svc = RateLimitService({"p": {"accounts": [{"id": "a"}]}})
    async with svc.acquire("p", agent="bob", cost_hint=50,
                           priority="retry") as h:
        h["actual_cost"] = 100.0
    assert svc.pools["p"].consumed["bob"] == 100.0


async def test_retry_priority_no_debit_on_failure():
    """Retry that also fails (rare, e.g. retry-ceiling exceeded) doesn't debit."""
    svc = RateLimitService({"p": {"accounts": [{"id": "a"}]}})
    async with svc.acquire("p", agent="bob", cost_hint=50,
                           priority="retry"):
        pass  # still no actual_cost
    assert svc.pools["p"].consumed.get("bob", 0.0) == 0.0


async def test_weighted_admission_favors_higher_weight():
    """weights a=3, b=1: a should dominate early slots.

    With max_concurrent=1 and a small hold per slot, the first few winners
    under contention reflect DWRR with initial credits of weight-each.
    """
    svc = RateLimitService({
        "p": {"accounts": [{"id": "x"}],
              "max_concurrent": 1,
              "weights": {"a": 3, "b": 1}},
    })
    served: list[str] = []

    async def _task(name):
        async with svc.acquire("p", agent=name, cost_hint=1) as h:
            served.append(name)
            h["actual_cost"] = 1
            await asyncio.sleep(0.01)

    tasks = [asyncio.create_task(_task("a")) for _ in range(4)]
    tasks += [asyncio.create_task(_task("b")) for _ in range(4)]
    await asyncio.gather(*tasks)

    # Everyone eventually runs
    assert served.count("a") == 4 and served.count("b") == 4
    # But "a" (weight 3) should dominate the first 4 — DWRR gives a 3 credits
    # and b only 1 per refill round.
    first_four = served[:4]
    assert first_four.count("a") >= 3


async def test_retry_lane_preempts_weighted_waiters():
    """A retry waiter that queues AFTER a normal waiter jumps ahead."""
    svc = RateLimitService({"p": {"accounts": [{"id": "x"}],
                                   "max_concurrent": 1}})
    served: list[str] = []
    release = asyncio.Event()

    async def _hold():
        async with svc.acquire("p", agent="holder", cost_hint=0) as h:
            h["actual_cost"] = 0
            await release.wait()

    async def _normal():
        async with svc.acquire("p", agent="normal", cost_hint=0) as h:
            served.append("normal")
            h["actual_cost"] = 0

    async def _retry():
        async with svc.acquire("p", agent="retrier", cost_hint=0,
                               priority="retry") as h:
            served.append("retry")
            h["actual_cost"] = 0

    holder = asyncio.create_task(_hold())
    await asyncio.sleep(0.02)
    n = asyncio.create_task(_normal())
    await asyncio.sleep(0.02)
    r = asyncio.create_task(_retry())
    await asyncio.sleep(0.02)
    release.set()
    await asyncio.gather(holder, n, r)
    assert served == ["retry", "normal"]


async def test_anon_caller_backwards_compatible():
    svc = RateLimitService({"p": {"accounts": [{"id": "x"}]}})
    async with svc.acquire("p") as h:
        assert h["agent"] == "_anon"
        assert h["priority"] == "normal"
        assert h["cost_hint"] == 0.0
        assert h["actual_cost"] is None


async def test_snapshot_exposes_weights_and_queue_sizes():
    svc = RateLimitService({
        "p": {"accounts": [{"id": "x"}],
              "weights": {"a": 2, "b": 1}},
    })
    snap = svc.snapshot()
    assert snap["p"]["weights"] == {"a": 2, "b": 1}
    assert snap["p"]["retry_waiters"] == 0
    assert snap["p"]["weighted_waiters"] == 0


async def test_invalid_priority_raises():
    svc = RateLimitService({"p": {"accounts": [{"id": "x"}]}})
    with pytest.raises(ValueError, match="priority"):
        async with svc.acquire("p", priority="urgent"):
            pass
