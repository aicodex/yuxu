"""Pairing registry — per-platform allowlist + pending-approval queue.

When a platform is in `pairing_required` mode, the first inbound message
from any user_id not in `allowed` gets:
  - recorded in `pending` with the first message snippet + timestamp
  - `gateway.pairing_requested` event fan-out for admin visibility
  - **held** (not published as gateway.user_message)

Admin approves via CLI (`yuxu pair approve`) or programmatically
(`registry.approve_pending()`); from then on the user is allowed.

Storage: yaml at `<project>/config/secrets/pairings.yaml`
(already gitignored by the scaffolded .gitignore).

Layout:
    feishu:
      allowed:
        - user_id: ou_abc
          approved_at: "2026-04-21T..."
          note: "Alice"
      pending:
        - user_id: ou_xyz
          first_seen: "2026-04-21T..."
          first_message: "hi"
    telegram:
      allowed: [...]
      pending: [...]
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger(__name__)

DEFAULT_PAIRING_PATH = Path("config/secrets/pairings.yaml")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PendingEntry:
    user_id: str
    platform: str
    first_seen: str = field(default_factory=_now_iso)
    first_message: str = ""
    chat_id: Optional[str] = None
    notified_at: Optional[str] = None   # last time we replied "still pending"

    def as_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "first_seen": self.first_seen,
            "first_message": self.first_message,
            "chat_id": self.chat_id,
            "notified_at": self.notified_at,
        }


@dataclass
class AllowedEntry:
    user_id: str
    platform: str
    approved_at: str = field(default_factory=_now_iso)
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "approved_at": self.approved_at,
            "note": self.note,
        }


class PairingRegistry:
    """File-backed allowlist + pending registry.

    Thread-unsafe. Intended to be held by a single GatewayManager within the
    daemon process; the CLI reloads on each invocation.
    """

    def __init__(self, path: Path | str = DEFAULT_PAIRING_PATH) -> None:
        self.path = Path(path)
        self._allowed: dict[str, dict[str, AllowedEntry]] = {}  # platform -> user_id -> entry
        self._pending: dict[str, dict[str, PendingEntry]] = {}
        self._last_mtime: float = 0.0
        self.reload()
        self._last_mtime = self._current_mtime()

    # -- mtime-based hot reload ------------------------------

    def _current_mtime(self) -> float:
        try:
            return self.path.stat().st_mtime
        except FileNotFoundError:
            return 0.0

    def reload_if_changed(self) -> bool:
        """Re-read the yaml iff its mtime changed since the last read/write.

        Returns True if a reload actually happened, False otherwise. Used by
        the gateway's pairing-watcher task so CLI-side `yuxu pair approve`
        takes effect inside a running daemon without a restart.
        """
        m = self._current_mtime()
        if m == self._last_mtime:
            return False
        self.reload()
        self._last_mtime = m
        return True

    # -- persistence -----------------------------------------

    def reload(self) -> None:
        self._allowed.clear()
        self._pending.clear()
        if not self.path.exists():
            return
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            log.exception("pairing: bad yaml at %s; starting empty", self.path)
            return
        if not isinstance(data, dict):
            return
        for platform, section in data.items():
            if not isinstance(section, dict):
                continue
            for raw in section.get("allowed") or []:
                if not isinstance(raw, dict) or "user_id" not in raw:
                    continue
                e = AllowedEntry(
                    user_id=str(raw["user_id"]),
                    platform=platform,
                    approved_at=str(raw.get("approved_at") or _now_iso()),
                    note=str(raw.get("note") or ""),
                )
                self._allowed.setdefault(platform, {})[e.user_id] = e
            for raw in section.get("pending") or []:
                if not isinstance(raw, dict) or "user_id" not in raw:
                    continue
                e = PendingEntry(
                    user_id=str(raw["user_id"]),
                    platform=platform,
                    first_seen=str(raw.get("first_seen") or _now_iso()),
                    first_message=str(raw.get("first_message") or ""),
                    chat_id=raw.get("chat_id"),
                    notified_at=raw.get("notified_at"),
                )
                self._pending.setdefault(platform, {})[e.user_id] = e

    def save(self) -> None:
        out: dict = {}
        for platform in sorted(set(self._allowed) | set(self._pending)):
            section: dict = {}
            if self._allowed.get(platform):
                section["allowed"] = [
                    e.as_dict() for e in sorted(
                        self._allowed[platform].values(),
                        key=lambda x: x.approved_at,
                    )
                ]
            if self._pending.get(platform):
                section["pending"] = [
                    e.as_dict() for e in sorted(
                        self._pending[platform].values(),
                        key=lambda x: x.first_seen,
                    )
                ]
            out[platform] = section
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            yaml.safe_dump(out, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        self._last_mtime = self._current_mtime()

    # -- queries ---------------------------------------------

    def is_allowed(self, platform: str, user_id: str) -> bool:
        return bool(user_id) and user_id in self._allowed.get(platform, {})

    def list_allowed(self, platform: Optional[str] = None) -> list[AllowedEntry]:
        out: list[AllowedEntry] = []
        for p, per_platform in self._allowed.items():
            if platform and p != platform:
                continue
            out.extend(per_platform.values())
        return sorted(out, key=lambda e: (e.platform, e.user_id))

    def list_pending(self, platform: Optional[str] = None) -> list[PendingEntry]:
        out: list[PendingEntry] = []
        for p, per_platform in self._pending.items():
            if platform and p != platform:
                continue
            out.extend(per_platform.values())
        return sorted(out, key=lambda e: (e.platform, e.first_seen))

    # -- mutations -------------------------------------------

    def add_pending(self, platform: str, user_id: str, *,
                    first_message: str = "",
                    chat_id: Optional[str] = None) -> PendingEntry:
        existing = self._pending.get(platform, {}).get(user_id)
        if existing is not None:
            # keep the first record; don't clobber timestamps
            return existing
        entry = PendingEntry(
            user_id=user_id, platform=platform,
            first_message=first_message, chat_id=chat_id,
        )
        self._pending.setdefault(platform, {})[user_id] = entry
        self.save()
        return entry

    def approve_pending(self, platform: str, user_id: str, *,
                         note: str = "") -> AllowedEntry:
        """Move from pending → allowed. If not pending, still add to allowed."""
        self._pending.get(platform, {}).pop(user_id, None)
        entry = AllowedEntry(user_id=user_id, platform=platform, note=note)
        self._allowed.setdefault(platform, {})[user_id] = entry
        self.save()
        return entry

    def reject_pending(self, platform: str, user_id: str) -> bool:
        removed = self._pending.get(platform, {}).pop(user_id, None)
        if removed is not None:
            self.save()
            return True
        return False

    def revoke_allowed(self, platform: str, user_id: str) -> bool:
        removed = self._allowed.get(platform, {}).pop(user_id, None)
        if removed is not None:
            self.save()
            return True
        return False

    def mark_notified(self, platform: str, user_id: str) -> None:
        entry = self._pending.get(platform, {}).get(user_id)
        if entry is not None:
            entry.notified_at = _now_iso()
            self.save()

    def allow(self, platform: str, user_id: str, *, note: str = "") -> AllowedEntry:
        """Pre-allow a user without them ever having been pending.

        Typical usage: admin pre-provisions known users before rollout,
        or test scripts seed the allowlist.
        """
        return self.approve_pending(platform, user_id, note=note)
