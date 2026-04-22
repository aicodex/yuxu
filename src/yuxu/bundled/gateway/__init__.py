"""gateway bundled agent."""
from __future__ import annotations

import logging
import os
from typing import Optional

from pathlib import Path

from .adapters import ConsoleAdapter, FeishuAdapter, TelegramAdapter
from .handler import GatewayManager
from .pairing import DEFAULT_PAIRING_PATH, PairingRegistry

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
    # telegram: env vars win, else fall back to config/secrets/telegram.yaml
    # (written by `yuxu setup`).
    tg = _load_telegram_config()
    if tg["bot_token"]:
        tg_kwargs: dict = {
            "bot_token": tg["bot_token"],
            "allowed_user_ids": _parse_allowed_user_ids(
                tg["allowed_user_ids"],
            ),
            "api_base": tg["api_base"],
        }
        if tg["webhook_host"] and tg["webhook_port"] is not None:
            tg_kwargs["webhook_host"] = tg["webhook_host"]
            tg_kwargs["webhook_port"] = tg["webhook_port"]
            tg_kwargs["webhook_path"] = tg["webhook_path"]
            tg_kwargs["webhook_secret_token"] = tg["webhook_secret_token"] or None
            tg_kwargs["webhook_public_url"] = tg["webhook_public_url"] or None
        adapters.append(TelegramAdapter(**tg_kwargs))
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


def _load_telegram_config() -> dict:
    """Merge env vars (win) with config/secrets/telegram.yaml (fallback).

    Returns a dict with:
        bot_token, allowed_user_ids (raw csv str or None), api_base,
        webhook_host, webhook_port (int|None), webhook_path,
        webhook_secret_token, webhook_public_url
    """
    from pathlib import Path as _P

    def _e(k, default=""):
        return os.environ.get(k, "").strip() or default

    data: dict = {}
    cfg_path = _P("config/secrets/telegram.yaml")
    if cfg_path.exists():
        try:
            import yaml as _yaml
            data = _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception:
            log.exception("gateway: failed to read %s", cfg_path)
            data = {}

    def _merge(env_key: str, yaml_key: str, default: str = "") -> str:
        return _e(env_key) or str(data.get(yaml_key) or "").strip() or default

    bot_token = _merge("TELEGRAM_BOT_TOKEN", "bot_token")

    # allowed_user_ids can be csv string in env, or list[int] in yaml.
    env_allowed = _e("TELEGRAM_ALLOWED_USER_IDS")
    if env_allowed:
        allowed_raw: Optional[str] = env_allowed
    else:
        yaml_allowed = data.get("allowed_user_ids")
        if isinstance(yaml_allowed, (list, tuple)):
            allowed_raw = ",".join(str(x) for x in yaml_allowed)
        elif isinstance(yaml_allowed, (int, str)) and str(yaml_allowed).strip():
            allowed_raw = str(yaml_allowed).strip()
        else:
            allowed_raw = None

    api_base = _merge("TELEGRAM_API_BASE", "api_base",
                      "https://api.telegram.org")

    webhook_host = _merge("TELEGRAM_WEBHOOK_HOST", "webhook_host")
    port_str = _merge("TELEGRAM_WEBHOOK_PORT", "webhook_port")
    webhook_port: Optional[int]
    if not port_str:
        webhook_port = None
    else:
        try:
            webhook_port = int(port_str)
        except ValueError:
            webhook_port = None

    return {
        "bot_token": bot_token,
        "allowed_user_ids": allowed_raw,
        "api_base": api_base,
        "webhook_host": webhook_host,
        "webhook_port": webhook_port,
        "webhook_path": _merge("TELEGRAM_WEBHOOK_PATH", "webhook_path",
                                "/telegram/webhook"),
        "webhook_secret_token": _merge("TELEGRAM_WEBHOOK_SECRET_TOKEN",
                                        "webhook_secret_token"),
        "webhook_public_url": _merge("TELEGRAM_WEBHOOK_PUBLIC_URL",
                                      "webhook_public_url"),
    }


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


def _build_pairing() -> tuple[PairingRegistry, set[str]]:
    path = Path(os.environ.get("GATEWAY_PAIRING_PATH") or DEFAULT_PAIRING_PATH)
    required = {
        p.strip() for p in
        (os.environ.get("GATEWAY_PAIRING_PLATFORMS") or "").split(",")
        if p.strip()
    }
    return PairingRegistry(path), required


async def start(ctx) -> None:
    global _manager
    pairing, required = _build_pairing()
    pending_tmpl = os.environ.get("GATEWAY_PAIRING_PENDING_MESSAGE") or None
    poll_raw = os.environ.get("GATEWAY_PAIRING_POLL_SEC", "").strip()
    try:
        poll_sec = float(poll_raw) if poll_raw else 1.0
    except ValueError:
        poll_sec = 1.0
    _manager = GatewayManager(
        ctx.bus, pairing=pairing,
        pairing_required_platforms=required,
        pending_reply_template=pending_tmpl,
        pairing_poll_seconds=max(0.0, poll_sec),
        loader=ctx.loader,
    )
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
