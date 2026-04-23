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
    _ensure_inner_frontmatter_defaults,
    _read_or_empty,
)
from yuxu.core.frontmatter import parse_frontmatter
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


def test_ensure_defaults_injects_missing_i6_fields():
    body = (
        "---\n"
        "name: Canary\n"
        "description: example\n"
        "type: feedback\n"
        "---\n\n"
        "body here\n"
    )
    out = _ensure_inner_frontmatter_defaults(body)
    fm, _ = parse_frontmatter(out)
    assert fm["evidence_level"] == "observed"
    assert fm["status"] == "current"
    assert isinstance(fm.get("updated"), (str, object))
    # name / description / type preserved
    assert fm["name"] == "Canary"
    assert fm["type"] == "feedback"


def test_ensure_defaults_preserves_existing_values():
    body = (
        "---\n"
        "name: Already graded\n"
        "description: example\n"
        "type: feedback\n"
        "evidence_level: consensus\n"
        "status: archived\n"
        "updated: 2020-01-01\n"
        "---\n\n"
        "body\n"
    )
    out = _ensure_inner_frontmatter_defaults(body)
    fm, _ = parse_frontmatter(out)
    assert fm["evidence_level"] == "consensus"
    assert fm["status"] == "archived"
    assert str(fm["updated"]) == "2020-01-01"


def test_ensure_defaults_passthrough_on_no_frontmatter():
    body = "plain text without frontmatter\n"
    assert _ensure_inner_frontmatter_defaults(body) == body


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

    # Draft on disk has I6 defaults injected into inner frontmatter
    draft_path = Path(r["drafts"][0]["path"])
    raw = draft_path.read_text(encoding="utf-8")
    # Outer frontmatter is the curator's staging metadata; inner begins
    # after the first '---...---' block.
    _, inner = parse_frontmatter(raw)
    inner_fm, _ = parse_frontmatter(inner)
    assert inner_fm["evidence_level"] == "observed"
    assert inner_fm["status"] == "current"
    assert "updated" in inner_fm

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


# -- auto target via performance_ranker (dogfood integration) ----


async def test_resolve_auto_hint_synthesizes_from_ranker(tmp_path):
    bus = Bus()

    async def fake_ranker(msg):
        return {"ok": True, "window_hours": 24,
                "ranked": [{"agent": "harness_pro_max", "score": 5.0,
                             "errors": 3, "rejections": 1}]}

    bus.register("performance_ranker", fake_ranker)
    ctx = _make_ctx(tmp_path, bus)
    curator = MemoryCurator(ctx)
    hint, top = await curator._resolve_auto_hint()
    assert top["agent"] == "harness_pro_max"
    assert "harness_pro_max" in hint
    assert "3 error" in hint
    assert "1 rejection" in hint


async def test_resolve_auto_hint_ranker_missing(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    curator = MemoryCurator(ctx)
    hint, top = await curator._resolve_auto_hint()
    assert hint is None and top is None


async def test_resolve_auto_hint_empty_rank(tmp_path):
    bus = Bus()

    async def fake_ranker(msg):
        return {"ok": True, "window_hours": 24, "ranked": []}

    bus.register("performance_ranker", fake_ranker)
    ctx = _make_ctx(tmp_path, bus)
    curator = MemoryCurator(ctx)
    hint, _ = await curator._resolve_auto_hint()
    assert hint is None


async def test_curate_auto_merges_ranker_hint_with_user_hint(tmp_path):
    """User's context_hint comes first; ranker's auto-hint appends after."""
    bus = Bus()

    async def fake_ranker(msg):
        return {"ok": True, "window_hours": 24,
                "ranked": [{"agent": "flaky_bot", "score": 4.0,
                             "errors": 4, "rejections": 0}]}

    bus.register("performance_ranker", fake_ranker)
    seen_llm = _register_llm(bus, _llm_payload())

    ctx = _make_ctx(tmp_path, bus)
    curator = MemoryCurator(ctx)
    await curator.curate(transcript=_long_transcript(),
                         context_hint="my own hint",
                         auto=True)
    assert len(seen_llm) == 1
    sys_prompt = seen_llm[0]["system_prompt"]
    # Both user hint and ranker hint should appear
    assert "my own hint" in sys_prompt
    assert "flaky_bot" in sys_prompt
    # User hint comes first
    assert sys_prompt.index("my own hint") < sys_prompt.index("flaky_bot")


async def test_curate_auto_records_auto_target(tmp_path):
    bus = Bus()

    async def fake_ranker(msg):
        return {"ok": True, "window_hours": 24,
                "ranked": [{"agent": "worst_bot", "score": 8.0,
                             "errors": 2, "rejections": 3}]}

    bus.register("performance_ranker", fake_ranker)
    _register_llm(bus, _llm_payload())
    ctx = _make_ctx(tmp_path, bus)
    curator = MemoryCurator(ctx)
    r = await curator.curate(transcript=_long_transcript(), auto=True)
    assert r["ok"] is True
    assert r["auto_target"]["agent"] == "worst_bot"


async def test_curate_auto_warns_but_proceeds_when_ranker_missing(tmp_path):
    """Soft failure: missing ranker records a warning, still curates."""
    bus = Bus()  # no ranker
    _register_llm(bus, _llm_payload())
    ctx = _make_ctx(tmp_path, bus)
    curator = MemoryCurator(ctx)
    r = await curator.curate(transcript=_long_transcript(), auto=True)
    assert r["ok"] is True  # proceeded despite auto unavailable
    assert "auto_target" not in r
    assert any("performance_ranker unavailable" in w
               for w in r["warnings"])


async def test_handle_auto_true_forwards_to_curate(tmp_path):
    bus = Bus()

    async def fake_ranker(msg):
        return {"ok": True, "window_hours": 24,
                "ranked": [{"agent": "x_bot", "score": 1.0,
                             "errors": 1, "rejections": 0}]}

    bus.register("performance_ranker", fake_ranker)
    _register_llm(bus, _llm_payload())
    ctx = _make_ctx(tmp_path, bus)
    curator = MemoryCurator(ctx)
    r = await curator.handle(_Msg({
        "op": "curate", "auto": True,
        "transcript": _long_transcript(),
    }))
    assert r["ok"] is True
    assert r["auto_target"]["agent"] == "x_bot"


async def test_slash_auto_keyword_triggers_auto_mode(tmp_path):
    bus = Bus()
    ranker_called = [False]

    async def fake_ranker(msg):
        ranker_called[0] = True
        return {"ok": True, "window_hours": 24,
                "ranked": [{"agent": "b", "score": 1.0,
                             "errors": 1, "rejections": 0}]}

    bus.register("performance_ranker", fake_ranker)
    _register_llm(bus, _llm_payload())

    async def fake_gateway(msg):
        return {"ok": True}

    bus.register("gateway", fake_gateway)
    ctx = _make_ctx(tmp_path, bus)
    # Need a session dir so default source loader has something to find
    (tmp_path / "proj" / "data" / "sessions").mkdir(parents=True)
    (tmp_path / "proj" / "data" / "sessions" / "s.md").write_text(
        _long_transcript()
    )

    curator = MemoryCurator(ctx)
    await curator._on_command({
        "topic": "gateway.command_invoked",
        "payload": {"command": COMMAND, "args": "auto",
                    "session_key": "console:test"},
    })
    await asyncio.sleep(0.02)
    assert ranker_called[0] is True


async def test_slash_explicit_hint_does_not_query_ranker(tmp_path):
    bus = Bus()
    ranker_called = [False]

    async def fake_ranker(msg):
        ranker_called[0] = True
        return {"ok": True, "window_hours": 24, "ranked": []}

    bus.register("performance_ranker", fake_ranker)
    _register_llm(bus, _llm_payload())

    async def fake_gateway(msg):
        return {"ok": True}

    bus.register("gateway", fake_gateway)
    ctx = _make_ctx(tmp_path, bus)
    (tmp_path / "proj" / "data" / "sessions").mkdir(parents=True)
    (tmp_path / "proj" / "data" / "sessions" / "s.md").write_text(
        _long_transcript()
    )

    curator = MemoryCurator(ctx)
    await curator._on_command({
        "topic": "gateway.command_invoked",
        "payload": {"command": COMMAND, "args": "focus on the CLI",
                    "session_key": "console:test"},
    })
    await asyncio.sleep(0.02)
    assert ranker_called[0] is False


# -- JSONL transcript rendering at session.ended -------------------


def _write_jsonl_transcript(path: Path, *, with_reasoning: bool = True,
                             filler_lines: int = 25) -> None:
    """Create a synthetic session JSONL long enough to pass MIN_SOURCE_CHARS."""
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = [
        {"ts": 1714000000.0, "event": "lifecycle", "state": "ready"},
        {"ts": 1714000001.0, "event": "message", "role": "user",
         "content": "the user asked a non-trivial question about how yuxu "
                    "handles transcript persistence across agent restarts"},
    ]
    if with_reasoning:
        entries.append({"ts": 1714000002.0, "event": "message",
                         "role": "assistant", "kind": "reasoning",
                         "content": "let me think about this carefully: "
                                    "transcripts are per-agent, append-only, "
                                    "lifecycle lines separate runs",
                         "iteration": 1})
    entries.append({"ts": 1714000003.0, "event": "message",
                     "role": "assistant",
                     "content": "Transcripts persist across restarts because "
                                "the JSONL is append-only and lifecycle "
                                "lines mark run boundaries",
                     "iteration": 1})
    # pad with filler so we comfortably clear MIN_SOURCE_CHARS
    for i in range(filler_lines):
        entries.append({"ts": 1714000010.0 + i, "event": "message",
                         "role": "user", "content":
                            f"follow-up question {i} about transcript "
                            "persistence in long-running agents"})
    entries.append({"ts": 1714000099.0, "event": "lifecycle",
                     "state": "stopped", "reason": "normal"})
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n",
                    encoding="utf-8")


async def test_session_ended_jsonl_path_is_rendered_not_raw(tmp_path):
    """When session.ended carries a .jsonl transcript_path, curator must
    feed the LLM the formatted readable text, not raw JSONL lines."""
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    seen = _register_llm(bus, _llm_payload(improvements=["learned something"],
                                             edits=[]))
    _register_aq(bus)

    jsonl_path = tmp_path / "proj" / "data" / "sessions" / "alpha.jsonl"
    _write_jsonl_transcript(jsonl_path)

    curator = MemoryCurator(ctx)
    await curator.install()
    got: list[dict] = []
    bus.subscribe("memory_curator.curated",
                  lambda e: got.append(e.get("payload") or {}))

    await bus.publish(SESSION_ENDED_TOPIC, {
        "agent": "alpha", "state": "stopped", "reason": "normal",
        "transcript_path": str(jsonl_path),
    })
    for _ in range(30):
        await asyncio.sleep(0)
        if got:
            break
    assert got, "curator should have fired"

    # The LLM received a user message with transcript text — NOT raw JSONL
    assert seen, "curator must have called llm_driver"
    payload = seen[0]
    user_msg = payload["messages"][-1]["content"]
    # Readable rendering markers from format_jsonl_transcript
    assert "lifecycle: ready" in user_msg
    assert "USER" in user_msg
    assert "ASSISTANT" in user_msg
    assert "ASSISTANT reasoning" in user_msg
    # Raw JSONL would have leaked these braces everywhere; tolerate some
    # escaping but verify the per-line JSON shape is gone
    assert '"event":' not in user_msg, (
        "transcript must be rendered, not fed as raw JSON lines"
    )


async def test_session_ended_jsonl_missing_file_falls_through(tmp_path):
    """Race: session.ended arrives before the transcript write lands,
    or path points somewhere non-existent. Curator must not crash."""
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    _register_llm(bus, _llm_payload())

    curator = MemoryCurator(ctx)
    await curator.install()
    skipped: list[dict] = []
    bus.subscribe("memory_curator.skipped",
                  lambda e: skipped.append(e.get("payload") or {}))

    await bus.publish(SESSION_ENDED_TOPIC, {
        "agent": "ghost", "state": "stopped",
        "transcript_path": str(tmp_path / "nonexistent.jsonl"),
    })
    for _ in range(20):
        await asyncio.sleep(0)
        if skipped:
            break
    # With no readable transcript, curator should have published skipped
    # (either "no readable sources" or "transcript too short") — never raise
    assert skipped
