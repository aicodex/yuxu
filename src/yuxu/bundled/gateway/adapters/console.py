"""Console adapter — stdin in, stdout out.

Primarily for local dev:
    echo "hi" | yuxu serve
    # or interactive: run yuxu serve in a terminal and type

For tests, `push_input(text)` feeds a synthetic user message without
actually touching stdin; `outbox` records sent messages.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import Optional

from ..session import InboundMessage, SendResult, SessionSource
from .base import PlatformAdapter

log = logging.getLogger(__name__)

DEFAULT_USER_ID = "local"


class ConsoleAdapter(PlatformAdapter):
    platform = "console"

    def __init__(self, user_id: str = DEFAULT_USER_ID, *,
                 read_stdin: bool = True) -> None:
        super().__init__()
        self._user_id = user_id
        self._read_stdin = read_stdin
        self._task: Optional[asyncio.Task] = None
        #: Test hook — lines pushed here look like stdin input to the loop.
        self._input_queue: asyncio.Queue[str] = asyncio.Queue()
        #: Test hook — every outbound send recorded here.
        self.outbox: list[dict] = []

    # ---- lifecycle ----

    async def connect(self) -> None:
        if self._read_stdin and sys.stdin is not None and sys.stdin.isatty():
            self._task = asyncio.create_task(self._stdin_loop(),
                                             name="gateway.console.stdin")
        # Regardless of stdin, a queue-fed loop is always running (tests use it).
        if self._task is None:
            self._task = asyncio.create_task(self._queue_loop(),
                                             name="gateway.console.queue")
        sys.stdout.write(
            f"[console] gateway ready. type a message and press Enter.\n"
        )
        sys.stdout.flush()

    async def disconnect(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    # ---- outbound ----

    async def send(self, source: SessionSource, text: str, *,
                   reply_to_message_id: Optional[str] = None) -> SendResult:
        line = f"[console → {source.chat_id}] {text}\n"
        try:
            sys.stdout.write(line)
            sys.stdout.flush()
        except Exception as e:
            return SendResult(ok=False, error=str(e))
        # Record for tests
        self.outbox.append({"source": source.as_dict(), "text": text,
                            "reply_to": reply_to_message_id})
        msg_id = f"console-{len(self.outbox)}"
        return SendResult(ok=True, message_id=msg_id)

    # ---- test / programmatic hook ----

    async def push_input(self, text: str, *, user_id: Optional[str] = None,
                         chat_id: Optional[str] = None) -> None:
        """Feed a line as if it came from stdin (useful in tests + scripts)."""
        await self._input_queue.put(
            _fmt_queue_item(text, user_id or self._user_id,
                            chat_id or self._user_id)
        )

    # ---- internal loops ----

    async def _queue_loop(self) -> None:
        while True:
            item = await self._input_queue.get()
            text, user_id, chat_id = _parse_queue_item(item)
            await self._deliver(
                InboundMessage(
                    source=SessionSource(
                        platform=self.platform,
                        chat_id=chat_id,
                        user_id=user_id,
                        chat_type="dm",
                    ),
                    text=text,
                )
            )

    async def _stdin_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
            except Exception:
                log.exception("console: stdin read failed")
                return
            if not line:  # EOF
                return
            line = line.rstrip("\r\n")
            if not line:
                continue
            await self._deliver(
                InboundMessage(
                    source=SessionSource(
                        platform=self.platform,
                        chat_id=self._user_id,
                        user_id=self._user_id,
                        chat_type="dm",
                    ),
                    text=line,
                )
            )


# The queue carries a compact "text\x00user\x00chat" string so it stays
# simple without importing dataclasses here.

def _fmt_queue_item(text: str, user_id: str, chat_id: str) -> str:
    return f"{text}\x00{user_id}\x00{chat_id}"


def _parse_queue_item(item: str) -> tuple[str, str, str]:
    parts = item.split("\x00", 2)
    while len(parts) < 3:
        parts.append("")
    return parts[0], parts[1], parts[2]
