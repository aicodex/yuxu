"""ReflectionAgent — multi-hypothesis exploration + memory edit proposals."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuxu.bundled.reflection_agent.handler import (
    COMMAND,
    FRAMINGS,
    ReflectionAgent,
    _content_hash,
    _extract_json,
    _format_sources,
    _load_sources,
    _slugify,
    _truncate_bytes,
)
from yuxu.core.bus import Bus

pytestmark = pytest.mark.asyncio


# -- pure helpers (sync) -----------------------------------------


def test_truncate_bytes_passthrough():
    assert _truncate_bytes("hi", 100) == "hi"


def test_truncate_bytes_clips_with_marker():
    out = _truncate_bytes("a" * 100, 20)
    assert out.startswith("a" * 20)
    assert "[...truncated]" in out


def test_extract_json_with_prose():
    assert _extract_json("noise {\"x\": 1} more noise") == {"x": 1}


def test_content_hash_stable():
    assert _content_hash("foo") == _content_hash("foo")
    assert _content_hash("foo") != _content_hash("bar")


def test_slugify():
    assert _slugify("Hello World!") == "hello_world"
    assert _slugify("") == "edit"
    assert len(_slugify("a" * 100)) <= 30


def test_load_sources_reads_explicit_paths(tmp_path):
    a = tmp_path / "a.md"
    a.write_text("alpha")
    b = tmp_path / "b.md"
    b.write_text("beta")
    out, warns = _load_sources([str(a), str(b)], default_root=tmp_path / "nope")
    assert {s["path"] for s in out} == {str(a), str(b)}
    assert warns == []


def test_load_sources_default_root_missing_warns(tmp_path):
    out, warns = _load_sources(None, default_root=tmp_path / "nope")
    assert out == []
    assert any("does not exist" in w for w in warns)


def test_load_sources_recurses_glob(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.md").write_text("deep")
    out, _ = _load_sources([str(tmp_path / "**" / "*.md")],
                           default_root=tmp_path)
    assert any(s["path"].endswith("deep.md") for s in out)


def test_format_sources_separates_with_rule():
    out = _format_sources([
        {"path": "/a", "text": "x"},
        {"path": "/b", "text": "y"},
    ])
    assert "Source: /a" in out and "Source: /b" in out
    assert "---" in out


# -- ctx + fakes -------------------------------------------------


def _make_ctx(tmp_path: Path, bus: Bus) -> SimpleNamespace:
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / "yuxu.json").write_text("{}")
    agent_dir = project_root / "_system" / "reflection_agent"
    agent_dir.mkdir(parents=True)
    return SimpleNamespace(bus=bus, agent_dir=agent_dir,
                           name="reflection_agent", loader=None)


def _wire_llm_driver(bus: Bus, *, extractor_payloads: list[dict] | None = None,
                     ranker_payload: dict | None = None,
                     extractor_ok: bool = True,
                     ranker_ok: bool = True):
    """Switches by system_prompt content. Returns the captured payload list."""
    seen: list[dict] = []
    extractor_iter = iter(extractor_payloads or [])

    async def handler(msg):
        payload = dict(msg.payload) if isinstance(msg.payload, dict) else {}
        seen.append(payload)
        sys = payload.get("system_prompt", "")
        if "reflection assistant" in sys:
            if not extractor_ok:
                return {"ok": False, "error": "extractor_simulated_failure"}
            try:
                body = next(extractor_iter)
            except StopIteration:
                body = {"edits": [], "summary": "no more canned"}
            return {"ok": True, "content": json.dumps(body),
                    "stop_reason": "complete", "usage": {}}
        if "memory-edit reviewer" in sys:
            if not ranker_ok:
                return {"ok": False, "error": "ranker_simulated_failure"}
            return {"ok": True, "content": json.dumps(ranker_payload or {"chosen": []}),
                    "stop_reason": "complete", "usage": {}}
        return {"ok": False, "error": f"unexpected system_prompt: {sys[:60]}"}

    bus.register("llm_driver", handler)
    return seen


def _basic_edit(target: str = "feedback_test.md",
                body: str | None = None) -> dict:
    return {
        "action": "add",
        "target": target,
        "title": "Test insight",
        "memory_type": "feedback",
        "body": body or ("---\nname: Test\ndescription: x\ntype: feedback\n"
                          "---\n## Rule\n\nbe brief"),
        "rationale": "noted in transcript",
    }


# -- happy path --------------------------------------------------


async def test_reflect_happy_path_writes_drafts_and_enqueues_approvals(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    src = tmp_path / "session1.md"
    src.write_text("user: I prefer terse replies\nassistant: ok")

    extractor_payloads = [
        {"edits": [_basic_edit("feedback_terse.md",
                                body="---\nname: A\n---\nbody A")],
         "summary": "pattern"},
        {"edits": [_basic_edit("feedback_anti.md",
                                body="---\nname: B\n---\nbody B")],
         "summary": "anti"},
        {"edits": [_basic_edit("project_synth.md",
                                body="---\nname: C\n---\nbody C")],
         "summary": "synth"},
    ]
    ranker_payload = {"chosen": [
        {"framing_id": "pattern_extractor", "edit_index": 0, "score": 0.9,
         "reason": "highest signal"},
        {"framing_id": "synthesizer", "edit_index": 0, "score": 0.7,
         "reason": "complementary"},
    ], "rejected_summary": "anti was redundant"}
    _wire_llm_driver(bus, extractor_payloads=extractor_payloads,
                     ranker_payload=ranker_payload)

    approval_calls = []

    async def fake_aq(msg):
        approval_calls.append(dict(msg.payload))
        return {"ok": True, "approval_id": f"AP-{len(approval_calls)}"}

    bus.register("approval_queue", fake_aq)

    a = ReflectionAgent(ctx)
    r = await a.reflect(need="terse style", sources=[str(src)])

    assert r["ok"] is True
    assert len(r["drafts"]) == 2
    assert r["approval_ids"] == ["AP-1", "AP-2"]
    assert r["n_sources"] == 1
    drafts_dir = Path(r["memory_root"]) / "_drafts"
    on_disk = sorted(drafts_dir.glob("reflection_*.md"))
    assert len(on_disk) == 2
    text = on_disk[0].read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "status: \"draft\"" in text
    assert "reflection_run_id" in text


async def test_reflect_empty_sources_short_circuits(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    a = ReflectionAgent(ctx)
    r = await a.reflect(need="x")
    assert r["ok"] is False
    assert r["stage"] == "load_sources"


async def test_reflect_no_edits_from_any_hypothesis(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    src = tmp_path / "s.md"; src.write_text("ignore")
    _wire_llm_driver(bus, extractor_payloads=[
        {"edits": [], "summary": "nada"},
        {"edits": [], "summary": "nada"},
        {"edits": [], "summary": "nada"},
    ], ranker_payload=None)
    a = ReflectionAgent(ctx)
    r = await a.reflect(need="x", sources=[str(src)])
    assert r["ok"] is False
    assert r["stage"] == "hypothesize"


async def test_reflect_extractor_failure_per_hypothesis_recorded_as_warning(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    src = tmp_path / "s.md"; src.write_text("x")
    # All extractor calls hit the sim failure
    _wire_llm_driver(bus, extractor_ok=False,
                     ranker_payload={"chosen": []})
    a = ReflectionAgent(ctx)
    r = await a.reflect(need="x", sources=[str(src)])
    # All hypotheses ok=False, so no edits → fails at hypothesize stage
    assert r["ok"] is False
    assert r["stage"] == "hypothesize"
    assert len(r["hypotheses"]) == 3
    assert all(h.get("ok") is False for h in r["hypotheses"])


async def test_reflect_ranker_failure_falls_back_to_all_edits(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    src = tmp_path / "s.md"; src.write_text("x")
    _wire_llm_driver(bus,
                     extractor_payloads=[
                         {"edits": [_basic_edit("a.md",
                                                body="---\nname: A\n---\nA")],
                          "summary": "."},
                         {"edits": [_basic_edit("b.md",
                                                body="---\nname: B\n---\nB")],
                          "summary": "."},
                         {"edits": [_basic_edit("c.md",
                                                body="---\nname: C\n---\nC")],
                          "summary": "."},
                     ],
                     ranker_ok=False)
    a = ReflectionAgent(ctx)
    r = await a.reflect(need="x", sources=[str(src)])
    assert r["ok"] is True
    # ranker fallback → all 3 unique edits drafted (deduped is unique here)
    assert len(r["drafts"]) == 3
    assert any("ranker failed" in w for w in r["warnings"])


async def test_reflect_dedups_identical_bodies_across_hypotheses(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    src = tmp_path / "s.md"; src.write_text("x")
    same_body = "---\nname: X\ndescription: same\ntype: feedback\n---\nidentical"
    _wire_llm_driver(bus,
                     extractor_payloads=[
                         {"edits": [_basic_edit("a.md", body=same_body)], "summary": "."},
                         {"edits": [_basic_edit("b.md", body=same_body)], "summary": "."},
                         {"edits": [_basic_edit("c.md", body=same_body)], "summary": "."},
                     ],
                     ranker_payload={"chosen": [
                         {"framing_id": "pattern_extractor", "edit_index": 0,
                          "score": 0.5, "reason": "."},
                         {"framing_id": "anti_pattern_spotter", "edit_index": 0,
                          "score": 0.5, "reason": "."},
                         {"framing_id": "synthesizer", "edit_index": 0,
                          "score": 0.5, "reason": "."},
                     ]})
    a = ReflectionAgent(ctx)
    r = await a.reflect(need="x", sources=[str(src)])
    assert r["ok"] is True
    assert len(r["drafts"]) == 1   # deduped


async def test_reflect_runs_without_approval_queue(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    src = tmp_path / "s.md"; src.write_text("x")
    _wire_llm_driver(bus,
                     extractor_payloads=[
                         {"edits": [_basic_edit()], "summary": "."},
                         {"edits": [], "summary": "."},
                         {"edits": [], "summary": "."},
                     ],
                     ranker_payload={"chosen": [
                         {"framing_id": "pattern_extractor", "edit_index": 0,
                          "score": 0.8, "reason": "."}]})
    a = ReflectionAgent(ctx)
    r = await a.reflect(need="x", sources=[str(src)])
    assert r["ok"] is True
    assert r["approval_ids"] == []   # no AQ available
    assert len(r["drafts"]) == 1


async def test_reflect_drops_malformed_edits(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    src = tmp_path / "s.md"; src.write_text("x")
    bad_payloads = [
        # missing target, action invalid, missing body, plus one good
        {"edits": [
            {"action": "delete", "target": "x", "body": "y"},
            {"action": "add", "target": "", "body": "y"},
            {"action": "add", "target": "x"},
            _basic_edit("good.md"),
        ], "summary": "."},
        {"edits": [], "summary": "."},
        {"edits": [], "summary": "."},
    ]
    _wire_llm_driver(bus, extractor_payloads=bad_payloads,
                     ranker_payload={"chosen": [
                         {"framing_id": "pattern_extractor", "edit_index": 0,
                          "score": 0.5, "reason": "."}]})
    a = ReflectionAgent(ctx)
    r = await a.reflect(need="x", sources=[str(src)])
    assert r["ok"] is True
    # Cleaned set has 1; index 0 of CLEANED list corresponds to good.md
    assert r["drafts"][0]["target"] == "good.md"


# -- handle() bus surface ---------------------------------------


class _Msg:
    def __init__(self, payload):
        self.payload = payload


async def test_handle_reflect_op(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    src = tmp_path / "s.md"; src.write_text("x")
    _wire_llm_driver(bus,
                     extractor_payloads=[
                         {"edits": [_basic_edit()], "summary": "."},
                         {"edits": [], "summary": "."},
                         {"edits": [], "summary": "."},
                     ],
                     ranker_payload={"chosen": [
                         {"framing_id": "pattern_extractor", "edit_index": 0,
                          "score": 0.5, "reason": "."}]})
    a = ReflectionAgent(ctx)
    r = await a.handle(_Msg({"op": "reflect", "need": "x",
                              "sources": [str(src)], "n_hypotheses": 3}))
    assert r["ok"] is True


async def test_handle_unknown_op(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    a = ReflectionAgent(ctx)
    r = await a.handle(_Msg({"op": "nope"}))
    assert r["ok"] is False
    assert "unknown op" in r["error"]


async def test_handle_missing_need(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    a = ReflectionAgent(ctx)
    r = await a.handle(_Msg({"op": "reflect"}))
    assert r["ok"] is False
    assert "need" in r["error"]


# -- slash command integration ----------------------------------


async def test_format_reply_produces_content_and_stats_footer(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    src = tmp_path / "s.md"; src.write_text("x")
    _wire_llm_driver(bus,
                     extractor_payloads=[
                         {"edits": [_basic_edit()], "summary": "."},
                         {"edits": [], "summary": "."},
                         {"edits": [], "summary": "."},
                     ],
                     ranker_payload={"chosen": [
                         {"framing_id": "pattern_extractor", "edit_index": 0,
                          "score": 0.5, "reason": "."}]})
    a = ReflectionAgent(ctx)
    result = await a.reflect(need="terse style", sources=[str(src)])
    content, footer = a._format_reply_parts(result)
    assert content.startswith("✅") and "draft(s) staged" in content
    # footer has structured metrics
    fkeys = {k for k, _ in footer}
    assert "sources" in fkeys
    assert "drafts" in fkeys
    assert "approvals" in fkeys
    # string form inlines footer as italic
    text = a._format_reply(result)
    assert text.endswith("_")
    assert "sources: 1" in text or "sources: " in text


async def test_slash_command_uses_draft_path_with_footer_meta(tmp_path):
    """`/reflect <need>` must call gateway `open_draft` with content +
    footer_meta (not plain `op:send`), then `close_draft`."""
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    src = tmp_path / "s.md"; src.write_text("x")
    _wire_llm_driver(bus,
                     extractor_payloads=[
                         {"edits": [_basic_edit()], "summary": "."},
                         {"edits": [], "summary": "."},
                         {"edits": [], "summary": "."},
                     ],
                     ranker_payload={"chosen": [
                         {"framing_id": "pattern_extractor", "edit_index": 0,
                          "score": 0.5, "reason": "."}]})
    ops_seen: list[dict] = []

    async def fake_gateway(msg):
        p = dict(msg.payload) if isinstance(msg.payload, dict) else {}
        op = p.get("op")
        ops_seen.append(p)
        if op == "register_command":
            return {"ok": True}
        if op == "open_draft":
            return {"ok": True, "draft_id": "DRAFT-1", "message_id": "M-1"}
        if op == "close_draft":
            return {"ok": True, "message_id": "M-1"}
        return {"ok": True}

    bus.register("gateway", fake_gateway)
    a = ReflectionAgent(ctx)
    await a.install()

    # Drive the slash command path
    # (we mirror _on_command's behaviour with a direct reply call since the
    # slash path can't reach our sources by default)
    result = await a.reflect(need="terse style", sources=[str(src)])
    await a._reply("sess-1", result, quote_text="/reflect terse style")

    opens = [p for p in ops_seen if p.get("op") == "open_draft"]
    closes = [p for p in ops_seen if p.get("op") == "close_draft"]
    sends = [p for p in ops_seen if p.get("op") == "send"]

    assert len(opens) == 1 and len(closes) == 1
    # Critical: no plain `send` in the happy path — gateway got the structured call
    assert sends == []
    open_payload = opens[0]
    # content has the reply body
    assert "draft(s) staged" in open_payload["content"]
    # footer_meta carries structured metrics
    footer = open_payload["footer_meta"]
    fkeys = {row[0] for row in footer}
    assert "sources" in fkeys
    assert "drafts" in fkeys
    # quote captured the user's original /reflect invocation
    assert open_payload["quote"].get("text") == "/reflect terse style"


async def test_reply_falls_back_to_send_when_open_draft_fails(tmp_path):
    """If gateway's open_draft returns {ok: False} (e.g. unknown session),
    reflection_agent falls back to plain `op:send` so the user still sees
    the reply with an inline italic footer."""
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    src = tmp_path / "s.md"; src.write_text("x")
    _wire_llm_driver(bus,
                     extractor_payloads=[
                         {"edits": [_basic_edit()], "summary": "."},
                         {"edits": [], "summary": "."},
                         {"edits": [], "summary": "."},
                     ],
                     ranker_payload={"chosen": [
                         {"framing_id": "pattern_extractor", "edit_index": 0,
                          "score": 0.5, "reason": "."}]})
    sends: list[dict] = []

    async def fake_gateway(msg):
        p = dict(msg.payload) if isinstance(msg.payload, dict) else {}
        op = p.get("op")
        if op == "open_draft":
            return {"ok": False, "error": "unknown session"}
        if op == "send":
            sends.append(p)
            return {"ok": True}
        return {"ok": True}

    bus.register("gateway", fake_gateway)
    a = ReflectionAgent(ctx)
    result = await a.reflect(need="terse style", sources=[str(src)])
    await a._reply("sess-1", result)

    assert len(sends) == 1
    assert "draft(s) staged" in sends[0]["text"]


async def test_slash_command_empty_args_still_goes_through_draft(tmp_path):
    """Even the usage-hint reply uses the draft path (same UX affordance
    as normal replies — quoted + footer-able on Telegram)."""
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    ops_seen: list[dict] = []

    async def fake_gateway(msg):
        p = dict(msg.payload) if isinstance(msg.payload, dict) else {}
        ops_seen.append(p)
        if p.get("op") == "open_draft":
            return {"ok": True, "draft_id": "D1", "message_id": "M1"}
        return {"ok": True}

    bus.register("gateway", fake_gateway)
    a = ReflectionAgent(ctx)
    await a.install()
    await bus.publish("gateway.command_invoked",
                       {"command": COMMAND, "args": "", "session_key": "k"})
    for _ in range(15):
        await asyncio.sleep(0)
        if any(p.get("op") == "open_draft" for p in ops_seen):
            break
    opens = [p for p in ops_seen if p.get("op") == "open_draft"]
    assert opens, "expected open_draft even for usage hint"
    assert "Usage:" in opens[0]["content"]


# -- framings inventory -----------------------------------------


def test_framings_have_distinct_ids():
    ids = [f["id"] for f in FRAMINGS]
    assert len(ids) == len(set(ids))
    assert {"pattern_extractor", "anti_pattern_spotter", "synthesizer"} <= set(ids)


# -- auto target via performance_ranker (dogfood integration) -----


async def test_resolve_auto_target_synthesizes_need_from_ranker(tmp_path):
    bus = Bus()

    async def fake_ranker(msg):
        return {"ok": True, "window_hours": 24,
                "ranked": [{"agent": "harness_pro_max", "score": 5.0,
                             "errors": 3, "rejections": 1}]}

    bus.register("performance_ranker", fake_ranker)
    ctx = _make_ctx(tmp_path, bus)
    agent = ReflectionAgent(ctx)
    need, top = await agent._resolve_auto_target()
    assert top["agent"] == "harness_pro_max"
    assert "harness_pro_max" in need
    assert "3 error" in need
    assert "1 rejection" in need


async def test_resolve_auto_target_ranker_missing_returns_none(tmp_path):
    # No handler registered for performance_ranker
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    agent = ReflectionAgent(ctx)
    need, top = await agent._resolve_auto_target()
    assert need is None
    assert top is None


async def test_resolve_auto_target_empty_rank_returns_none(tmp_path):
    bus = Bus()

    async def fake_ranker(msg):
        return {"ok": True, "window_hours": 24, "ranked": []}

    bus.register("performance_ranker", fake_ranker)
    ctx = _make_ctx(tmp_path, bus)
    agent = ReflectionAgent(ctx)
    need, top = await agent._resolve_auto_target()
    assert need is None


async def test_reflect_auto_resolves_and_runs_happy_path(tmp_path):
    """End-to-end: auto=True synthesizes need from ranker, then proceeds
    through normal hypothesize → rank → draft flow."""
    bus = Bus()

    async def fake_ranker(msg):
        return {"ok": True, "window_hours": 24,
                "ranked": [{"agent": "worst_bot", "score": 8.0,
                             "errors": 2, "rejections": 3}]}

    bus.register("performance_ranker", fake_ranker)
    _wire_llm_driver(
        bus,
        extractor_payloads=[
            {"edits": [{"action": "add", "target": "feedback_worst_bot.md",
                        "title": "worst_bot patterns",
                        "body": "worst_bot repeatedly fails at step X",
                        "memory_type": "feedback"}],
             "summary": "one useful edit"},
        ] * 3,
        ranker_payload={"chosen": [{"framing_id": "pattern_extractor",
                                     "edit_index": 0,
                                     "score": 0.9,
                                     "reason": "relevant"}],
                         "rejected_summary": ""},
    )

    ctx = _make_ctx(tmp_path, bus)
    session_root = tmp_path / "proj" / "data" / "sessions"
    session_root.mkdir(parents=True)
    (session_root / "s1.md").write_text("session transcript mentioning worst_bot")

    agent = ReflectionAgent(ctx)
    result = await agent.reflect(auto=True)

    assert result["ok"] is True
    assert "worst_bot" in result["need"]
    assert result["auto_target"]["agent"] == "worst_bot"
    assert result["auto_target"]["errors"] == 2


async def test_reflect_auto_with_no_ranker_fails_at_auto_target_stage(tmp_path):
    bus = Bus()  # no ranker registered
    ctx = _make_ctx(tmp_path, bus)
    session_root = tmp_path / "proj" / "data" / "sessions"
    session_root.mkdir(parents=True)
    (session_root / "s1.md").write_text("x")
    agent = ReflectionAgent(ctx)
    result = await agent.reflect(auto=True)
    assert result["ok"] is False
    assert result["stage"] == "auto_target"


async def test_handle_reflect_auto_true(tmp_path):
    bus = Bus()

    async def fake_ranker(msg):
        return {"ok": True, "window_hours": 24,
                "ranked": [{"agent": "flaky_bot", "score": 4.0,
                             "errors": 4, "rejections": 0}]}

    bus.register("performance_ranker", fake_ranker)

    ctx = _make_ctx(tmp_path, bus)
    agent = ReflectionAgent(ctx)
    # No sources → fails at load_sources, but auto_target ran successfully
    # and is carried through on the error path.
    result = await agent.handle(type("M", (), {
        "payload": {"op": "reflect", "auto": True}})())
    assert result["ok"] is False
    assert result["stage"] == "load_sources"   # reached past auto_target
    assert result["auto_target"]["agent"] == "flaky_bot"


async def test_slash_command_auto_keyword_triggers_auto(tmp_path):
    """`/reflect auto` invokes reflect(auto=True) rather than need='auto'."""
    bus = Bus()
    ranker_called = [False]

    async def fake_ranker(msg):
        ranker_called[0] = True
        return {"ok": True, "window_hours": 24,
                "ranked": [{"agent": "x_bot", "score": 1.0,
                             "errors": 1, "rejections": 0}]}

    bus.register("performance_ranker", fake_ranker)

    # stub gateway so _reply doesn't error
    async def fake_gateway(msg):
        return {"ok": True}

    bus.register("gateway", fake_gateway)
    # stub llm_driver to avoid real work — will be unreachable if auto fails
    _wire_llm_driver(
        bus,
        extractor_payloads=[{"edits": [], "summary": ""}] * 3,
        ranker_payload={"chosen": [], "rejected_summary": ""},
    )

    ctx = _make_ctx(tmp_path, bus)
    session_root = tmp_path / "proj" / "data" / "sessions"
    session_root.mkdir(parents=True)
    (session_root / "s1.md").write_text("transcript")

    agent = ReflectionAgent(ctx)

    await agent._on_command({
        "topic": "gateway.command_invoked",
        "payload": {"command": COMMAND, "args": "auto",
                    "session_key": "console:test"},
    })
    # Allow bus tasks to drain
    await asyncio.sleep(0.02)
    assert ranker_called[0] is True


async def test_slash_command_explicit_need_does_not_call_ranker(tmp_path):
    """`/reflect <normal need>` must NOT query ranker."""
    bus = Bus()
    ranker_called = [False]

    async def fake_ranker(msg):
        ranker_called[0] = True
        return {"ok": True, "window_hours": 24, "ranked": []}

    bus.register("performance_ranker", fake_ranker)

    async def fake_gateway(msg):
        return {"ok": True}

    bus.register("gateway", fake_gateway)
    _wire_llm_driver(
        bus,
        extractor_payloads=[{"edits": [], "summary": ""}] * 3,
        ranker_payload={"chosen": [], "rejected_summary": ""},
    )

    ctx = _make_ctx(tmp_path, bus)
    session_root = tmp_path / "proj" / "data" / "sessions"
    session_root.mkdir(parents=True)
    (session_root / "s1.md").write_text("transcript")

    agent = ReflectionAgent(ctx)

    await agent._on_command({
        "topic": "gateway.command_invoked",
        "payload": {"command": COMMAND, "args": "how did we handle logging",
                    "session_key": "console:test"},
    })
    await asyncio.sleep(0.02)
    assert ranker_called[0] is False
