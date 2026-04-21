"""gateway bundled agent."""
from __future__ import annotations

import logging
import os

from .adapters import ConsoleAdapter, TelegramAdapter
from .handler import GatewayManager

NAME = "gateway"

__all__ = ["NAME", "GatewayManager", "start", "stop", "get_handle"]

log = logging.getLogger(__name__)

_manager: GatewayManager | None = None


def _parse_allowed_user_ids(raw: str | None) -> set[int] | None:
    if not raw:
        return None
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            log.warning("gateway: TELEGRAM_ALLOWED_USER_IDS ignoring %r", part)
    return out or None


def _build_adapters() -> list:
    adapters: list = []
    # console: default on; can be disabled for headless runs
    console_flag = os.environ.get("GATEWAY_CONSOLE_ENABLED", "true").lower()
    if console_flag in ("1", "true", "yes", "on"):
        adapters.append(ConsoleAdapter())
    # telegram: opt-in via bot token
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if token:
        adapters.append(TelegramAdapter(
            bot_token=token,
            allowed_user_ids=_parse_allowed_user_ids(
                os.environ.get("TELEGRAM_ALLOWED_USER_IDS"),
            ),
            api_base=os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org"),
        ))
    return adapters


async def start(ctx) -> None:
    global _manager
    _manager = GatewayManager(ctx.bus)
    for adapter in _build_adapters():
        try:
            _manager.register_adapter(adapter)
        except Exception:
            log.exception("gateway: failed to register adapter %s",
                          getattr(adapter, "platform", "?"))
    ctx.bus.register(NAME, _manager.handle)
    await _manager.start()
    await ctx.ready()


async def stop(ctx) -> None:
    if _manager is not None:
        await _manager.stop()


def get_handle(ctx):
    return _manager
