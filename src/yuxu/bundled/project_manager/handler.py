"""ProjectManager — runtime supervisor surface for the loader.

Purely dynamic ops (start/stop/restart/get_state). All scaffolding logic
(create_project / create_agent / list_projects / list_agents) lives in the
`yuxu.bundled.*` skills — invoke those from the CLI directly, or via the
LLM through `bus.request("{name}", ...)` (unified Loader handles dispatch).
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)


class ProjectManager:
    def __init__(self, loader=None) -> None:
        self.loader = loader

    # -- dynamic ops (need a running loader) ---------------------

    async def start_agent(self, name: str) -> dict:
        if self.loader is None:
            return {"ok": False, "error": "not running inside yuxu daemon"}
        try:
            status = await self.loader.ensure_running(name)
            return {"ok": True, "status": status}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def stop_agent(self, name: str, cascade: bool = False) -> dict:
        if self.loader is None:
            return {"ok": False, "error": "not running inside yuxu daemon"}
        try:
            await self.loader.stop(name, cascade=cascade)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def restart_agent(self, name: str) -> dict:
        if self.loader is None:
            return {"ok": False, "error": "not running inside yuxu daemon"}
        try:
            status = await self.loader.restart(name)
            return {"ok": True, "status": status}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_state(self, name: Optional[str] = None) -> dict:
        if self.loader is None:
            return {"ok": False, "error": "not running inside yuxu daemon"}
        return {"ok": True, "state": self.loader.get_state(name)}

    # -- bus op dispatcher --------------------------------------

    async def handle(self, msg) -> dict:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        op = payload.get("op")
        try:
            if op == "start_agent":
                return await self.start_agent(payload["name"])
            if op == "stop_agent":
                return await self.stop_agent(
                    payload["name"],
                    cascade=bool(payload.get("cascade", False)),
                )
            if op == "restart_agent":
                return await self.restart_agent(payload["name"])
            if op == "get_state":
                return self.get_state(payload.get("name"))
            return {"ok": False, "error": f"unknown op: {op!r}"}
        except KeyError as e:
            return {"ok": False, "error": f"missing field: {e.args[0]}"}
        except (TypeError, ValueError) as e:
            return {"ok": False, "error": str(e)}
