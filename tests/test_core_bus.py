import asyncio

import pytest

from yuxu.core.bus import Bus, Message

pytestmark = pytest.mark.asyncio


async def test_register_and_send():
    bus = Bus()
    received = []

    async def h(msg: Message):
        received.append((msg.event, msg.payload))

    bus.register("alice", h)
    await bus.send("alice", "ping", {"n": 1})
    # send is fire-and-forget via task; yield to let it run
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert received == [("ping", {"n": 1})]


async def test_send_drops_when_no_handler(caplog):
    bus = Bus()
    await bus.send("nobody", "ping")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # no crash; nothing to assert beyond "did not raise"


async def test_request_reply():
    bus = Bus()

    async def handler(msg: Message):
        if msg.event == "request":
            return {"echo": msg.payload}

    bus.register("svc", handler)
    reply = await bus.request("svc", {"n": 42}, timeout=1.0)
    assert reply == {"echo": {"n": 42}}


async def test_request_propagates_handler_exception():
    bus = Bus()

    async def handler(msg: Message):
        raise ValueError("boom")

    bus.register("svc", handler)
    with pytest.raises(ValueError, match="boom"):
        await bus.request("svc", None, timeout=1.0)


async def test_request_missing_handler():
    bus = Bus()
    with pytest.raises(LookupError):
        await bus.request("ghost", None, timeout=1.0)


async def test_request_timeout():
    bus = Bus()

    async def handler(msg: Message):
        await asyncio.sleep(5)
        return "late"

    bus.register("slow", handler)
    with pytest.raises(asyncio.TimeoutError):
        await bus.request("slow", None, timeout=0.05)


async def test_subscribe_publish():
    bus = Bus()
    got = []

    def h(msg):
        got.append((msg["topic"], msg["payload"]))

    bus.subscribe("foo.bar", h)
    await bus.publish("foo.bar", 1)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert got == [("foo.bar", 1)]


async def test_subscribe_glob_pattern():
    bus = Bus()
    got = []
    bus.subscribe("theme_*.status", lambda m: got.append(m["topic"]))
    await bus.publish("theme_rank.status", "ready")
    await bus.publish("other.status", "ready")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert got == ["theme_rank.status"]


async def test_subscriber_exception_isolated():
    bus = Bus()
    got_b = []

    def bad(msg):
        raise RuntimeError("sub crashed")

    def good(msg):
        got_b.append(msg["topic"])

    bus.subscribe("evt", bad)
    bus.subscribe("evt", good)
    await bus.publish("evt", None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert got_b == ["evt"]


async def test_handler_exception_isolated():
    bus = Bus()

    async def bad(msg):
        raise RuntimeError("boom")

    bus.register("bad", bad)
    await bus.send("bad", "anything")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # bus keeps working
    got = []
    bus.register("good", lambda m: got.append(m.event))
    await bus.send("good", "ok")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert got == ["ok"]


async def test_publish_status_and_query():
    bus = Bus()
    await bus.publish_status("a", "loading")
    assert bus.query_status("a") == "loading"
    await bus.publish_status("a", "ready")
    assert bus.query_status("a") == "ready"


async def test_wait_for_service_success():
    bus = Bus()

    async def flip():
        await asyncio.sleep(0.01)
        await bus.publish_status("svc", "ready")

    asyncio.create_task(flip())
    await bus.wait_for_service("svc", timeout=1.0)
    assert bus.query_status("svc") == "ready"


async def test_wait_for_service_already_ready():
    bus = Bus()
    await bus.publish_status("svc", "ready")
    await bus.wait_for_service("svc", timeout=0.01)


async def test_wait_for_service_fails_on_failed_status():
    bus = Bus()

    async def flip():
        await asyncio.sleep(0.01)
        await bus.publish_status("svc", "failed")

    asyncio.create_task(flip())
    with pytest.raises(RuntimeError):
        await bus.wait_for_service("svc", timeout=1.0)


async def test_wait_for_service_timeout():
    bus = Bus()
    with pytest.raises(asyncio.TimeoutError):
        await bus.wait_for_service("never", timeout=0.05)


async def test_run_forever_and_stop():
    bus = Bus()
    task = asyncio.create_task(bus.run_forever())
    await asyncio.sleep(0.01)
    await bus.stop()
    await asyncio.wait_for(task, timeout=0.5)
