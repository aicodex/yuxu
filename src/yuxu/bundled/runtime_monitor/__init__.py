"""runtime_monitor bundled agent — per-serve registry + stale cleanup."""
from __future__ import annotations

from .handler import RuntimeMonitor

NAME = "runtime_monitor"

__all__ = ["RuntimeMonitor", "NAME", "start", "stop", "get_handle"]

_instance: RuntimeMonitor | None = None


async def start(ctx) -> None:
    global _instance
    _instance = RuntimeMonitor(ctx)
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
