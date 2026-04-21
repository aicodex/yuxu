"""project_manager bundled agent."""
from __future__ import annotations

from .handler import ProjectManager

NAME = "project_manager"

__all__ = ["ProjectManager", "NAME", "start", "get_handle"]

_manager: ProjectManager | None = None


async def start(ctx) -> None:
    global _manager
    _manager = ProjectManager(loader=ctx.loader)
    ctx.bus.register(NAME, _manager.handle)
    await ctx.ready()


def get_handle(ctx):
    return _manager
