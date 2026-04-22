"""In-process asyncio message bus.

Minimal mechanism only. No policy, no retry, no routing magic.
- send(to, event, payload) fire-and-forget direct message to one agent
- request(to, query) request/reply with future + timeout
- subscribe/publish topic fan-out with fnmatch patterns
- publish_status / query_status / wait_for_service lifecycle coordination

Exception isolation is critical: handler crashes must not propagate into the bus
loop or bring down the kernel. See feedback_kernel_invariants.md.

Things deliberately NOT in the bus (see docs/CORE_INTERFACE.md):
- rate_limit → use rate_limit_service agent directly (ctx.get_agent)
- cancel → publish("_meta.cancel", {...}) if needed; agents subscribe to react
"""
from __future__ import annotations

import asyncio
import fnmatch
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

Handler = Callable[["Message"], Any]
SubHandler = Callable[[dict], Any]


@dataclass
class Message:
    to: str
    event: str
    payload: Any = None
    sender: Optional[str] = None
    request_id: Optional[str] = None


class Bus:
    STATES = ("unloaded", "loading", "ready", "running", "idle", "failed", "stopped")

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}
        self._subs: dict[str, list[SubHandler]] = {}
        self._pending: dict[str, asyncio.Future] = {}
        self._status: dict[str, str] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._running = False
        self._stop_evt: Optional[asyncio.Event] = None

    # -- registration ------------------------------------------------

    def register(self, name: str, handler: Handler) -> None:
        if name in self._handlers:
            log.warning("bus: handler for %s replaced", name)
        self._handlers[name] = handler

    def unregister(self, name: str) -> None:
        self._handlers.pop(name, None)

    # -- direct messaging --------------------------------------------

    async def send(self, to: str, event: str, payload: Any = None, sender: Optional[str] = None) -> None:
        msg = Message(to=to, event=event, payload=payload, sender=sender)
        asyncio.create_task(self._dispatch_direct(msg))

    async def request(self, to: str, query: Any, timeout: float = 30.0,
                      sender: Optional[str] = None) -> Any:
        req_id = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        msg = Message(to=to, event="request", payload=query,
                      request_id=req_id, sender=sender)
        asyncio.create_task(self._dispatch_direct(msg))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(req_id, None)

    async def _dispatch_direct(self, msg: Message) -> None:
        handler = self._handlers.get(msg.to)
        if handler is None:
            log.warning("bus: no handler for %s, dropping event=%s", msg.to, msg.event)
            if msg.request_id:
                fut = self._pending.get(msg.request_id)
                if fut and not fut.done():
                    fut.set_exception(LookupError(f"no handler for {msg.to}"))
            return
        try:
            result = handler(msg)
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as e:
            log.exception("bus: handler %s raised on event=%s", msg.to, msg.event)
            if msg.request_id:
                fut = self._pending.get(msg.request_id)
                if fut and not fut.done():
                    fut.set_exception(e)
            return
        if msg.request_id:
            fut = self._pending.get(msg.request_id)
            if fut and not fut.done():
                fut.set_result(result)

    # -- pub/sub -----------------------------------------------------

    def subscribe(self, topic: str, handler: SubHandler) -> None:
        self._subs.setdefault(topic, []).append(handler)

    def unsubscribe(self, topic: str, handler: SubHandler) -> None:
        lst = self._subs.get(topic)
        if lst and handler in lst:
            lst.remove(handler)

    async def publish(self, topic: str, payload: Any = None) -> None:
        for pattern, handlers in list(self._subs.items()):
            if pattern == topic or fnmatch.fnmatchcase(topic, pattern):
                for h in list(handlers):
                    asyncio.create_task(self._call_sub(pattern, h, topic, payload))

    async def _call_sub(self, pattern: str, h: SubHandler, topic: str, payload: Any) -> None:
        try:
            result = h({"topic": topic, "payload": payload})
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            log.exception("bus: subscriber %s raised on topic=%s", pattern, topic)

    # -- status ------------------------------------------------------

    async def publish_status(self, agent: str, state: str) -> None:
        assert state in self.STATES, f"invalid state: {state}"
        self._status[agent] = state
        ev = self._events.setdefault(agent, asyncio.Event())
        if state in ("ready", "running", "failed", "stopped"):
            ev.set()
        else:
            ev.clear()
        await self.publish(f"{agent}.status", state)
        await self.publish("_meta.state_change", {"agent": agent, "state": state})

    def query_status(self, agent: str) -> str:
        return self._status.get(agent, "unloaded")

    async def wait_for_service(self, agent: str, timeout: Optional[float] = None) -> None:
        if self._status.get(agent) == "ready":
            return
        ev = self._events.setdefault(agent, asyncio.Event())
        await asyncio.wait_for(ev.wait(), timeout=timeout)
        status = self._status.get(agent)
        if status not in ("ready", "running"):
            raise RuntimeError(f"{agent} ended in status={status}")

    async def ready(self, agent: str) -> None:
        await self.publish_status(agent, "ready")

    # -- lifecycle ---------------------------------------------------

    async def run_forever(self) -> None:
        self._running = True
        self._stop_evt = asyncio.Event()
        try:
            await self._stop_evt.wait()
        finally:
            self._running = False

    async def stop(self) -> None:
        if self._stop_evt is not None:
            self._stop_evt.set()
