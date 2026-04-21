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
from .draft import DraftHandle, DraftMessage
from .session import InboundMessage, SendResult, SessionEntry, SessionSource

log = logging.getLogger(__name__)

CANCEL_TOKENS = {"/stop", "/cancel"}


class GatewayManager:
    NAME = "gateway"

    def __init__(self, bus) -> None:
        self.bus = bus
        self.adapters: dict[str, PlatformAdapter] = {}
        self.sessions: dict[str, SessionEntry] = {}
        self.drafts: dict[str, DraftHandle] = {}
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

    # -- structured drafts (quote + thinking + content + footer) ---

    def open_draft(self, *, session_key: Optional[str] = None,
                   source: Optional[SessionSource] = None,
                   quote_user: Optional[str] = None,
                   quote_text: Optional[str] = None,
                   footer_meta: Optional[list[tuple[str, str]]] = None,
                   throttle_seconds: Optional[float] = None) -> DraftHandle:
        """Create a DraftHandle. Python-native API (bus ops below wrap this).

        The returned handle is an async context manager:
            async with gw.open_draft(session_key=...) as draft:
                draft.set_thinking("...")
                await draft.flush()
                ...
        """
        resolved = source or self._resolve_source({"session_key": session_key})
        if resolved is None:
            raise KeyError("unknown session_key and no explicit source")
        adapter = self.adapters.get(resolved.platform)
        if adapter is None:
            raise LookupError(f"no adapter for platform={resolved.platform!r}")
        draft = DraftMessage(
            quote_user=quote_user,
            quote_text=quote_text,
            footer_meta=list(footer_meta) if footer_meta else [],
        )
        kwargs: dict = {
            "adapter": adapter, "source": resolved, "draft": draft,
            "on_close": self._drop_draft,
        }
        if throttle_seconds is not None:
            kwargs["throttle_seconds"] = throttle_seconds
        handle = DraftHandle(**kwargs)
        self.drafts[handle.id] = handle
        return handle

    def _drop_draft(self, handle: DraftHandle) -> None:
        """Called by DraftHandle.close() to GC long-lived handles."""
        self.drafts.pop(handle.id, None)

    def get_draft(self, draft_id: str) -> Optional[DraftHandle]:
        return self.drafts.get(draft_id)

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
            if op == "open_draft":
                return await self._op_open_draft(payload)
            if op == "update_draft":
                return await self._op_update_draft(payload)
            if op == "close_draft":
                return await self._op_close_draft(payload)
            return {"ok": False, "error": f"unknown op: {op!r}"}
        except KeyError as e:
            return {"ok": False, "error": f"missing field: {e.args[0]}"}
        except LookupError as e:
            return {"ok": False, "error": str(e)}
        except (TypeError, ValueError) as e:
            return {"ok": False, "error": str(e)}

    async def _op_open_draft(self, payload: dict) -> dict:
        quote = payload.get("quote") or {}
        footer = payload.get("footer_meta") or []
        footer_tuples = [tuple(x) if isinstance(x, list) else x for x in footer]
        handle = self.open_draft(
            session_key=payload.get("session_key"),
            source=self._resolve_source(payload)
            if "source" in payload else None,
            quote_user=quote.get("user"),
            quote_text=quote.get("text"),
            footer_meta=footer_tuples,
            throttle_seconds=payload.get("throttle_seconds"),
        )
        # Apply any initial content/thinking before the first send.
        if payload.get("thinking"):
            handle.set_thinking(payload["thinking"])
        if payload.get("content"):
            handle.set_content(payload["content"])
        await handle.open()
        return {
            "ok": True, "draft_id": handle.id,
            "message_id": handle.message_id,
        }

    async def _op_update_draft(self, payload: dict) -> dict:
        handle = self.drafts.get(payload.get("draft_id", ""))
        if handle is None:
            return {"ok": False, "error": "unknown draft_id"}
        if (v := payload.get("thinking")) is not None:
            handle.set_thinking(v)
        if (v := payload.get("thinking_append")) is not None:
            handle.append_thinking(v)
        if (v := payload.get("content")) is not None:
            handle.set_content(v)
        if (v := payload.get("content_append")) is not None:
            handle.append_content(v)
        if (v := payload.get("footer_meta")) is not None:
            handle.set_footer_meta(
                [tuple(x) if isinstance(x, list) else x for x in v]
            )
        if payload.get("flush_now", False):
            await handle.flush()
        else:
            await handle.maybe_flush()
        return {"ok": True, "message_id": handle.message_id}

    async def _op_close_draft(self, payload: dict) -> dict:
        handle = self.drafts.pop(payload.get("draft_id", ""), None)
        if handle is None:
            return {"ok": False, "error": "unknown draft_id"}
        await handle.close()
        return {"ok": True, "message_id": handle.message_id}
