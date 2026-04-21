"""PlatformAdapter ABC — contract every adapter honors.

Inbound flow: the adapter receives events from its platform, normalizes
them into InboundMessage, and calls the `on_inbound` callback that
GatewayManager wired in.

Outbound flow: GatewayManager calls `send(source, text, ...)` when any
agent publishes to `gateway.reply` or requests `send`.

Rich drafts: adapters may override `render_draft()` for card-style
rendering (quote + thinking + content + footer). Default implementation
falls back to flat `send()` with combined markdown text.
"""
from __future__ import annotations

import abc
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from ..session import InboundMessage, SendResult, SessionSource

if TYPE_CHECKING:
    from ..draft import DraftMessage

InboundCallback = Callable[[InboundMessage], Awaitable[None]]


class PlatformAdapter(abc.ABC):
    """Base class for any platform integration."""

    #: Short identifier used in SessionSource.platform (must be unique per adapter kind).
    platform: str = "unknown"

    #: Whether this adapter can edit an already-sent message. When True,
    #: streaming drafts update in place; when False, the first render_draft
    #: is a no-op and the final render at close() sends one consolidated message.
    supports_edit: bool = False

    def __init__(self, on_inbound: Optional[InboundCallback] = None) -> None:
        self._on_inbound: Optional[InboundCallback] = on_inbound

    def bind_inbound(self, on_inbound: InboundCallback) -> None:
        """GatewayManager wires this at startup."""
        self._on_inbound = on_inbound

    async def _deliver(self, msg: InboundMessage) -> None:
        """Adapter subclasses call this when they receive a user message."""
        if self._on_inbound is None:
            return
        await self._on_inbound(msg)

    # ---- lifecycle --------------------------------------------

    @abc.abstractmethod
    async def connect(self) -> None:
        """Start polling / connect to socket / etc."""

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Graceful shutdown."""

    # ---- outbound ---------------------------------------------

    @abc.abstractmethod
    async def send(self, source: SessionSource, text: str, *,
                   reply_to_message_id: Optional[str] = None) -> SendResult:
        """Send `text` to `source.chat_id` on this platform."""

    # ---- optional, default to no-op ---------------------------

    async def send_typing(self, source: SessionSource) -> None:
        """Optional: show a 'typing' indicator. No-op by default."""
        return None

    async def stop_typing(self, source: SessionSource) -> None:
        return None

    async def edit(self, source: SessionSource, message_id: str, text: str, *,
                   finalize: bool = False) -> SendResult:
        """Optional streaming-edit support. Default falls back to a new send."""
        return await self.send(source, text)

    async def render_draft(self, source: SessionSource, draft: "DraftMessage", *,
                           message_id: Optional[str],
                           finalize: bool) -> SendResult:
        """Render a structured draft (quote + thinking + content + footer).

        Default behavior:
            - supports_edit=True: send first render, then edit() on subsequent
            - supports_edit=False: only emit on finalize=True (one consolidated send)

        Platform adapters may override for native-card rendering (Feishu card,
        Slack blocks, ...). The default renderer composes markdown via
        `combine_draft_markdown` so MarkdownV2 / HTML-capable platforms look right.
        """
        from ..draft import combine_draft_markdown  # avoid circular import

        if draft.is_empty() and not finalize:
            return SendResult(ok=True, message_id=message_id)

        if not self.supports_edit and not finalize:
            # platforms that can't edit suppress intermediate flushes;
            # final text goes once at close().
            return SendResult(ok=True, message_id=message_id)

        text = combine_draft_markdown(draft)
        if message_id is None:
            return await self.send(source, text)
        return await self.edit(source, message_id, text, finalize=finalize)
