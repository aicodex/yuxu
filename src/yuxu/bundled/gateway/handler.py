"""GatewayManager — the agent body.

Wires adapters to the bus:
  inbound  adapter  ->  bus.publish("gateway.user_message", ...)
                                     or ("gateway.user_cancel", ...) for /stop
  outbound bus.publish("gateway.reply", ...)  ->  right adapter.send(...)
  outbound bus.request("gateway", {op: "send"})  ->  adapter.send(...) + reply
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .adapters.base import PlatformAdapter
from .session import InboundMessage, SendResult, SessionEntry, SessionSource

log = logging.getLogger(__name__)

CANCEL_TOKENS = {"/stop", "/cancel"}


class GatewayManager:
    NAME = "gateway"

    def __init__(self, bus) -> None:
        self.bus = bus
        self.adapters: dict[str, PlatformAdapter] = {}
        self.sessions: dict[str, SessionEntry] = {}
        self._started = False

    # -- adapter wiring --------------------------------------------

    def register_adapter(self, adapter: PlatformAdapter) -> None:
        if adapter.platform in self.adapters:
            raise ValueError(f"adapter already registered: {adapter.platform}")
        self.adapters[adapter.platform] = adapter
        adapter.bind_inbound(self._on_inbound)

    # -- lifecycle --------------------------------------------------

    async def start(self) -> None:
        if self._started:
            return
        self.bus.subscribe("gateway.reply", self._on_reply_topic)
        for adapter in self.adapters.values():
            try:
                await adapter.connect()
            except Exception:
                log.exception("gateway: adapter %s failed to connect",
                              adapter.platform)
        self._started = True

    async def stop(self) -> None:
        self._started = False
        self.bus.unsubscribe("gateway.reply", self._on_reply_topic)
        for adapter in self.adapters.values():
            try:
                await adapter.disconnect()
            except Exception:
                log.exception("gateway: adapter %s disconnect raised",
                              adapter.platform)

    # -- inbound: adapter -> bus -----------------------------------

    async def _on_inbound(self, msg: InboundMessage) -> None:
        entry = self.sessions.get(msg.session_key)
        if entry is None:
            entry = SessionEntry(source=msg.source)
            self.sessions[msg.session_key] = entry
        entry.last_inbound_ts = msg.ts

        if msg.text.strip() in CANCEL_TOKENS:
            await self.bus.publish("gateway.user_cancel",
                                   {"session_key": msg.session_key})
            return

        await self.bus.publish("gateway.user_message", msg.as_dict())

    # -- outbound: bus -> adapter ----------------------------------

    async def _on_reply_topic(self, event: dict) -> None:
        payload = event.get("payload") if isinstance(event, dict) else None
        if not isinstance(payload, dict):
            return
        await self._send_from_payload(payload)

    async def _send_from_payload(self, payload: dict) -> SendResult:
        source = self._resolve_source(payload)
        if source is None:
            return SendResult(ok=False, error="unknown session_key; no routing info")
        adapter = self.adapters.get(source.platform)
        if adapter is None:
            return SendResult(
                ok=False,
                error=f"no adapter for platform={source.platform!r}",
            )
        try:
            result = await adapter.send(
                source, str(payload.get("text", "")),
                reply_to_message_id=payload.get("reply_to"),
            )
        except Exception as e:
            log.exception("gateway: adapter %s.send raised", source.platform)
            return SendResult(ok=False, error=str(e))
        if result.ok and result.message_id:
            entry = self.sessions.get(source.session_key)
            if entry is not None:
                entry.last_outbound_message_id = result.message_id
        return result

    def _resolve_source(self, payload: dict) -> Optional[SessionSource]:
        # Either session_key alone (look up existing session) or explicit source.
        if "source" in payload and isinstance(payload["source"], dict):
            s = payload["source"]
            return SessionSource(
                platform=s["platform"],
                chat_id=s["chat_id"],
                user_id=s.get("user_id"),
                thread_id=s.get("thread_id"),
                chat_type=s.get("chat_type", "dm"),
            )
        key = payload.get("session_key")
        if not key:
            return None
        entry = self.sessions.get(key)
        return entry.source if entry is not None else None

    # -- bus ops ----------------------------------------------------

    async def handle(self, msg) -> dict:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        op = payload.get("op")
        try:
            if op == "send":
                result = await self._send_from_payload(payload)
                return {
                    "ok": result.ok,
                    "message_id": result.message_id,
                    "error": result.error,
                }
            if op == "sessions":
                return {
                    "ok": True,
                    "sessions": [e.as_dict() for e in self.sessions.values()],
                }
            return {"ok": False, "error": f"unknown op: {op!r}"}
        except KeyError as e:
            return {"ok": False, "error": f"missing field: {e.args[0]}"}
        except (TypeError, ValueError) as e:
            return {"ok": False, "error": str(e)}
