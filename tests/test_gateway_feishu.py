from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from yuxu.bundled.gateway.adapters.feishu import FeishuAdapter, _build_feishu_card
from yuxu.bundled.gateway.draft import DraftMessage
from yuxu.bundled.gateway.session import SessionSource

pytestmark = pytest.mark.asyncio


# -- helpers ---------------------------------------------------


def _tok_response():
    return httpx.Response(200, json={
        "code": 0,
        "msg": "success",
        "expire": 7200,
        "tenant_access_token": "t-test-abc",
    })


def _send_ok_response(message_id: str = "om_xyz"):
    return httpx.Response(200, json={
        "code": 0,
        "msg": "success",
        "data": {"message_id": message_id},
    })


def _patch_ok_response():
    return httpx.Response(200, json={"code": 0, "msg": "success"})


def _make_adapter(handler, *, app_id="cli_abc", app_secret="sec"):
    return FeishuAdapter(
        app_id=app_id, app_secret=app_secret,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _src(chat="oc_abc"):
    return SessionSource(platform="feishu", chat_id=chat)


# -- card builder ----------------------------------------------


def test_card_has_schema_and_markdown_body():
    draft = DraftMessage(content="hello")
    card = _build_feishu_card(draft)
    assert card["schema"] == "2.0"
    assert card["body"]["elements"][0]["tag"] == "markdown"
    assert "hello" in card["body"]["elements"][0]["content"]


def test_card_embeds_footer_as_grey_markdown():
    draft = DraftMessage(content="hi", footer_meta=[("Agent", "main")])
    card = _build_feishu_card(draft)
    elements = card["body"]["elements"]
    tags = [e["tag"] for e in elements]
    assert tags == ["markdown", "hr", "markdown"]
    assert "<font color='grey'>" in elements[-1]["content"]
    assert "Agent: main" in elements[-1]["content"]


def test_card_empty_draft_gets_placeholder_body():
    card = _build_feishu_card(DraftMessage())
    # Feishu rejects truly empty markdown; we insert a single space
    assert card["body"]["elements"][0]["content"].strip() in ("", "")
    assert card["body"]["elements"][0]["content"] == " "


def test_card_quote_and_thinking_go_into_body_markdown():
    draft = DraftMessage(quote_user="alice", quote_text="你好",
                         thinking="I think...", content="hi!",
                         footer_meta=[("A", "1")])
    card = _build_feishu_card(draft)
    body = card["body"]["elements"][0]["content"]
    assert "回复 alice: 你好" in body
    assert "💭" in body
    assert "I think..." in body
    assert "hi!" in body
    # footer NOT in body; it's the last element
    assert "A: 1" not in body
    assert "A: 1" in card["body"]["elements"][-1]["content"]


# -- auth flow -------------------------------------------------


async def test_auth_rejects_missing_app_secret():
    with pytest.raises(ValueError):
        FeishuAdapter(app_id="x", app_secret="")


async def test_connect_fetches_tenant_token():
    calls = []

    def route(req: httpx.Request):
        calls.append(str(req.url))
        if "tenant_access_token/internal" in str(req.url):
            return _tok_response()
        return httpx.Response(404)

    adapter = _make_adapter(route)
    await adapter.connect()
    try:
        assert adapter._token == "t-test-abc"
        assert adapter._token_exp > 0
        assert any("tenant_access_token" in c for c in calls)
    finally:
        await adapter.disconnect()


async def test_auth_failure_surfaces_via_send_result():
    def route(req):
        return httpx.Response(200, json={
            "code": 99991663, "msg": "bad app secret"
        })

    adapter = _make_adapter(route)
    # connect() should raise because token fetch fails
    with pytest.raises(RuntimeError):
        await adapter.connect()
    # cleanup
    if adapter._client:
        await adapter._client.aclose()


# -- send / reply / edit --------------------------------------


async def test_send_text_uses_correct_url_and_body():
    captured = {}

    def route(req: httpx.Request):
        if "tenant_access_token" in str(req.url):
            return _tok_response()
        captured["url"] = str(req.url)
        captured["headers"] = dict(req.headers)
        captured["body"] = json.loads(req.content)
        return _send_ok_response()

    adapter = _make_adapter(route)
    await adapter.connect()
    try:
        r = await adapter.send(_src("oc_123"), "hello")
        assert r.ok and r.message_id == "om_xyz"
        assert "/open-apis/im/v1/messages" in captured["url"]
        assert "receive_id_type=chat_id" in captured["url"]
        assert captured["body"]["receive_id"] == "oc_123"
        assert captured["body"]["msg_type"] == "text"
        assert json.loads(captured["body"]["content"]) == {"text": "hello"}
        assert captured["headers"]["authorization"] == "Bearer t-test-abc"
    finally:
        await adapter.disconnect()


async def test_send_with_reply_to_goes_to_reply_endpoint():
    captured = {}

    def route(req: httpx.Request):
        if "tenant_access_token" in str(req.url):
            return _tok_response()
        captured.setdefault("urls", []).append(str(req.url))
        return _send_ok_response()

    adapter = _make_adapter(route)
    await adapter.connect()
    try:
        r = await adapter.send(_src("oc"), "quoted", reply_to_message_id="om_parent")
        assert r.ok
        assert any("/messages/om_parent/reply" in u for u in captured["urls"])
    finally:
        await adapter.disconnect()


async def test_send_http_error_bubbles_up():
    def route(req):
        if "tenant_access_token" in str(req.url):
            return _tok_response()
        return httpx.Response(500, text="oops")

    adapter = _make_adapter(route)
    await adapter.connect()
    try:
        r = await adapter.send(_src(), "hi")
        assert r.ok is False
        assert "HTTP 500" in r.error
    finally:
        await adapter.disconnect()


async def test_send_code_nonzero_bubbles_up():
    def route(req):
        if "tenant_access_token" in str(req.url):
            return _tok_response()
        return httpx.Response(200, json={"code": 230001, "msg": "Receiver not found"})

    adapter = _make_adapter(route)
    await adapter.connect()
    try:
        r = await adapter.send(_src(), "hi")
        assert r.ok is False
        assert "Receiver not found" in r.error
    finally:
        await adapter.disconnect()


async def test_edit_text_via_patch():
    captured = {}

    def route(req: httpx.Request):
        if "tenant_access_token" in str(req.url):
            return _tok_response()
        captured["method"] = req.method
        captured["url"] = str(req.url)
        captured["body"] = json.loads(req.content)
        return _patch_ok_response()

    adapter = _make_adapter(route)
    await adapter.connect()
    try:
        r = await adapter.edit(_src(), "om_xyz", "updated text")
        assert r.ok
        assert captured["method"] == "PATCH"
        assert "/im/v1/messages/om_xyz" in captured["url"]
        assert json.loads(captured["body"]["content"]) == {"text": "updated text"}
    finally:
        await adapter.disconnect()


# -- render_draft: card send + card edit ----------------------


async def test_render_draft_sends_interactive_card():
    captured = {}

    def route(req: httpx.Request):
        if "tenant_access_token" in str(req.url):
            return _tok_response()
        captured["method"] = req.method
        captured["url"] = str(req.url)
        captured["body"] = json.loads(req.content)
        return _send_ok_response("om_card_1")

    adapter = _make_adapter(route)
    await adapter.connect()
    try:
        draft = DraftMessage(
            quote_user="alice", quote_text="你好",
            thinking="thinking...", content="你好！👋",
            footer_meta=[("Agent", "main"), ("Context", "3.2k")],
        )
        r = await adapter.render_draft(_src("oc_abc"), draft,
                                         message_id=None, finalize=False)
        assert r.ok and r.message_id == "om_card_1"
        assert captured["method"] == "POST"
        assert captured["body"]["msg_type"] == "interactive"
        card = json.loads(captured["body"]["content"])
        assert card["schema"] == "2.0"
        # body has markdown + hr + grey footer
        assert [e["tag"] for e in card["body"]["elements"]] == [
            "markdown", "hr", "markdown",
        ]
    finally:
        await adapter.disconnect()


async def test_render_draft_edits_existing_card_via_patch():
    calls = []

    def route(req: httpx.Request):
        if "tenant_access_token" in str(req.url):
            return _tok_response()
        calls.append((req.method, str(req.url)))
        return _patch_ok_response()

    adapter = _make_adapter(route)
    await adapter.connect()
    try:
        draft = DraftMessage(content="updated")
        r = await adapter.render_draft(_src("oc_abc"), draft,
                                         message_id="om_existing",
                                         finalize=False)
        assert r.ok and r.message_id == "om_existing"
        patches = [(m, u) for m, u in calls if m == "PATCH"]
        assert len(patches) == 1
        assert "/im/v1/messages/om_existing" in patches[0][1]
    finally:
        await adapter.disconnect()


async def test_render_draft_empty_no_op_before_finalize():
    calls = []

    def route(req):
        if "tenant_access_token" in str(req.url):
            return _tok_response()
        calls.append(str(req.url))
        return _send_ok_response()

    adapter = _make_adapter(route)
    await adapter.connect()
    try:
        r = await adapter.render_draft(_src(), DraftMessage(),
                                         message_id=None, finalize=False)
        assert r.ok and r.message_id is None
        # Only the token fetch happened; no message API hit.
        message_calls = [u for u in calls if "/messages" in u]
        assert message_calls == []
    finally:
        await adapter.disconnect()


# -- integration with DraftHandle streaming --------------------


async def test_draft_handle_with_feishu_streams_without_spamming():
    """Same content updates → dedup prevents repeated PATCH."""
    from yuxu.bundled.gateway.draft import DraftHandle

    calls = {"send": 0, "patch": 0}

    def route(req: httpx.Request):
        if "tenant_access_token" in str(req.url):
            return _tok_response()
        if req.method == "POST" and "/reply" not in str(req.url):
            calls["send"] += 1
            return _send_ok_response("om_1")
        if req.method == "PATCH":
            calls["patch"] += 1
            return _patch_ok_response()
        return httpx.Response(404)

    adapter = _make_adapter(route)
    await adapter.connect()
    try:
        h = DraftHandle(adapter=adapter, source=_src("oc"),
                        throttle_seconds=0.0)
        await h.open()                  # empty → no card yet
        h.set_content("step 1"); await h.flush()   # first real send
        h.set_content("step 1"); await h.flush()   # dedup'd
        h.set_content("step 2"); await h.flush()   # edit
        h.set_content("step 2"); await h.flush()   # dedup'd
        await h.close()                 # finalize edit
        assert calls["send"] == 1
        # exactly 2 content-change edits + 1 finalize edit = 3 at most
        assert calls["patch"] <= 3
    finally:
        await adapter.disconnect()
