"""PlatformAdapter ABC — contract every adapter honors.

Inbound flow: the adapter receives events from its platform, normalizes
them into InboundMessage, and calls the `on_inbound` callback that
GatewayManager wired in.

Outbound flow: GatewayManager calls `send(source, text, ...)` when any
agent publishes to `gateway.reply` or requests `send`.
"""
from __future__ import annotations

import abc
from typing import Awaitable, Callable, Optional

from ..session import InboundMessage, SendResult, SessionSource

InboundCallback = Callable[[InboundMessage], Awaitable[None]]


class PlatformAdapter(abc.ABC):
    """Base class for any platform integration."""

    #: Short identifier used in SessionSource.platform (must be unique per adapter kind).
    platform: str = "unknown"

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
