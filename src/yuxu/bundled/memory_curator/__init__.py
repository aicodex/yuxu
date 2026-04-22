"""memory_curator bundled agent — Hermes-inspired auto memory curation."""
from __future__ import annotations

from .handler import MemoryCurator

NAME = "memory_curator"

__all__ = ["MemoryCurator", "NAME", "start", "stop", "get_handle"]

_instance: MemoryCurator | None = None


async def start(ctx) -> None:
    global _instance
    _instance = MemoryCurator(ctx)
    await _instance.install()
    ctx.bus.register(NAME, _instance.handle)
    await ctx.ready()


async def stop(ctx) -> None:
    global _instance
    if _instance is not None:
        await _instance.uninstall()
        _instance = None


def get_handle(ctx):
    return _instance
