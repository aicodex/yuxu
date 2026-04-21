"""ResourceGuardian — subscribe to error/throttle events and emit warnings."""
from __future__ import annotations

import logging
import os
import time
from collections import deque
from typing import Optional

log = logging.getLogger(__name__)


class ResourceGuardian:
    DEFAULT_WINDOW_SEC = 60.0
    DEFAULT_ERROR_THRESHOLD = 5
    DEFAULT_THROTTLE_THRESHOLD = 3

    def __init__(self, bus, *,
                 window_sec: Optional[float] = None,
                 error_threshold: Optional[int] = None,
                 throttle_threshold: Optional[int] = None) -> None:
        self.bus = bus
        self.window_sec = float(window_sec if window_sec is not None else
                                os.environ.get("GUARDIAN_WINDOW_SEC",
                                               self.DEFAULT_WINDOW_SEC))
        self.error_threshold = int(error_threshold if error_threshold is not None else
                                   os.environ.get("GUARDIAN_ERROR_THRESHOLD",
                                                  self.DEFAULT_ERROR_THRESHOLD))
        self.throttle_threshold = int(throttle_threshold if throttle_threshold is not None else
                                      os.environ.get("GUARDIAN_THROTTLE_THRESHOLD",
                                                     self.DEFAULT_THROTTLE_THRESHOLD))
        self._events: dict[tuple[str, str], deque[float]] = {}
        self._last_warn: dict[tuple[str, str], float] = {}

    def install(self) -> None:
        self.bus.subscribe("*.error", self._on_error)
        self.bus.subscribe("_meta.ratelimit.throttled", self._on_throttle)

    def _threshold(self, kind: str) -> int:
        return self.throttle_threshold if kind == "throttled" else self.error_threshold

    def _prune(self, dq: deque[float], now: float) -> None:
        cutoff = now - self.window_sec
        while dq and dq[0] < cutoff:
            dq.popleft()

    async def _record(self, kind: str, agent: str) -> None:
        if not agent:
            return
        now = time.monotonic()
        key = (kind, agent)
        dq = self._events.setdefault(key, deque())
        dq.append(now)
        self._prune(dq, now)
        if len(dq) >= self._threshold(kind):
            last = self._last_warn.get(key, 0.0)
            if now - last > self.window_sec:
                self._last_warn[key] = now
                await self.bus.publish(f"{agent}.resource_warning", {
                    "kind": kind,
                    "agent": agent,
                    "count": len(dq),
                    "window_sec": self.window_sec,
                    "threshold": self._threshold(kind),
                })

    async def _on_error(self, event: dict) -> None:
        topic = event.get("topic", "")
        # ignore own warnings to prevent feedback loops
        if topic.endswith(".resource_warning"):
            return
        # topic shape: "{agent}.error"
        agent = topic.rsplit(".error", 1)[0]
        if agent == topic:  # didn't match suffix
            return
        await self._record("error", agent)

    async def _on_throttle(self, event: dict) -> None:
        payload = event.get("payload") or {}
        agent = payload.get("agent") if isinstance(payload, dict) else None
        await self._record("throttled", agent or "unknown")

    def report(self) -> dict:
        now = time.monotonic()
        per_agent: dict = {}
        for (kind, agent), dq in self._events.items():
            self._prune(dq, now)
            per_agent.setdefault(agent, {})[kind] = len(dq)
        return per_agent

    def reset(self) -> None:
        self._events.clear()
        self._last_warn.clear()

    async def handle(self, msg) -> dict:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        op = payload.get("op", "report")
        if op == "report":
            return {
                "ok": True,
                "window_sec": self.window_sec,
                "thresholds": {
                    "error": self.error_threshold,
                    "throttled": self.throttle_threshold,
                },
                "per_agent": self.report(),
            }
        if op == "reset":
            self.reset()
            return {"ok": True}
        return {"ok": False, "error": f"unknown op: {op!r}"}
