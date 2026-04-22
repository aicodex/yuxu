"""MemoryCurator — single-pass Hermes-style extraction + append-log + proposals."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuxu.bundled.memory_curator.handler import (
    COMMAND,
    MAX_IMPROVEMENTS,
    MAX_LOG_BYTES,
    MAX_MEMORY_EDITS,
    MIN_SOURCE_CHARS,
    MemoryCurator,
    SESSION_ENDED_TOPIC,
    _append_improvements,
    _read_or_empty,
)
from yuxu.core.bus import Bus

pytestmark = pytest.mark.asyncio


# -- pure helpers ----------------------------------------------


def test_append_improvements_dedup_and_new(tmp_path):
    lp = tmp_path / "log.md"
    a, d = _append_improvements(lp, ["first insight", "second insight"])
    assert a == 2 and d == 0
    # re-appending same should dedup
    a2, d2 = _append_improvements(lp, ["first insight", "third"])
    assert a2 == 1 and d2 == 1
    text = lp.read_text(encoding="utf-8")
    assert "first insight" in text
    assert "third" in text
    # appears once even after double append
    assert text.count("first insight") == 1


def test_append_improvements_skips_blank(tmp_path):
    lp = tmp_path / "log.md"
    a, d = _append_improvements(lp, ["", "   ", "real one"])
    assert a == 1


def test_append_improvements_roll_trim(tmp_path):
    lp = tmp_path / "log.md"
    # Fill past the hard cap
    for i in range(200):
        _append_improvements(lp, [f"insight number {i} with enough body to matter"],
                              max_bytes=2048)
    text = lp.read_text(encoding="utf-8")
    assert len(text.encode("utf-8")) <= 2048 + 500   # trim slack
    # latest entries should still be there
    assert "199" in text


# -- fixtures --------------------------------------------------


def _make_ctx(tmp_path: Path, bus: Bus) -> SimpleNamespace:
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / "yuxu.json").write_text("{}")
    agent_dir = project_root / "_system" / "memory_curator"
    agent_dir.mkdir(parents=True)
    return SimpleNamespace(bus=bus, agent_dir=agent_dir,
                            name="memory_curator", loader=None)


def _register_llm(bus: Bus, response_content: str, ok: bool = True):
    seen: list[dict] = []

    async def handler(msg):
        p = dict(msg.payload) if isinstance(msg.payload, dict) else {}
        seen.append(p)
        if not ok:
            return {"ok": False, "error": "simulated"}
        return {"ok": True, "content": response_content,
                "stop_reason": "complete", "usage": {}}

    bus.register("llm_driver", handler)
    return seen


def _register_aq(bus: Bus):
    calls: list[dict] = []

    async def handler(msg):
        p = dict(msg.payload) if isinstance(msg.payload, dict) else {}
        if p.get("op") == "enqueue":
            calls.append(p)
            return {"ok": True, "approval_id": f"AP-{len(calls)}"}
        return {"ok": False, "error": "unexpected op"}

    bus.register("approval_queue", handler)
    return calls


def _long_transcript(n_lines: int = 40) -> str:
    lines = [f"line {i}: user said something useful about yuxu testing strategy"
             for i in range(n_lines)]
    return "\n".join(lines)


def _llm_payload(improvements=None, edits=None, summary="s") -> str:
    return json.dumps({
        "improvements": improvements if improvements is not None else [
            "prefer terse replies", "mock network in unit tests",
        ],
        "memory_edits": edits if edits is not None else [],
        "summary": summary,
    })


# -- happy path: both lists populated ---------------------------


async def test_curate_happy_path_writes_log_and_drafts(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    _register_llm(bus, _llm_payload(
        improvements=["user prefers terse replies",
                       "always mock network in unit tests"],
        edits=[{
            "action": "add",
            "target": "feedback_testing.md",
            "title": "testing discipline",
            "memory_type": "feedback",
            "body": "---\nname: feedback_testing\n---\nbody",
            "rationale": "user said so in session",
        }],
        summary="captured two lessons",
    ))
    aq_calls = _register_aq(bus)

    curator = MemoryCurator(ctx)
    r = await curator.curate(transcript=_long_transcript())

    assert r["ok"] is True
    assert r["log_entries"] == 2
    assert len(r["drafts"]) == 1
    assert r["approval_ids"] == ["AP-1"]
    assert aq_calls[0]["action"] == "memory_edit"

    # improvement_log.md exists and contains both entries
    log = Path(r["memory_root"]) / "_improvement_log.md"
    text = log.read_text(encoding="utf-8")
    assert "user prefers terse replies" in text
    assert "always mock network" in text

    # draft file exists with curator_ prefix
    draft = Path(r["drafts"][0]["path"])
    assert draft.name.startswith("curator_")
    dtext = draft.read_text(encoding="utf-8")
    assert "status: \"draft\"" in dtext
    assert "source: \"memory_curator\"" in dtext


# -- floor skip --------------------------------------------------


async def test_short_transcript_is_skipped_early(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    llm_seen = _register_llm(bus, _llm_payload())

    curator = MemoryCurator(ctx)
    r = await curator.curate(transcript="too short")

    assert r["ok"] is False
    assert "too short" in r["reason"]
    assert llm_seen == []   # didn't call LLM


async def test_empty_sources_is_skipped(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    _register_llm(bus, _llm_payload())
    curator = MemoryCurator(ctx)
    r = await curator.curate(sources=[])
    assert r["ok"] is False
    assert "no readable sources" in r["reason"]


# -- LLM output handling ----------------------------------------


async def test_llm_failure_returned(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    _register_llm(bus, _llm_payload(), ok=False)
    curator = MemoryCurator(ctx)
    r = await curator.curate(transcript=_long_transcript())
    assert r["ok"] is False
    assert "simulated" in r["error"]


async def test_llm_garbage_json_returned_as_error(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    _register_llm(bus, "i'm sorry I can't JSON today")
    curator = MemoryCurator(ctx)
    r = await curator.curate(transcript=_long_transcript())
    assert r["ok"] is False
    assert "no JSON" in r["error"]


async def test_caps_improvements_and_edits(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    _register_llm(bus, _llm_payload(
        improvements=[f"improvement {i}" for i in range(20)],
        edits=[{
            "action": "add", "target": f"t_{i}.md", "title": f"t{i}",
            "memory_type": "feedback", "body": f"---\nname: t{i}\n---\nb{i}",
        } for i in range(20)],
    ))
    _register_aq(bus)
    curator = MemoryCurator(ctx)
    r = await curator.curate(transcript=_long_transcript())

    assert r["log_entries"] == MAX_IMPROVEMENTS
    assert len(r["drafts"]) == MAX_MEMORY_EDITS


async def test_malformed_edit_filtered(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    _register_llm(bus, _llm_payload(
        improvements=[],
        edits=[
            {"action": "delete", "target": "a", "body": "x"},  # bad action
            {"action": "add", "target": "", "body": "x"},      # empty target
            {"action": "add", "target": "b.md"},               # no body
            {"action": "add", "target": "good.md",
             "title": "g", "memory_type": "feedback", "body": "---\n---\nb"},
        ],
    ))
    _register_aq(bus)
    curator = MemoryCurator(ctx)
    r = await curator.curate(transcript=_long_transcript())
    assert len(r["drafts"]) == 1
    assert r["drafts"][0]["target"] == "good.md"


# -- dedup within single run + across runs ---------------------


async def test_dedup_within_run_by_body_hash(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    same_body = "---\nname: x\n---\nsame body"
    _register_llm(bus, _llm_payload(
        improvements=[],
        edits=[
            {"action": "add", "target": "a.md", "title": "A",
             "memory_type": "feedback", "body": same_body},
            {"action": "add", "target": "b.md", "title": "B",
             "memory_type": "feedback", "body": same_body},
        ],
    ))
    _register_aq(bus)
    curator = MemoryCurator(ctx)
    r = await curator.curate(transcript=_long_transcript())
    assert len(r["drafts"]) == 1


async def test_dedup_log_across_runs(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    _register_llm(bus, _llm_payload(
        improvements=["repeat this once"],
        edits=[],
    ))
    curator = MemoryCurator(ctx)
    r1 = await curator.curate(transcript=_long_transcript())
    assert r1["log_entries"] == 1
    r2 = await curator.curate(transcript=_long_transcript())
    assert r2["log_entries"] == 0
    assert r2["log_dupes_dropped"] == 1


# -- approval_queue optional ------------------------------------


async def test_runs_without_approval_queue(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    _register_llm(bus, _llm_payload(
        improvements=["x"],
        edits=[{"action": "add", "target": "t.md", "title": "t",
                "memory_type": "feedback",
                "body": "---\nname: t\n---\nb"}],
    ))
    # no AQ registered
    curator = MemoryCurator(ctx)
    r = await curator.curate(transcript=_long_transcript())
    assert r["ok"] is True
    assert r["approval_ids"] == []
    assert len(r["drafts"]) == 1  # still staged on disk


# -- event + command triggers -----------------------------------


async def test_session_ended_event_triggers_curate(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    _register_llm(bus, _llm_payload(improvements=["from event"], edits=[]))
    _register_aq(bus)

    curator = MemoryCurator(ctx)
    await curator.install()

    got_curated: list[dict] = []

    async def sub(e):
        p = e.get("payload") if isinstance(e, dict) else None
        if isinstance(p, dict):
            got_curated.append(p)

    bus.subscribe("memory_curator.curated", sub)

    await bus.publish(SESSION_ENDED_TOPIC, {
        "session_key": "s1", "transcript": _long_transcript(),
    })
    for _ in range(30):
        await asyncio.sleep(0)
        if got_curated:
            break

    assert got_curated and got_curated[0]["log_entries"] == 1


async def test_session_ended_short_transcript_publishes_skipped(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    _register_llm(bus, _llm_payload())

    curator = MemoryCurator(ctx)
    await curator.install()

    got: list[dict] = []

    async def sub(e):
        p = e.get("payload") if isinstance(e, dict) else None
        if isinstance(p, dict):
            got.append(p)

    bus.subscribe("memory_curator.skipped", sub)

    await bus.publish(SESSION_ENDED_TOPIC, {"transcript": "tiny"})
    for _ in range(10):
        await asyncio.sleep(0)
        if got:
            break
    assert got


async def test_slash_command_triggers_draft_path(tmp_path):
    """Happy draft path: gateway returns a draft_id → curator opens + closes,
    no plain `op:send` involved."""
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    _register_llm(bus, _llm_payload(improvements=["from slash"], edits=[]))

    ops_seen: list[dict] = []

    async def fake_gateway(msg):
        p = dict(msg.payload) if isinstance(msg.payload, dict) else {}
        ops_seen.append(p)
        if p.get("op") == "open_draft":
            return {"ok": True, "draft_id": "D-c1", "message_id": "M-c1"}
        return {"ok": True}

    bus.register("gateway", fake_gateway)
    curator = MemoryCurator(ctx)
    await curator.install()

    await bus.publish("gateway.command_invoked", {
        "command": COMMAND, "args": "debug week wrap-up",
        "session_key": "k1",
    })
    for _ in range(40):
        await asyncio.sleep(0)
        if any(p.get("op") == "close_draft" for p in ops_seen):
            break

    opens = [p for p in ops_seen if p.get("op") == "open_draft"]
    closes = [p for p in ops_seen if p.get("op") == "close_draft"]
    sends = [p for p in ops_seen if p.get("op") == "send"]
    assert opens and closes
    assert sends == []   # draft happy path: no plain send
    # `/curate` without sources → curate() skipped; content should reflect
    assert "skipped" in opens[0]["content"]
    # Quote carries the user's original invocation
    assert opens[0]["quote"].get("text", "").startswith("/curate")


async def test_slash_command_fallback_to_send_when_draft_fails(tmp_path):
    """If gateway's open_draft returns {ok:False}, curator falls back to
    plain `op:send` with the footer inlined as italic markdown."""
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    _register_llm(bus, _llm_payload(improvements=["from slash"], edits=[]))

    sends: list[dict] = []

    async def fake_gateway(msg):
        p = dict(msg.payload) if isinstance(msg.payload, dict) else {}
        if p.get("op") == "open_draft":
            return {"ok": False, "error": "unknown session"}
        if p.get("op") == "send":
            sends.append(p)
        return {"ok": True}

    bus.register("gateway", fake_gateway)
    curator = MemoryCurator(ctx)
    await curator.install()

    await bus.publish("gateway.command_invoked", {
        "command": COMMAND, "args": "x", "session_key": "k1",
    })
    for _ in range(20):
        await asyncio.sleep(0)
        if sends:
            break
    assert sends
    assert "skipped" in sends[0]["text"]


# -- handle() surface ------------------------------------------


class _Msg:
    def __init__(self, payload):
        self.payload = payload


async def test_handle_curate_op(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    _register_llm(bus, _llm_payload(improvements=["y"], edits=[]))
    curator = MemoryCurator(ctx)
    r = await curator.handle(_Msg({
        "op": "curate", "transcript": _long_transcript(),
    }))
    assert r["ok"] is True


async def test_handle_status_op_returns_counts(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    _register_llm(bus, _llm_payload(improvements=["a", "b"], edits=[]))
    curator = MemoryCurator(ctx)
    await curator.curate(transcript=_long_transcript())
    s = await curator.handle(_Msg({"op": "status"}))
    assert s["ok"] is True
    assert s["improvements_total"] == 2
    assert s["log_bytes"] > 0


async def test_handle_unknown_op(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    curator = MemoryCurator(ctx)
    r = await curator.handle(_Msg({"op": "weird"}))
    assert r["ok"] is False
    assert "unknown op" in r["error"]
