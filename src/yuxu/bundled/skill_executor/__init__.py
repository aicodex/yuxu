"""skill_executor bundled agent — skill runtime (Mode A bus + Mode B inline)."""
from __future__ import annotations

from .handler import SkillExecutor

NAME = "skill_executor"

__all__ = ["SkillExecutor", "NAME", "start", "stop", "get_handle"]

_instance: SkillExecutor | None = None


async def start(ctx) -> None:
    global _instance
    _instance = SkillExecutor(ctx)
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
