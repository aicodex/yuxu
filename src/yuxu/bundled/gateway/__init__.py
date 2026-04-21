"""gateway bundled agent."""
from __future__ import annotations

import logging
import os

from .adapters import ConsoleAdapter, FeishuAdapter, TelegramAdapter
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
    # feishu: env vars win, else fall back to config/secrets/feishu.yaml
    # (written by `yuxu feishu register`).
    fs_app_id, fs_app_secret, fs_api_base, fs_rit = _load_feishu_config()
    if fs_app_id and fs_app_secret:
        adapters.append(FeishuAdapter(
            app_id=fs_app_id,
            app_secret=fs_app_secret,
            api_base=fs_api_base,
            default_receive_id_type=fs_rit,
        ))
    return adapters


def _load_feishu_config() -> tuple[str, str, str, str]:
    """Return (app_id, app_secret, api_base, receive_id_type).

    Priority: env vars > ./config/secrets/feishu.yaml.
    """
    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    api_base = os.environ.get("FEISHU_API_BASE", "").strip()
    rit = os.environ.get("FEISHU_RECEIVE_ID_TYPE", "chat_id").strip()

    if not (app_id and app_secret):
        from pathlib import Path as _P
        cfg_path = _P("config/secrets/feishu.yaml")
        if cfg_path.exists():
            try:
                import yaml as _yaml
                data = _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            except Exception:
                log.exception("gateway: failed to read %s", cfg_path)
                data = {}
            app_id = app_id or str(data.get("app_id", "")).strip()
            app_secret = app_secret or str(data.get("app_secret", "")).strip()
            if not api_base:
                domain = str(data.get("domain") or "feishu")
                api_base = ("https://open.larksuite.com" if domain == "lark"
                             else "https://open.feishu.cn")
    if not api_base:
        api_base = "https://open.feishu.cn"
    return app_id, app_secret, api_base, rit


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
