"""gateway bundled agent."""
from __future__ import annotations

import logging
import os
from typing import Optional

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
    fs = _load_feishu_config()
    if fs["app_id"] and fs["app_secret"]:
        adapters.append(FeishuAdapter(
            app_id=fs["app_id"],
            app_secret=fs["app_secret"],
            api_base=fs["api_base"],
            default_receive_id_type=fs["receive_id_type"],
            webhook_host=fs["webhook_host"] or None,
            webhook_port=fs["webhook_port"] or None,
            webhook_path=fs["webhook_path"] or "/feishu/webhook",
            verification_token=fs["verification_token"] or None,
            encrypt_key=fs["encrypt_key"] or None,
            bot_open_id=fs["bot_open_id"] or None,
        ))
    return adapters


def _load_feishu_config() -> dict:
    """Merge env vars (win) with config/secrets/feishu.yaml (fallback).

    Returns a dict with:
        app_id, app_secret, api_base, receive_id_type,
        webhook_host, webhook_port, webhook_path,
        verification_token, encrypt_key, bot_open_id
    Every key is always present; string fields default to "", port to 0.
    """
    from pathlib import Path as _P

    def _e(k, default=""):
        return os.environ.get(k, "").strip() or default

    data: dict = {}
    cfg_path = _P("config/secrets/feishu.yaml")
    if cfg_path.exists():
        try:
            import yaml as _yaml
            data = _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception:
            log.exception("gateway: failed to read %s", cfg_path)
            data = {}

    def _merge(env_key: str, yaml_key: str, default: str = "") -> str:
        return _e(env_key) or str(data.get(yaml_key) or "").strip() or default

    app_id = _merge("FEISHU_APP_ID", "app_id")
    app_secret = _merge("FEISHU_APP_SECRET", "app_secret")

    domain = (data.get("domain") or "feishu")
    default_api_base = ("https://open.larksuite.com" if domain == "lark"
                        else "https://open.feishu.cn")
    api_base = _merge("FEISHU_API_BASE", "api_base", default_api_base)

    receive_id_type = _merge("FEISHU_RECEIVE_ID_TYPE",
                              "receive_id_type", "chat_id")

    webhook_host = _merge("FEISHU_WEBHOOK_HOST", "webhook_host")
    port_str = _merge("FEISHU_WEBHOOK_PORT", "webhook_port")
    # Absent / empty / 0 → None (adapter treats None as "disabled"; a real
    # port or 0 means the adapter should start listening).
    webhook_port: Optional[int]
    if not port_str or port_str == "0":
        webhook_port = None
    else:
        try:
            webhook_port = int(port_str)
        except ValueError:
            webhook_port = None
    webhook_path = _merge("FEISHU_WEBHOOK_PATH", "webhook_path",
                          "/feishu/webhook")

    verification_token = _merge("FEISHU_VERIFICATION_TOKEN", "verification_token")
    encrypt_key = _merge("FEISHU_ENCRYPT_KEY", "encrypt_key")
    bot_open_id = _merge("FEISHU_BOT_OPEN_ID", "bot_open_id")

    return {
        "app_id": app_id,
        "app_secret": app_secret,
        "api_base": api_base,
        "receive_id_type": receive_id_type,
        "webhook_host": webhook_host,
        "webhook_port": webhook_port,
        "webhook_path": webhook_path,
        "verification_token": verification_token,
        "encrypt_key": encrypt_key,
        "bot_open_id": bot_open_id,
    }


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
