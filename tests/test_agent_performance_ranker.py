"""performance_ranker — aggregate per-agent negative signals and rank worst."""
from __future__ import annotations

import asyncio
import time

import pytest

from yuxu.bundled.performance_ranker.handler import NAME, PerformanceRanker
from yuxu.core.bus import Bus
from yuxu.core.loader import Loader

pytestmark = pytest.mark.asyncio


class _M:
    def __init__(self, payload):
        self.payload = payload


# -- unit: scoring + windowing ---------------------------------


async def test_record_error_and_rejection_scores():
    bus = Bus()
    r = PerformanceRanker(bus)
    r._record("agent_a", "error")
    r._record("agent_a", "error")
    r._record("agent_a", "rejected")
    errs, rejs = r._breakdown("agent_a")
    assert (errs, rejs) == (2, 1)
    assert r._compute_score(errs, rejs) == 2 * 1.0 + 1 * 2.0


async def test_unknown_agent_zero_score():
    r = PerformanceRanker(Bus())
    assert r._breakdown("ghost") == (0, 0)


async def test_window_prunes_stale_events():
    r = PerformanceRanker(Bus(), window_hours=1.0 / 3600)  # 1-second window
    r._record("flaky", "error")
    # simulate time passage by rewriting the queued event's timestamp
    r._events["flaky"][0].ts = time.monotonic() - 10.0
    errs, rejs = r._breakdown("flaky")
    assert errs == 0 and rejs == 0


async def test_underscore_agents_ignored():
    r = PerformanceRanker(Bus())
    r._record("_meta", "error")
    r._record("", "error")
    assert r._events == {}


async def test_custom_weights():
    r = PerformanceRanker(Bus(), weight_error=3.0, weight_rejected=0.5)
    r._record("x", "error")
    r._record("x", "rejected")
    errs, rejs = r._breakdown("x")
    assert r._compute_score(errs, rejs) == 3.0 + 0.5


# -- subscription: *.error + approval_queue.rejected ------------


async def test_on_error_extracts_agent_from_topic():
    r = PerformanceRanker(Bus())
    await r._on_error({"topic": "llm_driver.error", "payload": {"x": 1}})
    assert r._breakdown("llm_driver") == (1, 0)


async def test_on_error_skips_resource_warning():
    r = PerformanceRanker(Bus())
    await r._on_error({"topic": "some.resource_warning"})
    assert r._events == {}


async def test_on_rejection_uses_requester():
    r = PerformanceRanker(Bus())
    await r._on_rejection({"topic": "approval_queue.rejected",
                            "payload": {"requester": "reflection_agent",
                                        "approval_id": "abc"}})
    assert r._breakdown("reflection_agent") == (0, 1)


async def test_on_rejection_missing_requester_ignored():
    r = PerformanceRanker(Bus())
    await r._on_rejection({"topic": "approval_queue.rejected",
                            "payload": {"approval_id": "abc"}})
    assert r._events == {}


# -- ops: rank / score / reset ---------------------------------


async def test_rank_orders_descending_by_score():
    r = PerformanceRanker(Bus())
    for _ in range(3):
        r._record("ord", "error")           # score 3
    r._record("worst", "error")
    r._record("worst", "rejected")          # score 3
    r._record("worst", "rejected")          # score 5 total
    r._record("mild", "error")              # score 1
    resp = await r.handle(_M({"op": "rank"}))
    agents = [row["agent"] for row in resp["ranked"]]
    assert agents == ["worst", "ord", "mild"]


async def test_rank_respects_limit_and_min_score():
    r = PerformanceRanker(Bus())
    r._record("a", "error")                 # 1.0
    r._record("b", "error")
    r._record("b", "error")                 # 2.0
    r._record("c", "rejected")              # 2.0
    resp = await r.handle(_M({"op": "rank", "limit": 2}))
    assert len(resp["ranked"]) == 2
    # Tie break by agent name ascending
    assert [x["agent"] for x in resp["ranked"]] == ["b", "c"]
    # min_score filters low scorers out
    resp = await r.handle(_M({"op": "rank", "min_score": 1.5}))
    assert {x["agent"] for x in resp["ranked"]} == {"b", "c"}


async def test_score_op_returns_breakdown():
    r = PerformanceRanker(Bus())
    r._record("x", "error")
    r._record("x", "rejected")
    resp = await r.handle(_M({"op": "score", "agent": "x"}))
    assert resp["ok"] is True
    assert resp["agent"] == "x"
    assert resp["errors"] == 1
    assert resp["rejections"] == 1
    assert resp["score"] == 3.0  # 1 * 1.0 + 1 * 2.0


async def test_score_op_missing_agent_errors():
    r = PerformanceRanker(Bus())
    resp = await r.handle(_M({"op": "score"}))
    assert resp["ok"] is False
    assert "missing" in resp["error"]


async def test_reset_specific_agent_clears_only_that():
    r = PerformanceRanker(Bus())
    r._record("a", "error")
    r._record("b", "error")
    resp = await r.handle(_M({"op": "reset", "agent": "a"}))
    assert resp["ok"] is True
    assert resp["cleared"] == 1
    assert r._breakdown("a") == (0, 0)
    assert r._breakdown("b") == (1, 0)


async def test_reset_all_wipes_everything():
    r = PerformanceRanker(Bus())
    r._record("a", "error")
    r._record("b", "rejected")
    r._record("c", "error")
    resp = await r.handle(_M({"op": "reset"}))
    assert resp["cleared"] == 3
    assert r._events == {}


async def test_unknown_op_returns_error():
    r = PerformanceRanker(Bus())
    resp = await r.handle(_M({"op": "salsa"}))
    assert resp["ok"] is False


# -- integration via Loader + bus pub/sub -----------------------


async def test_loader_starts_and_subscribes(bundled_dir):
    bus = Bus()
    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()
    await loader.ensure_running("performance_ranker")
    assert bus.query_status("performance_ranker") == "ready"

    # Publish error + rejection through the real bus
    await bus.publish("alpha_bot.error", {"trace": "boom"})
    await bus.publish("approval_queue.rejected",
                      {"requester": "beta_bot", "approval_id": "a1"})
    # Extra signal
    await bus.publish("alpha_bot.error", {"trace": "boom"})
    await asyncio.sleep(0.02)

    resp = await bus.request("performance_ranker", {"op": "rank"}, timeout=1.0)
    ranked = {row["agent"]: row for row in resp["ranked"]}
    assert ranked["alpha_bot"]["errors"] == 2
    assert ranked["alpha_bot"]["score"] == 2.0
    assert ranked["beta_bot"]["rejections"] == 1
    assert ranked["beta_bot"]["score"] == 2.0

    await loader.stop("performance_ranker")


@pytest.fixture
def bundled_dir():
    import yuxu as _y
    from pathlib import Path as _P
    return str(_P(_y.__file__).parent / "bundled")
