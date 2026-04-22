"""minimax_budget — MiniMax-specific quota tracker + per-agent attribution."""
from __future__ import annotations

from .handler import MiniMaxBudget

NAME = "minimax_budget"

__all__ = ["MiniMaxBudget", "NAME", "start", "stop", "get_handle"]

_instance: MiniMaxBudget | None = None


async def start(ctx) -> None:
    global _instance
    _instance = MiniMaxBudget(ctx)
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
