"""RecoveryAgent — checkpoint inventory and GC.

Does NOT decide how to resume; that is the owning agent's job. We only
classify existing checkpoints by age and let callers act on the inventory.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


def _parse_iso(s: str) -> Optional[datetime]:
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


class RecoveryAgent:
    DEFAULT_FRESH_SEC = 3600
    DEFAULT_STALE_SEC = 24 * 3600

    def __init__(self, bus, *,
                 fresh_sec: Optional[int] = None,
                 stale_sec: Optional[int] = None) -> None:
        self.bus = bus
        self.fresh_sec = fresh_sec or int(
            os.environ.get("RECOVERY_FRESH_SEC", self.DEFAULT_FRESH_SEC))
        self.stale_sec = stale_sec or int(
            os.environ.get("RECOVERY_STALE_SEC", self.DEFAULT_STALE_SEC))
        self._inventory: dict = {}
        self._scanned_at: Optional[str] = None

    def _classify(self, age_sec: float) -> str:
        if age_sec < self.fresh_sec:
            return "fresh"
        if age_sec < self.stale_sec:
            return "stale"
        return "abandoned"

    async def scan(self) -> dict:
        now = datetime.now(timezone.utc)
        inventory: dict[str, dict] = {}
        counts = {"fresh": 0, "stale": 0, "abandoned": 0, "unknown": 0}

        ns_reply = await self.bus.request(
            "checkpoint_store", {"op": "list_namespaces"}, timeout=10.0)
        if not ns_reply.get("ok"):
            return {"ok": False, "error": f"list_namespaces failed: {ns_reply.get('error')}"}

        for ns in ns_reply.get("namespaces", []):
            keys_reply = await self.bus.request(
                "checkpoint_store", {"op": "list", "namespace": ns}, timeout=10.0)
            if not keys_reply.get("ok"):
                continue
            ns_entries = []
            for key in keys_reply.get("keys", []):
                load_reply = await self.bus.request(
                    "checkpoint_store",
                    {"op": "load", "namespace": ns, "key": key},
                    timeout=10.0,
                )
                if not load_reply.get("ok"):
                    continue
                saved_at = load_reply.get("saved_at")
                parsed = _parse_iso(saved_at) if saved_at else None
                if parsed is None:
                    category = "unknown"
                    age_sec = None
                else:
                    age_sec = (now - parsed).total_seconds()
                    category = self._classify(age_sec)
                counts[category] = counts.get(category, 0) + 1
                ns_entries.append({
                    "key": key,
                    "saved_at": saved_at,
                    "age_sec": age_sec,
                    "category": category,
                })
            inventory[ns] = {"keys": ns_entries}

        self._inventory = inventory
        self._scanned_at = now.isoformat()
        result = {
            "ok": True,
            "scanned_at": self._scanned_at,
            "inventory": inventory,
            "counts": counts,
        }
        await self.bus.publish("recovery_agent.scan_complete", {
            "scanned_at": self._scanned_at,
            "counts": counts,
            "namespaces": sorted(inventory.keys()),
        })
        return result

    async def gc(self, max_age_days: float) -> dict:
        if max_age_days < 0:
            return {"ok": False, "error": "max_age_days must be >= 0"}
        cutoff_sec = max_age_days * 24 * 3600
        deleted: list[dict] = []
        # Re-scan is cheaper than trusting stale inventory, but we can skip if
        # caller just scanned. For simplicity, re-use existing inventory and
        # fall back to scanning if empty.
        if not self._inventory:
            await self.scan()
        for ns, info in list(self._inventory.items()):
            for entry in list(info["keys"]):
                age = entry.get("age_sec")
                if age is None or age < cutoff_sec:
                    continue
                r = await self.bus.request(
                    "checkpoint_store",
                    {"op": "delete", "namespace": ns, "key": entry["key"]},
                    timeout=10.0,
                )
                if r.get("ok"):
                    deleted.append({"namespace": ns, "key": entry["key"],
                                    "age_sec": age})
        # Refresh inventory so next report is consistent
        await self.scan()
        return {"ok": True, "deleted": deleted}

    async def handle(self, msg) -> dict:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        op = payload.get("op", "status")
        try:
            if op == "status":
                if not self._scanned_at:
                    return await self.scan()
                return {
                    "ok": True,
                    "scanned_at": self._scanned_at,
                    "inventory": self._inventory,
                    "counts": self._count_inventory(),
                }
            if op == "rescan":
                return await self.scan()
            if op == "gc":
                return await self.gc(float(payload.get("max_age_days", 30)))
            return {"ok": False, "error": f"unknown op: {op!r}"}
        except (KeyError, TypeError, ValueError) as e:
            return {"ok": False, "error": f"bad request: {e}"}

    def _count_inventory(self) -> dict:
        counts = {"fresh": 0, "stale": 0, "abandoned": 0, "unknown": 0}
        for info in self._inventory.values():
            for entry in info.get("keys", []):
                c = entry.get("category", "unknown")
                counts[c] = counts.get(c, 0) + 1
        return counts
