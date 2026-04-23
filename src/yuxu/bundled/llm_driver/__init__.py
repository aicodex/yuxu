"""llm_driver bundled agent."""
from __future__ import annotations

from .handler import LlmDriver

NAME = "llm_driver"

_driver: LlmDriver | None = None


async def start(ctx) -> None:
    global _driver
    _driver = LlmDriver(ctx.bus, loader=ctx.loader)
    ctx.bus.register(NAME, _driver.handle)
    await ctx.ready()


def get_handle(ctx):
    return _driver
