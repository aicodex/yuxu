"""list_projects skill — read ~/.yuxu/projects.yaml + hydrate each entry."""
from __future__ import annotations

from .._shared import hydrate_project_info, read_projects_yaml


def list_projects() -> list[dict]:
    return [hydrate_project_info(p) for p in read_projects_yaml()]


async def execute(input: dict, ctx) -> dict:
    return {"ok": True, "projects": list_projects()}
