from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from yuxu.bundled.gateway.adapters.base import PlatformAdapter
from yuxu.bundled.gateway.adapters.console import ConsoleAdapter
from yuxu.bundled.gateway.adapters.telegram import (
    TelegramAdapter,
    _render_draft_telegram_html,
)
from yuxu.bundled.gateway.draft import (
    DEFAULT_THROTTLE_SECONDS,
    DraftHandle,
    DraftMessage,
    combine_draft_markdown,
)
from yuxu.bundled.gateway.handler import GatewayManager
from yuxu.bundled.gateway.session import SendResult, SessionSource

pytestmark = pytest.mark.asyncio


# -- DraftMessage / combine rendering --------------------------


def test_combine_empty_is_empty_string():
    assert combine_draft_markdown(DraftMessage()) == ""


def test_combine_quote_and_content():
    d = DraftMessage(quote_user="alice", quote_text="你好",
                     content="Hi Alice!")
    out = combine_draft_markdown(d)
    assert out.startswith("> 回复 alice: 你好")
    assert "Hi Alice!" in out


def test_combine_thinking_block():
    d = DraftMessage(thinking="stepping through...\nsecond thought",
                     content="answer")
    out = combine_draft_markdown(d)
    assert "> 💭 **Thinking**" in out
    assert "> stepping through..." in out
    assert "> second thought" in out
    # content appears after thinking
    assert out.index("answer") > out.index("💭")


def test_combine_footer_as_italic():
    d = DraftMessage(content="hello",
                     footer_meta=[("Agent", "main"), ("Context", "3.2k / 32k")])
    out = combine_draft_markdown(d)
    assert "_Agent: main | Context: 3.2k / 32k_" in out


def test_combine_full_stack_order():
    d = DraftMessage(
        quote_user="alice", quote_text="hey",
        thinking="brief thought",
        content="Hi!",
        footer_meta=[("Agent", "main")],
    )
    out = combine_draft_markdown(d)
    # Order: quote -> thinking -> content -> footer
    pos_quote = out.index("回复 alice")
    pos_think = out.index("Thinking")
    pos_content = out.index("Hi!")
    pos_footer = out.index("_Agent: main_")
    assert pos_quote < pos_think < pos_content < pos_footer


def test_combine_multiline_quote_keeps_blockquote():
    d = DraftMessage(quote_user="alice", quote_text="one\ntwo\nthree",
                     content="ok")
    out = combine_draft_markdown(d)
    # every quote line should start with '>'
    block_lines = [ln for ln in out.splitlines() if "two" in ln or "three" in ln]
    assert all(ln.startswith(">") for ln in block_lines)


# -- Telegram HTML rendering -----------------------------------


def test_telegram_html_blockquote_and_escape():
    d = DraftMessage(quote_user="bob", quote_text="<script>")
    html = _render_draft_telegram_html(d)
    assert "<blockquote>" in html
    assert "&lt;script&gt;" in html
    assert "<script>" not in html


def test_telegram_html_thinking_and_content():
    d = DraftMessage(thinking="a & b", content="just text <b>", footer_meta=[("A", "1")])
    html = _render_draft_telegram_html(d)
    assert "💭 <b>Thinking</b>" in html
    assert "<blockquote>a &amp; b</blockquote>" in html
    assert "just text &lt;b&gt;" in html
    assert "<i>A: 1</i>" in html


# -- DraftHandle lifecycle -------------------------------------


class RecordingAdapter(PlatformAdapter):
    platform = "record"
    supports_edit = True

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict]] = []

    async def connect(self) -> None: pass
    async def disconnect(self) -> None: pass

    async def send(self, source, text, *, reply_to_message_id=None) -> SendResult:
        self.calls.append(("send", {"text": text}))
        return SendResult(ok=True, message_id=f"m-{len(self.calls)}")

    async def edit(self, source, message_id, text, *, finalize=False) -> SendResult:
        self.calls.append(("edit", {"message_id": message_id, "text": text,
                                     "finalize": finalize}))
        return SendResult(ok=True, message_id=message_id)


class NoEditAdapter(RecordingAdapter):
    platform = "noedit"
    supports_edit = False


def _src(platform="record", chat="u1"):
    return SessionSource(platform=platform, chat_id=chat)


async def test_handle_open_sends_first_render_with_message_id():
    ad = RecordingAdapter()
    h = DraftHandle(adapter=ad, source=_src())
    h.set_content("hello")
    msg_id = await h.open()
    assert msg_id == "m-1"
    assert h.message_id == "m-1"
    assert len(ad.calls) == 1
    kind, data = ad.calls[0]
    assert kind == "send"
    assert "hello" in data["text"]


async def test_handle_close_always_finalizes():
    ad = RecordingAdapter()
    h = DraftHandle(adapter=ad, source=_src(), throttle_seconds=5.0)
    h.set_content("first")
    await h.open()
    h.set_content("final")
    await h.close()
    # Expect at least one send + one finalize edit
    finalize_calls = [c for c in ad.calls if c[0] == "edit" and c[1]["finalize"]]
    assert len(finalize_calls) == 1
    assert "final" in finalize_calls[0][1]["text"]


async def test_handle_throttle_collapses_rapid_updates():
    ad = RecordingAdapter()
    h = DraftHandle(adapter=ad, source=_src(), throttle_seconds=0.1)
    await h.open()
    for i in range(5):
        h.set_content(f"t{i}")
        await h.maybe_flush()
    # first flush + maybe a trailing one; should NOT have 5 edits
    await asyncio.sleep(0.2)
    await h.close()
    edit_calls = [c for c in ad.calls if c[0] == "edit"]
    # At least the close-finalize; at most close + one trailing = 2 edits.
    # (plus possibly an immediate flush inside the first maybe_flush)
    assert len(edit_calls) <= 3


async def test_handle_flush_bypasses_throttle():
    ad = RecordingAdapter()
    h = DraftHandle(adapter=ad, source=_src(), throttle_seconds=5.0)
    await h.open()                  # empty draft → no adapter call yet
    h.set_content("x"); await h.flush()    # first real render → send
    h.set_content("y"); await h.flush()    # now has message_id → edit, bypasses throttle
    sends = [c for c in ad.calls if c[0] == "send"]
    edits = [c for c in ad.calls if c[0] == "edit"]
    assert len(sends) == 1 and len(edits) == 1
    assert edits[0][1]["text"].find("y") != -1


async def test_handle_context_manager_auto_closes():
    ad = RecordingAdapter()
    async with DraftHandle(adapter=ad, source=_src(),
                           throttle_seconds=5.0) as h:
        h.set_content("hello")
    # open() with empty draft was a no-op; close's finalize is the first
    # adapter call — a send (since no message_id yet).
    assert any(c[0] == "send" and "hello" in c[1]["text"] for c in ad.calls)


async def test_handle_raises_after_close():
    ad = RecordingAdapter()
    h = DraftHandle(adapter=ad, source=_src())
    await h.open()
    await h.close()
    with pytest.raises(RuntimeError):
        h.set_content("too late")


async def test_handle_append_accumulates():
    ad = RecordingAdapter()
    h = DraftHandle(adapter=ad, source=_src(), throttle_seconds=5.0)
    await h.open()
    h.append_content("foo ")
    h.append_content("bar")
    h.append_thinking("t1 ")
    h.append_thinking("t2")
    assert h.draft.content == "foo bar"
    assert h.draft.thinking == "t1 t2"


# -- Base adapter default render_draft -------------------------


async def test_base_render_draft_no_edit_only_finalizes():
    ad = NoEditAdapter()
    h = DraftHandle(adapter=ad, source=_src(platform="noedit"),
                    throttle_seconds=5.0)
    await h.open()        # supports_edit=False ⇒ open suppresses (empty+finalize=False)
    h.set_content("mid")
    await h.flush()       # still supressed (finalize=False)
    h.set_content("final")
    await h.close()       # finalize=True ⇒ one send
    sends = [c for c in ad.calls if c[0] == "send"]
    assert len(sends) == 1
    assert "final" in sends[0][1]["text"]


# -- Console adapter render_draft ------------------------------


async def test_console_render_draft_finalize_only(capsys):
    ad = ConsoleAdapter()
    src = _src(platform="console", chat="dev")
    r = await ad.render_draft(src, DraftMessage(content="mid"),
                              message_id=None, finalize=False)
    assert r.ok and ad.outbox == []   # suppressed

    r = await ad.render_draft(
        src,
        DraftMessage(
            quote_user="alice", quote_text="hi",
            thinking="thinking about it",
            content="hello back",
            footer_meta=[("Agent", "main")],
        ),
        message_id=None, finalize=True,
    )
    assert r.ok
    assert ad.outbox[-1]["finalize"] is True
    captured = capsys.readouterr().out
    assert "回复 alice: hi" in captured
    assert "💭 Thinking" in captured
    assert "hello back" in captured
    assert "Agent: main" in captured


# -- Telegram adapter edit + render_draft ----------------------


def _tg_handler(calls):
    def route(req):
        calls.append(str(req.url))
        if req.url.path.endswith("/sendMessage"):
            return httpx.Response(200, json={"ok": True,
                                             "result": {"message_id": 101}})
        if req.url.path.endswith("/editMessageText"):
            return httpx.Response(200, json={"ok": True,
                                             "result": {"message_id": 101}})
        return httpx.Response(400, json={"ok": False})
    return route


async def test_telegram_adapter_render_draft_uses_html_edit():
    calls = []
    adapter = TelegramAdapter(
        bot_token="ABC",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_tg_handler(calls))),
    )
    adapter._owned_client = True
    src = SessionSource(platform="telegram", chat_id="42", user_id="7")

    # first render -> sendMessage
    r1 = await adapter.render_draft(
        src, DraftMessage(content="hi"),
        message_id=None, finalize=False,
    )
    assert r1.ok and r1.message_id == "101"
    assert calls[-1].endswith("/sendMessage")

    # second render -> editMessageText
    r2 = await adapter.render_draft(
        src,
        DraftMessage(thinking="thinking", content="hi there",
                     footer_meta=[("Agent", "main")]),
        message_id="101", finalize=True,
    )
    assert r2.ok
    assert calls[-1].endswith("/editMessageText")
    await adapter.disconnect()


async def test_telegram_edit_same_text_is_benign():
    def route(req):
        return httpx.Response(200, json={
            "ok": False,
            "description": "Bad Request: message is not modified",
        })
    adapter = TelegramAdapter(
        bot_token="ABC",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(route)),
    )
    adapter._owned_client = True
    src = SessionSource(platform="telegram", chat_id="42")
    r = await adapter.edit(src, "1", "same", finalize=False)
    assert r.ok is True   # "not modified" treated as success
    await adapter.disconnect()


# -- GatewayManager bus ops ------------------------------------


async def test_bus_op_open_update_close_draft():
    bus = __import__("yuxu").core.Bus()
    gm = GatewayManager(bus)
    adapter = RecordingAdapter()
    gm.register_adapter(adapter)
    await gm.start()
    try:
        class _M1:
            payload = {
                "op": "open_draft",
                "source": {"platform": "record", "chat_id": "A"},
                "quote": {"user": "alice", "text": "你好"},
                "footer_meta": [["Agent", "main"]],
                "thinking": "starting",
            }
        r = await gm.handle(_M1())
        assert r["ok"] is True
        draft_id = r["draft_id"]
        assert r["message_id"] is not None

        class _M2:
            payload = {"op": "update_draft", "draft_id": draft_id,
                       "content_append": "hello!", "flush_now": True}
        r = await gm.handle(_M2())
        assert r["ok"] is True

        class _M3:
            payload = {"op": "close_draft", "draft_id": draft_id}
        r = await gm.handle(_M3())
        assert r["ok"] is True
        # at least one finalize edit
        assert any(c[0] == "edit" and c[1]["finalize"] for c in adapter.calls)
    finally:
        await gm.stop()


async def test_python_open_draft_async_context_manager():
    bus = __import__("yuxu").core.Bus()
    gm = GatewayManager(bus)
    adapter = RecordingAdapter()
    gm.register_adapter(adapter)
    await gm.start()
    try:
        src = SessionSource(platform="record", chat_id="B")
        async with gm.open_draft(source=src,
                                 quote_user="bob", quote_text="hi",
                                 throttle_seconds=5.0,
                                 footer_meta=[("Agent", "x")]) as draft:
            draft.set_thinking("mm")
            draft.set_content("ok")
        # open + finalize edit
        assert any(c[0] == "edit" and c[1]["finalize"] for c in adapter.calls)
    finally:
        await gm.stop()


async def test_open_draft_unknown_session_raises():
    bus = __import__("yuxu").core.Bus()
    gm = GatewayManager(bus)
    with pytest.raises(KeyError):
        gm.open_draft(session_key="ghost:0:default")


async def test_bus_op_update_close_unknown_draft_ids_fail_gracefully():
    bus = __import__("yuxu").core.Bus()
    gm = GatewayManager(bus)
    class _M1: payload = {"op": "update_draft", "draft_id": "nope",
                          "content": "x"}
    r = await gm.handle(_M1())
    assert r["ok"] is False
    class _M2: payload = {"op": "close_draft", "draft_id": "nope"}
    r = await gm.handle(_M2())
    assert r["ok"] is False
