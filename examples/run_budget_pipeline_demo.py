"""Closed-loop budget pipeline demo (phases 1 + 2 + 3 integration).

Walks through three scenarios against a mocked LLM provider:

  1. cost-per-call estimate (minimax_budget):
     feed synthetic per-agent usage, watch 1h rolling mean override the
     cross-agent fallback as samples accumulate.

  2. weighted admission (rate_limit_service):
     two agents with weights 4 and 1 share a 1-slot pool; the 4-weight
     agent wins ≈4× more slots over a contention batch.

  3. 1002 retry with priority lane (llm_service + llm_driver + rate_limit):
     mock provider returns 1002 on first call, success on retry; driver
     detects the error_kind, waits backoff, re-acquires via the retry lane
     (jumps the queue past other waiters), succeeds on attempt 2.

No real network, no real LLM, no disk. Pure mocks + in-memory scheduling.

    python examples/run_budget_pipeline_demo.py
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from yuxu.bundled.llm_driver.handler import LlmDriver
from yuxu.bundled.llm_service.handler import LLMService
from yuxu.bundled.minimax_budget.handler import (
    COMPLETION_TOPIC,
    MiniMaxBudget,
)
from yuxu.bundled.rate_limit_service.handler import RateLimitService
from yuxu.core.bus import Bus


def _now() -> str:
    return time.strftime("%H:%M:%S", time.localtime())


def _make_ctx(bus):
    class _Ctx:
        def __init__(self):
            self.bus = bus
        def get_agent(self, name):
            return None
    return _Ctx()


# ============ Scenario 1: cost-per-call estimate =============


async def scenario_cost_estimate() -> None:
    print(f"\n[{_now()}] " + "=" * 60)
    print(f"[{_now()}] Scenario 1: minimax_budget rolling cost estimate")
    print(f"[{_now()}] " + "=" * 60)
    bus = Bus()
    budget = MiniMaxBudget(_make_ctx(bus),
                           http_client=httpx.AsyncClient(
                               transport=httpx.MockTransport(
                                   lambda r: httpx.Response(200,
                                       json={"base_resp": {"status_code": 0},
                                             "model_remains": []}))))

    def show(agent):
        r = budget.estimate_cost_per_call(agent)
        print(f"          {agent:<15} → cost={r['value']:<10.1f} "
              f"source={r['source']:<15} calls_1h={r['calls_1h']}")

    # A: no history anywhere → DEFAULT (2000)
    print(f"\n[{_now()}] Stage A: fresh install, no history")
    show("newbie")

    # B: heavy_bot logs 4 calls → new call from other agent hits global mean
    print(f"\n[{_now()}] Stage B: 'heavy_bot' posts 4 calls @ 3000 each")
    for _ in range(4):
        await budget._on_llm_completed({
            "topic": COMPLETION_TOPIC,
            "payload": {"agent": "heavy_bot", "model": "M",
                        "usage": {"total_tokens": 3000}},
        })
    show("newbie")              # uses global mean (3000)
    show("heavy_bot")            # per-agent: 4 samples ≥ N_MIN_1H=3

    # C: new "light_bot" posts 2 calls (below N_MIN_1H) → still global mean
    print(f"\n[{_now()}] Stage C: 'light_bot' posts 2 calls @ 100 each")
    for _ in range(2):
        await budget._on_llm_completed({
            "topic": COMPLETION_TOPIC,
            "payload": {"agent": "light_bot", "model": "M",
                        "usage": {"total_tokens": 100}},
        })
    show("light_bot")            # still global_mean (12200 / 6 ≈ 2033)

    # D: light_bot posts 2 more → crosses N_MIN_1H=3 threshold
    print(f"\n[{_now()}] Stage D: 'light_bot' posts 2 more → crosses 3-sample threshold")
    for _ in range(2):
        await budget._on_llm_completed({
            "topic": COMPLETION_TOPIC,
            "payload": {"agent": "light_bot", "model": "M",
                        "usage": {"total_tokens": 100}},
        })
    show("light_bot")            # now per_agent_1h mean = 100
    show("heavy_bot")            # still per_agent_1h mean = 3000
    await budget.uninstall()


# ============ Scenario 2: weighted admission =================


async def scenario_weighted_admission() -> None:
    print(f"\n[{_now()}] " + "=" * 60)
    print(f"[{_now()}] Scenario 2: rate_limit_service weighted admission")
    print(f"[{_now()}] " + "=" * 60)
    svc = RateLimitService({
        "p": {"accounts": [{"id": "x"}],
              "max_concurrent": 1,     # force serialization to see WRR effect
              "weights": {"alpha": 4, "beta": 1}},
    })
    served: list[str] = []

    async def task(name):
        async with svc.acquire("p", agent=name, cost_hint=1) as h:
            served.append(name)
            h["actual_cost"] = 1
            await asyncio.sleep(0.01)

    print(f"\n[{_now()}] Firing 5 tasks each from alpha (weight=4) and beta (weight=1)")
    coros = [task("alpha") for _ in range(5)] + [task("beta") for _ in range(5)]
    await asyncio.gather(*coros)
    print(f"          serve order: {' '.join(served)}")
    print(f"          counts:      alpha={served.count('alpha')} beta={served.count('beta')}")
    print(f"          first-8 ratio: {served[:8].count('alpha')}:{served[:8].count('beta')}"
          "   (expect ~4:1 early, evens out once alpha drains)")
    print(f"          credits after:  {dict(svc.pools['p'].credits)}")
    print(f"          consumed after: {dict(svc.pools['p'].consumed)}")


# ============ Scenario 3: 1002 retry via priority lane ========


async def scenario_retry_with_priority() -> None:
    print(f"\n[{_now()}] " + "=" * 60)
    print(f"[{_now()}] Scenario 3: 1002 retry via priority lane")
    print(f"[{_now()}] " + "=" * 60)
    # Mock provider: first call → 1002, subsequent → 200 OK
    call_count = [0]

    def route(req):
        call_count[0] += 1
        if call_count[0] == 1:
            print(f"          [provider] attempt {call_count[0]} → "
                  "returning MiniMax 1002")
            return httpx.Response(200, json={
                "base_resp": {"status_code": 1002,
                              "status_msg": "RPM exceeded"},
            })
        print(f"          [provider] attempt {call_count[0]} → returning 200 OK")
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "hello"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                      "total_tokens": 15},
            "base_resp": {"status_code": 0},
        })

    rl = RateLimitService({
        "minimax": {"accounts": [{"id": "k", "api_key": "x",
                                   "base_url": "http://a/v1"}]},
    })
    bus = Bus()
    svc = LLMService(rl.acquire, bus=bus,
                     client=httpx.AsyncClient(transport=httpx.MockTransport(route)))
    bus.register("llm_service", svc.handle)
    driver = LlmDriver(bus)
    driver.RETRY_BACKOFF_BASE_SEC = 0.05   # small so demo runs quickly

    print(f"\n[{_now()}] Running driver.run_turn with agent='solo_bot'")
    t0 = time.perf_counter()
    result = await driver.run_turn(
        system_prompt="you are helpful",
        messages=[{"role": "user", "content": "hi"}],
        pool="minimax", model="M", agent="solo_bot",
    )
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"\n          result ok={result['ok']} "
          f"content={result['content']!r}")
    print(f"          retries={result['retries']} "
          f"elapsed_wall={elapsed:.0f}ms")
    print(f"          pool consumed (debited on SUCCESS only): "
          f"{dict(rl.pools['minimax'].consumed)}   "
          "← successful retry debited 15 tokens; the 1002 attempt didn't")

    await svc.close()


async def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )
    await scenario_cost_estimate()
    await scenario_weighted_admission()
    await scenario_retry_with_priority()
    print(f"\n[{_now()}] Done.")


if __name__ == "__main__":
    asyncio.run(main())
