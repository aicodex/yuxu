"""performance_ranker v0.1 demo.

Spins up the ranker, publishes a realistic mix of `*.error` and
`approval_queue.rejected` events across a handful of synthetic agents,
then pulls `rank` / `score` / `reset` snapshots so you can see how the
weighted score falls out.

No LLM, no disk — pure in-memory.

    uv run python examples/run_performance_ranker_demo.py
"""
from __future__ import annotations

import asyncio
import logging
import time

from yuxu.bundled.performance_ranker.handler import NAME, PerformanceRanker
from yuxu.core.bus import Bus


def _now() -> str:
    return time.strftime("%H:%M:%S", time.localtime())


async def _print_rank(ranker: PerformanceRanker, title: str, **op_kwargs) -> None:
    resp = await ranker.handle(type("M", (), {
        "payload": {"op": "rank", **op_kwargs}})())
    print(f"\n[{_now()}] === {title} ===")
    print(f"          window={resp['window_hours']}h "
          f"rows={len(resp['ranked'])}")
    print(f"          {'agent':<22} {'score':>6}  {'err':>3} {'rej':>3}")
    for row in resp["ranked"]:
        print(f"          {row['agent']:<22} {row['score']:>6.1f}  "
              f"{row['errors']:>3} {row['rejections']:>3}")


async def _print_score(ranker: PerformanceRanker, agent: str) -> None:
    resp = await ranker.handle(type("M", (), {
        "payload": {"op": "score", "agent": agent}})())
    print(f"[{_now()}] score({agent}): {resp}")


async def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )

    bus = Bus()
    ranker = PerformanceRanker(bus)
    ranker.install()
    bus.register(NAME, ranker.handle)

    # -- Phase 1: drip realistic signals --------------------------
    # Scenario: a typical yuxu day where reflection_agent has been iffy,
    # harness_pro_max is hitting occasional errors, and the newsfeed_demo
    # agent is relatively healthy.
    print(f"[{_now()}] Publishing synthetic signals …")

    # harness_pro_max: 4 errors (e.g. classify_intent fails)
    for _ in range(4):
        await bus.publish("harness_pro_max.error",
                          {"stage": "classify_intent", "detail": "json parse"})

    # reflection_agent: 2 errors + 3 rejections (its proposals got
    # user-rejected by approval_queue)
    for _ in range(2):
        await bus.publish("reflection_agent.error",
                          {"stage": "hypothesize", "detail": "timeout"})
    for i in range(3):
        await bus.publish("approval_queue.rejected",
                          {"approval_id": f"ref-{i}",
                           "requester": "reflection_agent",
                           "action": "memory_edit",
                           "reason": "not actionable"})

    # memory_curator: 1 rejection (milder)
    await bus.publish("approval_queue.rejected",
                      {"approval_id": "cur-1",
                       "requester": "memory_curator",
                       "action": "memory_edit"})

    # newsfeed_demo: 1 error (isolated blip — should rank low)
    await bus.publish("newsfeed_demo.error",
                      {"stage": "fetch", "detail": "404"})

    # Not counted: _meta.* (bus infra), resource_warning (guardian alerts)
    await bus.publish("_meta.something.error", {"skip": "infra"})
    await bus.publish("harness_pro_max.resource_warning",
                      {"kind": "error", "count": 10})

    # Let subscriber tasks drain
    await asyncio.sleep(0.05)

    # -- Phase 2: rank --------------------------------------------
    await _print_rank(ranker, "full ranking (worst first)")
    print()
    print(f"         Expected tie-break: reflection_agent (2*1 + 3*2 = 8)")
    print(f"                             harness_pro_max (4*1 + 0*2 = 4)")
    print(f"                             memory_curator  (0*1 + 1*2 = 2)")
    print(f"                             newsfeed_demo   (1*1 + 0*2 = 1)")

    # -- Phase 3: limit + min_score -------------------------------
    await _print_rank(ranker, "top-2 by limit", limit=2)
    await _print_rank(ranker, "min_score >= 3.0 (drop low performers)",
                       min_score=3.0)

    # -- Phase 4: per-agent score ---------------------------------
    print()
    await _print_score(ranker, "reflection_agent")
    await _print_score(ranker, "newsfeed_demo")
    await _print_score(ranker, "nonexistent_agent")   # zero baseline

    # -- Phase 5: reset one then all ------------------------------
    print(f"\n[{_now()}] Reset single agent (reflection_agent) …")
    resp = await ranker.handle(type("M", (), {
        "payload": {"op": "reset", "agent": "reflection_agent"}})())
    print(f"          cleared {resp['cleared']} events")
    await _print_rank(ranker, "after single-agent reset")

    print(f"\n[{_now()}] Reset all …")
    resp = await ranker.handle(type("M", (), {"payload": {"op": "reset"}})())
    print(f"          cleared {resp['cleared']} events total")
    await _print_rank(ranker, "after full reset (should be empty)")

    ranker.uninstall()


if __name__ == "__main__":
    asyncio.run(main())
