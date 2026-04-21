"""Tests for Feishu webhook (events + server + adapter integration).

We test:
  - feishu_events: signature, token, decrypt, URL challenge detection,
    message parsing (text, post, mentions)
  - feishu_webhook: aiohttp server end-to-end via aiohttp test client
  - FeishuAdapter: inbound event → _deliver (incl. mention gating)
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from typing import Any, Optional

import httpx
import pytest
from aiohttp.test_utils import TestServer, TestClient

from yuxu.bundled.gateway.adapters.feishu import FeishuAdapter
from yuxu.bundled.gateway.adapters.feishu_events import (
    VerifyError,
    decrypt_payload,
    event_type_of,
    is_url_verification,
    parse_message_event,
    unwrap_event,
    url_verification_response,
    verify_signature,
    verify_token,
)
from yuxu.bundled.gateway.adapters.feishu_webhook import FeishuWebhook
from yuxu.bundled.gateway.session import InboundMessage

pytestmark = pytest.mark.asyncio


# -- signature & token -----------------------------------------


def test_signature_ok():
    body = b'{"hello":"world"}'
    ts = "1712345678"
    nonce = "abc"
    key = "sekrit"
    sig = hashlib.sha256((ts + nonce + key).encode() + body).hexdigest()
    verify_signature(timestamp=ts, nonce=nonce, body=body,
                     encrypt_key=key, signature=sig)


def test_signature_mismatch():
    with pytest.raises(VerifyError):
        verify_signature(timestamp="1", nonce="n", body=b"x",
                         encrypt_key="k", signature="bad")


def test_signature_missing_components():
    with pytest.raises(VerifyError):
        verify_signature(timestamp="", nonce="", body=b"",
                         encrypt_key="", signature="")


def test_verify_token_v1_style():
    verify_token({"token": "abc123"}, "abc123")
    with pytest.raises(VerifyError):
        verify_token({"token": "nope"}, "abc123")


def test_verify_token_v2_style():
    verify_token({"header": {"token": "xyz"}}, "xyz")
    with pytest.raises(VerifyError):
        verify_token({"header": {}}, "xyz")
    with pytest.raises(VerifyError):
        verify_token({}, "xyz")


# -- encryption ------------------------------------------------


def _encrypt(plaintext_bytes: bytes, encrypt_key: str) -> str:
    """Mirror of decrypt_payload for test fixtures."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.padding import PKCS7
    import os as _os

    key = hashlib.sha256(encrypt_key.encode()).digest()
    iv = _os.urandom(16)
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext_bytes) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv),
                     backend=default_backend())
    encryptor = cipher.encryptor()
    ct = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(iv + ct).decode()


def test_decrypt_round_trip():
    key = "my-encrypt-key"
    plain = b'{"event":"sample"}'
    enc = _encrypt(plain, key)
    assert decrypt_payload(enc, key) == plain


def test_decrypt_wrong_key_raises():
    key = "k1"
    plain = b"secret"
    enc = _encrypt(plain, key)
    with pytest.raises(VerifyError):
        decrypt_payload(enc, "k2")


def test_unwrap_plaintext_event_passthrough():
    event = {"type": "im.message.receive_v1", "event": {"message": {}}}
    assert unwrap_event(event, None) is event


def test_unwrap_encrypted_event():
    key = "xyz"
    plain_event = {"type": "im.message.receive_v1", "foo": "bar"}
    enc = _encrypt(json.dumps(plain_event).encode(), key)
    body = {"encrypt": enc}
    assert unwrap_event(body, key) == plain_event


def test_unwrap_encrypted_without_key_errors():
    with pytest.raises(VerifyError):
        unwrap_event({"encrypt": "xxx"}, None)


# -- URL challenge ---------------------------------------------


def test_is_url_verification_v1():
    assert is_url_verification({"type": "url_verification",
                                 "challenge": "abc"})
    assert url_verification_response({"challenge": "abc"}) == {"challenge": "abc"}


def test_is_url_verification_v2():
    assert is_url_verification({
        "header": {"event_type": "url_verification"},
        "challenge": "xyz",
    })


def test_not_url_verification():
    assert not is_url_verification({"type": "im.message.receive_v1"})


# -- message parsing -------------------------------------------


def _text_event(text: str, *, chat_type="p2p", chat_id="oc_x",
                open_id="ou_alice", mentions=None, parent_id=None):
    return {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": open_id}},
            "message": {
                "message_id": "om_1",
                "message_type": "text",
                "chat_id": chat_id,
                "chat_type": chat_type,
                "content": json.dumps({"text": text}),
                "mentions": mentions or [],
                "parent_id": parent_id,
            },
        },
    }


def test_parse_text_message():
    p = parse_message_event(_text_event("hello"))
    assert p is not None
    assert p.text == "hello"
    assert p.chat_type == "p2p"
    assert p.sender_open_id == "ou_alice"
    assert p.message_type == "text"


def test_parse_post_extracts_rich_text():
    post_content = {
        "zh_cn": {
            "title": "Meeting Notes",
            "content": [
                [{"tag": "text", "text": "Line 1 "},
                 {"tag": "at", "user_id": "bob"}],
                [{"tag": "text", "text": "Line 2"}],
            ],
        },
    }
    event = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_a"}},
            "message": {
                "message_id": "om_2",
                "message_type": "post",
                "chat_id": "oc",
                "chat_type": "p2p",
                "content": json.dumps(post_content),
            },
        },
    }
    p = parse_message_event(event)
    assert "Meeting Notes" in p.text
    assert "Line 1" in p.text and "@bob" in p.text
    assert "Line 2" in p.text


def test_parse_mentions():
    event = _text_event("hi @bot", mentions=[
        {"id": {"open_id": "ou_bot"}},
        {"id": {"open_id": "ou_other"}},
    ])
    p = parse_message_event(event)
    assert p.mentions == ["ou_bot", "ou_other"]


def test_parse_image_fills_media_keys():
    event = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_a"}},
            "message": {
                "message_id": "om_3",
                "message_type": "image",
                "chat_id": "oc",
                "chat_type": "p2p",
                "content": json.dumps({"image_key": "img_xyz"}),
            },
        },
    }
    p = parse_message_event(event)
    assert p.media_keys == ["img_xyz"]
    assert p.text == ""


def test_parse_non_message_event_returns_none():
    assert parse_message_event({}) is None
    assert parse_message_event({"event": {"not_a_message": 1}}) is None


def test_event_type_reads_v1_and_v2():
    assert event_type_of({"type": "a"}) == "a"
    assert event_type_of({"header": {"event_type": "b"}}) == "b"
    assert event_type_of({}) == ""


# -- webhook server (aiohttp) ----------------------------------


async def _run_webhook_and_post(webhook: FeishuWebhook, *, body: bytes,
                                  headers: Optional[dict] = None) -> tuple[int, dict]:
    """Spin webhook up on a random free port and POST."""
    webhook.host = "127.0.0.1"
    webhook.port = 0   # random
    await webhook.start()
    try:
        # aiohttp.TCPSite picks a port; fetch it
        sock = webhook._site._server.sockets[0]
        port = sock.getsockname()[1]
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"http://127.0.0.1:{port}{webhook.path}",
                content=body,
                headers=headers or {"Content-Type": "application/json"},
                timeout=5.0,
            )
        return resp.status_code, resp.json()
    finally:
        await webhook.stop()


async def test_webhook_url_verification_challenge():
    delivered = []

    async def on_event(ev): delivered.append(ev)

    wh = FeishuWebhook(on_event=on_event)
    body = json.dumps({
        "type": "url_verification", "challenge": "hello",
    }).encode()
    status, data = await _run_webhook_and_post(wh, body=body)
    assert status == 200
    assert data == {"challenge": "hello"}
    assert delivered == []   # challenge doesn't call on_event


async def test_webhook_plaintext_event_with_token():
    delivered = []

    async def on_event(ev): delivered.append(ev)

    wh = FeishuWebhook(verification_token="tok_abc", on_event=on_event)
    body = json.dumps({
        "token": "tok_abc",
        "header": {"event_type": "im.message.receive_v1"},
        "event": {"sender": {"sender_id": {}}, "message": {
            "message_id": "m1", "message_type": "text", "chat_id": "c",
            "chat_type": "p2p", "content": '{"text":"hi"}',
        }},
    }).encode()
    status, data = await _run_webhook_and_post(wh, body=body)
    assert status == 200
    assert data == {"ok": True}
    assert len(delivered) == 1


async def test_webhook_bad_token_rejected():
    wh = FeishuWebhook(verification_token="right")
    body = json.dumps({
        "token": "wrong",
        "header": {"event_type": "im.message.receive_v1"},
        "event": {"message": {}},
    }).encode()
    status, data = await _run_webhook_and_post(wh, body=body)
    assert status == 403


async def test_webhook_signature_required_when_encrypt_key_set():
    wh = FeishuWebhook(encrypt_key="key")
    body = b'{"hello":1}'
    headers = {
        "Content-Type": "application/json",
        "X-Lark-Signature": "bad",
        "X-Lark-Request-Timestamp": "1",
        "X-Lark-Request-Nonce": "n",
    }
    status, _ = await _run_webhook_and_post(wh, body=body, headers=headers)
    assert status == 403


async def test_webhook_encrypted_event_round_trip():
    delivered = []

    async def on_event(ev): delivered.append(ev)

    key = "mykey"
    wh = FeishuWebhook(encrypt_key=key, on_event=on_event)
    plain_event = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {"sender": {}, "message": {
            "message_id": "m", "message_type": "text",
            "chat_id": "c", "chat_type": "p2p",
            "content": '{"text":"encrypted hi"}',
        }},
    }
    body = json.dumps({"encrypt": _encrypt(
        json.dumps(plain_event).encode(), key,
    )}).encode()
    # Build a valid signature so the webhook doesn't 403 before decrypt.
    import time as _t
    ts = str(int(_t.time()))
    nonce = "n"
    sig = hashlib.sha256((ts + nonce + key).encode() + body).hexdigest()
    status, data = await _run_webhook_and_post(wh, body=body, headers={
        "Content-Type": "application/json",
        "X-Lark-Signature": sig,
        "X-Lark-Request-Timestamp": ts,
        "X-Lark-Request-Nonce": nonce,
    })
    assert status == 200
    assert data == {"ok": True}
    assert len(delivered) == 1


async def test_webhook_health_endpoint():
    wh = FeishuWebhook()
    wh.host = "127.0.0.1"
    wh.port = 0
    await wh.start()
    try:
        sock = wh._site._server.sockets[0]
        port = sock.getsockname()[1]
        async with httpx.AsyncClient() as c:
            resp = await c.get(f"http://127.0.0.1:{port}{wh.path}/health")
        assert resp.status_code == 200
    finally:
        await wh.stop()


# -- FeishuAdapter inbound integration -------------------------


def _fake_token_route():
    def route(req):
        if "tenant_access_token" in str(req.url):
            return httpx.Response(200, json={
                "code": 0, "tenant_access_token": "t-x",
                "expire": 7200,
            })
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om"}})
    return route


async def test_adapter_webhook_delivers_text_message_in_dm():
    delivered = []

    async def capture(msg: InboundMessage):
        delivered.append(msg)

    adapter = FeishuAdapter(
        app_id="x", app_secret="y",
        webhook_host="127.0.0.1", webhook_port=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_fake_token_route())),
    )
    adapter._owned_client = True
    adapter.bind_inbound(capture)
    await adapter.connect()
    try:
        # Post a text event directly to the webhook
        sock = adapter._webhook._site._server.sockets[0]
        port = sock.getsockname()[1]
        async with httpx.AsyncClient() as client:
            await client.post(
                f"http://127.0.0.1:{port}{adapter._webhook.path}",
                json={
                    "header": {"event_type": "im.message.receive_v1"},
                    "event": {
                        "sender": {"sender_id": {"open_id": "ou_user"}},
                        "message": {
                            "message_id": "om_1",
                            "message_type": "text",
                            "chat_id": "oc_xyz",
                            "chat_type": "p2p",
                            "content": json.dumps({"text": "hello bot"}),
                        },
                    },
                },
                timeout=5.0,
            )
        # Let event task complete
        for _ in range(30):
            await asyncio.sleep(0.02)
            if delivered:
                break
        assert len(delivered) == 1
        msg = delivered[0]
        assert msg.text == "hello bot"
        assert msg.source.platform == "feishu"
        assert msg.source.chat_id == "oc_xyz"
        assert msg.source.user_id == "ou_user"
        assert msg.source.chat_type == "dm"
    finally:
        await adapter.disconnect()


async def test_adapter_mention_gating_drops_group_without_mention():
    delivered = []

    async def capture(msg: InboundMessage):
        delivered.append(msg)

    adapter = FeishuAdapter(
        app_id="x", app_secret="y",
        webhook_host="127.0.0.1", webhook_port=0,
        bot_open_id="ou_bot",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_fake_token_route())),
    )
    adapter._owned_client = True
    adapter.bind_inbound(capture)
    await adapter.connect()
    try:
        sock = adapter._webhook._site._server.sockets[0]
        port = sock.getsockname()[1]
        async with httpx.AsyncClient() as c:
            # group message without mention → dropped
            await c.post(
                f"http://127.0.0.1:{port}{adapter._webhook.path}",
                json={
                    "header": {"event_type": "im.message.receive_v1"},
                    "event": {"sender": {"sender_id": {"open_id": "ou_a"}},
                              "message": {"message_id": "m1",
                                           "message_type": "text",
                                           "chat_id": "c",
                                           "chat_type": "group",
                                           "content": '{"text":"random chatter"}',
                                           "mentions": []}},
                },
                timeout=5.0,
            )
            # group message WITH mention → delivered
            await c.post(
                f"http://127.0.0.1:{port}{adapter._webhook.path}",
                json={
                    "header": {"event_type": "im.message.receive_v1"},
                    "event": {"sender": {"sender_id": {"open_id": "ou_a"}},
                              "message": {"message_id": "m2",
                                           "message_type": "text",
                                           "chat_id": "c",
                                           "chat_type": "group",
                                           "content": '{"text":"@bot hi"}',
                                           "mentions": [{"id": {"open_id": "ou_bot"}}]}},
                },
                timeout=5.0,
            )
        for _ in range(30):
            await asyncio.sleep(0.02)
            if delivered:
                break
        assert len(delivered) == 1     # first dropped, second delivered
        assert delivered[0].text == "@bot hi"
    finally:
        await adapter.disconnect()


async def test_adapter_ignores_non_message_events():
    delivered = []

    async def capture(msg): delivered.append(msg)

    adapter = FeishuAdapter(
        app_id="x", app_secret="y",
        webhook_host="127.0.0.1", webhook_port=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_fake_token_route())),
    )
    adapter._owned_client = True
    adapter.bind_inbound(capture)
    await adapter.connect()
    try:
        sock = adapter._webhook._site._server.sockets[0]
        port = sock.getsockname()[1]
        async with httpx.AsyncClient() as c:
            await c.post(
                f"http://127.0.0.1:{port}{adapter._webhook.path}",
                json={"header": {"event_type": "im.reaction.created_v1"},
                      "event": {"reaction_type": {"emoji_type": "THUMBSUP"}}},
                timeout=5.0,
            )
        await asyncio.sleep(0.1)
        assert delivered == []
    finally:
        await adapter.disconnect()


async def test_adapter_without_webhook_still_outbound_only():
    """No webhook_host/port → no server started, outbound still works."""
    adapter = FeishuAdapter(
        app_id="x", app_secret="y",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_fake_token_route())),
    )
    adapter._owned_client = True
    await adapter.connect()
    try:
        assert adapter._webhook is None
    finally:
        await adapter.disconnect()
