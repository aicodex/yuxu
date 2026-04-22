"""SkillPicker — bus surface over SkillRegistry.

Discovers skills across three scopes (global, project, agent) using the
SkillRegistry library, then exposes catalog/load/enable/disable via bus ops.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .registry import SkillRegistry, SkillScope, installed_skills_bundled_root

log = logging.getLogger(__name__)

DEFAULT_GLOBAL_ENABLE = Path("config/skills_enabled.yaml")


class SkillPicker:
    def __init__(self, bus, loader, *,
                 global_root: Path | str | None = None,
                 global_enable_file: Path | str = DEFAULT_GLOBAL_ENABLE) -> None:
        self.bus = bus
        self.loader = loader
        self.global_root = Path(global_root) if global_root is not None \
            else installed_skills_bundled_root()
        self.global_enable_file = Path(global_enable_file)
        self.registry = SkillRegistry()
        # project scopes can be added on demand (projects aren't first-class yet)
        self._extra_projects: list[tuple[Path, str]] = []
        self.rescan()

    def _build_scopes(self) -> list[SkillScope]:
        scopes: list[SkillScope] = [
            SkillScope.global_scope(self.global_root, self.global_enable_file),
        ]
        for agent_name, spec in self.loader.specs.items():
            if (spec.path / "skills").exists():
                scopes.append(SkillScope.agent(spec.path, agent_name))
        for pdir, pid in self._extra_projects:
            scopes.append(SkillScope.project(pdir, pid))
        return scopes

    def rescan(self, extra_projects: Optional[list[tuple[Path, str]]] = None) -> int:
        if extra_projects is not None:
            self._extra_projects = list(extra_projects)
        self.registry.scan(self._build_scopes())
        return len(self.registry.skills)

    async def handle(self, msg) -> dict:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        op = payload.get("op", "catalog")
        try:
            if op == "catalog":
                skills = self.registry.catalog(
                    for_agent=payload.get("for_agent"),
                    for_project=payload.get("for_project"),
                    only_enabled=payload.get("only_enabled", True),
                    triggers_any=payload.get("triggers_any"),
                )
                return {"ok": True, "skills": skills}

            if op == "load":
                if "name" not in payload:
                    return {"ok": False, "error": "name required"}
                data = self.registry.load(
                    payload["name"],
                    for_agent=payload.get("for_agent"),
                    for_project=payload.get("for_project"),
                    only_enabled=payload.get("only_enabled", True),
                )
                return {"ok": True, **data}

            if op == "enable":
                if "name" not in payload or "scope" not in payload:
                    return {"ok": False, "error": "name and scope required"}
                self.registry.enable(
                    payload["name"],
                    scope=payload["scope"],
                    owner=payload.get("owner"),
                )
                return {"ok": True}

            if op == "disable":
                if "name" not in payload or "scope" not in payload:
                    return {"ok": False, "error": "name and scope required"}
                self.registry.disable(
                    payload["name"],
                    scope=payload["scope"],
                    owner=payload.get("owner"),
                )
                return {"ok": True}

            if op == "list_all":
                return {"ok": True, "skills": self.registry.list_all()}

            if op == "rescan":
                extra = payload.get("extra_projects")
                if extra is not None:
                    extra_list = [(Path(p), pid) for p, pid in extra]
                else:
                    extra_list = None
                count = self.rescan(extra_projects=extra_list)
                return {"ok": True, "count": count}

            return {"ok": False, "error": f"unknown op: {op!r}"}
        except KeyError as e:
            return {"ok": False, "error": str(e)}
        except (TypeError, ValueError) as e:
            return {"ok": False, "error": f"bad request: {e}"}
