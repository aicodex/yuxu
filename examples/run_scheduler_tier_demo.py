"""Scheduler tier MVP demo.

Spins up scheduler with three schedules (one per priority), ticks them for
a few seconds at each throttle level, and prints a per-schedule fire/skip
table so you can see nice_to_have drop out on soft_cap and normal drop out
on hard_cap.

No real LLM, no MiniMax — we publish synthetic cap events directly on the bus.

    uv run python examples/run_scheduler_tier_demo.py
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict

from yuxu.bundled.scheduler.handler import NAME, Scheduler
from yuxu.core.bus import Bus


INTERVAL = 0.2           # schedule period (seconds)
PHASE_DURATION = 1.6     # wall-clock we let each phase run


def _now() -> str:
    return time.strftime("%H:%M:%S", time.localtime())


async def _print_stats(scheduler: Scheduler, phase: str) -> None:
    """Snapshot + pretty-print per-schedule fires / skips + throttle state."""
    r = await scheduler.handle(type("M", (), {"payload": {"op": "status"}})())
    throttle = r["throttle"]
    print(f"\n[{_now()}] === phase: {phase} ===")
    print(f"          throttle: level={throttle['level']} "
          f"ttl_remaining={throttle['ttl_remaining_sec']:.1f}s "
          f"last_cap={throttle['last_cap_topic']}")
    print(f"          {'name':<14} {'priority':<13} {'fires':>6} {'skips':>6}")
    for s in r["schedules"]:
        print(f"          {s['name']:<14} {s['priority']:<13} "
              f"{s['fires']:>6} {s['skips']:>6}")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )
    # Dial noisy loggers down — we care about demo output, not library chatter.
    logging.getLogger("yuxu.core.bus").setLevel(logging.WARNING)
    log = logging.getLogger("demo")

    bus = Bus()

    # Count what reached each target via bus.send.
    reached: dict[str, int] = defaultdict(int)

    async def _sink(msg):
        reached[msg.to] += 1
        return {"ok": True}

    for target in ("crit_target", "norm_target", "nice_target"):
        bus.register(target, _sink)
        await bus.ready(target)

    # Mirror scheduler.skipped to stdout so we can see the gating decisions.
    async def _on_skip(event):
        p = event["payload"]
        log.info("[SKIP] %s (priority=%s, level=%s)",
                 p["schedule"], p["priority"], p["throttle_level"])
    bus.subscribe("scheduler.skipped", _on_skip)

    scheduler = Scheduler(bus, [
        {"name": "crit_job", "target": "crit_target", "event": "run",
         "interval_sec": INTERVAL, "priority": "critical"},
        {"name": "norm_job", "target": "norm_target", "event": "run",
         "interval_sec": INTERVAL, "priority": "normal"},
        {"name": "nice_job", "target": "nice_target", "event": "run",
         "interval_sec": INTERVAL, "priority": "nice_to_have"},
    ], throttle_ttl_sec=120.0)
    bus.register(NAME, scheduler.handle)  # so bus.request("scheduler", ...) works
    await scheduler.start_all()

    try:
        # ------------- Phase 1: normal --------------
        print(f"\n[{_now()}] Phase 1: no cap — all three priorities should tick.")
        await asyncio.sleep(PHASE_DURATION)
        await _print_stats(scheduler, "normal (baseline)")

        # ------------- Phase 2: soft cap ------------
        print(f"\n[{_now()}] Publishing minimax_budget.interval_soft_cap …")
        await bus.publish("minimax_budget.interval_soft_cap",
                          {"agent": "demo", "pool": "minimax"})
        await asyncio.sleep(0.05)  # let subscriber wake
        await asyncio.sleep(PHASE_DURATION)
        await _print_stats(scheduler, "soft_cap (nice_to_have should stall)")

        # ------------- Phase 3: hard cap ------------
        print(f"\n[{_now()}] Publishing minimax_budget.interval_hard_cap …")
        await bus.publish("minimax_budget.interval_hard_cap",
                          {"agent": "demo", "pool": "minimax"})
        await asyncio.sleep(0.05)
        await asyncio.sleep(PHASE_DURATION)
        await _print_stats(scheduler, "hard_cap (only critical should tick)")

        # ------------- Phase 4: manual override ----
        print(f"\n[{_now()}] Manual /override_throttle → normal …")
        await bus.request(
            NAME,
            {"op": "override_throttle", "level": "normal"},
            timeout=2.0,
        )
        await asyncio.sleep(PHASE_DURATION)
        await _print_stats(scheduler, "override → normal (all resume)")

        # ------------- Summary ---------------------
        print(f"\n[{_now()}] === reached-target totals ===")
        for k in sorted(reached):
            print(f"          {k:<14} → {reached[k]}")

    finally:
        await scheduler.stop_all()


if __name__ == "__main__":
    asyncio.run(main())
