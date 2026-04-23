"""Scheduler — cron-lite trigger registry with budget-aware tiering.

v0.1 trigger types (exactly one per schedule):
  - interval_sec: N     fire every N seconds; first fire at start+N
  - daily_at: "HH:MM"   fire once per day at local wall-clock HH:MM

v0.2 priority tiers (optional, default "normal"):
  - critical     always fires
  - normal       fires unless throttle level is hard
  - nice_to_have fires only when throttle level is normal (i.e. no cap)

Each fire does bus.send(target, event, payload) and publishes scheduler.tick.
Throttle escalates on minimax_budget.{interval,weekly}_{soft,hard}_cap events
and auto-expires after THROTTLE_TTL_SEC (default 1800s / 30min) — new cap
events renew. Skipped fires publish scheduler.skipped with the reason.

Send failures are logged and emit scheduler.error; they do not kill the loop.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger(__name__)

NAME = "scheduler"

VALID_PRIORITIES = ("critical", "normal", "nice_to_have")
VALID_LEVELS = ("normal", "soft", "hard")
_LEVEL_RANK = {"normal": 0, "soft": 1, "hard": 2}

# How long after a cap event we assume the throttle still applies. Renewed by
# any subsequent cap fire; auto-downgrades to "normal" on expiry.
DEFAULT_THROTTLE_TTL_SEC = 1800.0

# Cap topics we listen to from minimax_budget (and any future vendor budget
# agents that follow the same convention).
_CAP_TOPIC_SOFT = ("minimax_budget.interval_soft_cap",
                   "minimax_budget.weekly_soft_cap")
_CAP_TOPIC_HARD = ("minimax_budget.interval_hard_cap",
                   "minimax_budget.weekly_hard_cap")


class Scheduler:
    def __init__(self, bus, schedules: list[dict], *,
                 throttle_ttl_sec: float = DEFAULT_THROTTLE_TTL_SEC,
                 reservation_check: bool = False) -> None:
        self.bus = bus
        self._schedules = [s for s in schedules if self._validate(s)]
        self._tasks: list[asyncio.Task] = []
        self._fire_counts: dict[str, int] = {}
        self._skip_counts: dict[str, int] = {}
        self._throttle_ttl = float(throttle_ttl_sec)
        self._throttle_level: str = "normal"
        self._throttle_until: float = 0.0
        self._last_cap_topic: Optional[str] = None
        # When True, scheduler asks minimax_budget.can_serve(target) before
        # firing. Denied → skip with reason=reservation_locked.
        self._reservation_check = bool(reservation_check)

    # -- validation -------------------------------------------------

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
        prio = s.get("priority", "normal")
        if prio not in VALID_PRIORITIES:
            log.warning("scheduler: drop %s: priority %r not in %s",
                        name, prio, VALID_PRIORITIES)
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

    # -- throttle ---------------------------------------------------

    def _current_throttle_level(self) -> str:
        """Return the effective level, auto-expiring if TTL passed."""
        if self._throttle_level != "normal" and time.time() >= self._throttle_until:
            log.info("scheduler: throttle TTL expired, resuming from %s → normal",
                     self._throttle_level)
            self._throttle_level = "normal"
            self._throttle_until = 0.0
        return self._throttle_level

    @staticmethod
    def _should_fire(priority: str, level: str) -> bool:
        """A schedule fires if its priority clears the current level.

        level=normal → all priorities fire
        level=soft   → nice_to_have skips; normal + critical fire
        level=hard   → only critical fires
        """
        if level == "normal":
            return True
        if level == "soft":
            return priority in ("critical", "normal")
        return priority == "critical"

    async def _check_reservation(self, s: dict) -> Optional[dict]:
        """Query minimax_budget.can_serve for this schedule's target.

        Returns None when allowed or when the check can't run (budget agent
        not loaded, request raised). Returns a diagnostic dict when DENIED
        so the caller can publish it in scheduler.skipped.
        """
        target = s.get("target")
        if not isinstance(target, str) or not target:
            return None
        try:
            r = await self.bus.request(
                "minimax_budget", {"op": "can_serve", "agent": target},
                timeout=2.0,
            )
        except LookupError:
            # budget agent not running; don't gate
            return None
        except Exception:
            log.exception("scheduler: can_serve check raised for %s", target)
            return None
        if not isinstance(r, dict) or not r.get("ok"):
            return None
        if r.get("allowed"):
            return None
        return r

    def _on_cap_event(self, event: dict) -> None:
        topic = (event or {}).get("topic", "")
        if topic in _CAP_TOPIC_HARD:
            new_level = "hard"
        elif topic in _CAP_TOPIC_SOFT:
            new_level = "soft"
        else:
            return
        now = time.time()
        cur_rank = _LEVEL_RANK[self._throttle_level]
        new_rank = _LEVEL_RANK[new_level]
        if new_rank > cur_rank:
            self._throttle_level = new_level
        # Always extend TTL on any cap event (renewal semantics).
        self._throttle_until = now + self._throttle_ttl
        self._last_cap_topic = topic
        log.warning(
            "scheduler: %s → throttle level=%s (ttl=%.0fs)",
            topic, self._throttle_level, self._throttle_ttl,
        )

    # -- lifecycle --------------------------------------------------

    async def start_all(self) -> None:
        for pattern in _CAP_TOPIC_SOFT + _CAP_TOPIC_HARD:
            self.bus.subscribe(pattern, self._on_cap_event)
        for s in self._schedules:
            t = asyncio.create_task(self._run(s), name=f"schedule:{s['name']}")
            self._tasks.append(t)

    async def stop_all(self) -> None:
        for pattern in _CAP_TOPIC_SOFT + _CAP_TOPIC_HARD:
            self.bus.unsubscribe(pattern, self._on_cap_event)
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
        priority = s.get("priority", "normal")
        level = self._current_throttle_level()
        if not self._should_fire(priority, level):
            self._skip_counts[name] = self._skip_counts.get(name, 0) + 1
            await self.bus.publish(f"{NAME}.skipped", {
                "schedule": name,
                "priority": priority,
                "throttle_level": level,
                "reason": "budget_throttle",
                "skipped_at": datetime.now(timezone.utc).isoformat(),
                "skip_count": self._skip_counts[name],
            })
            return
        # Optional reservation gate — asks budget_agent whether the target can
        # still draw requests this interval. Denied means someone else's
        # reservation floor would be violated if we fire. Hard-fail to skip
        # rather than queue; schedules re-fire on next tick anyway.
        if self._reservation_check:
            denied = await self._check_reservation(s)
            if denied is not None:
                self._skip_counts[name] = self._skip_counts.get(name, 0) + 1
                await self.bus.publish(f"{NAME}.skipped", {
                    "schedule": name,
                    "priority": priority,
                    "throttle_level": level,
                    "reason": "reservation_locked",
                    "diagnostic": denied,
                    "skipped_at": datetime.now(timezone.utc).isoformat(),
                    "skip_count": self._skip_counts[name],
                })
                return
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
            "priority": priority,
        })

    # -- introspection ---------------------------------------------

    def _list(self) -> list[dict]:
        return [
            {
                "name": s["name"],
                "target": s["target"],
                "event": s["event"],
                "trigger": (f"interval_sec={s['interval_sec']}"
                            if "interval_sec" in s
                            else f"daily_at={s['daily_at']}"),
                "priority": s.get("priority", "normal"),
                "fires": self._fire_counts.get(s["name"], 0),
                "skips": self._skip_counts.get(s["name"], 0),
            }
            for s in self._schedules
        ]

    def _throttle_state(self) -> dict:
        level = self._current_throttle_level()
        remaining = max(0.0, self._throttle_until - time.time()) \
            if level != "normal" else 0.0
        return {
            "level": level,
            "ttl_remaining_sec": remaining,
            "expires_at": self._throttle_until if level != "normal" else None,
            "last_cap_topic": self._last_cap_topic,
        }

    async def handle(self, msg) -> dict:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        op = payload.get("op", "status")
        if op in ("status", "list"):
            return {
                "ok": True,
                "schedules": self._list(),
                "total_fires": sum(self._fire_counts.values()),
                "total_skips": sum(self._skip_counts.values()),
                "throttle": self._throttle_state(),
            }
        if op == "override_throttle":
            level = payload.get("level", "normal")
            if level not in VALID_LEVELS:
                return {"ok": False, "error": f"invalid level: {level!r}"}
            ttl = float(payload.get("ttl_sec", self._throttle_ttl))
            self._throttle_level = level
            self._throttle_until = time.time() + ttl if level != "normal" else 0.0
            self._last_cap_topic = "manual_override"
            return {"ok": True, "throttle": self._throttle_state()}
        return {"ok": False, "error": f"unknown op: {op!r}"}
