"""performance_ranker bundled agent."""
from __future__ import annotations

import logging

from .handler import NAME, PerformanceRanker

log = logging.getLogger(__name__)

__all__ = ["NAME", "PerformanceRanker", "start", "stop", "get_handle"]

_ranker: PerformanceRanker | None = None


async def start(ctx) -> None:
    global _ranker
    _ranker = PerformanceRanker(ctx.bus)
    _ranker.install()
    ctx.bus.register(NAME, _ranker.handle)
    await ctx.ready()


async def stop(ctx) -> None:
    global _ranker
    if _ranker is not None:
        _ranker.uninstall()


def get_handle(ctx):
    return _ranker
