"""dashboard bundled agent — live-refreshing status card."""
from __future__ import annotations

from .handler import Dashboard

NAME = "dashboard"
COMMAND = "/dashboard"

_dashboard: Dashboard | None = None


async def start(ctx) -> None:
    global _dashboard
    _dashboard = Dashboard(ctx)
    _dashboard.install()
    # Register with gateway's command registry so /help can see us.
    try:
        await ctx.bus.request("gateway", {
            "op": "register_command",
            "command": COMMAND,
            "agent": NAME,
            "help": "Open a live-refreshing dashboard of this project.",
        }, timeout=2.0)
    except Exception:
        ctx.logger.exception("dashboard: register_command failed")
    await ctx.ready()


async def stop(ctx) -> None:
    if _dashboard is not None:
        await _dashboard.shutdown()


def get_handle(ctx):
    return _dashboard
