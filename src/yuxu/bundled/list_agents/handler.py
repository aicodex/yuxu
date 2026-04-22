"""list_agents skill — enumerate bundled + user agents in a yuxu project."""
from __future__ import annotations

import json
from pathlib import Path


def list_agents(project_dir: Path | str) -> list[dict]:
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


async def execute(input: dict, ctx) -> dict:
    if "project_dir" not in input:
        return {"ok": False, "error": "missing field: project_dir"}
    try:
        agents = list_agents(input["project_dir"])
    except FileNotFoundError as e:
        return {"ok": False, "error": f"not found: {e}"}
    except (TypeError, ValueError, OSError, json.JSONDecodeError) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "agents": agents}
