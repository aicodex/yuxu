"""`yuxu feishu inject-event` CLI — posts synthetic event to a local webhook.

We spin up a real TelegramWebhook-clone (well, FeishuWebhook) server in
the test, point `inject-event` at it, and assert the event round-trips
through to the on_update handler.
"""
from __future__ import annotations

import asyncio

import pytest

from yuxu.bundled.gateway.adapters.feishu_webhook import FeishuWebhook
from yuxu.cli.app import main as cli_main


async def _run_cli_async(argv):
    """Run CLI in a worker thread via asyncio.to_thread so the pytest event
    loop (which serves the aiohttp webhook) is free during the HTTP POST."""
    return await asyncio.to_thread(cli_main, argv)


@pytest.mark.asyncio
async def test_inject_event_text_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("YUXU_HOME", str(tmp_path / ".yuxu"))

    delivered: list[dict] = []

    async def on_event(ev):
        delivered.append(ev)

    wh = FeishuWebhook(host="127.0.0.1", port=0,
                        path="/feishu/webhook",
                        on_event=on_event)
    await wh.start()
    try:
        sock = wh._site._server.sockets[0]
        port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}{wh.path}"

        rc = await _run_cli_async([
            "feishu", "inject-event",
            "--url", url,
            "--text", "hello from cli",
            "--user-id", "ou_cli_tester",
            "--chat-id", "oc_cli",
        ])
        assert rc == 0

        for _ in range(30):
            await asyncio.sleep(0.02)
            if delivered:
                break
        assert len(delivered) == 1
        ev = delivered[0]
        assert ev["header"]["event_type"] == "im.message.receive_v1"
        msg = ev["event"]["message"]
        assert msg["chat_id"] == "oc_cli"
        import json
        assert json.loads(msg["content"])["text"] == "hello from cli"
    finally:
        await wh.stop()


@pytest.mark.asyncio
async def test_inject_event_from_file(tmp_path, monkeypatch):
    monkeypatch.setenv("YUXU_HOME", str(tmp_path / ".yuxu"))
    import json as _json

    payload = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_file"}},
            "message": {
                "message_id": "om_file_1", "message_type": "text",
                "chat_id": "oc_file", "chat_type": "p2p",
                "content": _json.dumps({"text": "from a file"}),
            },
        },
    }
    payload_path = tmp_path / "ev.json"
    payload_path.write_text(_json.dumps(payload))

    delivered = []

    async def on_event(ev):
        delivered.append(ev)

    wh = FeishuWebhook(host="127.0.0.1", port=0, on_event=on_event)
    await wh.start()
    try:
        sock = wh._site._server.sockets[0]
        port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}{wh.path}"
        rc = await _run_cli_async([
            "feishu", "inject-event",
            "--url", url,
            "--file", str(payload_path),
        ])
        assert rc == 0
        for _ in range(30):
            await asyncio.sleep(0.02)
            if delivered:
                break
        assert len(delivered) == 1
        assert (delivered[0]["event"]["sender"]["sender_id"]["open_id"]
                == "ou_file")
    finally:
        await wh.stop()


def test_inject_event_missing_input(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("YUXU_HOME", str(tmp_path / ".yuxu"))
    rc = cli_main(["feishu", "inject-event",
                    "--url", "http://127.0.0.1:1"])
    assert rc == 1
    assert "--file or --text" in capsys.readouterr().err
