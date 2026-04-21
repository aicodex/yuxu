"""ProjectManager — one-source-of-truth for project & agent scaffolding.

Usable two ways:
1. As a library (from CLI, pre-daemon): call classmethods directly.
   Example: `ProjectManager.create_project(Path("/tmp/foo"))`
2. As a bus agent (daemon mode): `bus.request("project_manager", {op, ...})`.
   Instance holds `self.loader` for dynamic ops (start_agent, stop_agent, ...).
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
from pathlib import Path
from typing import Any, Optional

import yaml

log = logging.getLogger(__name__)

# -- template text (used when scaffolding a project) -------------

PROJECT_GITIGNORE = """# yuxu project
_system/
.yuxu/
data/
config/secrets/
*.log

# Python
__pycache__/
*.py[cod]
.venv/
venv/
"""

DEFAULT_YUXU_JSON = {
    "name": "",
    "yuxu_version": "",
    "scan_order": ["_system", "agents"],
    "skills_dir": "skills",
    "data_dir": "data",
}

DEFAULT_RATE_LIMITS = """# Rate-limit pools for this project.
# See https://github.com/aicodex/yuxu for the schema.
#
# Example:
# minimax:
#   max_concurrent: 5
#   rpm: 60
#   accounts:
#     - id: key1
#       api_key: your-key-here
#       base_url: https://api.minimaxi.com/v1
"""

DEFAULT_SKILLS_ENABLED = """# Enabled global-scope skills for this project.
enabled: []
"""


# -- helpers -----------------------------------------------------


def _bundled_source() -> Path:
    """Where shipped bundled agents live in the installed yuxu package."""
    import yuxu.bundled
    return Path(yuxu.bundled.__file__).parent


def _templates_source() -> Path:
    """Where shipped templates live in the installed yuxu package."""
    import yuxu
    return Path(yuxu.__file__).parent / "templates"


def _home_dir() -> Path:
    """~/.yuxu (or $YUXU_HOME). Mirrors cli.bootstrap.home_dir."""
    import os
    override = os.environ.get("YUXU_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".yuxu"


def _copy_bundled(into: Path) -> list[dict]:
    """Copy every bundled agent folder into `into/`. Returns a manifest."""
    src = _bundled_source()
    manifest: list[dict] = []
    into.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        dest = into / entry.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(entry, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        agent_md = dest / "AGENT.md"
        sha = hashlib.sha256(agent_md.read_bytes()).hexdigest()[:12] \
            if agent_md.exists() else None
        manifest.append({"name": entry.name, "agent_md_sha12": sha})
    return manifest


# -- main class --------------------------------------------------


class ProjectManager:
    def __init__(self, loader=None) -> None:
        self.loader = loader

    # -- static ops (work without daemon) ------------------------

    @classmethod
    def create_project(cls, target: Path | str, *, force: bool = False) -> Path:
        from ... import __version__ as ver

        target = Path(target).expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)

        yuxu_json = target / "yuxu.json"
        if yuxu_json.exists() and not force:
            raise FileExistsError(
                f"{yuxu_json} already exists. Use force=True to overwrite."
            )

        for rel in ["agents", "skills", "_system", "config",
                    "data/checkpoints", "data/logs", "data/memory", "data/sessions",
                    ".yuxu"]:
            (target / rel).mkdir(parents=True, exist_ok=True)
        for rel in ["agents/.gitkeep", "skills/.gitkeep"]:
            p = target / rel
            if not p.exists():
                p.write_text("")

        cfg = dict(DEFAULT_YUXU_JSON)
        cfg["name"] = target.name
        cfg["yuxu_version"] = ver
        yuxu_json.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        rl = target / "config" / "rate_limits.yaml"
        if not rl.exists():
            rl.write_text(DEFAULT_RATE_LIMITS, encoding="utf-8")
        se = target / "config" / "skills_enabled.yaml"
        if not se.exists():
            se.write_text(DEFAULT_SKILLS_ENABLED, encoding="utf-8")

        gi = target / ".gitignore"
        if not gi.exists():
            gi.write_text(PROJECT_GITIGNORE, encoding="utf-8")

        manifest = _copy_bundled(target / "_system")

        (target / ".yuxu" / "version").write_text(ver + "\n", encoding="utf-8")
        (target / ".yuxu" / "manifest.json").write_text(
            json.dumps({"yuxu_version": ver, "bundled": manifest}, indent=2,
                       ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        cls._register_in_home(target)
        return target

    @classmethod
    def create_agent(cls, project_dir: Path | str, name: str, *,
                     template: str = "default") -> Path:
        """Copy the agent template into project_dir/agents/name/.

        `template` is a subfolder under yuxu/templates/. For now only
        "default" (which resolves to templates/agent/) is supported — the
        kwarg exists for future expansion (e.g. llm-only, hybrid).
        """
        project_dir = Path(project_dir).expanduser().resolve()
        agents_root = project_dir / "agents"
        if not agents_root.exists():
            raise FileNotFoundError(
                f"{agents_root} does not exist; is {project_dir} a yuxu project?"
            )

        agent_dir = agents_root / name
        if agent_dir.exists():
            raise FileExistsError(f"{agent_dir} already exists")

        if template == "default":
            template_src = _templates_source() / "agent"
        else:
            template_src = _templates_source() / template
        if not template_src.exists():
            raise FileNotFoundError(f"template not found: {template_src}")

        shutil.copytree(
            template_src, agent_dir,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

        # Substitute `my_agent` placeholder in __init__.py NAME line with the
        # actual name so the scaffolded agent registers under `name` by default.
        init = agent_dir / "__init__.py"
        if init.exists():
            init.write_text(
                init.read_text(encoding="utf-8").replace('NAME = "my_agent"',
                                                          f'NAME = "{name}"'),
                encoding="utf-8",
            )
        return agent_dir

    @classmethod
    def list_projects(cls) -> list[dict]:
        home = _home_dir()
        path = home / "projects.yaml"
        if not path.exists():
            return []
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            return []
        paths = data.get("projects") or []
        out = []
        for p in paths:
            pp = Path(p)
            info: dict[str, Any] = {"path": str(pp), "exists": pp.exists()}
            cfg_path = pp / "yuxu.json"
            if cfg_path.exists():
                try:
                    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                    info["name"] = cfg.get("name")
                    info["yuxu_version"] = cfg.get("yuxu_version")
                except json.JSONDecodeError:
                    pass
            out.append(info)
        return out

    @classmethod
    def list_agents(cls, project_dir: Path | str) -> list[dict]:
        project_dir = Path(project_dir).expanduser().resolve()
        cfg_path = project_dir / "yuxu.json"
        if not cfg_path.exists():
            raise FileNotFoundError(f"no yuxu.json at {project_dir}")
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        scan_order = cfg.get("scan_order", ["_system", "agents"])

        agents: list[dict] = []
        for source in scan_order:
            src = project_dir / source
            if not src.exists():
                continue
            label = "bundled" if source == "_system" else "user"
            for agent_path in sorted(src.iterdir()):
                if not agent_path.is_dir() or agent_path.name.startswith((".", "_")):
                    continue
                if not ((agent_path / "AGENT.md").exists()
                        or (agent_path / "__init__.py").exists()):
                    continue
                agents.append({
                    "name": agent_path.name,
                    "source": label,
                    "path": str(agent_path),
                })
        return agents

    @classmethod
    def _register_in_home(cls, project_dir: Path) -> None:
        home = _home_dir()
        home.mkdir(parents=True, exist_ok=True)
        path = home / "projects.yaml"
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() \
                else {"projects": []}
        except (yaml.YAMLError, FileNotFoundError):
            data = {"projects": []}
        if not isinstance(data, dict):
            data = {"projects": []}
        projects = data.get("projects") or []
        proj_str = str(project_dir.resolve())
        if proj_str not in projects:
            projects.append(proj_str)
        data["projects"] = projects
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                        encoding="utf-8")

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
            if op == "create_project":
                p = self.create_project(payload["dir"],
                                        force=bool(payload.get("force", False)))
                return {"ok": True, "path": str(p)}
            if op == "create_agent":
                p = self.create_agent(
                    payload["project_dir"],
                    payload["name"],
                    template=payload.get("template", "default"),
                )
                return {"ok": True, "path": str(p)}
            if op == "list_projects":
                return {"ok": True, "projects": self.list_projects()}
            if op == "list_agents":
                return {"ok": True, "agents": self.list_agents(payload["project_dir"])}
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
        except FileExistsError as e:
            return {"ok": False, "error": f"already exists: {e}"}
        except FileNotFoundError as e:
            return {"ok": False, "error": f"not found: {e}"}
        except KeyError as e:
            return {"ok": False, "error": f"missing field: {e.args[0]}"}
        except (TypeError, ValueError) as e:
            return {"ok": False, "error": str(e)}
