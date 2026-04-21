"""help_plugin bundled agent — /help command."""
from __future__ import annotations

from .handler import HelpPlugin

NAME = "help_plugin"
COMMAND = "/help"

_plugin: HelpPlugin | None = None


async def start(ctx) -> None:
    global _plugin
    _plugin = HelpPlugin(ctx)
    _plugin.install()
    try:
        await ctx.bus.request("gateway", {
            "op": "register_command",
            "command": COMMAND,
            "agent": NAME,
            "help": "List available slash commands. Usage: /help [/cmd]",
        }, timeout=2.0)
    except Exception:
        ctx.logger.exception("help_plugin: register_command failed")
    await ctx.ready()


def get_handle(ctx):
    return _plugin
