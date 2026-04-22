"""create_project skill — scaffold a new yuxu project on disk."""
from __future__ import annotations

import json
from pathlib import Path

from .._shared import (
    DEFAULT_RATE_LIMITS,
    DEFAULT_SKILLS_ENABLED,
    DEFAULT_YUXU_JSON,
    PROJECT_GITIGNORE,
    copy_bundled_into,
    register_project_in_home,
)


def create_project(target: Path | str, *, force: bool = False) -> Path:
    """Scaffold a project at `target`. Returns the resolved project path.

    Raises FileExistsError if `target/yuxu.json` already exists and force=False.
    """
    from yuxu import __version__ as ver

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

    manifest = copy_bundled_into(target / "_system")

    (target / ".yuxu" / "version").write_text(ver + "\n", encoding="utf-8")
    (target / ".yuxu" / "manifest.json").write_text(
        json.dumps({"yuxu_version": ver, "bundled": manifest}, indent=2,
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    register_project_in_home(target)
    return target


async def execute(input: dict, ctx) -> dict:
    """Skill protocol entry. Wraps create_project() with error→dict translation."""
    if "dir" not in input:
        return {"ok": False, "error": "missing field: dir"}
    try:
        p = create_project(input["dir"], force=bool(input.get("force", False)))
    except FileExistsError as e:
        return {"ok": False, "error": f"already exists: {e}"}
    except (TypeError, ValueError, OSError) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "path": str(p)}
