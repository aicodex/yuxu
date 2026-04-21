"""skill_picker bundled agent."""
from __future__ import annotations

from .handler import SkillPicker
from .registry import SkillRegistry, SkillScope, SkillSpec, default_scopes

NAME = "skill_picker"

__all__ = ["SkillPicker", "SkillRegistry", "SkillScope", "SkillSpec",
           "default_scopes", "NAME", "start", "get_handle"]

_picker: SkillPicker | None = None


async def start(ctx) -> None:
    global _picker
    _picker = SkillPicker(ctx.bus, ctx.loader)
    ctx.bus.register(NAME, _picker.handle)
    await ctx.ready()


def get_handle(ctx):
    return _picker
