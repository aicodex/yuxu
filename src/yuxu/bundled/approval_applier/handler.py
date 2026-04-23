"""ApprovalApplier — consume approved `memory_edit` items, write to memory.

Closes the reflection_agent loop: the LLM proposes, the user approves via
approval_queue, and this agent does the actual filesystem mutation. No LLM
calls, no semantic checks — that's all upstream. Here it's strict,
idempotent file IO.

Per I6 retention: rejected drafts are archived under
`<memory_root>/_archive/rejected/<timestamp>-<original_name>`, not deleted.
Forgotten failures repeat; the archive preserves why a proposal didn't
land so future reflection can learn from it.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from yuxu.bundled._shared import dump_frontmatter
from yuxu.core.frontmatter import parse_frontmatter

log = logging.getLogger(__name__)

APPLIED_TOPIC = "approval_applier.applied"
REJECTED_TOPIC = "approval_applier.rejected"
SKIPPED_TOPIC = "approval_applier.skipped"
GATED_TOPIC = "approval_applier.gated"

ALLOWED_ACTIONS = ("add", "update")


def _strip_outer_frontmatter(text: str) -> Optional[str]:
    """Drop the first `---...---` block (staging metadata) and return the
    remainder (the real memory entry, with its own inner frontmatter).

    Returns None if `text` doesn't start with frontmatter — draft malformed.
    """
    if not text or not text.lstrip().startswith("---"):
        return None
    _outer_fm, inner = parse_frontmatter(text)
    if not isinstance(_outer_fm, dict) or not _outer_fm:
        return None
    return inner


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _stamp_probation_on_update(inner: str) -> str:
    """For `update` actions: the new version inherits the prior
    evidence_level but must be re-validated before trusted. Per I6:
    score resets, `probation: true` is set. execute mode filters
    probation entries until a helped-threshold clears them.

    If the entry has no frontmatter, return it unchanged — downstream
    readers will skip it from the index anyway.
    """
    fm, rest = parse_frontmatter(inner or "")
    if not isinstance(fm, dict) or not fm:
        return inner
    fm["probation"] = True
    fm["score"] = {
        "applied": 0, "helped": 0, "hurt": 0,
        "last_evaluated": time.strftime("%Y-%m-%d", time.localtime()),
    }
    head = dump_frontmatter(fm)
    tail = rest if rest.startswith("\n") else ("\n" + rest)
    return head + tail


def _archive_draft(draft_path: Path, *, bucket: str = "rejected") -> Path:
    """Move a draft to `<memory_root>/_archive/<bucket>/` with a timestamp
    prefix. Preserves the file for future reflection per I6 (archive,
    don't delete).

    `bucket` — default `rejected` (user said no); `gated` for admission
    gate blocks. Returns the archived path. `memory_root` is derived as
    `draft_path.parent.parent` — the same convention _apply_approval uses.
    """
    memory_root = draft_path.parent.parent
    archive_dir = memory_root / "_archive" / bucket
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    dest = archive_dir / f"{ts}-{draft_path.name}"
    # Handle collision (same second, same name) by appending an index
    if dest.exists():
        i = 1
        while True:
            candidate = archive_dir / f"{ts}-{i}-{draft_path.name}"
            if not candidate.exists():
                dest = candidate
                break
            i += 1
    os.replace(draft_path, dest)
    return dest


class ApprovalApplier:
    def __init__(self, ctx) -> None:
        self.ctx = ctx

    async def install(self) -> None:
        self.ctx.bus.subscribe("approval_queue.decided", self._on_decided)

    async def uninstall(self) -> None:
        try:
            self.ctx.bus.unsubscribe("approval_queue.decided", self._on_decided)
        except Exception:
            pass

    # -- event handler --------------------------------------------

    async def _on_decided(self, event: dict) -> None:
        payload = event.get("payload") if isinstance(event, dict) else None
        if not isinstance(payload, dict):
            return
        if payload.get("action") != "memory_edit":
            return
        aid = payload.get("approval_id")
        decision = payload.get("decision")
        if not aid or decision not in ("approved", "rejected"):
            return

        # Fetch the full entry to get `detail`, which the decided event drops.
        try:
            got = await self.ctx.bus.request(
                "approval_queue", {"op": "get", "approval_id": aid}, timeout=5.0,
            )
        except Exception as e:
            log.exception("approval_applier: failed to fetch entry %s", aid)
            await self._skip(aid, f"approval_queue.get raised: {e}")
            return
        if not got.get("ok"):
            await self._skip(aid, f"approval_queue.get: {got.get('error')}")
            return
        entry = got.get("item") or {}
        detail = entry.get("detail") or {}

        if decision == "rejected":
            await self._apply_rejection(aid, detail)
        else:
            await self._apply_approval(aid, detail)

    # -- branches -------------------------------------------------

    async def _apply_approval(self, aid: str, detail: dict) -> None:
        draft_path_s = detail.get("draft_path")
        target = detail.get("proposed_target")
        action = detail.get("proposed_action")
        if not draft_path_s or not target or action not in ALLOWED_ACTIONS:
            await self._skip(aid, f"malformed detail: {detail!r}")
            return

        draft_path = Path(draft_path_s)
        if not draft_path.exists():
            await self._skip(aid, f"draft missing: {draft_path}")
            return

        try:
            raw = draft_path.read_text(encoding="utf-8")
        except OSError as e:
            await self._skip(aid, f"read draft {draft_path}: {e}")
            return

        inner = _strip_outer_frontmatter(raw)
        if inner is None:
            await self._skip(aid, f"draft {draft_path} has no outer frontmatter")
            return

        memory_root = draft_path.parent.parent
        target_path = memory_root / target
        if action == "add" and target_path.exists():
            await self._skip(aid,
                             f"add refused: {target_path} exists; "
                             "propose an update or delete manually first")
            return
        if action == "update" and not target_path.exists():
            await self._skip(aid,
                             f"update refused: {target_path} does not exist; "
                             "propose an add instead")
            return

        # Updates enter probation per I6: new version inherits the prior
        # evidence_level but score resets and filters exclude it from
        # execute mode until a helped-threshold clears the flag.
        if action == "update":
            inner = _stamp_probation_on_update(inner)

        # I6 write-admission gate. Hard-block on fail; archive the draft
        # under _archive/gated/ with the gate report so future reflection
        # can see why it didn't land. Gate unavailable → proceed with a
        # warning (optional_deps pattern).
        gate = await self._run_admission_gate(inner, memory_root, target_path)
        if not gate["pass"]:
            archived: Optional[Path] = None
            if draft_path.exists():
                try:
                    archived = _archive_draft(draft_path, bucket="gated")
                except OSError as e:
                    log.warning("approval_applier: gate-archive %s: %s",
                                draft_path, e)
            await self.ctx.bus.publish(GATED_TOPIC, {
                "approval_id": aid,
                "target_path": str(target_path),
                "draft_path": str(draft_path),
                "archived_path": str(archived) if archived else None,
                "stages": gate.get("stages", {}),
                "verdict": gate.get("verdict", ""),
            })
            return

        try:
            _atomic_write(target_path, inner)
        except OSError as e:
            await self._skip(aid, f"write {target_path}: {e}")
            return

        # Success — remove the draft, emit applied event.
        try:
            draft_path.unlink()
        except OSError:
            log.warning("approval_applier: applied %s but couldn't delete draft %s",
                        target_path, draft_path)

        await self.ctx.bus.publish(APPLIED_TOPIC, {
            "approval_id": aid,
            "target_path": str(target_path),
            "action": action,
        })

    async def _apply_rejection(self, aid: str, detail: dict) -> None:
        draft_path_s = detail.get("draft_path")
        if not draft_path_s:
            await self._skip(aid, "malformed detail: no draft_path on rejection")
            return
        draft_path = Path(draft_path_s)
        archived_path: Optional[Path] = None
        if draft_path.exists():
            try:
                archived_path = _archive_draft(draft_path)
            except OSError as e:
                await self._skip(aid, f"archive {draft_path} on reject: {e}")
                return
        await self.ctx.bus.publish(REJECTED_TOPIC, {
            "approval_id": aid,
            "draft_path": str(draft_path),
            "archived_path": str(archived_path) if archived_path else None,
        })

    async def _skip(self, aid: str, reason: str) -> None:
        log.warning("approval_applier: skip %s: %s", aid, reason)
        await self.ctx.bus.publish(SKIPPED_TOPIC, {
            "approval_id": aid, "reason": reason,
        })

    async def _run_admission_gate(self, inner: str, memory_root: Path,
                                    target_path: Path) -> dict:
        """Ask the admission_gate skill to vet the final entry text.

        Gate unavailable (LookupError) → treat as pass with a skipped
        note. Gate raises (network, timeout) → also pass with a note:
        gate is an advisory circuit, it must not turn into a new
        single-point-of-failure for writes. Gate returning `ok=false` is
        treated the same way (the skill errored, not rejected).
        """
        try:
            r = await self.ctx.bus.request("admission_gate", {
                "op": "check",
                "entry_body": inner,
                "memory_root": str(memory_root),
                "target_path": str(target_path),
            }, timeout=120.0)
        except LookupError:
            return {"pass": True,
                    "verdict": "PASS [gate not loaded]",
                    "stages": {}}
        except Exception as e:
            log.warning("approval_applier: admission_gate raised: %s", e)
            return {"pass": True,
                    "verdict": f"PASS [gate raised: {e}]",
                    "stages": {}}
        if not isinstance(r, dict) or not r.get("ok"):
            err = r.get("error") if isinstance(r, dict) else "non-dict"
            log.warning("approval_applier: admission_gate not ok: %s", err)
            return {"pass": True,
                    "verdict": f"PASS [gate not ok: {err}]",
                    "stages": {}}
        return {
            "pass": bool(r.get("pass")),
            "verdict": r.get("verdict", ""),
            "stages": r.get("stages", {}),
        }

    # -- bus surface ----------------------------------------------

    async def handle(self, msg) -> dict:
        """Manual entry for tests / ad-hoc invocations. Does NOT publish events."""
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        op = payload.get("op", "apply_draft")
        if op != "apply_draft":
            return {"ok": False, "error": f"unknown op: {op!r}"}
        draft_path_s = payload.get("draft_path")
        target = payload.get("proposed_target")
        action = payload.get("proposed_action")
        if not draft_path_s or not target:
            return {"ok": False, "error": "missing draft_path or proposed_target"}
        if action not in ALLOWED_ACTIONS:
            return {"ok": False, "error": f"invalid proposed_action: {action!r}"}

        draft_path = Path(draft_path_s)
        if not draft_path.exists():
            return {"ok": False, "error": f"draft missing: {draft_path}"}
        raw = draft_path.read_text(encoding="utf-8")
        inner = _strip_outer_frontmatter(raw)
        if inner is None:
            return {"ok": False, "error": "draft has no outer frontmatter"}

        memory_root = draft_path.parent.parent
        target_path = memory_root / target
        if action == "add" and target_path.exists():
            return {"ok": False, "error": f"add refused: {target_path} exists"}
        if action == "update" and not target_path.exists():
            return {"ok": False, "error": f"update refused: {target_path} missing"}

        if action == "update":
            inner = _stamp_probation_on_update(inner)

        _atomic_write(target_path, inner)
        try:
            draft_path.unlink()
        except OSError:
            pass
        return {"ok": True, "target_path": str(target_path)}
