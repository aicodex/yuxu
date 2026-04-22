"""create_agent skill — scaffold a new user agent under <project>/agents/."""
from __future__ import annotations

import shutil
from pathlib import Path

from .._shared import templates_source


def create_agent(project_dir: Path | str, name: str, *,
                 template: str = "default") -> Path:
    """Copy the agent template into project_dir/agents/name/.

    `template` is a subfolder under yuxu/templates/. "default" → templates/agent/.
    Raises FileNotFoundError / FileExistsError on the obvious shape errors.
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
        template_src = templates_source() / "agent"
    else:
        template_src = templates_source() / template
    if not template_src.exists():
        raise FileNotFoundError(f"template not found: {template_src}")

    shutil.copytree(
        template_src, agent_dir,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    init = agent_dir / "__init__.py"
    if init.exists():
        init.write_text(
            init.read_text(encoding="utf-8").replace('NAME = "my_agent"',
                                                      f'NAME = "{name}"'),
            encoding="utf-8",
        )
    return agent_dir


async def execute(input: dict, ctx) -> dict:
    """Skill protocol entry."""
    for k in ("project_dir", "name"):
        if k not in input:
            return {"ok": False, "error": f"missing field: {k}"}
    try:
        p = create_agent(
            input["project_dir"],
            input["name"],
            template=input.get("template", "default"),
        )
    except FileExistsError as e:
        return {"ok": False, "error": f"already exists: {e}"}
    except FileNotFoundError as e:
        return {"ok": False, "error": f"not found: {e}"}
    except (TypeError, ValueError, OSError) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "path": str(p)}
