"""Data types shared between the gateway manager and platform adapters."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class SessionSource:
    """Where a message came from. Keys together define a session."""
    platform: str              # "console" / "telegram" / "feishu" / ...
    chat_id: str               # platform-specific (Telegram chat, console user label, ...)
    user_id: Optional[str] = None
    thread_id: Optional[str] = None
    chat_type: str = "dm"      # "dm" | "group" | "channel" | "thread"

    @property
    def session_key(self) -> str:
        thread = self.thread_id or "default"
        return f"{self.platform}:{self.chat_id}:{thread}"

    def as_dict(self) -> dict:
        return {
            "platform": self.platform,
            "chat_id": self.chat_id,
            "user_id": self.user_id,
            "thread_id": self.thread_id,
            "chat_type": self.chat_type,
        }


@dataclass
class InboundMessage:
    """Normalized user-facing event from any platform adapter."""
    source: SessionSource
    text: str
    reply_to_message_id: Optional[str] = None
    media_urls: list[str] = field(default_factory=list)
    ts: str = field(default_factory=_now_iso)
    raw: Any = None            # platform-native payload (for adapter-specific needs)

    @property
    def session_key(self) -> str:
        return self.source.session_key

    def as_dict(self) -> dict:
        return {
            "session_key": self.session_key,
            "source": self.source.as_dict(),
            "text": self.text,
            "reply_to_message_id": self.reply_to_message_id,
            "media_urls": list(self.media_urls),
            "ts": self.ts,
        }


@dataclass
class SendResult:
    ok: bool
    message_id: Optional[str] = None
    error: Optional[str] = None


@dataclass
class SessionEntry:
    source: SessionSource
    created_at: str = field(default_factory=_now_iso)
    last_inbound_ts: Optional[str] = None
    last_outbound_message_id: Optional[str] = None
    # TODO(yuxu/compaction-gateway): when yuxu grows a per-session
    # conversation history (not just routing metadata), add `history:
    # list[dict] = field(default_factory=list)` here and wire an auto-
    # trigger on _on_inbound that calls compactor.microcompact when
    # len(history) or byte-size passes a threshold. compactor skill is
    # already shipped; only the history buffer + trigger are missing.
    # See `project_pending_todos.md` under 🔧 compaction.

    def as_dict(self) -> dict:
        return {
            "session_key": self.source.session_key,
            "source": self.source.as_dict(),
            "created_at": self.created_at,
            "last_inbound_ts": self.last_inbound_ts,
            "last_outbound_message_id": self.last_outbound_message_id,
        }
