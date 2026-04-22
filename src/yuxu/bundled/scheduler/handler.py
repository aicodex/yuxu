"""Scheduler — cron-lite trigger registry.

v0.1 trigger types (exactly one per schedule):
  - interval_sec: N     fire every N seconds; first fire at start+N
  - daily_at: "HH:MM"   fire once per day at local wall-clock HH:MM

Each fire does bus.send(target, event, payload) and publishes scheduler.tick.
Send failures are logged and emit scheduler.error; they do not kill the loop.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger(__name__)

NAME = "scheduler"


class Scheduler:
    def __init__(self, bus, schedules: list[dict]) -> None:
        self.bus = bus
        self._schedules = [s for s in schedules if self._validate(s)]
        self._tasks: list[asyncio.Task] = []
        self._fire_counts: dict[str, int] = {}

    @staticmethod
    def _validate(s: dict) -> bool:
        if not isinstance(s, dict):
            return False
        name = s.get("name")
        if not isinstance(name, str) or not name:
            log.warning("scheduler: drop entry missing 'name': %r", s)
            return False
        for field in ("target", "event"):
            if not isinstance(s.get(field), str) or not s[field]:
                log.warning("scheduler: drop %s: missing %r", name, field)
                return False
        has_int = "interval_sec" in s
        has_day = "daily_at" in s
        if has_int == has_day:
            log.warning("scheduler: drop %s: must have exactly one of "
                        "interval_sec/daily_at", name)
            return False
        if has_int:
            try:
                iv = float(s["interval_sec"])
            except (TypeError, ValueError):
                log.warning("scheduler: drop %s: bad interval_sec", name)
                return False
            if iv <= 0:
                log.warning("scheduler: drop %s: interval_sec must be > 0", name)
                return False
        else:
            try:
                datetime.strptime(s["daily_at"], "%H:%M")
            except (TypeError, ValueError):
                log.warning("scheduler: drop %s: bad daily_at (want 'HH:MM')",
                            name)
                return False
        return True

    @staticmethod
    def _seconds_until_daily(hm: str,
                             now: Optional[datetime] = None) -> float:
        now = now or datetime.now().astimezone()
        hh, mm = (int(x) for x in hm.split(":"))
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)
        return (target - now).total_seconds()

    async def start_all(self) -> None:
        for s in self._schedules:
            t = asyncio.create_task(self._run(s), name=f"schedule:{s['name']}")
            self._tasks.append(t)

    async def stop_all(self) -> None:
        for t in self._tasks:
            if not t.done():
                t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

    async def _run(self, s: dict) -> None:
        name = s["name"]
        try:
            while True:
                if "interval_sec" in s:
                    wait = float(s["interval_sec"])
                else:
                    wait = self._seconds_until_daily(s["daily_at"])
                await asyncio.sleep(wait)
                await self._fire(s)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduler: %s loop crashed", name)
            await self.bus.publish(f"{NAME}.error", {
                "schedule": name, "error": "loop crashed (see log)",
            })

    async def _fire(self, s: dict) -> None:
        name = s["name"]
        fired_at = datetime.now(timezone.utc).isoformat()
        self._fire_counts[name] = self._fire_counts.get(name, 0) + 1
        try:
            await self.bus.send(s["target"], s["event"], s.get("payload") or {})
        except Exception as e:
            log.exception("scheduler: send failed for %s", name)
            await self.bus.publish(f"{NAME}.error", {
                "schedule": name, "target": s["target"], "error": str(e),
            })
            return
        await self.bus.publish(f"{NAME}.tick", {
            "schedule": name, "target": s["target"], "event": s["event"],
            "fired_at": fired_at, "count": self._fire_counts[name],
        })

    def _list(self) -> list[dict]:
        return [
            {
                "name": s["name"],
                "target": s["target"],
                "event": s["event"],
                "trigger": (f"interval_sec={s['interval_sec']}"
                            if "interval_sec" in s
                            else f"daily_at={s['daily_at']}"),
                "fires": self._fire_counts.get(s["name"], 0),
            }
            for s in self._schedules
        ]

    async def handle(self, msg) -> dict:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        op = payload.get("op", "status")
        if op in ("status", "list"):
            return {
                "ok": True,
                "schedules": self._list(),
                "total_fires": sum(self._fire_counts.values()),
            }
        return {"ok": False, "error": f"unknown op: {op!r}"}
