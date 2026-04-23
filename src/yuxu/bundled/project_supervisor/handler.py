"""ProjectSupervisor — watchdog for persistent bundled/user agents."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from typing import Optional

log = logging.getLogger(__name__)

NAME = "project_supervisor"


class ProjectSupervisor:
    DEFAULT_MAX_RESTARTS = 5
    DEFAULT_WINDOW_SEC = 300.0
    DEFAULT_DELAY_SEC = 2.0

    def __init__(self, bus, loader, *,
                 max_restarts: Optional[int] = None,
                 window_sec: Optional[float] = None,
                 restart_delay: Optional[float] = None) -> None:
        self.bus = bus
        self.loader = loader
        self.max_restarts = int(max_restarts if max_restarts is not None else
                                os.environ.get("SUPERVISOR_MAX_RESTARTS",
                                               self.DEFAULT_MAX_RESTARTS))
        self.window_sec = float(window_sec if window_sec is not None else
                                os.environ.get("SUPERVISOR_WINDOW_SEC",
                                               self.DEFAULT_WINDOW_SEC))
        self.restart_delay = float(restart_delay if restart_delay is not None else
                                   os.environ.get("SUPERVISOR_DELAY_SEC",
                                                  self.DEFAULT_DELAY_SEC))
        self._restarts: dict[str, deque[float]] = {}
        self._give_ups: list[dict] = []
        self._in_flight: set[str] = set()

    def install(self) -> None:
        self.bus.subscribe("_meta.state_change", self._on_state_change)

    async def scan_and_heal(self) -> None:
        """On startup, rescue any already-failed persistent agents."""
        for name, spec in list(self.loader.specs.items()):
            if spec.run_mode != "persistent" or name == NAME:
                continue
            if self.bus.query_status(name) == "failed":
                await self._attempt_restart(name)

    async def _on_state_change(self, event: dict) -> None:
        payload = event.get("payload") or {}
        agent = payload.get("agent") if isinstance(payload, dict) else None
        state = payload.get("state") if isinstance(payload, dict) else None
        if not agent or state != "failed":
            return
        if agent == NAME or agent in self._in_flight:
            return
        spec = self.loader.specs.get(agent)
        if spec is None or spec.run_mode != "persistent":
            return
        asyncio.create_task(self._schedule_restart(agent))

    async def _schedule_restart(self, agent: str) -> None:
        self._in_flight.add(agent)
        try:
            if self.restart_delay > 0:
                await asyncio.sleep(self.restart_delay)
            await self._attempt_restart(agent)
        finally:
            self._in_flight.discard(agent)

    def _prune(self, agent: str, now: float) -> deque[float]:
        dq = self._restarts.setdefault(agent, deque())
        cutoff = now - self.window_sec
        while dq and dq[0] < cutoff:
            dq.popleft()
        return dq

    async def _attempt_restart(self, agent: str) -> None:
        now = time.monotonic()
        dq = self._prune(agent, now)
        if len(dq) >= self.max_restarts:
            info = {
                "agent": agent,
                "attempts": len(dq),
                "window_sec": self.window_sec,
            }
            self._give_ups.append(info)
            log.error("supervisor: giving up on %s after %d restarts in %.0fs",
                      agent, len(dq), self.window_sec)
            await self.bus.publish(f"{NAME}.giveup", info)
            return
        dq.append(now)
        log.info("supervisor: restarting %s (attempt %d)", agent, len(dq))
        try:
            status = await self.loader.restart(
                agent, reason=f"supervisor_restart_attempt_{len(dq)}",
            )
        except Exception as e:
            log.exception("supervisor: restart of %s failed", agent)
            await self.bus.publish(f"{NAME}.restart_failed", {
                "agent": agent, "error": str(e), "attempt": len(dq),
            })
            return
        await self.bus.publish(f"{NAME}.restarted", {
            "agent": agent, "status": status, "attempt": len(dq),
        })

    def report(self) -> dict:
        now = time.monotonic()
        return {
            "max_restarts": self.max_restarts,
            "window_sec": self.window_sec,
            "restarts": {
                agent: [now - t for t in self._prune(agent, now)]
                for agent in self._restarts
            },
            "give_ups": list(self._give_ups),
        }

    def reset(self) -> None:
        self._restarts.clear()
        self._give_ups.clear()

    async def handle(self, msg) -> dict:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        op = payload.get("op", "report")
        if op == "report":
            return {"ok": True, **self.report()}
        if op == "reset":
            self.reset()
            return {"ok": True}
        return {"ok": False, "error": f"unknown op: {op!r}"}
