"""resource_guardian bundled agent."""
from __future__ import annotations

from .handler import ResourceGuardian

NAME = "resource_guardian"

_guardian: ResourceGuardian | None = None


async def start(ctx) -> None:
    global _guardian
    _guardian = ResourceGuardian(ctx.bus)
    _guardian.install()
    ctx.bus.register(NAME, _guardian.handle)
    await ctx.ready()


def get_handle(ctx):
    return _guardian
