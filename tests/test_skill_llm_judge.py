"""llm_judge — pairwise / pointwise LLM judge skill.

Unit coverage stubs llm_driver via a fake bus. Real-LLM integration is
gated behind `-m slow` per `feedback_test_with_real_llm.md` so CI / fast
loops don't burn MiniMax quota.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuxu.bundled.llm_judge.handler import (
    ANCHOR_THRESHOLDS,
    DEFAULT_DIMENSIONS,
    NAME,
    VERDICT_TOPIC,
    _aggregate_compare,
    _aggregate_score,
    _anchor_label,
    _compare_fallback,
    _extract_json,
    _jaccard,
    _normalize_dimensions,
    _parse_compare_vote,
    _parse_score_vote,
    _score_fallback,
    _tokens,
    execute,
)
from yuxu.core.bus import Bus

pytestmark = pytest.mark.asyncio


# -- stub bus for driver routing ------------------------------


class _StubDriver:
    """Replayable llm_driver substitute. Queue replies; each bus.request
    pops the next. When queue is empty, falls back to `default_reply`
    (None => LookupError, simulating skill not loaded)."""

    def __init__(self, replies=None, default_reply=None, raises=None):
        self.replies = list(replies or [])
        self.default_reply = default_reply
        self.raises = raises
        self.calls: list[dict] = []

    async def request(self, target: str, payload: dict, timeout: float = 5.0):
        self.calls.append({"target": target, "payload": payload})
        if self.raises is not None:
            raise self.raises
        if self.replies:
            return self.replies.pop(0)
        if self.default_reply is None:
            raise LookupError(f"no handler for {target}")
        return self.default_reply

    async def publish(self, topic: str, payload: dict) -> None:
        self.calls.append({"publish": topic, "payload": payload})


def _ctx(bus) -> SimpleNamespace:
    return SimpleNamespace(bus=bus)


def _llm_reply(content_obj: dict | str, ok: bool = True) -> dict:
    if isinstance(content_obj, dict):
        content = json.dumps(content_obj)
    else:
        content = content_obj
    return {"ok": ok, "content": content}


# -- primitives -------------------------------------------------


async def test_extract_json_tolerates_fences():
    assert _extract_json('{"a": 1}') == {"a": 1}
    assert _extract_json("```json\n{\"a\": 2}\n```") == {"a": 2}
    assert _extract_json("some preamble {\"a\": 3} trailing") == {"a": 3}
    assert _extract_json("") is None
    assert _extract_json("not json at all") is None


async def test_jaccard_basics():
    assert _jaccard(set(), set()) == 0.0
    assert _jaccard({"a"}, {"a"}) == 1.0
    assert _jaccard({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3)


async def test_tokens_splits_on_words():
    assert "hello" in _tokens("Hello, world!")
    assert "world" in _tokens("Hello, world!")
    # unicode ok
    assert "记忆" in _tokens("记忆 system")


async def test_anchor_label_boundaries():
    assert _anchor_label(1.0) == "fully correct"
    assert _anchor_label(0.85) == "fully correct"
    assert _anchor_label(0.7) == "mostly correct"
    assert _anchor_label(0.4) == "partially correct"
    assert _anchor_label(0.1) == "mentions topic"
    assert _anchor_label(0.0) == "incorrect"
    assert _anchor_label(-1.0) == "incorrect"
    assert _anchor_label(99.0) == "fully correct"  # clamped


async def test_normalize_dimensions_defaults_when_empty():
    assert _normalize_dimensions(None) == list(DEFAULT_DIMENSIONS)
    assert _normalize_dimensions([]) == list(DEFAULT_DIMENSIONS)
    assert _normalize_dimensions(["x"]) == ["x"]
    assert _normalize_dimensions(["x", "", None]) == ["x"]


# -- compare: parsing + aggregation ---------------------------


_COMPARE_A_WIN = {
    "per_dimension": {
        "correctness": {"winner": "a", "margin": 0.6, "reason": "a covers edge"},
        "specificity": {"winner": "a", "margin": 0.3, "reason": "a more concrete"},
        "actionability": {"winner": "tie", "margin": 0.0, "reason": "both ok"},
    },
    "overall": {"winner": "a", "reason": "a better on two of three"}
}

_COMPARE_B_WIN = {
    "per_dimension": {
        "correctness": {"winner": "b", "margin": 0.4, "reason": "b avoids pitfall"},
        "specificity": {"winner": "tie", "margin": 0.0, "reason": "similar"},
        "actionability": {"winner": "b", "margin": 0.8, "reason": "b cites action"},
    },
    "overall": {"winner": "b", "reason": "b dominates actionability"}
}


async def test_parse_compare_vote_happy():
    raw = json.dumps(_COMPARE_A_WIN)
    v = _parse_compare_vote(raw, list(DEFAULT_DIMENSIONS))
    assert v is not None
    assert v["winner"] == "a"
    assert v["per_dimension"]["correctness"]["winner"] == "a"
    assert v["per_dimension"]["correctness"]["margin"] == 0.6


async def test_parse_compare_vote_missing_overall_rejects():
    raw = json.dumps({"per_dimension": {}})
    assert _parse_compare_vote(raw, list(DEFAULT_DIMENSIONS)) is None


async def test_parse_compare_vote_illegal_winner_rejects():
    raw = json.dumps({
        "per_dimension": {},
        "overall": {"winner": "neither", "reason": "hmm"},
    })
    assert _parse_compare_vote(raw, list(DEFAULT_DIMENSIONS)) is None


async def test_parse_compare_vote_clamps_margin():
    raw = json.dumps({
        "per_dimension": {
            "correctness": {"winner": "a", "margin": 99, "reason": "x"},
        },
        "overall": {"winner": "a", "reason": "x"},
    })
    v = _parse_compare_vote(raw, ["correctness"])
    assert v["per_dimension"]["correctness"]["margin"] == 1.0


async def test_aggregate_compare_majority():
    votes = [
        {"winner": "a", "per_dimension": {}},
        {"winner": "a", "per_dimension": {}},
        {"winner": "b", "per_dimension": {}},
    ]
    agg = _aggregate_compare(votes, list(DEFAULT_DIMENSIONS))
    assert agg["winner"] == "a"
    assert agg["confidence"] == round(2 / 3, 3)


async def test_aggregate_compare_split_tie():
    votes = [
        {"winner": "a", "per_dimension": {}},
        {"winner": "b", "per_dimension": {}},
    ]
    agg = _aggregate_compare(votes, list(DEFAULT_DIMENSIONS))
    assert agg["winner"] == "tie"


async def test_aggregate_compare_per_dim_winner_differs_from_overall():
    votes = [
        {"winner": "a",
         "per_dimension": {
             "correctness": {"winner": "b", "margin": 0.5},
             "specificity": {"winner": "a", "margin": 0.8},
         }},
        {"winner": "a",
         "per_dimension": {
             "correctness": {"winner": "b", "margin": 0.3},
             "specificity": {"winner": "a", "margin": 0.7},
         }},
    ]
    agg = _aggregate_compare(votes, ["correctness", "specificity"])
    assert agg["winner"] == "a"
    # Despite overall a, correctness dim is b
    assert agg["per_dimension"]["correctness"]["winner"] == "b"
    assert agg["per_dimension"]["specificity"]["winner"] == "a"


# -- compare: end-to-end via execute --------------------------


async def test_compare_happy_path_a_wins():
    drv = _StubDriver(replies=[_llm_reply(_COMPARE_A_WIN)])
    r = await execute({
        "op": "compare",
        "candidates": [
            {"id": "v1", "body": "a body"},
            {"id": "v2", "body": "b body"},
        ],
        "task": "write a kernel invariant",
        "seed": 0,
        "randomize_order": False,
    }, _ctx(drv))
    assert r["ok"] is True
    assert r["winner"] == "a"
    assert r["fallback_used"] is False
    assert r["candidate_ids"] == {"a": "v1", "b": "v2"}
    assert r["confidence"] == 1.0
    # per-dimension preserved
    assert r["per_dimension"]["correctness"]["winner"] == "a"


async def test_compare_requires_exactly_two_candidates():
    drv = _StubDriver()
    r1 = await execute({"op": "compare", "candidates": []}, _ctx(drv))
    r2 = await execute({"op": "compare",
                         "candidates": [{"id": "a", "body": "x"}]},
                        _ctx(drv))
    r3 = await execute({
        "op": "compare",
        "candidates": [{"id": "a", "body": "x"},
                        {"id": "b", "body": "y"},
                        {"id": "c", "body": "z"}],
    }, _ctx(drv))
    assert r1["ok"] is False and r2["ok"] is False and r3["ok"] is False


async def test_compare_rejects_empty_bodies():
    drv = _StubDriver()
    r = await execute({
        "op": "compare",
        "candidates": [{"id": "a", "body": ""}, {"id": "b", "body": "y"}],
    }, _ctx(drv))
    assert r["ok"] is False


async def test_compare_n_votes_aggregates():
    drv = _StubDriver(replies=[
        _llm_reply(_COMPARE_A_WIN),
        _llm_reply(_COMPARE_A_WIN),
        _llm_reply(_COMPARE_B_WIN),
    ])
    r = await execute({
        "op": "compare",
        "candidates": [{"id": "a", "body": "a"},
                        {"id": "b", "body": "b"}],
        "task": "something",
        "n_votes": 3,
        "randomize_order": False,
        "seed": 0,
    }, _ctx(drv))
    assert r["winner"] == "a"
    assert len(r["votes"]) == 3
    assert r["confidence"] == round(2 / 3, 3)


async def test_compare_order_flip_reverts_labels():
    """When randomize_order produces a flipped order, parsed winner must
    swap back so final label refers to the original A/B ids."""
    # Force flip by choosing a seed that yields rng.random() < 0.5.
    # Stub: LLM sees (b_body, a_body) → says 'a' (which is really original b).
    drv = _StubDriver(replies=[_llm_reply(_COMPARE_A_WIN)])
    r = await execute({
        "op": "compare",
        "candidates": [{"id": "a", "body": "alpha body"},
                        {"id": "b", "body": "beta body"}],
        "task": "something",
        "randomize_order": True,
        "seed": 1,  # deterministic
    }, _ctx(drv))
    # Whatever happened, the result is one of a/b/tie
    assert r["winner"] in ("a", "b", "tie")
    # The raw content is cached and labelled with the actual order sent.
    assert r["votes"][0]["order"] in ("ab", "ba")


async def test_compare_fallback_when_llm_unavailable():
    drv = _StubDriver()  # empty queue → LookupError
    r = await execute({
        "op": "compare",
        "candidates": [
            {"id": "a", "body": "the answer is 42 and also 7"},
            {"id": "b", "body": "bananas pears apples"},
        ],
        "task": "what is the answer 42",
    }, _ctx(drv))
    assert r["ok"] is True
    assert r["fallback_used"] is True
    assert r["confidence"] == 0.3
    # A has more task overlap → should win
    assert r["winner"] == "a"


async def test_compare_fallback_disabled_fails_out():
    drv = _StubDriver()
    r = await execute({
        "op": "compare",
        "candidates": [{"id": "a", "body": "x"},
                        {"id": "b", "body": "y"}],
        "task": "q",
        "fallback_enabled": False,
    }, _ctx(drv))
    assert r["ok"] is False


async def test_compare_unparseable_llm_falls_through_to_fallback():
    drv = _StubDriver(replies=[_llm_reply("this is not json at all")])
    r = await execute({
        "op": "compare",
        "candidates": [{"id": "a", "body": "foo bar"},
                        {"id": "b", "body": "baz qux"}],
        "task": "foo",
    }, _ctx(drv))
    assert r["ok"] is True
    assert r["fallback_used"] is True


async def test_compare_publishes_verdict_event():
    bus = Bus()
    seen: list[dict] = []

    async def _h(e):
        seen.append(e.get("payload") or {})
    bus.subscribe(VERDICT_TOPIC, _h)

    # Register a fake llm_driver on the real bus
    async def fake_driver(msg):
        return _llm_reply(_COMPARE_A_WIN)
    bus.register("llm_driver", fake_driver)

    r = await execute({
        "op": "compare",
        "candidates": [{"id": "a", "body": "a"},
                        {"id": "b", "body": "b"}],
        "task": "x",
        "randomize_order": False,
    }, _ctx(bus))
    assert r["winner"] == "a"
    for _ in range(20):
        if seen:
            break
        await asyncio.sleep(0.005)
    assert seen
    assert seen[0]["op"] == "compare"
    assert seen[0]["winner"] == "a"
    assert seen[0]["fallback_used"] is False
    assert seen[0]["vote_count"] == 1


# -- score: parsing + aggregation + e2e -----------------------


_SCORE_STRONG = {
    "per_dimension": {
        "correctness": {"score": 0.9, "reason": "spot on"},
        "specificity": {"score": 0.85, "reason": "concrete"},
        "actionability": {"score": 0.8, "reason": "clear next step"},
    },
    "overall": {"score": 0.85, "reason": "strong"},
}

_SCORE_WEAK = {
    "per_dimension": {
        "correctness": {"score": 0.3, "reason": "misses point"},
    },
    "overall": {"score": 0.3, "reason": "partial"},
}


async def test_parse_score_vote_happy():
    v = _parse_score_vote(json.dumps(_SCORE_STRONG), list(DEFAULT_DIMENSIONS))
    assert v is not None
    assert v["score"] == 0.85
    assert v["per_dimension"]["correctness"] == 0.9


async def test_parse_score_vote_clamps():
    raw = json.dumps({
        "per_dimension": {"correctness": {"score": 99}},
        "overall": {"score": 99},
    })
    v = _parse_score_vote(raw, ["correctness"])
    assert v["score"] == 1.0
    assert v["per_dimension"]["correctness"] == 1.0


async def test_aggregate_score_mean_and_confidence():
    votes = [
        {"score": 0.8, "per_dimension": {"correctness": 0.9}},
        {"score": 0.7, "per_dimension": {"correctness": 0.8}},
        {"score": 0.9, "per_dimension": {"correctness": 1.0}},
    ]
    agg = _aggregate_score(votes, ["correctness"])
    assert agg["score"] == round((0.8 + 0.7 + 0.9) / 3, 3)
    assert agg["per_dimension"]["correctness"] == 0.9
    # with variance ~= 0.08 and mean 0.8, confidence ≈ 1 - 0.1 = 0.9
    assert 0.85 < agg["confidence"] <= 1.0


async def test_score_happy_path():
    drv = _StubDriver(replies=[_llm_reply(_SCORE_STRONG)])
    r = await execute({
        "op": "score",
        "candidate": "pretty good answer",
        "task": "q",
    }, _ctx(drv))
    assert r["ok"] is True
    assert r["score"] == 0.85
    assert r["anchor_label"] == "fully correct"
    assert r["per_dimension"]["correctness"] == 0.9
    assert r["fallback_used"] is False


async def test_score_anchor_label_is_derived_from_mean():
    drv = _StubDriver(replies=[_llm_reply(_SCORE_WEAK)])
    r = await execute({
        "op": "score", "candidate": "weak", "task": "q",
    }, _ctx(drv))
    assert r["score"] == 0.3
    assert r["anchor_label"] == "partially correct"


async def test_score_n_votes_variance_lowers_confidence():
    drv = _StubDriver(replies=[
        _llm_reply({"per_dimension": {"correctness": {"score": 0.2}},
                     "overall": {"score": 0.2}}),
        _llm_reply({"per_dimension": {"correctness": {"score": 0.9}},
                     "overall": {"score": 0.9}}),
        _llm_reply({"per_dimension": {"correctness": {"score": 0.5}},
                     "overall": {"score": 0.5}}),
    ])
    r = await execute({
        "op": "score", "candidate": "c", "task": "q", "n_votes": 3,
    }, _ctx(drv))
    # High variance → confidence noticeably below 1
    assert r["confidence"] < 0.6


async def test_score_fallback_when_llm_unavailable():
    drv = _StubDriver()  # LookupError
    r = await execute({
        "op": "score",
        "candidate": "the answer is 42",
        "task": "what is the answer 42",
    }, _ctx(drv))
    assert r["ok"] is True
    assert r["fallback_used"] is True
    assert r["confidence"] == 0.3
    assert r["anchor_label"]  # label derived from overlap


async def test_score_requires_non_empty_candidate():
    r = await execute({"op": "score", "candidate": "  "},
                       _ctx(_StubDriver()))
    assert r["ok"] is False


# -- unknown op ------------------------------------------------


async def test_unknown_op_errors():
    drv = _StubDriver()
    r = await execute({"op": "frobnicate"}, _ctx(drv))
    assert r["ok"] is False


# -- integration with Loader ----------------------------------


async def test_loader_discovers_skill():
    from yuxu.core.loader import Loader
    import yuxu as _y

    bundled_dir = str(Path(_y.__file__).parent / "bundled")
    bus = Bus()
    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()
    assert NAME in loader.specs


# -- real LLM (slow, gated) -----------------------------------


def _minimax_configured() -> bool:
    """llm_driver needs MINIMAX_API_KEY + rate_limit_service pool setup.
    We use presence of the key as a proxy — actual routing uses the
    project's yuxu.json pool config."""
    return bool(os.environ.get("MINIMAX_API_KEY"))


@pytest.mark.slow
@pytest.mark.skipif(not _minimax_configured(),
                     reason="MINIMAX_API_KEY not set; skipping real-LLM test")
async def test_real_llm_compare_smoke():
    """Tight smoke test: real MiniMax, one cheap pairwise, assert shape."""
    from yuxu.core.loader import Loader
    import yuxu as _y

    bundled_dir = str(Path(_y.__file__).parent / "bundled")
    bus = Bus()
    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()
    await loader.ensure_running("rate_limit_service")
    await loader.ensure_running("llm_service")
    await loader.ensure_running("llm_driver")

    r = await bus.request("llm_judge", {
        "op": "compare",
        "candidates": [
            {"id": "clear",
             "body": "The Linux kernel scheduler uses the CFS algorithm."},
            {"id": "vague",
             "body": "Kernels have schedulers."},
        ],
        "task": "Which answer is more specific and informative?",
        "randomize_order": False,
    }, timeout=120.0)

    assert r["ok"] is True
    assert r["winner"] in ("a", "b", "tie")
    assert "per_dimension" in r
    assert r["fallback_used"] is False


@pytest.mark.slow
@pytest.mark.skipif(not _minimax_configured(),
                     reason="MINIMAX_API_KEY not set; skipping real-LLM test")
async def test_real_llm_score_smoke():
    from yuxu.core.loader import Loader
    import yuxu as _y

    bundled_dir = str(Path(_y.__file__).parent / "bundled")
    bus = Bus()
    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()
    await loader.ensure_running("rate_limit_service")
    await loader.ensure_running("llm_service")
    await loader.ensure_running("llm_driver")

    r = await bus.request("llm_judge", {
        "op": "score",
        "candidate": "The Earth orbits the Sun at an average distance of "
                      "about 150 million km.",
        "task": "Is this statement correct and specific?",
    }, timeout=120.0)

    assert r["ok"] is True
    assert 0.0 <= r["score"] <= 1.0
    assert r["anchor_label"]
    assert r["fallback_used"] is False
