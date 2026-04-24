"""context_compressor — map-reduce summarization skill.

Unit coverage stubs llm_driver. Real-LLM integration gated by `-m slow`
per `feedback_test_with_real_llm.md`.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuxu.bundled.context_compressor.handler import (
    COMPRESSED_TOPIC,
    DEFAULT_MAX_BYTES_PER_MAP,
    DEFAULT_TARGET_TOKENS,
    NAME,
    _build_map_prompt,
    _build_reduce_prompt,
    _estimate_tokens,
    _format_summary,
    _head_tail_truncate,
    execute,
)
from yuxu.core.bus import Bus

pytestmark = pytest.mark.asyncio


# -- stub driver ------------------------------------------------


class _StubDriver:
    """Stub for llm_driver. Queue `replies`; each request pops next.
    Empty queue → LookupError (simulates skill not loaded).
    """
    def __init__(self, replies=None, raises=None):
        self.replies = list(replies or [])
        self.raises = raises
        self.calls: list[dict] = []
        self.published: list[tuple[str, dict]] = []

    async def request(self, target, payload, timeout=5.0):
        self.calls.append({"target": target, "payload": payload})
        if self.raises is not None:
            raise self.raises
        if not self.replies:
            raise LookupError(f"no handler for {target}")
        return self.replies.pop(0)

    async def publish(self, topic, payload):
        self.published.append((topic, payload))


def _ctx(bus) -> SimpleNamespace:
    return SimpleNamespace(bus=bus)


def _ok(content: str) -> dict:
    return {"ok": True, "content": content}


def _llm_summary(body: str) -> str:
    """Wrap body in the expected CC-style <analysis>/<summary> shape."""
    return (
        "<analysis>\nI analyzed the input and focused on key points.\n"
        "</analysis>\n"
        f"<summary>\n{body}\n</summary>"
    )


# -- primitives -------------------------------------------------


async def test_estimate_tokens_basic():
    assert _estimate_tokens("") == 0
    assert _estimate_tokens("a") >= 1
    long = "x" * 400
    # 400 bytes / 4 = 100 tokens
    assert _estimate_tokens(long) == 100


async def test_head_tail_truncate_noop_when_small():
    t, trunc = _head_tail_truncate("hello world", 1000)
    assert t == "hello world"
    assert trunc is False


async def test_head_tail_truncate_preserves_ends():
    s = "START_MARK" + ("x" * 5000) + "END_MARK"
    t, trunc = _head_tail_truncate(s, 500)
    assert trunc is True
    assert "START_MARK" in t
    assert "END_MARK" in t
    assert "middle elided" in t
    assert len(t.encode("utf-8")) <= 600  # marker adds some bytes


async def test_format_summary_strips_analysis_extracts_summary():
    raw = (
        "<analysis>\nthinking hard\nabout this\n</analysis>\n"
        "<summary>\n1. Section one\n2. Section two\n</summary>"
    )
    out = _format_summary(raw)
    assert "thinking hard" not in out
    assert "1. Section one" in out
    assert "2. Section two" in out


async def test_format_summary_returns_cleaned_when_no_summary_tag():
    raw = "<analysis>\nhmm\n</analysis>\nbare text without summary tags"
    out = _format_summary(raw)
    assert "hmm" not in out
    assert "bare text" in out


async def test_build_map_prompt_contains_key_instructions():
    p = _build_map_prompt(None)
    # NO_TOOLS double reinforcement — CC pattern
    assert "CRITICAL: Respond with TEXT ONLY" in p
    assert "REMINDER: Do NOT call any tools" in p
    # OpenClaw identifier preservation
    assert "Preserve all opaque identifiers" in p
    # CC 9-section structure anchors
    assert "Primary Request and Intent" in p
    assert "Current Work" in p
    assert "Optional Next Step" in p
    # yuxu divergence — flex sections
    assert "Omit any section that does not apply" in p
    # <analysis> scratchpad
    assert "<analysis>" in p
    assert "<summary>" in p


async def test_build_map_prompt_injects_custom_instructions():
    p = _build_map_prompt("focus on error/fix pairs")
    assert "focus on error/fix pairs" in p
    assert "Additional Instructions" in p


async def test_build_reduce_prompt_has_must_preserve():
    p = _build_reduce_prompt(None)
    assert "MUST PRESERVE" in p
    assert "Preserve all opaque identifiers" in p
    assert "<analysis>" in p


# -- summarize: input validation -----------------------------


async def test_summarize_requires_documents():
    drv = _StubDriver()
    r1 = await execute({"op": "summarize"}, _ctx(drv))
    r2 = await execute({"op": "summarize", "documents": []}, _ctx(drv))
    assert r1["ok"] is False and r2["ok"] is False


async def test_summarize_rejects_non_dict_document():
    drv = _StubDriver()
    r = await execute({"op": "summarize", "documents": ["x"]}, _ctx(drv))
    assert r["ok"] is False


async def test_summarize_rejects_non_string_body():
    drv = _StubDriver()
    r = await execute({"op": "summarize",
                        "documents": [{"id": "a", "body": 123}]},
                       _ctx(drv))
    assert r["ok"] is False


async def test_unknown_op_errors():
    drv = _StubDriver()
    r = await execute({"op": "frobnicate"}, _ctx(drv))
    assert r["ok"] is False


# -- summarize: skip short-circuit ---------------------------


async def test_summarize_skips_when_under_target():
    drv = _StubDriver()  # should never be called
    r = await execute({
        "op": "summarize",
        "documents": [{"id": "tiny", "body": "just a few words here"}],
        "target_tokens": 1000,
    }, _ctx(drv))
    assert r["ok"] is True
    assert r["skipped"] is True
    assert r["savings_ratio"] == 0.0
    assert "just a few words here" in r["merged_summary"]
    assert len(drv.calls) == 0


# -- summarize: single-doc LLM path --------------------------


async def test_summarize_single_doc_llm_path():
    drv = _StubDriver(replies=[_ok(_llm_summary("SHORT SUMMARY BODY"))])
    big = "content " * 2000  # ~16k bytes, well over 2000-token budget
    r = await execute({
        "op": "summarize",
        "documents": [{"id": "big", "body": big}],
        "target_tokens": 200,
        "task": "distill what happened",
    }, _ctx(drv))
    assert r["ok"] is True
    assert r["skipped"] is False
    assert r["fallback_used"] is False
    assert "SHORT SUMMARY BODY" in r["merged_summary"]
    assert r["total_tokens_after"] < r["total_tokens_before"]
    assert 0.0 < r["savings_ratio"] <= 1.0
    # one LLM call (map), no reduce needed for single doc
    assert len(drv.calls) == 1


async def test_summarize_llm_failure_triggers_fallback():
    drv = _StubDriver()  # empty → LookupError
    big = "x" * 20000
    r = await execute({
        "op": "summarize",
        "documents": [{"id": "d", "body": big}],
        "target_tokens": 200,
    }, _ctx(drv))
    assert r["ok"] is True
    assert r["fallback_used"] is True
    # Fallback summary is byte-truncated head+tail
    assert "middle elided" in r["merged_summary"]


async def test_summarize_fallback_disabled_returns_empty_summary():
    drv = _StubDriver()
    r = await execute({
        "op": "summarize",
        "documents": [{"id": "d", "body": "x" * 20000}],
        "target_tokens": 200,
        "fallback_enabled": False,
    }, _ctx(drv))
    assert r["ok"] is True
    assert r["per_document"][0]["summary"] == ""
    assert r["per_document"][0]["fallback_used"] is True


async def test_summarize_empty_body_short_circuits():
    drv = _StubDriver()
    r = await execute({
        "op": "summarize",
        "documents": [{"id": "d", "body": ""}],
        "target_tokens": 0,  # force non-skipping path
    }, _ctx(drv))
    # body is empty → summary is empty, no LLM call needed, no fallback
    assert r["ok"] is True
    assert r["per_document"][0]["summary"] == ""
    assert r["per_document"][0]["fallback_used"] is False


# -- summarize: multi-doc map+reduce -------------------------


async def test_summarize_two_docs_triggers_reduce():
    drv = _StubDriver(replies=[
        _ok(_llm_summary("PARTIAL-A")),
        _ok(_llm_summary("PARTIAL-B")),
        _ok(_llm_summary("MERGED SUMMARY of A and B")),
    ])
    big = "content " * 2000
    r = await execute({
        "op": "summarize",
        "documents": [
            {"id": "docA", "body": big},
            {"id": "docB", "body": big},
        ],
        "target_tokens": 200,
    }, _ctx(drv))
    assert r["ok"] is True
    assert "MERGED SUMMARY" in r["merged_summary"]
    # 2 map + 1 reduce
    assert len(drv.calls) == 3
    # Each partial recorded separately
    assert len(r["per_document"]) == 2
    assert r["per_document"][0]["summary"] == "PARTIAL-A"
    assert r["per_document"][1]["summary"] == "PARTIAL-B"


async def test_summarize_reduce_fallback_concats_partials_when_llm_fails():
    drv = _StubDriver(replies=[
        _ok(_llm_summary("PARTIAL-A")),
        _ok(_llm_summary("PARTIAL-B")),
        # reduce call will fail — no more replies → LookupError
    ])
    big = "content " * 2000
    r = await execute({
        "op": "summarize",
        "documents": [
            {"id": "docA", "body": big},
            {"id": "docB", "body": big},
        ],
        "target_tokens": 200,
    }, _ctx(drv))
    assert r["fallback_used"] is True
    # Both partials present in merged via concat fallback
    assert "PARTIAL-A" in r["merged_summary"]
    assert "PARTIAL-B" in r["merged_summary"]


async def test_summarize_unparseable_llm_falls_through_to_fallback():
    """When the LLM returns bare text without <summary> tags, we still
    extract something usable (analysis stripped, rest kept); but if
    extraction yields empty, fall back to byte truncation."""
    drv = _StubDriver(replies=[_ok("<analysis>thinking</analysis>")])  # no summary, nothing after analysis
    r = await execute({
        "op": "summarize",
        "documents": [{"id": "d", "body": "x" * 20000}],
        "target_tokens": 200,
    }, _ctx(drv))
    assert r["ok"] is True
    # empty summary → fallback path
    assert r["fallback_used"] is True


# -- summarize: document pre-truncation ----------------------


async def test_summarize_truncates_oversized_document_before_llm():
    """Docs larger than max_bytes_per_map are head+tail-truncated
    before the LLM call — keeps the LLM call within provider limits."""
    drv = _StubDriver(replies=[_ok(_llm_summary("ok"))])
    huge = "A" * 1_000_000  # 1 MB
    r = await execute({
        "op": "summarize",
        "documents": [{"id": "huge", "body": huge}],
        "target_tokens": 200,
        "max_bytes_per_map": 20_000,
    }, _ctx(drv))
    assert r["ok"] is True
    # LLM saw at most ~20k bytes of input (plus prompt overhead)
    sent_user = drv.calls[0]["payload"]["messages"][0]["content"]
    assert len(sent_user.encode("utf-8")) < 40_000
    assert "middle elided" in sent_user


# -- summarize: event publication ----------------------------


async def test_summarize_publishes_context_compressed_event():
    bus = Bus()
    seen: list[dict] = []

    async def _h(e):
        seen.append(e.get("payload") or {})
    bus.subscribe(COMPRESSED_TOPIC, _h)

    async def fake_driver(msg):
        return _ok(_llm_summary("summary"))
    bus.register("llm_driver", fake_driver)

    r = await execute({
        "op": "summarize",
        "documents": [{"id": "d", "body": "x" * 20000}],
        "target_tokens": 200,
    }, _ctx(bus))
    assert r["ok"] is True
    for _ in range(20):
        if seen:
            break
        await asyncio.sleep(0.005)
    assert seen
    ev = seen[0]
    assert ev["op"] == "summarize"
    assert ev["document_count"] == 1
    assert ev["total_tokens_before"] > ev["total_tokens_after"]
    assert ev["fallback_used"] is False
    assert ev["skipped"] is False


async def test_summarize_skip_still_publishes_event():
    bus = Bus()
    seen: list[dict] = []

    async def _h(e):
        seen.append(e.get("payload") or {})
    bus.subscribe(COMPRESSED_TOPIC, _h)

    r = await execute({
        "op": "summarize",
        "documents": [{"id": "d", "body": "short"}],
        "target_tokens": 1000,
    }, _ctx(bus))
    assert r["skipped"] is True
    for _ in range(20):
        if seen:
            break
        await asyncio.sleep(0.005)
    assert seen
    assert seen[0]["skipped"] is True


# -- loader discovery ----------------------------------------


async def test_loader_discovers_skill():
    from yuxu.core.loader import Loader
    import yuxu as _y

    bundled_dir = str(Path(_y.__file__).parent / "bundled")
    bus = Bus()
    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()
    assert NAME in loader.specs


# -- real LLM (slow) -----------------------------------------


def _minimax_configured() -> bool:
    return bool(os.environ.get("MINIMAX_API_KEY"))


@pytest.mark.slow
@pytest.mark.skipif(not _minimax_configured(),
                     reason="MINIMAX_API_KEY not set; skipping real-LLM test")
async def test_real_llm_summarize_smoke():
    """Real MiniMax, cheap single-doc summary, assert shape + savings."""
    from yuxu.core.loader import Loader
    import yuxu as _y

    bundled_dir = str(Path(_y.__file__).parent / "bundled")
    bus = Bus()
    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()
    await loader.ensure_running("rate_limit_service")
    await loader.ensure_running("llm_service")
    await loader.ensure_running("llm_driver")

    # A chunky input — a fake session-like transcript, enough to exceed
    # the 200-token target so we really get a compression pass.
    body = "\n\n".join([
        "User: please refactor the auth module to use JWT",
        "Assistant: I'll start by reading the existing session handler...",
        "(read auth.py, 800 lines)",
        "Assistant: found the session middleware. Now writing JWT handler...",
        "(wrote jwt.py, 200 lines)",
        "User: don't forget to rotate the key on logout",
        "Assistant: noted, adding rotation hook to the logout endpoint",
    ] * 50)

    r = await bus.request("context_compressor", {
        "op": "summarize",
        "documents": [{"id": "session1", "body": body}],
        "task": "continue the auth refactor",
        "target_tokens": 300,
    }, timeout=300.0)

    assert r["ok"] is True
    assert r["fallback_used"] is False
    assert r["skipped"] is False
    assert r["total_tokens_after"] < r["total_tokens_before"]
    assert r["merged_summary"]
    # Should contain something reflecting the task
    assert any(key in r["merged_summary"].lower() for key in
                ("jwt", "auth", "refactor", "session"))
