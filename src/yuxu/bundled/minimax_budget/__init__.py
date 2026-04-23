"""minimax_budget — MiniMax-specific quota tracker + per-agent attribution."""
from __future__ import annotations

import json
import logging
import os

from .handler import MiniMaxBudget

NAME = "minimax_budget"

__all__ = ["MiniMaxBudget", "NAME", "start", "stop", "get_handle"]

log = logging.getLogger(__name__)

_instance: MiniMaxBudget | None = None


def _load_reservations_from_env() -> dict[str, int]:
    """Read `MINIMAX_RESERVATIONS` as a JSON map {agent: requests_per_interval}.

    Example: `MINIMAX_RESERVATIONS='{"scheduled_newsfeed": 50, "critical_bot": 30}'`.
    Invalid JSON or non-int values are logged and ignored so a typo never
    kills startup.
    """
    raw = os.environ.get("MINIMAX_RESERVATIONS", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("minimax_budget: MINIMAX_RESERVATIONS is not valid JSON; ignoring")
        return {}
    if not isinstance(data, dict):
        log.warning("minimax_budget: MINIMAX_RESERVATIONS must be an object; ignoring")
        return {}
    out: dict[str, int] = {}
    for k, v in data.items():
        try:
            n = int(v)
        except (TypeError, ValueError):
            log.warning("minimax_budget: reservation for %s is not an int: %r", k, v)
            continue
        if n <= 0:
            log.warning("minimax_budget: reservation for %s must be > 0, got %d", k, n)
            continue
        out[str(k)] = n
    return out


async def start(ctx) -> None:
    global _instance
    reservations = _load_reservations_from_env()
    if reservations:
        log.info("minimax_budget: reservations loaded: %s", reservations)
    _instance = MiniMaxBudget(ctx, reservations=reservations)
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
