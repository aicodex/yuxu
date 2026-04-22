"""HarnessProMax — v0 agent-creator agent.

Listens for `/new <description>` slash commands on the gateway, drives the
classify_intent → generate_agent_md → write-to-disk → loader.scan() →
ensure_running flow, and replies via gateway.

The two LLM-mediated skills are imported as Python modules (skill_picker
doesn't yet dispatch skill execution over the bus). This is fine for a
bundled agent — we know the skills ship in the same package.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from yuxu.skills_bundled.classify_intent.handler import execute as classify_intent
from yuxu.skills_bundled.generate_agent_md.handler import execute as generate_agent_md

log = logging.getLogger(__name__)

COMMAND = "/new"
COMMAND_HELP = ("Create a new yuxu agent from a natural-language description "
                "(v0: LLM-only agents). Usage: `/new <description>`.")


def _find_project_root(start: Path) -> Optional[Path]:
    """Walk up from `start` looking for a directory containing yuxu.json."""
    for cand in (start, *start.parents):
        if (cand / "yuxu.json").exists():
            return cand
    return None


class HarnessProMax:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self._registered_command = False

    async def install(self) -> None:
        self.ctx.bus.subscribe("gateway.command_invoked", self._on_command)
        try:
            r = await self.ctx.bus.request("gateway", {
                "op": "register_command",
                "command": COMMAND,
                "agent": "harness_pro_max",
                "help": COMMAND_HELP,
            }, timeout=2.0)
            if isinstance(r, dict) and r.get("ok"):
                self._registered_command = True
            else:
                log.warning("harness_pro_max: register_command failed: %s", r)
        except Exception:
            log.exception("harness_pro_max: register_command raised")

    async def uninstall(self) -> None:
        try:
            self.ctx.bus.unsubscribe("gateway.command_invoked", self._on_command)
        except Exception:
            pass
        if self._registered_command:
            try:
                await self.ctx.bus.request("gateway", {
                    "op": "unregister_command", "command": COMMAND,
                }, timeout=2.0)
            except Exception:
                pass
            self._registered_command = False

    # -- event handlers --------------------------------------------

    async def _on_command(self, event: dict) -> None:
        payload = event.get("payload") if isinstance(event, dict) else None
        if not isinstance(payload, dict) or payload.get("command") != COMMAND:
            return
        session_key = payload.get("session_key", "")
        description = (payload.get("args") or "").strip()
        if not description:
            await self._reply(session_key,
                              f"Usage: `{COMMAND} <description>`\n\n{COMMAND_HELP}")
            return
        result = await self.create_agent_from_description(description)
        await self._reply(session_key, self._format_reply(result))

    # -- core flow -------------------------------------------------

    async def create_agent_from_description(
        self,
        description: str,
        *,
        project_dir: Optional[Path | str] = None,
        name_override: Optional[str] = None,
    ) -> dict:
        """End-to-end: classify → generate → write → ensure_running."""
        # 1. classify
        cls_resp = await classify_intent(
            {"description": description}, ctx=self.ctx,
        )
        if not cls_resp.get("ok"):
            return {"ok": False, "stage": "classify_intent",
                    "error": cls_resp.get("error"), "raw": cls_resp.get("raw"),
                    "parsed": cls_resp.get("parsed")}
        classification = cls_resp["classification"]

        # 2. v0 forces driver=llm regardless of classifier suggestion
        warnings: list[str] = []
        original_driver = classification.get("driver")
        if original_driver != "llm":
            warnings.append(
                f"v0 forces driver=llm (classifier suggested {original_driver!r}); "
                "extend harness_pro_max to scaffold handler.py for python/hybrid"
            )

        name = name_override or classification["suggested_name"]
        depends_on = list(classification.get("depends_on") or [])
        run_mode = classification.get("run_mode", "one_shot")

        # 3. project root
        if project_dir is None:
            root = _find_project_root(Path(self.ctx.agent_dir).resolve())
            if root is None:
                return {"ok": False, "stage": "find_project_root",
                        "error": ("could not locate yuxu.json walking up from "
                                  f"{self.ctx.agent_dir}; pass project_dir explicitly")}
        else:
            root = Path(project_dir).expanduser().resolve()
            if not (root / "yuxu.json").exists():
                return {"ok": False, "stage": "find_project_root",
                        "error": f"{root} is not a yuxu project (no yuxu.json)"}

        # 4. conflict check
        agent_dir = root / "agents" / name
        if agent_dir.exists():
            return {"ok": False, "stage": "conflict",
                    "error": f"agent dir already exists: {agent_dir}",
                    "name": name}

        # 5. generate AGENT.md
        gen_resp = await generate_agent_md({
            "name": name,
            "description": description,
            "run_mode": run_mode,
            "driver": "llm",
            "depends_on": depends_on,
            "scope": "user",
            "extra_hints": classification.get("reasoning", ""),
        }, ctx=self.ctx)
        if not gen_resp.get("ok"):
            return {"ok": False, "stage": "generate_agent_md",
                    "error": gen_resp.get("error"), "raw": gen_resp.get("raw"),
                    "name": name}
        warnings.extend(gen_resp.get("warnings") or [])

        # 6. write to disk
        try:
            agent_dir.mkdir(parents=True, exist_ok=False)
            (agent_dir / "AGENT.md").write_text(gen_resp["agent_md"],
                                                 encoding="utf-8")
        except FileExistsError:
            return {"ok": False, "stage": "write",
                    "error": f"race: {agent_dir} appeared between check and create"}
        except OSError as e:
            return {"ok": False, "stage": "write", "error": str(e)}

        # 7. rescan + start
        try:
            await self.ctx.loader.scan()
            status = await self.ctx.loader.ensure_running(name)
        except Exception as e:
            log.exception("harness_pro_max: ensure_running %s failed", name)
            return {"ok": False, "stage": "ensure_running",
                    "error": str(e), "name": name, "path": str(agent_dir),
                    "agent_md_written": True, "warnings": warnings}

        return {"ok": True, "name": name, "path": str(agent_dir),
                "status": status, "classification": classification,
                "warnings": warnings, "usage": {
                    "classify": cls_resp.get("usage"),
                    "generate": gen_resp.get("usage"),
                }}

    # -- bus surface ----------------------------------------------

    async def handle(self, msg) -> dict:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        op = payload.get("op", "create_agent")
        if op != "create_agent":
            return {"ok": False, "error": f"unknown op: {op!r}"}
        if "description" not in payload:
            return {"ok": False, "error": "missing field: description"}
        return await self.create_agent_from_description(
            payload["description"],
            project_dir=payload.get("project_dir"),
            name_override=payload.get("name"),
        )

    # -- reply formatting -----------------------------------------

    def _format_reply(self, result: dict) -> str:
        if not result.get("ok"):
            stage = result.get("stage", "?")
            err = result.get("error", "(no error message)")
            extra = ""
            if result.get("raw"):
                # truncate to keep the reply small
                raw = str(result["raw"])
                extra = f"\n\nRaw output (truncated):\n```\n{raw[:500]}\n```"
            return f"❌ /new failed at `{stage}`: {err}{extra}"

        warnings = result.get("warnings") or []
        warn_block = ""
        if warnings:
            warn_block = "\n\n**Warnings**:\n" + "\n".join(f"- {w}" for w in warnings)
        return (
            f"✅ Created agent `{result['name']}` (status: {result.get('status')})\n"
            f"Path: `{result['path']}`{warn_block}"
        )

    async def _reply(self, session_key: str, text: str) -> None:
        if not session_key:
            return
        try:
            await self.ctx.bus.request("gateway", {
                "op": "send", "session_key": session_key, "text": text,
            }, timeout=5.0)
        except Exception:
            log.exception("harness_pro_max: reply failed")
