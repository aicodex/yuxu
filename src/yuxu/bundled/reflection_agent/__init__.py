"""reflection_agent bundled agent — iterative exploration + memory proposal."""
from __future__ import annotations

from .handler import ReflectionAgent

NAME = "reflection_agent"

__all__ = ["ReflectionAgent", "NAME", "start", "stop", "get_handle"]

_instance: ReflectionAgent | None = None


async def start(ctx) -> None:
    global _instance
    _instance = ReflectionAgent(ctx)
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
