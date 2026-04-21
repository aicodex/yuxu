"""recovery_agent bundled agent."""
from __future__ import annotations

import logging

from .handler import RecoveryAgent

NAME = "recovery_agent"

_agent: RecoveryAgent | None = None


async def start(ctx) -> None:
    global _agent
    _agent = RecoveryAgent(ctx.bus)
    ctx.bus.register(NAME, _agent.handle)
    # Announce ready before the initial scan so dependents don't block on a
    # potentially slow inventory pass.
    await ctx.ready()
    try:
        await _agent.scan()
    except Exception:
        logging.getLogger(__name__).exception("recovery_agent: initial scan failed")


def get_handle(ctx):
    return _agent
