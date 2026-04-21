"""echo_bot example — mock-streaming gateway consumer."""
from __future__ import annotations

from .handler import EchoBot

NAME = "echo_bot"

_bot: EchoBot | None = None


async def start(ctx) -> None:
    global _bot
    _bot = EchoBot(ctx)
    _bot.install()
    await ctx.ready()


def get_handle(ctx):
    return _bot
