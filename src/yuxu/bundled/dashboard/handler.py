"""Dashboard plugin handler.

Opens a DraftHandle per session on `/dashboard`; a background task refreshes
its content every `refresh_seconds`; any user message in that session (or
another slash-command) closes the dashboard with a finalize marking it
`📴 Exited`.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_REFRESH_SECONDS = float(os.environ.get("DASHBOARD_REFRESH_SEC", "1.0"))


class Dashboard:
    COMMAND = "/dashboard"

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self._active: dict[str, dict] = {}   # session_key -> {draft, task}
        self._refresh_seconds = DEFAULT_REFRESH_SECONDS

    def install(self) -> None:
        self.ctx.bus.subscribe("gateway.command_invoked", self._on_command)
        self.ctx.bus.subscribe("gateway.user_message", self._on_user_message)

    async def shutdown(self) -> None:
        for key in list(self._active.keys()):
            await self._stop(key)

    # -- bus handlers ---------------------------------------------

    async def _on_command(self, event: dict) -> None:
        payload = event.get("payload") if isinstance(event, dict) else None
        if not isinstance(payload, dict):
            return
        session_key = payload.get("session_key", "")
        command = payload.get("command")
        if not session_key:
            return
        if command == self.COMMAND:
            await self._start(session_key, payload)
        else:
            # Any OTHER slash command → exit existing dashboard in this session
            if session_key in self._active:
                await self._stop(session_key)

    async def _on_user_message(self, event: dict) -> None:
        payload = event.get("payload") if isinstance(event, dict) else None
        if not isinstance(payload, dict):
            return
        session_key = payload.get("session_key", "")
        if session_key in self._active:
            await self._stop(session_key)

    # -- lifecycle -----------------------------------------------

    async def _start(self, session_key: str, payload: dict) -> None:
        if session_key in self._active:
            # Restart: close previous, open new.
            await self._stop(session_key)
        gw = self.ctx.get_agent("gateway")
        if gw is None:
            log.warning("dashboard: gateway handle missing; cannot open")
            return
        try:
            source = payload.get("source") or {}
            draft = gw.open_draft(
                session_key=session_key,
                quote_user=source.get("user_id"),
                quote_text=payload.get("raw_text", self.COMMAND),
                footer_meta=self._footer_meta(status="🔄 Live"),
                throttle_seconds=max(0.1, self._refresh_seconds / 2),
            )
            await draft.open()
        except Exception:
            log.exception("dashboard: open_draft failed")
            return
        task = asyncio.create_task(
            self._refresh_loop(session_key, draft),
            name=f"dashboard.refresh.{session_key}",
        )
        self._active[session_key] = {"draft": draft, "task": task}

    async def _stop(self, session_key: str) -> None:
        entry = self._active.pop(session_key, None)
        if entry is None:
            return
        task = entry["task"]
        draft = entry["draft"]
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass
        # Final frame marks exit + freezes content
        try:
            draft.set_footer_meta(self._footer_meta(status="📴 Exited"))
            # Update content with an exit marker too so the freeze is obvious
            draft.set_content(self._collect_snapshot(exited=True))
            await draft.close()
        except Exception:
            log.exception("dashboard: close failed for %s", session_key)

    async def _refresh_loop(self, session_key: str, draft) -> None:
        while True:
            try:
                draft.set_content(self._collect_snapshot())
                draft.set_footer_meta(self._footer_meta(status="🔄 Live"))
                await draft.flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("dashboard: refresh iteration failed")
            try:
                await asyncio.sleep(self._refresh_seconds)
            except asyncio.CancelledError:
                raise

    # -- snapshot + footer ---------------------------------------

    def _collect_snapshot(self, *, exited: bool = False) -> str:
        loader = self.ctx.loader
        states = loader.get_state() if loader is not None else {}
        lines: list[str] = []
        lines.append(f"🗂 Project dashboard")
        # Bucket agent states
        status_of: dict[str, list[str]] = {}
        for name, st in sorted(states.items()):
            status_of.setdefault(st, []).append(name)
        for st in ("ready", "running", "loading", "idle",
                   "failed", "stopped", "unloaded"):
            names = status_of.get(st) or []
            if not names:
                continue
            lines.append(f"  [{st}] ({len(names)})")
            for n in names:
                lines.append(f"    • {n}")
        lines.append("")
        tag = "frozen" if exited else "live"
        lines.append(f"Updated: {datetime.now().isoformat(timespec='seconds')} · {tag}")
        return "\n".join(lines)

    def _footer_meta(self, *, status: str) -> list[tuple[str, str]]:
        return [
            ("Dashboard", status),
            ("Refresh", f"{self._refresh_seconds:g}s"),
        ]
