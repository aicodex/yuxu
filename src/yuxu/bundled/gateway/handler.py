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
from .pairing import PairingRegistry
from .session import InboundMessage, SendResult, SessionEntry, SessionSource

log = logging.getLogger(__name__)

CANCEL_TOKENS = {"/stop", "/cancel"}

# How often the pairing-watcher task re-stats pairings.yaml. Cheap (one
# stat() call) so the default is aggressive enough for CLI→daemon hot
# reload to feel immediate without burning measurable CPU.
DEFAULT_PAIRING_POLL_SEC = 1.0

DEFAULT_PENDING_TEMPLATE = (
    "👋 Hi — I'm not yet authorized to chat with you.\n\n"
    "<b>Your id</b>: <code>{user_id}</code>\n\n"
    "Send the admin the command below (long-press / tap the copy icon):\n"
    "<pre>yuxu pair approve {platform} {user_id}</pre>\n"
    "Once approved, just keep messaging here — no restart needed."
)
DEFAULT_PENDING_PARSE_MODE = "HTML"


class GatewayManager:
    NAME = "gateway"

    def __init__(self, bus, *,
                 pairing: Optional[PairingRegistry] = None,
                 pairing_required_platforms: Optional[set[str]] = None,
                 pending_reply_template: Optional[str] = None,
                 pairing_poll_seconds: float = DEFAULT_PAIRING_POLL_SEC,
                 loader=None) -> None:
        self.bus = bus
        self.loader = loader
        self.adapters: dict[str, PlatformAdapter] = {}
        self.sessions: dict[str, SessionEntry] = {}
        self.drafts: dict[str, DraftHandle] = {}
        self.pairing = pairing
        self.pairing_required: set[str] = set(pairing_required_platforms or [])
        self.pending_reply_template = (
            pending_reply_template or DEFAULT_PENDING_TEMPLATE
        )
        # Plugin command registry:  "/dashboard" -> {agent, help}
        self.commands: dict[str, dict] = {}
        self._pairing_poll_seconds = pairing_poll_seconds
        self._pairing_watch_task: Optional[asyncio.Task] = None
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
        if self.pairing is not None and self._pairing_poll_seconds > 0:
            self._pairing_watch_task = asyncio.create_task(
                self._pairing_watch_loop(),
                name="gateway.pairing_watch",
            )
        self._started = True

    async def stop(self) -> None:
        self._started = False
        self.bus.unsubscribe("gateway.reply", self._on_reply_topic)
        if self._pairing_watch_task is not None:
            self._pairing_watch_task.cancel()
            try:
                await asyncio.wait_for(self._pairing_watch_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
            self._pairing_watch_task = None
        for adapter in self.adapters.values():
            try:
                await adapter.disconnect()
            except Exception:
                log.exception("gateway: adapter %s disconnect raised",
                              adapter.platform)

    async def _pairing_watch_loop(self) -> None:
        """Poll pairings.yaml mtime; reload in-memory registry on change.

        Lets `yuxu pair approve` in another process take effect inside a
        running daemon without a restart. Cheap: one stat() per tick.
        """
        while True:
            try:
                await asyncio.sleep(self._pairing_poll_seconds)
            except asyncio.CancelledError:
                raise
            if self.pairing is None:
                continue
            try:
                if self.pairing.reload_if_changed():
                    log.info("gateway: pairings reloaded from %s",
                             self.pairing.path)
                    await self.bus.publish("gateway.pairings_reloaded", {
                        "path": str(self.pairing.path),
                    })
            except Exception:
                log.exception("gateway: pairings reload failed")

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

        # Pairing gate: if platform requires pairing and user isn't allowed,
        # stash as pending + notify admins instead of delivering.
        if self._pairing_gate_blocks(msg):
            await self._record_and_notify_pending(msg)
            return

        # Slash-command routing: if the first token is a registered command,
        # publish gateway.command_invoked instead of gateway.user_message.
        # Unknown commands fall through as a normal user message.
        if msg.text.startswith("/") and " " not in msg.text[:1]:
            parts = msg.text.split(maxsplit=1)
            command = parts[0]
            if command in self.commands:
                await self.bus.publish("gateway.command_invoked", {
                    "command": command,
                    "args": parts[1] if len(parts) > 1 else "",
                    "handler_agent": self.commands[command].get("agent"),
                    "source": msg.source.as_dict(),
                    "session_key": msg.session_key,
                    "raw_text": msg.text,
                    "ts": msg.ts,
                })
                return

        await self.bus.publish("gateway.user_message", msg.as_dict())

    def _pairing_gate_blocks(self, msg: InboundMessage) -> bool:
        if self.pairing is None:
            return False
        if msg.source.platform not in self.pairing_required:
            return False
        user_id = msg.source.user_id or ""
        if not user_id:
            # Anonymous inbound on a pairing-required platform — block.
            return True
        return not self.pairing.is_allowed(msg.source.platform, user_id)

    async def _record_and_notify_pending(self, msg: InboundMessage) -> None:
        user_id = msg.source.user_id or "<anonymous>"
        self.pairing.add_pending(
            msg.source.platform, user_id,
            first_message=msg.text[:200],
            chat_id=msg.source.chat_id,
        )
        await self.bus.publish("gateway.pairing_requested", {
            "platform": msg.source.platform,
            "user_id": user_id,
            "chat_id": msg.source.chat_id,
            "first_message": msg.text,
            "session_key": msg.session_key,
        })
        await self._send_pending_reply(msg, user_id)

    async def _send_pending_reply(self, msg: InboundMessage, user_id: str) -> None:
        """Reply to the unapproved user with the approval hint."""
        adapter = self.adapters.get(msg.source.platform)
        if adapter is None:
            return
        try:
            text = self.pending_reply_template.format(
                platform=msg.source.platform,
                user_id=user_id,
                chat_id=msg.source.chat_id,
            )
        except (KeyError, IndexError):
            text = DEFAULT_PENDING_TEMPLATE.format(
                platform=msg.source.platform, user_id=user_id,
                chat_id=msg.source.chat_id,
            )
        log.info("gateway: pending-reply to %s/%s (first 60 chars): %r",
                 msg.source.platform, user_id, text[:60])
        try:
            await adapter.send(msg.source, text,
                                parse_mode=DEFAULT_PENDING_PARSE_MODE)
        except Exception:
            log.exception("gateway: pending-reply send failed for %s/%s",
                          msg.source.platform, user_id)
            return
        if self.pairing is not None:
            self.pairing.mark_notified(msg.source.platform, user_id)

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
            if op == "register_command":
                return self._op_register_command(payload)
            if op == "unregister_command":
                return self._op_unregister_command(payload)
            if op == "list_commands":
                return {"ok": True, "commands": dict(self.commands)}
            if op == "list_menu":
                return self._op_list_menu(payload)
            if op == "pair_list":
                return self._op_pair_list(payload)
            if op == "pair_approve":
                return self._op_pair_approve(payload)
            if op == "pair_reject":
                return self._op_pair_reject(payload)
            if op == "pair_revoke":
                return self._op_pair_revoke(payload)
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

    # -- command registry ops ---------------------------------------

    def _op_register_command(self, payload: dict) -> dict:
        command = str(payload.get("command", "")).strip()
        if not command or not command.startswith("/"):
            return {"ok": False, "error": "command must start with '/'"}
        if " " in command:
            return {"ok": False, "error": "command may not contain spaces"}
        self.commands[command] = {
            "agent": payload.get("agent", ""),
            "help": payload.get("help", ""),
        }
        return {"ok": True}

    def _op_unregister_command(self, payload: dict) -> dict:
        command = str(payload.get("command", "")).strip()
        self.commands.pop(command, None)
        return {"ok": True}

    # -- menu (surface-tagged agents + skills) ---------------------

    def _op_list_menu(self, payload: dict) -> dict:
        """Return units (agents + skills) whose frontmatter declares the
        requested surface. Used by gateway to render `/menu`-style UI.
        """
        if self.loader is None:
            return {"ok": False, "error": "loader not available"}
        surface = payload.get("surface") or "menu"
        kind = payload.get("kind")
        items = self.loader.filter(surface=surface, kind=kind)
        return {
            "ok": True,
            "surface": surface,
            "items": [{
                "name": s.name,
                "kind": s.kind,
                "scope": s.scope,
                "description": s.frontmatter.get("description", ""),
                "triggers": s.frontmatter.get("triggers") or [],
                "surface": s.frontmatter.get("surface") or [],
            } for s in items],
        }

    # -- pairing ops ------------------------------------------------

    def _op_pair_list(self, payload: dict) -> dict:
        if self.pairing is None:
            return {"ok": False, "error": "pairing not enabled"}
        platform = payload.get("platform")
        return {
            "ok": True,
            "allowed": [e.as_dict() | {"platform": e.platform}
                        for e in self.pairing.list_allowed(platform)],
            "pending": [e.as_dict() | {"platform": e.platform}
                        for e in self.pairing.list_pending(platform)],
            "required_platforms": sorted(self.pairing_required),
        }

    def _op_pair_approve(self, payload: dict) -> dict:
        if self.pairing is None:
            return {"ok": False, "error": "pairing not enabled"}
        platform = payload.get("platform")
        user_id = payload.get("user_id")
        if not platform or not user_id:
            return {"ok": False, "error": "platform and user_id required"}
        entry = self.pairing.approve_pending(
            platform, user_id, note=payload.get("note", ""),
        )
        return {"ok": True, "approved": entry.as_dict() | {"platform": platform}}

    def _op_pair_reject(self, payload: dict) -> dict:
        if self.pairing is None:
            return {"ok": False, "error": "pairing not enabled"}
        removed = self.pairing.reject_pending(
            payload.get("platform", ""), payload.get("user_id", ""),
        )
        return {"ok": True, "removed": removed}

    def _op_pair_revoke(self, payload: dict) -> dict:
        if self.pairing is None:
            return {"ok": False, "error": "pairing not enabled"}
        removed = self.pairing.revoke_allowed(
            payload.get("platform", ""), payload.get("user_id", ""),
        )
        return {"ok": True, "removed": removed}
