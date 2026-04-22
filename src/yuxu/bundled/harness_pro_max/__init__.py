"""harness_pro_max bundled agent — agent creator (v0)."""
from __future__ import annotations

from .handler import HarnessProMax

NAME = "harness_pro_max"

__all__ = ["HarnessProMax", "NAME", "start", "stop", "get_handle"]

_instance: HarnessProMax | None = None


async def start(ctx) -> None:
    global _instance
    _instance = HarnessProMax(ctx)
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
