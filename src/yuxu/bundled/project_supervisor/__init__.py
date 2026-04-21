"""project_supervisor bundled agent."""
from __future__ import annotations

from .handler import NAME, ProjectSupervisor

_supervisor: ProjectSupervisor | None = None


async def start(ctx) -> None:
    global _supervisor
    _supervisor = ProjectSupervisor(ctx.bus, ctx.loader)
    _supervisor.install()
    ctx.bus.register(NAME, _supervisor.handle)
    await ctx.ready()
    # Rescue anything that already failed during earlier boot steps.
    await _supervisor.scan_and_heal()


def get_handle(ctx):
    return _supervisor
