"""ApprovalApplier — closes reflection_agent's memory_edit loop."""
from __future__ import annotations

import asyncio
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import pytest

from yuxu.bundled.approval_applier.handler import (
    APPLIED_TOPIC,
    GATED_TOPIC,
    REJECTED_TOPIC,
    SKIPPED_TOPIC,
    ApprovalApplier,
    _strip_outer_frontmatter,
)
from yuxu.bundled.approval_queue.handler import ApprovalQueue
from yuxu.bundled.checkpoint_store.handler import CheckpointStore
from yuxu.core.bus import Bus

pytestmark = pytest.mark.asyncio


# -- pure helper -----------------------------------------------


def test_strip_outer_frontmatter_extracts_inner():
    draft = dedent("""\
        ---
        status: draft
        proposed_target: a.md
        ---
        ---
        name: real_entry
        description: y
        type: reference
        ---
        # Title

        body
        """)
    inner = _strip_outer_frontmatter(draft)
    assert inner is not None
    assert inner.startswith("---\nname: real_entry")
    assert "# Title" in inner


def test_strip_returns_none_on_missing_outer_fm():
    assert _strip_outer_frontmatter("no frontmatter at all\n# Title") is None
    assert _strip_outer_frontmatter("") is None


# -- fixtures --------------------------------------------------


def _make_ctx(bus: Bus) -> SimpleNamespace:
    return SimpleNamespace(bus=bus, agent_dir=Path("/tmp"), name="approval_applier",
                            loader=None)


def _make_draft_file(tmp_path: Path, *, proposed_target: str = "feedback_x.md",
                     inner_body: str | None = None) -> Path:
    """Write a realistic two-frontmatter draft file and return its path."""
    memory_root = tmp_path / "mem"
    drafts_dir = memory_root / "_drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    draft = drafts_dir / "reflection_1_ab_x.md"
    inner = inner_body or dedent("""\
        ---
        name: feedback_x
        description: a test feedback entry
        type: feedback
        ---
        # feedback_x

        Real memory body.
        """)
    draft.write_text(
        "---\n"
        f"status: \"draft\"\nproposed_target: \"{proposed_target}\"\n"
        "proposed_action: \"add\"\nreflection_run_id: \"ab\"\n"
        "---\n" + inner,
        encoding="utf-8",
    )
    return draft


def _fake_aq_entry(aid: str, draft_path: Path, *,
                   target: str = "feedback_x.md",
                   action: str = "add") -> dict:
    return {
        "approval_id": aid,
        "action": "memory_edit",
        "requester": "reflection_agent",
        "status": "approved",
        "detail": {
            "run_id": "ab", "need": "test",
            "draft_path": str(draft_path),
            "proposed_target": target,
            "proposed_action": action,
            "title": "x", "score": 0.9,
        },
    }


def _register_fake_aq(bus: Bus, entries_by_id: dict[str, dict]):
    async def handler(msg):
        p = dict(msg.payload) if isinstance(msg.payload, dict) else {}
        if p.get("op") == "get":
            aid = p.get("approval_id")
            if aid in entries_by_id:
                return {"ok": True, "item": entries_by_id[aid]}
            return {"ok": False, "error": "no such approval"}
        return {"ok": False, "error": "unexpected op in fake aq"}
    bus.register("approval_queue", handler)


async def _yield(n: int = 20) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


def _collect_events(bus: Bus, topic: str) -> list[dict]:
    got: list[dict] = []

    async def _h(e):
        p = e.get("payload") if isinstance(e, dict) else None
        if isinstance(p, dict):
            got.append(p)

    bus.subscribe(topic, _h)
    return got


# -- approved + add happy path --------------------------------


async def test_approved_add_writes_inner_body_and_deletes_draft(tmp_path):
    bus = Bus()
    draft = _make_draft_file(tmp_path)
    _register_fake_aq(bus, {"A1": _fake_aq_entry("A1", draft)})
    applied = _collect_events(bus, APPLIED_TOPIC)

    applier = ApprovalApplier(_make_ctx(bus))
    await applier.install()

    await bus.publish("approval_queue.decided", {
        "approval_id": "A1", "action": "memory_edit",
        "requester": "reflection_agent", "reason": None,
        "decision": "approved",
    })
    await _yield()

    target = tmp_path / "mem" / "feedback_x.md"
    assert target.exists()
    written = target.read_text(encoding="utf-8")
    assert written.startswith("---\nname: feedback_x")
    assert "Real memory body." in written
    assert not draft.exists(), "draft should be deleted after apply"
    assert applied and applied[0]["target_path"] == str(target)
    assert applied[0]["action"] == "add"


async def test_approved_update_overwrites_existing_target(tmp_path):
    bus = Bus()
    draft = _make_draft_file(tmp_path)
    target = tmp_path / "mem" / "feedback_x.md"
    target.write_text("old content", encoding="utf-8")
    _register_fake_aq(bus, {
        "A1": _fake_aq_entry("A1", draft, action="update"),
    })
    applier = ApprovalApplier(_make_ctx(bus))
    await applier.install()

    await bus.publish("approval_queue.decided", {
        "approval_id": "A1", "action": "memory_edit",
        "decision": "approved", "requester": "x", "reason": None,
    })
    await _yield()

    assert target.read_text(encoding="utf-8") != "old content"
    assert "Real memory body." in target.read_text(encoding="utf-8")
    assert not draft.exists()


# -- refusal paths ---------------------------------------------


async def test_add_refused_if_target_exists(tmp_path):
    bus = Bus()
    draft = _make_draft_file(tmp_path)
    target = tmp_path / "mem" / "feedback_x.md"
    target.write_text("do not overwrite me", encoding="utf-8")
    _register_fake_aq(bus, {"A1": _fake_aq_entry("A1", draft)})
    skipped = _collect_events(bus, SKIPPED_TOPIC)

    applier = ApprovalApplier(_make_ctx(bus))
    await applier.install()
    await bus.publish("approval_queue.decided", {
        "approval_id": "A1", "action": "memory_edit",
        "decision": "approved", "requester": "x", "reason": None,
    })
    await _yield()

    assert target.read_text(encoding="utf-8") == "do not overwrite me"
    assert draft.exists(), "draft preserved on skip"
    assert skipped and "exists" in skipped[0]["reason"]


async def test_update_refused_if_target_missing(tmp_path):
    bus = Bus()
    draft = _make_draft_file(tmp_path)
    _register_fake_aq(bus, {
        "A1": _fake_aq_entry("A1", draft, action="update"),
    })
    skipped = _collect_events(bus, SKIPPED_TOPIC)

    applier = ApprovalApplier(_make_ctx(bus))
    await applier.install()
    await bus.publish("approval_queue.decided", {
        "approval_id": "A1", "action": "memory_edit",
        "decision": "approved", "requester": "x", "reason": None,
    })
    await _yield()

    assert not (tmp_path / "mem" / "feedback_x.md").exists()
    assert draft.exists()
    assert skipped and "does not exist" in skipped[0]["reason"]


async def test_missing_draft_file_is_idempotent_skip(tmp_path):
    bus = Bus()
    draft_path = tmp_path / "mem" / "_drafts" / "gone.md"
    _register_fake_aq(bus, {
        "A1": _fake_aq_entry("A1", draft_path),
    })
    skipped = _collect_events(bus, SKIPPED_TOPIC)

    applier = ApprovalApplier(_make_ctx(bus))
    await applier.install()
    await bus.publish("approval_queue.decided", {
        "approval_id": "A1", "action": "memory_edit",
        "decision": "approved", "requester": "x", "reason": None,
    })
    await _yield()

    assert skipped and "draft missing" in skipped[0]["reason"]


async def test_malformed_draft_without_outer_frontmatter_skipped(tmp_path):
    bus = Bus()
    drafts_dir = tmp_path / "mem" / "_drafts"
    drafts_dir.mkdir(parents=True)
    bad = drafts_dir / "bad.md"
    bad.write_text("# not a draft, no frontmatter at all\n", encoding="utf-8")
    _register_fake_aq(bus, {"A1": _fake_aq_entry("A1", bad)})
    skipped = _collect_events(bus, SKIPPED_TOPIC)

    applier = ApprovalApplier(_make_ctx(bus))
    await applier.install()
    await bus.publish("approval_queue.decided", {
        "approval_id": "A1", "action": "memory_edit",
        "decision": "approved", "requester": "x", "reason": None,
    })
    await _yield()

    assert skipped and "no outer frontmatter" in skipped[0]["reason"]
    assert bad.exists(), "malformed draft kept for inspection"


# -- rejection path --------------------------------------------


async def test_rejection_archives_draft_preserves_contents(tmp_path):
    """I6 retention: rejected drafts move to _archive/rejected/, don't delete."""
    bus = Bus()
    draft = _make_draft_file(tmp_path)
    original_contents = draft.read_text(encoding="utf-8")
    _register_fake_aq(bus, {"A1": _fake_aq_entry("A1", draft)})
    rejected = _collect_events(bus, REJECTED_TOPIC)

    applier = ApprovalApplier(_make_ctx(bus))
    await applier.install()
    await bus.publish("approval_queue.decided", {
        "approval_id": "A1", "action": "memory_edit",
        "decision": "rejected", "requester": "x", "reason": "unwanted",
    })
    await _yield()

    # Original draft is gone but contents live on under _archive/rejected/
    assert not draft.exists()
    assert not (tmp_path / "mem" / "feedback_x.md").exists()
    archive_dir = tmp_path / "mem" / "_archive" / "rejected"
    assert archive_dir.exists()
    archived = list(archive_dir.iterdir())
    assert len(archived) == 1
    assert archived[0].read_text(encoding="utf-8") == original_contents
    assert rejected and rejected[0]["approval_id"] == "A1"
    assert rejected[0]["archived_path"] == str(archived[0])


def test_archive_draft_handles_same_name_collision(tmp_path):
    """Two rejects with identical draft names get unique archive paths."""
    from yuxu.bundled.approval_applier.handler import _archive_draft
    drafts_dir = tmp_path / "mem" / "_drafts"
    drafts_dir.mkdir(parents=True)

    # First reject
    d1 = drafts_dir / "dup.md"
    d1.write_text("v1", encoding="utf-8")
    a1 = _archive_draft(d1)
    assert a1.exists()
    assert a1.read_text(encoding="utf-8") == "v1"
    assert not d1.exists()

    # Second reject with the same base name, created in the same second
    d2 = drafts_dir / "dup.md"
    d2.write_text("v2", encoding="utf-8")
    a2 = _archive_draft(d2)
    assert a2.exists()
    assert a2 != a1   # collision suffix kept them distinct
    assert a2.read_text(encoding="utf-8") == "v2"

    # Both versions preserved — neither overwrote the other
    archive_dir = tmp_path / "mem" / "_archive" / "rejected"
    contents = {p.read_text(encoding="utf-8") for p in archive_dir.iterdir()}
    assert contents == {"v1", "v2"}


async def test_rejection_idempotent_on_missing_draft(tmp_path):
    bus = Bus()
    draft_path = tmp_path / "mem" / "_drafts" / "gone.md"
    _register_fake_aq(bus, {"A1": _fake_aq_entry("A1", draft_path)})
    rejected = _collect_events(bus, REJECTED_TOPIC)

    applier = ApprovalApplier(_make_ctx(bus))
    await applier.install()
    await bus.publish("approval_queue.decided", {
        "approval_id": "A1", "action": "memory_edit",
        "decision": "rejected", "requester": "x", "reason": None,
    })
    await _yield()

    assert rejected and rejected[0]["approval_id"] == "A1"
    # Missing draft: no archive created, event still fires with null archived
    assert rejected[0].get("archived_path") is None


# -- filtering -------------------------------------------------


async def test_non_memory_edit_actions_are_ignored(tmp_path):
    bus = Bus()
    draft = _make_draft_file(tmp_path)
    _register_fake_aq(bus, {"A1": _fake_aq_entry("A1", draft)})
    applied = _collect_events(bus, APPLIED_TOPIC)
    skipped = _collect_events(bus, SKIPPED_TOPIC)

    applier = ApprovalApplier(_make_ctx(bus))
    await applier.install()
    await bus.publish("approval_queue.decided", {
        "approval_id": "A1", "action": "delete_memory",  # different action
        "decision": "approved", "requester": "x", "reason": None,
    })
    await _yield()

    assert applied == []
    assert skipped == []
    assert draft.exists()  # unmodified


async def test_malformed_detail_skipped(tmp_path):
    bus = Bus()
    bad_entry = {
        "approval_id": "A1", "action": "memory_edit",
        "requester": "x", "status": "approved",
        "detail": {"run_id": "x"},   # missing draft_path / proposed_target / action
    }
    _register_fake_aq(bus, {"A1": bad_entry})
    skipped = _collect_events(bus, SKIPPED_TOPIC)

    applier = ApprovalApplier(_make_ctx(bus))
    await applier.install()
    await bus.publish("approval_queue.decided", {
        "approval_id": "A1", "action": "memory_edit",
        "decision": "approved", "requester": "x", "reason": None,
    })
    await _yield()

    assert skipped and "malformed detail" in skipped[0]["reason"]


# -- manual handle() op ----------------------------------------


class _Msg:
    def __init__(self, payload):
        self.payload = payload


async def test_handle_apply_draft_ok(tmp_path):
    bus = Bus()
    draft = _make_draft_file(tmp_path)
    applier = ApprovalApplier(_make_ctx(bus))
    r = await applier.handle(_Msg({
        "op": "apply_draft",
        "draft_path": str(draft),
        "proposed_target": "feedback_x.md",
        "proposed_action": "add",
    }))
    assert r["ok"] is True
    assert (tmp_path / "mem" / "feedback_x.md").exists()
    assert not draft.exists()


async def test_handle_unknown_op(tmp_path):
    bus = Bus()
    applier = ApprovalApplier(_make_ctx(bus))
    r = await applier.handle(_Msg({"op": "weird"}))
    assert r["ok"] is False


async def test_handle_missing_fields(tmp_path):
    bus = Bus()
    applier = ApprovalApplier(_make_ctx(bus))
    r = await applier.handle(_Msg({"op": "apply_draft"}))
    assert r["ok"] is False
    assert "missing" in r["error"]


async def test_handle_invalid_action(tmp_path):
    bus = Bus()
    draft = _make_draft_file(tmp_path)
    applier = ApprovalApplier(_make_ctx(bus))
    r = await applier.handle(_Msg({
        "op": "apply_draft",
        "draft_path": str(draft),
        "proposed_target": "x.md",
        "proposed_action": "remove",
    }))
    assert r["ok"] is False
    assert "invalid proposed_action" in r["error"]


# -- integration with real approval_queue ----------------------


async def test_end_to_end_with_real_approval_queue_and_checkpoint(tmp_path,
                                                                    monkeypatch):
    """Enqueue a memory_edit, approve it, verify applier materializes it."""
    monkeypatch.setenv("CHECKPOINT_ROOT", str(tmp_path / "chkp"))
    bus = Bus()

    # real checkpoint_store + approval_queue + applier (all on same bus)
    store = CheckpointStore(tmp_path / "chkp")
    bus.register("checkpoint_store", store.handle)

    aq = ApprovalQueue(bus)
    await aq.load_state()
    bus.register("approval_queue", aq.handle)

    applier = ApprovalApplier(_make_ctx(bus))
    await applier.install()

    # pre-stage a draft on disk
    draft = _make_draft_file(tmp_path)
    applied = _collect_events(bus, APPLIED_TOPIC)

    # reflection_agent-style enqueue
    r = await aq.enqueue(
        action="memory_edit",
        detail={
            "run_id": "ab", "need": "test",
            "draft_path": str(draft),
            "proposed_target": "feedback_x.md",
            "proposed_action": "add",
            "title": "x", "score": 0.9,
        },
        requester="reflection_agent",
    )
    assert r["ok"]
    aid = r["approval_id"]

    # user approves
    r2 = await aq.approve(aid, reason="looks right")
    assert r2["ok"]

    await _yield(30)

    target = tmp_path / "mem" / "feedback_x.md"
    assert target.exists()
    assert "Real memory body." in target.read_text(encoding="utf-8")
    assert not draft.exists()
    assert applied and applied[0]["approval_id"] == aid


# -- Phase 5: probation on update ------------------------------


def test_stamp_probation_injects_flag_and_resets_score():
    from yuxu.bundled.approval_applier.handler import _stamp_probation_on_update
    from yuxu.core.frontmatter import parse_frontmatter
    inner = (
        "---\n"
        "name: feedback_testing\n"
        "description: old value\n"
        "type: feedback\n"
        "evidence_level: consensus\n"
        "status: current\n"
        "score:\n"
        "  applied: 12\n"
        "  helped: 8\n"
        "  hurt: 1\n"
        "  last_evaluated: 2026-04-01\n"
        "---\n\n"
        "body\n"
    )
    out = _stamp_probation_on_update(inner)
    fm, _ = parse_frontmatter(out)
    assert fm["probation"] is True
    assert fm["score"]["applied"] == 0
    assert fm["score"]["helped"] == 0
    assert fm["score"]["hurt"] == 0
    # evidence_level inherited, not wiped
    assert fm["evidence_level"] == "consensus"
    # name / description preserved
    assert fm["name"] == "feedback_testing"


def test_stamp_probation_passthrough_on_no_frontmatter():
    from yuxu.bundled.approval_applier.handler import _stamp_probation_on_update
    raw = "plain text, no frontmatter\n"
    assert _stamp_probation_on_update(raw) == raw


async def test_update_approval_writes_probation_to_target(tmp_path):
    """Full update flow: approved update lands on disk with probation=true."""
    from yuxu.core.frontmatter import parse_frontmatter
    bus = Bus()
    # Seed an existing target
    memory_root = tmp_path / "mem"
    drafts = memory_root / "_drafts"
    drafts.mkdir(parents=True)
    target_rel = "feedback_x.md"
    target_abs = memory_root / target_rel
    target_abs.write_text(
        "---\nname: feedback_x\ndescription: v1\ntype: feedback\n"
        "evidence_level: consensus\n---\n\nold body\n",
        encoding="utf-8",
    )

    # Create a draft proposing an update
    draft = drafts / "curator_upd.md"
    draft.write_text(
        "---\n"
        f"status: \"draft\"\nproposed_target: \"{target_rel}\"\n"
        "proposed_action: \"update\"\nreflection_run_id: \"u1\"\n"
        "---\n"
        "---\n"
        "name: feedback_x\n"
        "description: v2 updated\n"
        "type: feedback\n"
        "evidence_level: consensus\n"
        "status: current\n"
        "---\n\nnew body\n",
        encoding="utf-8",
    )

    _register_fake_aq(bus, {"A1": _fake_aq_entry(
        "A1", draft, target=target_rel, action="update")})
    _collect_events(bus, "approval_applier.applied")

    applier = ApprovalApplier(_make_ctx(bus))
    await applier.install()
    await bus.publish("approval_queue.decided", {
        "approval_id": "A1", "action": "memory_edit",
        "decision": "approved", "requester": "x",
    })
    await _yield()

    # Target now has probation=true and score reset
    landed = target_abs.read_text(encoding="utf-8")
    fm, _ = parse_frontmatter(landed)
    assert fm["probation"] is True
    assert fm["score"]["applied"] == 0
    assert fm["evidence_level"] == "consensus"  # inherited from proposal
    assert fm["description"] == "v2 updated"    # real content applied


async def test_add_approval_does_not_stamp_probation(tmp_path):
    """Only updates enter probation; a brand-new `add` lands as-is."""
    from yuxu.core.frontmatter import parse_frontmatter
    bus = Bus()
    draft = _make_draft_file(tmp_path)  # defaults to add on feedback_x.md
    _register_fake_aq(bus, {"A1": _fake_aq_entry("A1", draft)})
    _collect_events(bus, "approval_applier.applied")

    applier = ApprovalApplier(_make_ctx(bus))
    await applier.install()
    await bus.publish("approval_queue.decided", {
        "approval_id": "A1", "action": "memory_edit",
        "decision": "approved", "requester": "x",
    })
    await _yield()

    target = tmp_path / "mem" / "feedback_x.md"
    assert target.exists()
    fm, _ = parse_frontmatter(target.read_text(encoding="utf-8"))
    # add: no probation injection
    assert fm.get("probation", False) is False


# -- admission gate hook (I6 write-admission) -----------------


def _register_gate(bus: Bus, *, verdict: dict):
    """Install a stub admission_gate skill that returns `verdict`.

    verdict shape: `{"ok": bool, "pass": bool, "stages": {...}, "verdict": str}`
    """
    async def handler(msg):
        # Echo back whatever the test wants. Ignore payload details.
        return verdict
    bus.register("admission_gate", handler)


async def test_gate_pass_lets_write_through(tmp_path):
    bus = Bus()
    draft = _make_draft_file(tmp_path)
    _register_fake_aq(bus, {"A1": _fake_aq_entry("A1", draft)})
    _register_gate(bus, verdict={
        "ok": True, "pass": True,
        "stages": {"surface_check": {"pass": True, "reason": "ok"}},
        "verdict": "PASS",
    })
    gated = _collect_events(bus, GATED_TOPIC)
    applied = _collect_events(bus, APPLIED_TOPIC)

    applier = ApprovalApplier(_make_ctx(bus))
    await applier.install()
    await bus.publish("approval_queue.decided", {
        "approval_id": "A1", "action": "memory_edit",
        "decision": "approved", "requester": "x",
    })
    await _yield()

    target = tmp_path / "mem" / "feedback_x.md"
    assert target.exists()
    assert not gated
    assert applied


async def test_gate_fail_archives_and_emits_event(tmp_path):
    bus = Bus()
    draft = _make_draft_file(tmp_path)
    _register_fake_aq(bus, {"A1": _fake_aq_entry("A1", draft)})
    _register_gate(bus, verdict={
        "ok": True, "pass": False,
        "stages": {
            "surface_check": {"pass": False, "reason": "verbose-obvious"},
            "golden_replay": {"pass": True, "reason": "no citation"},
            "noop_baseline": {"pass": True, "reason": "unique"},
        },
        "verdict": "FAIL [surface_check=fail]",
    })
    gated = _collect_events(bus, GATED_TOPIC)
    applied = _collect_events(bus, APPLIED_TOPIC)

    applier = ApprovalApplier(_make_ctx(bus))
    await applier.install()
    await bus.publish("approval_queue.decided", {
        "approval_id": "A1", "action": "memory_edit",
        "decision": "approved", "requester": "x",
    })
    await _yield()

    target = tmp_path / "mem" / "feedback_x.md"
    assert not target.exists(), "gated draft must not be written"
    assert not applied
    assert gated and gated[0]["approval_id"] == "A1"
    assert gated[0]["verdict"].startswith("FAIL")
    archived = gated[0]["archived_path"]
    assert archived is not None
    archived_p = Path(archived)
    assert archived_p.exists()
    assert "_archive" in archived_p.parts and "gated" in archived_p.parts


async def test_gate_missing_falls_through_as_pass(tmp_path):
    """When admission_gate isn't loaded, write proceeds with a warning.

    This matches the `optional_deps` pattern — a missing advisory circuit
    cannot brick the memory write path.
    """
    bus = Bus()
    draft = _make_draft_file(tmp_path)
    _register_fake_aq(bus, {"A1": _fake_aq_entry("A1", draft)})
    # NOTE: no _register_gate() — bus.request("admission_gate") → LookupError
    applied = _collect_events(bus, APPLIED_TOPIC)
    gated = _collect_events(bus, GATED_TOPIC)

    applier = ApprovalApplier(_make_ctx(bus))
    await applier.install()
    await bus.publish("approval_queue.decided", {
        "approval_id": "A1", "action": "memory_edit",
        "decision": "approved", "requester": "x",
    })
    await _yield()

    target = tmp_path / "mem" / "feedback_x.md"
    assert target.exists()
    assert applied and not gated


async def test_gate_raises_falls_through_as_pass(tmp_path):
    bus = Bus()
    draft = _make_draft_file(tmp_path)
    _register_fake_aq(bus, {"A1": _fake_aq_entry("A1", draft)})

    async def broken_gate(msg):
        raise RuntimeError("boom")
    bus.register("admission_gate", broken_gate)

    applied = _collect_events(bus, APPLIED_TOPIC)
    applier = ApprovalApplier(_make_ctx(bus))
    await applier.install()
    await bus.publish("approval_queue.decided", {
        "approval_id": "A1", "action": "memory_edit",
        "decision": "approved", "requester": "x",
    })
    await _yield()

    target = tmp_path / "mem" / "feedback_x.md"
    assert target.exists()
    assert applied


async def test_gate_update_path_also_gated(tmp_path):
    """Updates must go through the gate too — gate excludes self via
    target_path."""
    bus = Bus()
    draft = _make_draft_file(tmp_path)
    target = tmp_path / "mem" / "feedback_x.md"
    target.write_text("old content", encoding="utf-8")
    _register_fake_aq(bus, {"A1": _fake_aq_entry("A1", draft, action="update")})
    _register_gate(bus, verdict={
        "ok": True, "pass": False,
        "stages": {"noop_baseline": {"pass": False,
                                       "reason": "dup of something else"}},
        "verdict": "FAIL [dup]",
    })
    gated = _collect_events(bus, GATED_TOPIC)
    applier = ApprovalApplier(_make_ctx(bus))
    await applier.install()
    await bus.publish("approval_queue.decided", {
        "approval_id": "A1", "action": "memory_edit",
        "decision": "approved", "requester": "x",
    })
    await _yield()

    # old content preserved — gate blocked the update
    assert target.read_text(encoding="utf-8") == "old content"
    assert gated
    assert gated[0]["verdict"].startswith("FAIL")


async def test_apply_draft_manual_bypasses_gate(tmp_path):
    """Manual `apply_draft` op skips the gate entirely (test/debug hatch)."""
    bus = Bus()
    draft = _make_draft_file(tmp_path)

    # Install a gate that would hard-fail everything it sees.
    _register_gate(bus, verdict={
        "ok": True, "pass": False,
        "stages": {"surface_check": {"pass": False, "reason": "blocked"}},
        "verdict": "FAIL [always]",
    })

    applier = ApprovalApplier(_make_ctx(bus))
    r = await applier.handle(SimpleNamespace(payload={
        "op": "apply_draft",
        "draft_path": str(draft),
        "proposed_target": "feedback_x.md",
        "proposed_action": "add",
    }))
    assert r["ok"] is True
    target = tmp_path / "mem" / "feedback_x.md"
    assert target.exists()
