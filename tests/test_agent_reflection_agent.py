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


async def test_slash_command_triggers_and_replies(tmp_path):
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
    sent = []

    async def fake_gateway(msg):
        p = dict(msg.payload) if isinstance(msg.payload, dict) else {}
        if p.get("op") == "send":
            sent.append(p)
        return {"ok": True}

    bus.register("gateway", fake_gateway)
    a = ReflectionAgent(ctx)
    await a.install()

    # Need to override default sources path; can't via slash command yet.
    # So call reflect directly to verify reply formatting works:
    result = await a.reflect(need="terse style", sources=[str(src)])
    text = a._format_reply(result)
    assert text.startswith("✅") and "draft(s) staged" in text


async def test_slash_command_empty_args_prints_usage(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    sent = []

    async def fake_gateway(msg):
        p = dict(msg.payload) if isinstance(msg.payload, dict) else {}
        if p.get("op") == "send":
            sent.append(p)
        return {"ok": True}

    bus.register("gateway", fake_gateway)
    a = ReflectionAgent(ctx)
    await a.install()
    await bus.publish("gateway.command_invoked",
                       {"command": COMMAND, "args": "", "session_key": "k"})
    for _ in range(10):
        await asyncio.sleep(0)
        if sent:
            break
    assert sent and sent[0]["text"].startswith("Usage:")


# -- framings inventory -----------------------------------------


def test_framings_have_distinct_ids():
    ids = [f["id"] for f in FRAMINGS]
    assert len(ids) == len(set(ids))
    assert {"pattern_extractor", "anti_pattern_spotter", "synthesizer"} <= set(ids)
