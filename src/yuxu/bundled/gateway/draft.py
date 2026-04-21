"""Draft message model + streaming handle.

Ports the OpenClaw feishu reply-dispatcher pattern:
  quote (user's input)   | thinking (💭 block) | content | footer (meta)

Data is platform-neutral. Each adapter renders it to its native form:
  - Telegram → MarkdownV2 blockquote + horizontal rule + italic footer
  - Feishu   → Card Schema 2.0 (markdown + hr + grey markdown)
  - Console  → text dividers

Streaming uses a per-draft handle that throttles adapter.edit() calls and
guarantees a final flush on close. Throttle ≈ 250ms matches OpenClaw's
draft-stream-loop.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .adapters.base import PlatformAdapter
    from .session import SessionSource

log = logging.getLogger(__name__)

DEFAULT_THROTTLE_SECONDS = 0.25
DEFAULT_FINALIZE_TIMEOUT = 5.0


# -- data model ------------------------------------------------


@dataclass
class DraftMessage:
    content: str = ""
    thinking: str = ""

    # Quote (platforms without native reply show this as a blockquote header).
    quote_user: Optional[str] = None
    quote_text: Optional[str] = None

    # Free-form key/value pairs for the footer. Kept simple so the caller
    # picks the schema (e.g. Agent / Context / Model / Tokens).
    footer_meta: list[tuple[str, str]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.content or self.thinking or self.footer_meta
                    or self.quote_text)

    def copy(self) -> "DraftMessage":
        return DraftMessage(
            content=self.content,
            thinking=self.thinking,
            quote_user=self.quote_user,
            quote_text=self.quote_text,
            footer_meta=list(self.footer_meta),
        )


# -- rendering -------------------------------------------------


def combine_draft_markdown(draft: DraftMessage, *,
                           hr: str = "―" * 20,
                           include_footer: bool = True) -> str:
    """Combined Markdown string. Used by Telegram + as Feishu card body.

    Console has its own renderer so the layout can use terminal dividers.
    """
    parts: list[str] = []

    if draft.quote_user and draft.quote_text:
        quoted = draft.quote_text.strip().splitlines()
        # prefix each line with '>' so a multi-line quote stays a blockquote
        parts.append(f"> 回复 {draft.quote_user}: {quoted[0] if quoted else ''}")
        for extra in quoted[1:]:
            parts.append(f"> {extra}")
        parts.append("")

    if draft.thinking:
        parts.append("> 💭 **Thinking**")
        for line in draft.thinking.splitlines():
            parts.append(f"> {line}")
        parts.append("")

    if draft.content:
        parts.append(draft.content)

    if include_footer and draft.footer_meta:
        if parts and parts[-1] != "":
            parts.append("")
        parts.append(hr)
        parts.append(
            "_" + " | ".join(f"{k}: {v}" for k, v in draft.footer_meta) + "_"
        )

    return "\n".join(parts).rstrip()


# -- streaming handle ------------------------------------------


class DraftHandle:
    """Lifecycle:
        await handle.open()          # first send, may yield a message_id
        handle.set_thinking(...)
        handle.append_content(...)
        await handle.flush()         # manual (otherwise auto-throttled)
        ... more updates ...
        await handle.close()         # final edit, always sent regardless of throttle

    Multiple updates in quick succession are collapsed into at most one
    adapter.edit() per `throttle_seconds`. `close()` always triggers a
    final edit and disables further updates.
    """

    def __init__(self, *, adapter: "PlatformAdapter", source: "SessionSource",
                 draft: Optional[DraftMessage] = None,
                 throttle_seconds: float = DEFAULT_THROTTLE_SECONDS,
                 draft_id: Optional[str] = None,
                 on_close: Optional[callable] = None) -> None:
        self.id = draft_id or uuid.uuid4().hex[:12]
        self.adapter = adapter
        self.source = source
        self.draft = draft or DraftMessage()
        self.throttle = throttle_seconds
        self.message_id: Optional[str] = None
        self._open = False
        self._closed = False
        self._last_flush_mono = 0.0
        self._pending_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._last_state_key: Optional[tuple] = None
        self._on_close = on_close

    # -- public update API -------------------------------------

    def set_quote(self, user: Optional[str], text: Optional[str]) -> None:
        self._require_open_not_closed()
        self.draft.quote_user = user
        self.draft.quote_text = text

    def set_footer_meta(self, meta: list[tuple[str, str]]) -> None:
        self._require_open_not_closed()
        self.draft.footer_meta = list(meta)

    def set_thinking(self, text: str) -> None:
        self._require_open_not_closed()
        self.draft.thinking = text

    def append_thinking(self, chunk: str) -> None:
        self._require_open_not_closed()
        self.draft.thinking = (self.draft.thinking or "") + chunk

    def set_content(self, text: str) -> None:
        self._require_open_not_closed()
        self.draft.content = text

    def append_content(self, chunk: str) -> None:
        self._require_open_not_closed()
        self.draft.content = (self.draft.content or "") + chunk

    # -- lifecycle ---------------------------------------------

    async def open(self) -> Optional[str]:
        """Send the initial message. Returns platform message_id if any."""
        if self._open:
            return self.message_id
        self._open = True
        result = await self.adapter.render_draft(
            self.source, self.draft, message_id=None, finalize=False,
        )
        if result.ok and result.message_id:
            self.message_id = result.message_id
        return self.message_id

    async def flush(self) -> None:
        """Force an edit now (does not bypass the lock but does bypass throttle)."""
        async with self._lock:
            await self._do_flush(finalize=False)

    async def maybe_flush(self) -> None:
        """Respecting throttle. Safe to call after each update."""
        now = time.monotonic()
        if now - self._last_flush_mono >= self.throttle:
            async with self._lock:
                await self._do_flush(finalize=False)
        else:
            # coalesce: schedule one trailing edit if none pending
            if self._pending_task is None or self._pending_task.done():
                delay = self.throttle - (now - self._last_flush_mono)
                self._pending_task = asyncio.create_task(
                    self._schedule_trailing(delay)
                )

    async def _schedule_trailing(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        async with self._lock:
            await self._do_flush(finalize=False)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._pending_task is not None and not self._pending_task.done():
            self._pending_task.cancel()
            try:
                await self._pending_task
            except (asyncio.CancelledError, Exception):
                pass
        async with self._lock:
            try:
                await asyncio.wait_for(
                    self._do_flush(finalize=True),
                    timeout=DEFAULT_FINALIZE_TIMEOUT,
                )
            except (asyncio.TimeoutError, Exception):
                log.exception("draft: finalize flush failed for %s", self.id)
        # Notify owner (e.g. GatewayManager.drafts registry) so it can GC.
        if self._on_close is not None:
            try:
                self._on_close(self)
            except Exception:
                log.exception("draft: on_close callback raised for %s", self.id)

    # -- internal ----------------------------------------------

    async def _do_flush(self, *, finalize: bool) -> None:
        if not self._open:
            return
        snapshot = self.draft.copy()
        state_key = (
            snapshot.content, snapshot.thinking,
            snapshot.quote_user, snapshot.quote_text,
            tuple(snapshot.footer_meta),
        )
        # Skip redundant edits: unchanged state + not finalize = no-op.
        # Finalize always flushes because some adapters (console) only emit
        # on finalize, and because idempotent final send is a safety.
        if not finalize and state_key == self._last_state_key:
            return
        self._last_state_key = state_key
        result = await self.adapter.render_draft(
            self.source, snapshot,
            message_id=self.message_id, finalize=finalize,
        )
        self._last_flush_mono = time.monotonic()
        if result.ok and result.message_id and self.message_id is None:
            self.message_id = result.message_id

    def _require_open_not_closed(self) -> None:
        if self._closed:
            raise RuntimeError("draft is closed")
        if not self._open:
            # fine: updates buffered; open() will pick them up
            return

    # -- convenience (async context manager) -------------------

    async def __aenter__(self) -> "DraftHandle":
        await self.open()
        return self

    async def __aexit__(self, *a) -> None:
        await self.close()
