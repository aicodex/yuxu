"""Feishu / Lark event-subscription primitives: verify, decrypt, parse.

Pure functions — no HTTP, no state. Consumed by `feishu_webhook.py` and by
the adapter itself (when processing either plaintext or encrypted events).

References:
  Feishu open platform, "订阅事件 / 事件订阅" pages.
  Signature = base64(sha256(timestamp + nonce + encrypt_key + body))
  Token = plain string included in `token` field of v1 schema or under header.token in v2.
  Encryption = AES-256-CBC, key = sha256(encrypt_key), PKCS7 padding, base64.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger(__name__)


class VerifyError(ValueError):
    """Signature / token / decrypt failure. Maps to HTTP 403 at webhook layer."""


# -- signature & token ---------------------------------------


def verify_signature(*, timestamp: str, nonce: str, body: bytes,
                     encrypt_key: str, signature: str) -> None:
    """Raise VerifyError if the base64(sha256(...)) signature doesn't match.

    Only called when encrypt_key is configured — Feishu signs the raw body.
    """
    if not (timestamp and nonce and encrypt_key and signature):
        raise VerifyError("missing signature components")
    to_sign = (timestamp + nonce + encrypt_key).encode("utf-8") + body
    digest = hashlib.sha256(to_sign).hexdigest()
    if not hmac.compare_digest(digest, signature):
        raise VerifyError("signature mismatch")


def verify_token(event: dict, expected_token: str) -> None:
    """For plaintext events, Feishu embeds a `token` field.

    v1 events:  event["token"]
    v2 events:  event["header"]["token"]
    """
    token = event.get("token")
    if token is None:
        token = (event.get("header") or {}).get("token")
    if not token or not hmac.compare_digest(str(token), expected_token):
        raise VerifyError("token mismatch")


# -- decryption ----------------------------------------------


def decrypt_payload(encrypt_b64: str, encrypt_key: str) -> bytes:
    """AES-256-CBC decrypt the `encrypt` field.

    Key = sha256(encrypt_key) (32 bytes).
    Layout: first 16 bytes of the decoded bytes are the IV; rest is ciphertext.
    PKCS7 padding.
    """
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives.padding import PKCS7
    except ImportError as e:
        raise VerifyError(
            "cryptography library required for encrypted Feishu events; "
            "install with `pip install cryptography`"
        ) from e

    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    try:
        blob = base64.b64decode(encrypt_b64)
    except Exception as e:
        raise VerifyError(f"bad base64 in encrypt: {e}") from e
    if len(blob) < 17:
        raise VerifyError("encrypt blob too short")
    iv, ct = blob[:16], blob[16:]
    try:
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv),
                        backend=default_backend())
        decryptor = cipher.decryptor()
        padded = decryptor.update(ct) + decryptor.finalize()
        unpadder = PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except Exception as e:
        raise VerifyError(f"decrypt failed: {e}") from e


def unwrap_event(body: dict, encrypt_key: Optional[str]) -> dict:
    """If body has `encrypt` field, decrypt & parse; else return body."""
    if "encrypt" in body:
        if not encrypt_key:
            raise VerifyError("encrypted event but no encrypt_key configured")
        plain = decrypt_payload(body["encrypt"], encrypt_key).decode("utf-8")
        return json.loads(plain)
    return body


# -- URL challenge -------------------------------------------


def is_url_verification(event: dict) -> bool:
    """Detect Feishu's handshake event for event-subscription setup."""
    if event.get("type") == "url_verification":
        return True
    header = event.get("header") or {}
    return header.get("event_type") == "url_verification"


def url_verification_response(event: dict) -> dict:
    """Build the response body Feishu expects: {'challenge': <echo>}."""
    return {"challenge": event.get("challenge", "")}


# -- message parsing -----------------------------------------


@dataclass
class ParsedMessage:
    text: str
    mentions: list[str] = field(default_factory=list)      # open_ids
    media_keys: list[str] = field(default_factory=list)     # Feishu-internal keys
    reply_to_message_id: Optional[str] = None
    chat_type: str = "p2p"                                  # "p2p" | "group"
    chat_id: str = ""
    sender_open_id: Optional[str] = None
    message_id: Optional[str] = None
    message_type: str = ""
    raw: dict = field(default_factory=dict)


def parse_message_event(event: dict) -> Optional[ParsedMessage]:
    """Parse an im.message.receive_v1 event into a normalized form.

    Returns None if this isn't a message event or content can't be parsed.
    """
    e = event.get("event") or event
    message = e.get("message")
    if not isinstance(message, dict):
        return None
    msg_type = message.get("message_type", "")
    content_str = message.get("content", "{}")
    try:
        content = json.loads(content_str) if isinstance(content_str, str) else content_str
    except (json.JSONDecodeError, TypeError):
        content = {}

    text = ""
    media_keys: list[str] = []

    if msg_type == "text":
        text = content.get("text") or ""
    elif msg_type == "post":
        text = _extract_post_text(content)
    elif msg_type == "image":
        key = content.get("image_key") or ""
        if key:
            media_keys.append(key)
    elif msg_type in ("file", "audio", "media"):
        key = content.get("file_key") or ""
        if key:
            media_keys.append(key)
    elif msg_type == "sticker":
        text = "[sticker]"
    else:
        # Unknown type: keep the JSON for downstream inspection.
        text = content_str if isinstance(content_str, str) else ""

    mentions: list[str] = []
    for m in message.get("mentions") or []:
        m_id = (m.get("id") or {}) if isinstance(m, dict) else {}
        oid = m_id.get("open_id")
        if oid:
            mentions.append(oid)

    sender = (e.get("sender") or {}).get("sender_id") or {}

    return ParsedMessage(
        text=text.strip() if isinstance(text, str) else "",
        mentions=mentions,
        media_keys=media_keys,
        reply_to_message_id=message.get("parent_id"),
        chat_type=message.get("chat_type", "p2p"),
        chat_id=message.get("chat_id", ""),
        sender_open_id=sender.get("open_id"),
        message_id=message.get("message_id"),
        message_type=msg_type,
        raw=message,
    )


def _extract_post_text(content: Any) -> str:
    """Flatten a rich-post payload into text. Feishu post layout:
        { "zh_cn": {"title": "...", "content": [[block, block], [block, block]]} }
    """
    if not isinstance(content, dict):
        return ""
    lines: list[str] = []
    for lang_key, post in content.items():
        if not isinstance(post, dict):
            continue
        if post.get("title"):
            lines.append(str(post["title"]))
        for para in post.get("content") or []:
            if not isinstance(para, list):
                continue
            row: list[str] = []
            for block in para:
                if not isinstance(block, dict):
                    continue
                tag = block.get("tag")
                if tag == "text":
                    row.append(str(block.get("text", "")))
                elif tag == "a":
                    row.append(f"{block.get('text', '')}({block.get('href', '')})")
                elif tag == "at":
                    row.append("@" + str(block.get("user_id") or block.get("user_name") or ""))
                elif tag == "img":
                    row.append(f"[img:{block.get('image_key', '')}]")
            if row:
                lines.append("".join(row))
    return "\n".join(lines).strip()


# -- event-type dispatch helpers -----------------------------


def event_type_of(event: dict) -> str:
    """Return the normalized event type, spanning v1 and v2 schemas."""
    header = event.get("header") or {}
    return header.get("event_type") or event.get("type") or ""
