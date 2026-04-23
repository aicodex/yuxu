"""performance_ranker — aggregate per-agent negative signals and rank worst."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuxu.bundled.performance_ranker.handler import (
    MEMORY_DEMOTED_TOPIC,
    NAME,
    PerformanceRanker,
    _bump_applied,
    _demote_for_staleness,
    _demote_level,
    _parse_date,
)
from yuxu.core.bus import Bus
from yuxu.core.frontmatter import parse_frontmatter
from yuxu.core.loader import Loader

pytestmark = pytest.mark.asyncio


class _M:
    def __init__(self, payload):
        self.payload = payload


# -- unit: scoring + windowing ---------------------------------


async def test_record_error_and_rejection_scores():
    bus = Bus()
    r = PerformanceRanker(bus)
    r._record("agent_a", "error")
    r._record("agent_a", "error")
    r._record("agent_a", "rejected")
    errs, rejs = r._breakdown("agent_a")
    assert (errs, rejs) == (2, 1)
    assert r._compute_score(errs, rejs) == 2 * 1.0 + 1 * 2.0


async def test_unknown_agent_zero_score():
    r = PerformanceRanker(Bus())
    assert r._breakdown("ghost") == (0, 0)


async def test_window_prunes_stale_events():
    r = PerformanceRanker(Bus(), window_hours=1.0 / 3600)  # 1-second window
    r._record("flaky", "error")
    # simulate time passage by rewriting the queued event's timestamp
    r._events["flaky"][0].ts = time.monotonic() - 10.0
    errs, rejs = r._breakdown("flaky")
    assert errs == 0 and rejs == 0


async def test_underscore_agents_ignored():
    r = PerformanceRanker(Bus())
    r._record("_meta", "error")
    r._record("", "error")
    assert r._events == {}


async def test_custom_weights():
    r = PerformanceRanker(Bus(), weight_error=3.0, weight_rejected=0.5)
    r._record("x", "error")
    r._record("x", "rejected")
    errs, rejs = r._breakdown("x")
    assert r._compute_score(errs, rejs) == 3.0 + 0.5


# -- subscription: *.error + approval_queue.rejected ------------


async def test_on_error_extracts_agent_from_topic():
    r = PerformanceRanker(Bus())
    await r._on_error({"topic": "llm_driver.error", "payload": {"x": 1}})
    assert r._breakdown("llm_driver") == (1, 0)


async def test_on_error_skips_resource_warning():
    r = PerformanceRanker(Bus())
    await r._on_error({"topic": "some.resource_warning"})
    assert r._events == {}


async def test_on_rejection_uses_requester():
    r = PerformanceRanker(Bus())
    await r._on_rejection({"topic": "approval_queue.rejected",
                            "payload": {"requester": "reflection_agent",
                                        "approval_id": "abc"}})
    assert r._breakdown("reflection_agent") == (0, 1)


async def test_on_rejection_missing_requester_ignored():
    r = PerformanceRanker(Bus())
    await r._on_rejection({"topic": "approval_queue.rejected",
                            "payload": {"approval_id": "abc"}})
    assert r._events == {}


# -- ops: rank / score / reset ---------------------------------


async def test_rank_orders_descending_by_score():
    r = PerformanceRanker(Bus())
    for _ in range(3):
        r._record("ord", "error")           # score 3
    r._record("worst", "error")
    r._record("worst", "rejected")          # score 3
    r._record("worst", "rejected")          # score 5 total
    r._record("mild", "error")              # score 1
    resp = await r.handle(_M({"op": "rank"}))
    agents = [row["agent"] for row in resp["ranked"]]
    assert agents == ["worst", "ord", "mild"]


async def test_rank_respects_limit_and_min_score():
    r = PerformanceRanker(Bus())
    r._record("a", "error")                 # 1.0
    r._record("b", "error")
    r._record("b", "error")                 # 2.0
    r._record("c", "rejected")              # 2.0
    resp = await r.handle(_M({"op": "rank", "limit": 2}))
    assert len(resp["ranked"]) == 2
    # Tie break by agent name ascending
    assert [x["agent"] for x in resp["ranked"]] == ["b", "c"]
    # min_score filters low scorers out
    resp = await r.handle(_M({"op": "rank", "min_score": 1.5}))
    assert {x["agent"] for x in resp["ranked"]} == {"b", "c"}


async def test_score_op_returns_breakdown():
    r = PerformanceRanker(Bus())
    r._record("x", "error")
    r._record("x", "rejected")
    resp = await r.handle(_M({"op": "score", "agent": "x"}))
    assert resp["ok"] is True
    assert resp["agent"] == "x"
    assert resp["errors"] == 1
    assert resp["rejections"] == 1
    assert resp["score"] == 3.0  # 1 * 1.0 + 1 * 2.0


async def test_score_op_missing_agent_errors():
    r = PerformanceRanker(Bus())
    resp = await r.handle(_M({"op": "score"}))
    assert resp["ok"] is False
    assert "missing" in resp["error"]


async def test_reset_specific_agent_clears_only_that():
    r = PerformanceRanker(Bus())
    r._record("a", "error")
    r._record("b", "error")
    resp = await r.handle(_M({"op": "reset", "agent": "a"}))
    assert resp["ok"] is True
    assert resp["cleared"] == 1
    assert r._breakdown("a") == (0, 0)
    assert r._breakdown("b") == (1, 0)


async def test_reset_all_wipes_everything():
    r = PerformanceRanker(Bus())
    r._record("a", "error")
    r._record("b", "rejected")
    r._record("c", "error")
    resp = await r.handle(_M({"op": "reset"}))
    assert resp["cleared"] == 3
    assert r._events == {}


async def test_unknown_op_returns_error():
    r = PerformanceRanker(Bus())
    resp = await r.handle(_M({"op": "salsa"}))
    assert resp["ok"] is False


# -- integration via Loader + bus pub/sub -----------------------


async def test_loader_starts_and_subscribes(bundled_dir):
    bus = Bus()
    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()
    await loader.ensure_running("performance_ranker")
    assert bus.query_status("performance_ranker") == "ready"

    # Publish error + rejection through the real bus
    await bus.publish("alpha_bot.error", {"trace": "boom"})
    await bus.publish("approval_queue.rejected",
                      {"requester": "beta_bot", "approval_id": "a1"})
    # Extra signal
    await bus.publish("alpha_bot.error", {"trace": "boom"})
    await asyncio.sleep(0.02)

    resp = await bus.request("performance_ranker", {"op": "rank"}, timeout=1.0)
    ranked = {row["agent"]: row for row in resp["ranked"]}
    assert ranked["alpha_bot"]["errors"] == 2
    assert ranked["alpha_bot"]["score"] == 2.0
    assert ranked["beta_bot"]["rejections"] == 1
    assert ranked["beta_bot"]["score"] == 2.0

    await loader.stop("performance_ranker")


@pytest.fixture
def bundled_dir():
    import yuxu as _y
    from pathlib import Path as _P
    return str(_P(_y.__file__).parent / "bundled")


# -- Phase 4 minimum: memory.retrieved bumps score.applied -----


def _write_entry(root: Path, rel: str, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _fm_of(p: Path) -> dict:
    fm, _ = parse_frontmatter(p.read_text(encoding="utf-8"))
    return fm


NO_SCORE_ENTRY = """---
name: Foo
description: something observed
type: feedback
evidence_level: observed
---
body text
"""

PROBATION_ENTRY = """---
name: Bar
description: recently updated
type: feedback
evidence_level: observed
probation: true
score: {"applied": 0, "helped": 0, "hurt": 0, "last_evaluated": "2026-04-20"}
---
body text
"""

NO_FRONTMATTER_ENTRY = "just plain text, no frontmatter block\n"


async def test_bump_applied_initializes_score_when_missing(tmp_path: Path):
    p = _write_entry(tmp_path, "a.md", NO_SCORE_ENTRY)
    _bump_applied(p, probation_clear_threshold=3)
    fm = _fm_of(p)
    assert fm["score"]["applied"] == 1
    assert fm["score"]["helped"] == 0
    assert fm["score"]["hurt"] == 0
    assert "last_evaluated" in fm["score"]
    # body preserved
    assert "body text" in p.read_text(encoding="utf-8")


async def test_bump_applied_increments_existing(tmp_path: Path):
    p = _write_entry(tmp_path, "b.md", PROBATION_ENTRY)
    _bump_applied(p, probation_clear_threshold=3)
    _bump_applied(p, probation_clear_threshold=3)
    fm = _fm_of(p)
    assert fm["score"]["applied"] == 2
    # threshold not yet reached
    assert fm.get("probation") is True


async def test_bump_applied_clears_probation_at_threshold(tmp_path: Path):
    p = _write_entry(tmp_path, "c.md", PROBATION_ENTRY)
    for _ in range(3):
        _bump_applied(p, probation_clear_threshold=3)
    fm = _fm_of(p)
    assert fm["score"]["applied"] == 3
    assert fm.get("probation") is False


async def test_bump_applied_no_promote_on_non_probation(tmp_path: Path):
    p = _write_entry(tmp_path, "d.md", NO_SCORE_ENTRY)
    for _ in range(5):
        _bump_applied(p, probation_clear_threshold=3)
    fm = _fm_of(p)
    assert fm["score"]["applied"] == 5
    # never had probation → don't invent the field just to flip it
    assert "probation" not in fm or fm["probation"] is False
    # evidence level untouched — Phase 4 minimum does not promote levels
    assert fm["evidence_level"] == "observed"


async def test_bump_applied_skips_entry_without_frontmatter(tmp_path: Path):
    p = _write_entry(tmp_path, "e.md", NO_FRONTMATTER_ENTRY)
    _bump_applied(p, probation_clear_threshold=3)
    # content unchanged
    assert p.read_text(encoding="utf-8") == NO_FRONTMATTER_ENTRY


async def test_bump_applied_non_int_applied_coerced(tmp_path: Path):
    p = _write_entry(tmp_path, "f.md", """---
name: Foo
description: d
type: feedback
score: {"applied": "oops", "helped": 0, "hurt": 0, "last_evaluated": "2026-04-20"}
---
body
""")
    _bump_applied(p, probation_clear_threshold=3)
    fm = _fm_of(p)
    assert fm["score"]["applied"] == 1


async def test_on_memory_retrieved_bumps_each_path(tmp_path: Path):
    r = PerformanceRanker(Bus(), probation_clear_threshold=3)
    a = _write_entry(tmp_path, "a.md", NO_SCORE_ENTRY)
    b = _write_entry(tmp_path, "b.md", PROBATION_ENTRY)
    await r._on_memory_retrieved({
        "topic": "memory.retrieved",
        "payload": {
            "op": "list",
            "paths": ["a.md", "b.md"],
            "memory_root": str(tmp_path),
            "mode": "reflect",
        },
    })
    assert _fm_of(a)["score"]["applied"] == 1
    assert _fm_of(b)["score"]["applied"] == 1


async def test_on_memory_retrieved_does_not_pollute_ranking(tmp_path: Path):
    r = PerformanceRanker(Bus(), probation_clear_threshold=3)
    _write_entry(tmp_path, "a.md", NO_SCORE_ENTRY)
    await r._on_memory_retrieved({
        "topic": "memory.retrieved",
        "payload": {"paths": ["a.md"], "memory_root": str(tmp_path)},
    })
    # memory bookkeeping must not push anything into the sliding window
    assert r._events == {}


async def test_on_memory_retrieved_rejects_path_escape(tmp_path: Path):
    r = PerformanceRanker(Bus(), probation_clear_threshold=3)
    outside = tmp_path.parent / "outside.md"
    outside.write_text(NO_SCORE_ENTRY, encoding="utf-8")
    try:
        await r._on_memory_retrieved({
            "topic": "memory.retrieved",
            "payload": {
                "paths": ["../outside.md"],
                "memory_root": str(tmp_path),
            },
        })
        # file outside the root must not be mutated
        assert _fm_of(outside).get("score") is None
    finally:
        outside.unlink(missing_ok=True)


async def test_on_memory_retrieved_tolerates_missing_file(tmp_path: Path):
    r = PerformanceRanker(Bus(), probation_clear_threshold=3)
    # no file created; must not raise
    await r._on_memory_retrieved({
        "topic": "memory.retrieved",
        "payload": {"paths": ["ghost.md"], "memory_root": str(tmp_path)},
    })


async def test_on_memory_retrieved_ignores_empty_payload():
    r = PerformanceRanker(Bus(), probation_clear_threshold=3)
    await r._on_memory_retrieved({"topic": "memory.retrieved"})
    await r._on_memory_retrieved({"topic": "memory.retrieved",
                                   "payload": {}})
    await r._on_memory_retrieved({"topic": "memory.retrieved",
                                   "payload": {"paths": [],
                                               "memory_root": "/tmp"}})


async def test_loader_subscribes_to_memory_retrieved(bundled_dir, tmp_path: Path):
    bus = Bus()
    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()
    await loader.ensure_running("performance_ranker")

    entry = _write_entry(tmp_path, "subject.md", PROBATION_ENTRY)
    # Publish 3 retrievals through the real bus — hit the threshold.
    for _ in range(3):
        await bus.publish("memory.retrieved", {
            "op": "list",
            "paths": ["subject.md"],
            "memory_root": str(tmp_path),
            "mode": "reflect",
        })
    await asyncio.sleep(0.02)
    fm = _fm_of(entry)
    assert fm["score"]["applied"] == 3
    assert fm.get("probation") is False
    await loader.stop("performance_ranker")


# -- staleness auto-demote (I6) --------------------------------


import datetime as _dt


ENTRY_VALIDATED_STALE = """---
name: Old Validated
description: a stale validated entry
type: feedback
evidence_level: validated
updated: 2026-01-01
---
body
"""

ENTRY_OBSERVED_FRESH = """---
name: Fresh Observed
description: written yesterday
type: feedback
evidence_level: observed
updated: {today}
---
body
"""

ENTRY_MANDATORY_STALE = """---
name: Mandatory Rule
description: never ages
type: feedback
evidence_level: validated
tags: [mandatory]
updated: 2026-01-01
---
body
"""

ENTRY_SPECULATIVE_STALE = """---
name: Already Floor
description: can't go lower
type: reference
evidence_level: speculative
updated: 2026-01-01
---
body
"""

ENTRY_NO_UPDATED = """---
name: No Date
description: no updated field
type: feedback
evidence_level: observed
---
body
"""


async def test_parse_date_handles_common_shapes():
    assert _parse_date("2026-04-24") == _dt.date(2026, 4, 24)
    # tolerates datetime-as-str longer than 10 chars
    assert _parse_date("2026-04-24T10:00:00") == _dt.date(2026, 4, 24)
    assert _parse_date(_dt.date(2026, 4, 24)) == _dt.date(2026, 4, 24)
    assert _parse_date(_dt.datetime(2026, 4, 24, 10)) == _dt.date(2026, 4, 24)
    assert _parse_date(None) is None
    assert _parse_date("not a date") is None


async def test_demote_level_walks_down():
    assert _demote_level("validated") == "consensus"
    assert _demote_level("consensus") == "observed"
    assert _demote_level("observed") == "speculative"
    # floor
    assert _demote_level("speculative") is None
    # unknown level refuses to mutate
    assert _demote_level("gold") is None
    assert _demote_level(None) is None


async def test_demote_for_staleness_demotes_validated(tmp_path: Path):
    p = _write_entry(tmp_path, "a.md", ENTRY_VALIDATED_STALE)
    report = _demote_for_staleness(
        p, window_days=30, today=_dt.date(2026, 4, 24),
    )
    assert report is not None
    assert report["from_level"] == "validated"
    assert report["to_level"] == "consensus"
    assert report["age_days"] > 30
    fm = _fm_of(p)
    assert fm["evidence_level"] == "consensus"
    # updated was bumped forward to avoid immediate re-demote
    # (yaml loader coerces YYYY-MM-DD to date; str or date both count)
    assert _parse_date(fm["updated"]) == _dt.date(2026, 4, 24)


async def test_demote_for_staleness_skips_fresh(tmp_path: Path):
    today = _dt.date(2026, 4, 24)
    p = _write_entry(tmp_path, "a.md",
                     ENTRY_OBSERVED_FRESH.format(today=today.isoformat()))
    report = _demote_for_staleness(p, window_days=30, today=today)
    assert report is None
    fm = _fm_of(p)
    assert fm["evidence_level"] == "observed"


async def test_demote_for_staleness_exempts_mandatory(tmp_path: Path):
    p = _write_entry(tmp_path, "a.md", ENTRY_MANDATORY_STALE)
    report = _demote_for_staleness(
        p, window_days=30, today=_dt.date(2026, 4, 24),
    )
    assert report is None
    fm = _fm_of(p)
    assert fm["evidence_level"] == "validated"


async def test_demote_for_staleness_speculative_is_floor(tmp_path: Path):
    p = _write_entry(tmp_path, "a.md", ENTRY_SPECULATIVE_STALE)
    report = _demote_for_staleness(
        p, window_days=30, today=_dt.date(2026, 4, 24),
    )
    assert report is None
    fm = _fm_of(p)
    assert fm["evidence_level"] == "speculative"


async def test_demote_for_staleness_skips_without_updated(tmp_path: Path):
    p = _write_entry(tmp_path, "a.md", ENTRY_NO_UPDATED)
    report = _demote_for_staleness(
        p, window_days=30, today=_dt.date(2026, 4, 24),
    )
    assert report is None


async def test_sweep_returns_empty_when_no_roots_known():
    r = PerformanceRanker(Bus())
    out = await r.sweep_staleness_once()
    assert out == []


async def test_sweep_uses_constructor_override(tmp_path: Path):
    _write_entry(tmp_path, "a.md", ENTRY_VALIDATED_STALE)
    _write_entry(tmp_path, "b.md",
                 ENTRY_OBSERVED_FRESH.format(today="2026-04-24"))
    bus = Bus()
    r = PerformanceRanker(bus, staleness_window_days=30,
                          memory_root=str(tmp_path))
    # Override is registered on install, but for this unit test we add
    # it manually to avoid spawning the background task.
    r._known_memory_roots.add(tmp_path.resolve())
    events: list[dict] = []

    async def _capture(e):
        events.append(e.get("payload") or {})
    bus.subscribe(MEMORY_DEMOTED_TOPIC, _capture)
    reports = await r.sweep_staleness_once(today=_dt.date(2026, 4, 24))
    assert len(reports) == 1
    assert reports[0]["from_level"] == "validated"
    # bus dispatches subscribers via tasks — let them drain
    for _ in range(20):
        if events:
            break
        await asyncio.sleep(0.005)
    assert events and events[0]["to_level"] == "consensus"
    assert events[0]["path"] == "a.md"


async def test_sweep_skips_archive_and_draft_dirs(tmp_path: Path):
    _write_entry(tmp_path, "_archive/old.md", ENTRY_VALIDATED_STALE)
    _write_entry(tmp_path, "_drafts/new.md", ENTRY_VALIDATED_STALE)
    _write_entry(tmp_path, "real.md", ENTRY_VALIDATED_STALE)
    r = PerformanceRanker(Bus(), memory_root=str(tmp_path))
    r._known_memory_roots.add(tmp_path.resolve())
    reports = await r.sweep_staleness_once(today=_dt.date(2026, 4, 24))
    assert len(reports) == 1
    assert reports[0]["path"] == "real.md"


async def test_memory_retrieved_registers_root_for_sweep(tmp_path: Path):
    _write_entry(tmp_path, "a.md", ENTRY_VALIDATED_STALE)
    r = PerformanceRanker(Bus(), memory_root=None)
    assert not r._known_memory_roots
    await r._on_memory_retrieved({
        "topic": "memory.retrieved",
        "payload": {"paths": ["a.md"], "memory_root": str(tmp_path)},
    })
    assert tmp_path.resolve() in r._known_memory_roots
    reports = await r.sweep_staleness_once(today=_dt.date(2026, 4, 24))
    assert len(reports) == 1


async def test_sweep_op_accepts_memory_root_and_runs(tmp_path: Path):
    _write_entry(tmp_path, "a.md", ENTRY_VALIDATED_STALE)
    r = PerformanceRanker(Bus())
    msg = SimpleNamespace(payload={
        "op": "sweep_staleness",
        "memory_root": str(tmp_path),
    })
    resp = await r.handle(msg)
    assert resp["ok"] is True
    assert len(resp["demoted"]) == 1
    assert resp["demoted"][0]["from_level"] == "validated"


async def test_sweep_demotion_resets_updated(tmp_path: Path):
    p = _write_entry(tmp_path, "a.md", ENTRY_VALIDATED_STALE)
    r = PerformanceRanker(Bus(), memory_root=str(tmp_path))
    r._known_memory_roots.add(tmp_path.resolve())
    reports = await r.sweep_staleness_once(today=_dt.date(2026, 4, 24))
    assert reports
    # second pass on the same day must not demote again
    reports2 = await r.sweep_staleness_once(today=_dt.date(2026, 4, 24))
    assert reports2 == []
    fm = _fm_of(p)
    assert fm["evidence_level"] == "consensus"


async def test_background_sweep_starts_and_can_stop(tmp_path: Path):
    """Install spawns the background task; uninstall cancels it cleanly."""
    bus = Bus()
    # very short interval for deterministic test wake-up, but sweep
    # runs only if there's something to find. We use override to
    # preseed a root so sweep has something to scan.
    r = PerformanceRanker(bus, sweep_interval_hours=1.0 / 3600.0,
                          memory_root=str(tmp_path))
    _write_entry(tmp_path, "a.md", ENTRY_VALIDATED_STALE)
    r.install()
    assert r._sweep_task is not None
    # give the loop a tick to enter sleep
    await asyncio.sleep(0.05)
    r.uninstall()
    # task should be cancelled / done within a short grace
    for _ in range(20):
        if r._sweep_task is None or (r._sweep_task is not None
                                      and r._sweep_task.done()):
            break
        await asyncio.sleep(0.01)
    # uninstall cleared the reference
    assert r._sweep_task is None
