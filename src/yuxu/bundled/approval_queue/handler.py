"""ApprovalQueue — destructive-action approval wall.

v0.1: no grace period, no auto-expiry. Each approval sits until user explicitly
approves or rejects. Persisted via checkpoint_store so restarts don't lose
pending items.

Callers (requester pattern):
  1. aid = await bus.request("approval_queue",
         {"op": "enqueue", "action": "delete_memory", "detail": {...}})
  2. subscribe to "approval_queue.decided", filter on aid, branch on decision

Users (decider pattern):
  - list pending via bus.request("approval_queue", {"op": "list", "status": "pending"})
  - approve/reject with approval_id from that list
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

NAME = "approval_queue"
NAMESPACE = "approval_queue"
STATE_KEY = "state"


class ApprovalQueue:
    def __init__(self, bus) -> None:
        self.bus = bus
        self._queue: dict[str, dict[str, Any]] = {}

    async def load_state(self) -> None:
        """Populate _queue from checkpoint_store. Silent if nothing saved yet."""
        r = await self.bus.request(
            "checkpoint_store",
            {"op": "load", "namespace": NAMESPACE, "key": STATE_KEY},
            timeout=5.0,
        )
        if r.get("ok") and isinstance(r.get("data"), dict):
            q = r["data"].get("queue")
            if isinstance(q, dict):
                self._queue = q

    async def _persist(self) -> None:
        await self.bus.request(
            "checkpoint_store",
            {"op": "save", "namespace": NAMESPACE, "key": STATE_KEY,
             "data": {"queue": self._queue}},
            timeout=5.0,
        )

    async def enqueue(self, *, action: str, detail: Any, requester: str) -> dict:
        if not isinstance(action, str) or not action:
            return {"ok": False, "error": "action must be non-empty str"}
        approval_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        entry = {
            "approval_id": approval_id,
            "action": action,
            "detail": detail,
            "requester": requester,
            "status": "pending",
            "created_at": now,
            "decided_at": None,
            "reason": None,
        }
        self._queue[approval_id] = entry
        await self._persist()
        await self.bus.publish(f"{NAME}.pending", {
            "approval_id": approval_id,
            "action": action,
            "requester": requester,
            "detail": detail,
        })
        return {"ok": True, "approval_id": approval_id, "status": "pending"}

    async def _decide(self, approval_id: str, decision: str,
                      reason: Optional[str]) -> dict:
        entry = self._queue.get(approval_id)
        if entry is None:
            return {"ok": False, "error": f"no such approval: {approval_id}"}
        if entry["status"] != "pending":
            return {"ok": False,
                    "error": f"already {entry['status']}; cannot {decision}"}
        entry["status"] = decision
        entry["decided_at"] = datetime.now(timezone.utc).isoformat()
        entry["reason"] = reason
        await self._persist()
        common = {
            "approval_id": approval_id,
            "action": entry["action"],
            "requester": entry["requester"],
            "reason": reason,
        }
        await self.bus.publish(f"{NAME}.{decision}", common)
        await self.bus.publish(f"{NAME}.decided", {**common, "decision": decision})
        return {"ok": True, "approval_id": approval_id, "status": decision}

    async def approve(self, approval_id: str,
                      reason: Optional[str] = None) -> dict:
        return await self._decide(approval_id, "approved", reason)

    async def reject(self, approval_id: str,
                     reason: Optional[str] = None) -> dict:
        return await self._decide(approval_id, "rejected", reason)

    def _list(self, status_filter: Optional[str] = None) -> list[dict]:
        items = list(self._queue.values())
        if status_filter:
            items = [e for e in items if e["status"] == status_filter]
        return sorted(items, key=lambda e: e["created_at"])

    def _counts(self) -> dict:
        counts = {"pending": 0, "approved": 0, "rejected": 0}
        for e in self._queue.values():
            s = e.get("status", "pending")
            counts[s] = counts.get(s, 0) + 1
        return counts

    async def handle(self, msg) -> dict:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        op = payload.get("op", "status")
        try:
            if op == "enqueue":
                requester = payload.get("requester") or getattr(msg, "sender", None) or "unknown"
                return await self.enqueue(
                    action=payload.get("action", ""),
                    detail=payload.get("detail"),
                    requester=requester,
                )
            if op == "approve":
                aid = payload.get("approval_id")
                if not aid:
                    return {"ok": False, "error": "approval_id required"}
                return await self.approve(aid, payload.get("reason"))
            if op == "reject":
                aid = payload.get("approval_id")
                if not aid:
                    return {"ok": False, "error": "approval_id required"}
                return await self.reject(aid, payload.get("reason"))
            if op == "list":
                return {"ok": True, "items": self._list(payload.get("status"))}
            if op == "get":
                aid = payload.get("approval_id")
                if not aid:
                    return {"ok": False, "error": "approval_id required"}
                entry = self._queue.get(aid)
                if entry is None:
                    return {"ok": False, "error": f"no such approval: {aid}"}
                return {"ok": True, "item": entry}
            if op == "status":
                counts = self._counts()
                return {"ok": True, "pending_count": counts["pending"],
                        "total": len(self._queue), "counts": counts}
            return {"ok": False, "error": f"unknown op: {op!r}"}
        except (KeyError, TypeError, ValueError) as e:
            return {"ok": False, "error": f"bad request: {e}"}
