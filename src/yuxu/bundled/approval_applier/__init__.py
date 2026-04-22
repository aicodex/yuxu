"""approval_applier bundled agent — consume approved memory_edit items."""
from __future__ import annotations

from .handler import ApprovalApplier

NAME = "approval_applier"

__all__ = ["ApprovalApplier", "NAME", "start", "stop", "get_handle"]

_instance: ApprovalApplier | None = None


async def start(ctx) -> None:
    global _instance
    _instance = ApprovalApplier(ctx)
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
