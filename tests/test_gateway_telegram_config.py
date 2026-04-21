"""config/secrets/telegram.yaml loader (env wins; yaml is the fallback)."""
from __future__ import annotations

from pathlib import Path

import pytest


def _reload_gateway():
    """Force a fresh import so the yaml-path is re-read against CWD."""
    import importlib
    import yuxu.bundled.gateway as _gw
    return importlib.reload(_gw)


def test_telegram_yaml_fallback_used_when_no_env(tmp_path, monkeypatch):
    # Clean env so we exercise the yaml branch.
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USER_IDS",
              "TELEGRAM_API_BASE", "TELEGRAM_WEBHOOK_HOST",
              "TELEGRAM_WEBHOOK_PORT", "TELEGRAM_WEBHOOK_PATH",
              "TELEGRAM_WEBHOOK_SECRET_TOKEN", "TELEGRAM_WEBHOOK_PUBLIC_URL"):
        monkeypatch.delenv(k, raising=False)

    monkeypatch.chdir(tmp_path)
    secrets = tmp_path / "config" / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "telegram.yaml").write_text(
        "bot_token: 111:xyz\n"
        "allowed_user_ids: [7, 8]\n",
        encoding="utf-8",
    )

    gw = _reload_gateway()
    cfg = gw._load_telegram_config()
    assert cfg["bot_token"] == "111:xyz"
    assert cfg["allowed_user_ids"] == "7,8"
    assert cfg["api_base"] == "https://api.telegram.org"
    assert cfg["webhook_port"] is None


def test_telegram_env_wins_over_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:fromenv")
    monkeypatch.chdir(tmp_path)
    secrets = tmp_path / "config" / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "telegram.yaml").write_text(
        "bot_token: 111:fromfile\n", encoding="utf-8",
    )

    gw = _reload_gateway()
    cfg = gw._load_telegram_config()
    assert cfg["bot_token"] == "999:fromenv"


def test_telegram_yaml_webhook_block(tmp_path, monkeypatch):
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_HOST",
              "TELEGRAM_WEBHOOK_PORT", "TELEGRAM_WEBHOOK_SECRET_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    secrets = tmp_path / "config" / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "telegram.yaml").write_text(
        "bot_token: 111:xyz\n"
        "webhook_host: 0.0.0.0\n"
        "webhook_port: 9090\n"
        "webhook_secret_token: xyz\n",
        encoding="utf-8",
    )
    gw = _reload_gateway()
    cfg = gw._load_telegram_config()
    assert cfg["webhook_host"] == "0.0.0.0"
    assert cfg["webhook_port"] == 9090
    assert cfg["webhook_path"] == "/telegram/webhook"
    assert cfg["webhook_secret_token"] == "xyz"
