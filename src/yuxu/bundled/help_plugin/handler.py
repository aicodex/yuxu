"""Help plugin — subscribes to gateway.command_invoked + replies with /cmd list."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

COMMAND = "/help"


class HelpPlugin:
    def __init__(self, ctx) -> None:
        self.ctx = ctx

    def install(self) -> None:
        self.ctx.bus.subscribe("gateway.command_invoked", self._on_command)

    async def _on_command(self, event: dict) -> None:
        payload = event.get("payload") if isinstance(event, dict) else None
        if not isinstance(payload, dict):
            return
        if payload.get("command") != COMMAND:
            return
        session_key = payload.get("session_key", "")
        args = (payload.get("args") or "").strip()
        try:
            r = await self.ctx.bus.request(
                "gateway", {"op": "list_commands"}, timeout=2.0,
            )
        except Exception as e:
            await self._reply(session_key, f"error: {e}")
            return
        commands = r.get("commands", {}) if isinstance(r, dict) else {}
        text = self._format(commands, selector=args)
        await self._reply(session_key, text)

    def _format(self, commands: dict, *, selector: str = "") -> str:
        if not commands:
            return "no commands registered"
        if selector and selector.startswith("/"):
            info = commands.get(selector)
            if info is None:
                return f"unknown command: {selector}"
            lines = [f"**{selector}**"]
            if info.get("help"):
                lines.append(info["help"])
            if info.get("agent"):
                lines.append(f"(handled by: {info['agent']})")
            return "\n".join(lines)
        # Full list
        lines = ["**Available commands**", ""]
        for cmd in sorted(commands.keys()):
            info = commands[cmd]
            lines.append(f"  {cmd}  —  {info.get('help', '')}")
        lines.append("")
        lines.append("Tip: `/help /cmd` for details on one command.")
        return "\n".join(lines)

    async def _reply(self, session_key: str, text: str) -> None:
        if not session_key:
            return
        try:
            await self.ctx.bus.request(
                "gateway",
                {"op": "send", "session_key": session_key, "text": text},
                timeout=5.0,
            )
        except Exception:
            log.exception("help_plugin: reply failed")
