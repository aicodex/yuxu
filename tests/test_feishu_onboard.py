"""Tests for the scan-to-create Feishu/Lark onboarding flow.

All HTTP is mocked at urlopen level — no network, no real Feishu.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from yuxu.bundled.gateway.adapters import feishu_onboard as fo


def _urlopen_queue(responses):
    """Return a side_effect function that yields responses one by one."""
    it = iter(responses)

    class _Resp:
        def __init__(self, body_bytes):
            self._body = body_bytes
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def _side(req, timeout=None):
        item = next(it)
        if isinstance(item, Exception):
            raise item
        return _Resp(json.dumps(item).encode("utf-8"))
    return _side


# -- stage: init ----------------------------------------------


def test_init_requires_client_secret_method():
    responses = [{
        "supported_auth_methods": ["oauth_code"],  # no client_secret
    }]
    with patch("yuxu.bundled.gateway.adapters.feishu_onboard.urlopen",
               side_effect=_urlopen_queue(responses)):
        with pytest.raises(RuntimeError, match="client_secret"):
            fo._init_registration("feishu")


def test_init_ok():
    responses = [{"supported_auth_methods": ["client_secret", "oauth_code"]}]
    with patch("yuxu.bundled.gateway.adapters.feishu_onboard.urlopen",
               side_effect=_urlopen_queue(responses)):
        fo._init_registration("feishu")  # no raise


# -- stage: begin ---------------------------------------------


def test_begin_returns_device_code_and_branded_qr():
    responses = [{
        "device_code": "dc-abc",
        "verification_uri_complete": "https://accounts.feishu.cn/qr/xyz",
        "user_code": "ABC-123",
        "interval": 5,
        "expire_in": 300,
    }]
    with patch("yuxu.bundled.gateway.adapters.feishu_onboard.urlopen",
               side_effect=_urlopen_queue(responses)):
        r = fo._begin_registration("feishu")
    assert r["device_code"] == "dc-abc"
    assert "from=yuxu" in r["qr_url"]    # branding appended
    assert r["interval"] == 5
    assert r["expire_in"] == 300


def test_begin_appends_branding_with_existing_querystring():
    responses = [{
        "device_code": "dc",
        "verification_uri_complete": "https://x/q?a=1",
    }]
    with patch("yuxu.bundled.gateway.adapters.feishu_onboard.urlopen",
               side_effect=_urlopen_queue(responses)):
        r = fo._begin_registration("feishu")
    assert r["qr_url"] == "https://x/q?a=1&from=yuxu&tp=yuxu"


def test_begin_missing_device_code_raises():
    with patch("yuxu.bundled.gateway.adapters.feishu_onboard.urlopen",
               side_effect=_urlopen_queue([{}])):
        with pytest.raises(RuntimeError):
            fo._begin_registration("feishu")


# -- stage: poll ----------------------------------------------


def test_poll_succeeds_on_credentials():
    responses = [
        {"error": "authorization_pending"},
        {
            "client_id": "cli_abc",
            "client_secret": "sec_xyz",
            "user_info": {"open_id": "ou_user", "tenant_brand": "feishu"},
        },
    ]
    with patch("yuxu.bundled.gateway.adapters.feishu_onboard.urlopen",
               side_effect=_urlopen_queue(responses)):
        with patch("yuxu.bundled.gateway.adapters.feishu_onboard.time.sleep"):
            r = fo._poll_registration(
                device_code="dc", interval=1, expire_in=60, domain="feishu",
            )
    assert r == {
        "app_id": "cli_abc",
        "app_secret": "sec_xyz",
        "domain": "feishu",
        "open_id": "ou_user",
    }


def test_poll_detects_lark_tenant_and_switches_domain():
    responses = [{
        "client_id": "cli",
        "client_secret": "sec",
        "user_info": {"tenant_brand": "lark", "open_id": "ou_lark"},
    }]
    with patch("yuxu.bundled.gateway.adapters.feishu_onboard.urlopen",
               side_effect=_urlopen_queue(responses)):
        r = fo._poll_registration(
            device_code="dc", interval=1, expire_in=60, domain="feishu",
        )
    assert r["domain"] == "lark"


def test_poll_access_denied_returns_none():
    with patch("yuxu.bundled.gateway.adapters.feishu_onboard.urlopen",
               side_effect=_urlopen_queue([{"error": "access_denied"}])):
        r = fo._poll_registration(
            device_code="dc", interval=1, expire_in=60, domain="feishu",
        )
    assert r is None


def test_poll_expired_token_returns_none():
    with patch("yuxu.bundled.gateway.adapters.feishu_onboard.urlopen",
               side_effect=_urlopen_queue([{"error": "expired_token"}])):
        r = fo._poll_registration(
            device_code="dc", interval=1, expire_in=60, domain="feishu",
        )
    assert r is None


def test_poll_times_out_returns_none():
    """If deadline passes without success/denial, return None."""
    import time as _time

    t0 = 1000.0

    def fake_time():
        # Advance time 10s per call so deadline (60s) is crossed quickly.
        fake_time.n += 10
        return t0 + fake_time.n
    fake_time.n = 0  # type: ignore[attr-defined]

    with patch("yuxu.bundled.gateway.adapters.feishu_onboard.time.time",
               side_effect=fake_time):
        with patch("yuxu.bundled.gateway.adapters.feishu_onboard.time.sleep"):
            # Responses all pending, ad infinitum. Use a generator cycle.
            def _resp_gen():
                while True:
                    yield {"error": "authorization_pending"}
            gen = _resp_gen()

            def _urlopen(req, timeout=None):
                from yuxu.bundled.gateway.adapters.feishu_onboard import (
                    _post_registration as _ignored,  # noqa
                )
                class _R:
                    def __init__(self, d): self._d = d
                    def read(self): return json.dumps(self._d).encode()
                    def __enter__(self): return self
                    def __exit__(self, *a): return False
                return _R(next(gen))

            with patch("yuxu.bundled.gateway.adapters.feishu_onboard.urlopen",
                       side_effect=_urlopen):
                r = fo._poll_registration(
                    device_code="dc", interval=1, expire_in=60, domain="feishu",
                )
    assert r is None


# -- render_qr graceful fallback ------------------------------


def test_render_qr_returns_false_without_qrcode():
    # Force qrcode absent
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "qrcode":
            raise ImportError("no qrcode installed")
        return real_import(name, *a, **kw)

    with patch("builtins.__import__", side_effect=fake_import):
        assert fo.render_qr("https://x/y") is False


# -- full orchestrator ----------------------------------------


def test_register_feishu_happy_path(capsys):
    """init → begin → poll → probe — all mocked to succeed."""
    responses = [
        # init
        {"supported_auth_methods": ["client_secret"]},
        # begin
        {
            "device_code": "dc", "user_code": "U",
            "verification_uri_complete": "https://accounts.feishu.cn/qr/x",
            "interval": 1, "expire_in": 30,
        },
        # poll — first call succeeds
        {
            "client_id": "cli_success",
            "client_secret": "sec_success",
            "user_info": {"open_id": "ou_me", "tenant_brand": "feishu"},
        },
        # probe_bot: token fetch
        {"code": 0, "tenant_access_token": "tok", "expire": 7200},
        # probe_bot: bot info
        {"code": 0, "bot": {"bot_name": "YuxuBot", "open_id": "ou_bot"}},
    ]
    with patch("yuxu.bundled.gateway.adapters.feishu_onboard.urlopen",
               side_effect=_urlopen_queue(responses)):
        with patch("yuxu.bundled.gateway.adapters.feishu_onboard.time.sleep"):
            r = fo.register_feishu(quiet=True)
    assert r is not None
    assert r["app_id"] == "cli_success"
    assert r["app_secret"] == "sec_success"
    assert r["domain"] == "feishu"
    assert r["open_id"] == "ou_me"
    assert r["bot_name"] == "YuxuBot"
    assert r["bot_open_id"] == "ou_bot"


def test_register_feishu_returns_none_on_denial():
    responses = [
        {"supported_auth_methods": ["client_secret"]},
        {
            "device_code": "dc", "user_code": "U",
            "verification_uri_complete": "https://x/qr",
            "interval": 1, "expire_in": 10,
        },
        {"error": "access_denied"},
    ]
    with patch("yuxu.bundled.gateway.adapters.feishu_onboard.urlopen",
               side_effect=_urlopen_queue(responses)):
        with patch("yuxu.bundled.gateway.adapters.feishu_onboard.time.sleep"):
            r = fo.register_feishu(quiet=True)
    assert r is None


def test_register_feishu_swallows_network_errors():
    """network errors during init/begin don't propagate; return None."""
    from urllib.error import URLError
    with patch("yuxu.bundled.gateway.adapters.feishu_onboard.urlopen",
               side_effect=URLError("dns fail")):
        r = fo.register_feishu(quiet=True)
    assert r is None


# -- gateway config fallback ----------------------------------


def test_gateway_feishu_config_fallback_reads_yaml(tmp_path, monkeypatch):
    # Set cwd to a temp project; write config/secrets/feishu.yaml
    (tmp_path / "config" / "secrets").mkdir(parents=True)
    (tmp_path / "config" / "secrets" / "feishu.yaml").write_text(
        "app_id: cli_from_file\n"
        "app_secret: sec_from_file\n"
        "domain: lark\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    monkeypatch.delenv("FEISHU_API_BASE", raising=False)

    from yuxu.bundled.gateway import _load_feishu_config
    cfg = _load_feishu_config()
    assert cfg["app_id"] == "cli_from_file"
    assert cfg["app_secret"] == "sec_from_file"
    assert cfg["api_base"] == "https://open.larksuite.com"
    assert cfg["receive_id_type"] == "chat_id"


def test_gateway_feishu_env_wins_over_file(tmp_path, monkeypatch):
    (tmp_path / "config" / "secrets").mkdir(parents=True)
    (tmp_path / "config" / "secrets" / "feishu.yaml").write_text(
        "app_id: file_id\napp_secret: file_sec\ndomain: feishu\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FEISHU_APP_ID", "env_id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "env_sec")
    monkeypatch.setenv("FEISHU_API_BASE", "https://custom/v1")

    from yuxu.bundled.gateway import _load_feishu_config
    cfg = _load_feishu_config()
    assert cfg["app_id"] == "env_id"
    assert cfg["app_secret"] == "env_sec"
    assert cfg["api_base"] == "https://custom/v1"


def test_gateway_feishu_config_absent_returns_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

    from yuxu.bundled.gateway import _load_feishu_config
    cfg = _load_feishu_config()
    assert cfg["app_id"] == ""
    assert cfg["app_secret"] == ""
    # api_base still has a default
    assert cfg["api_base"] == "https://open.feishu.cn"
