"""approval_queue bundled agent."""
from __future__ import annotations

import logging

from .handler import NAME, ApprovalQueue

_agent: ApprovalQueue | None = None


async def start(ctx) -> None:
    global _agent
    _agent = ApprovalQueue(ctx.bus)
    try:
        await _agent.load_state()
    except Exception:
        logging.getLogger(__name__).exception(
            "approval_queue: load_state failed, starting with empty queue")
    ctx.bus.register(NAME, _agent.handle)
    await ctx.ready()


def get_handle(ctx):
    return _agent
