"""invoke_skill — LLM-side tool that loads a SKILL.md body on demand.

Designed as the fetcher half of the progressive-disclosure pair:
- `skill_index.list` (L1) renders `<available_skills>` into the LLM's
  prompt as an attachment.
- `invoke_skill` (this) is exposed as a tool-use function. When the LLM
  picks a skill from the catalog, it tool-calls here with
  `{"name": "<skill>"}` and the tool returns the full body for that
  skill by delegating to `skill_index.read`.

Why not let the LLM call `skill_index` directly?
- `skill_index` is an ops-style skill (`op` = stats/list/read) and that
  shape leaks through `llm_driver`'s `{"op": "execute", "input": ...}`
  envelope; asking the model to produce the nested shape is fragile.
- Keeping a dedicated `invoke_skill` tool aligns yuxu with Claude Code's
  `SkillTool` contract: one tool, one argument, body flows back via the
  normal tool_result round-trip.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

NAME = "invoke_skill"

# Ported verbatim from Claude Code 2.1.88 `tools/SkillTool/prompt.ts`
# (L173-195) with yuxu-specific adaptations only:
#   - `Skill tool` / `this tool` → `invoke_skill tool`
#   - Example skill names switched to yuxu bundled skills
#   - Dropped the `built-in CLI commands` bullet (yuxu has no /help, /clear
#     CLI shortcuts competing with skills)
#   - Dropped the slash-command paragraph (yuxu's chat gateway is not
#     slash-command-invoked yet; revisit when classify_intent routes
#     natural language to skills)
#   - `<COMMAND_NAME_TAG>`-in-user-msg sentinel → "a previous invoke_skill
#     tool call in this turn already returned a SKILL.md body" (yuxu's
#     skill bodies enter the conversation via tool_result, not user msg)
DESCRIPTION = """Execute a skill within the main conversation

When users ask you to perform tasks, check if any of the available skills match. Skills provide specialized capabilities and domain knowledge.

How to invoke:
- Set `name` to the exact name of an available skill (no leading slash).
- Examples:
  - `name: "memory"` - invoke the memory skill
  - `name: "session_compressor"` - invoke the session_compressor skill
  - `name: "skill_index"` - invoke the skill_index skill

Important:
- Available skills are listed in system-reminder messages in the conversation
- When a skill matches the user's request, this is a BLOCKING REQUIREMENT: invoke the relevant Skill tool BEFORE generating any other response about the task
- NEVER mention a skill without actually calling this tool
- Do not invoke a skill that is already running
- If a previous invoke_skill tool call in this turn has already returned a SKILL.md body for a skill, that skill has ALREADY been loaded - follow the instructions in that body directly instead of calling this tool again
"""

TOOL_SCHEMA: dict = {
    "name": NAME,
    "description": DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Skill name from the `<available_skills>` catalog "
                    "(exact match, e.g. `memory` or `session_compressor`)."
                ),
            },
        },
        "required": ["name"],
    },
}


def _unwrap_args(input: Any) -> Optional[dict]:
    """Tolerate both direct calls (`{"name": ...}`) and llm_driver-wrapped
    calls (`{"op": "execute", "input": {"name": ...}}`). Returns None on
    malformed input."""
    if not isinstance(input, dict):
        return None
    if input.get("op") == "execute" and isinstance(input.get("input"), dict):
        return input["input"]
    return input


async def execute(input: dict, ctx) -> dict:
    args = _unwrap_args(input)
    if args is None:
        return {"ok": False, "error": "invalid input shape"}
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        return {"ok": False, "error": "missing field: name"}
    name = name.strip()

    try:
        result = await ctx.bus.request(
            "skill_index",
            {"op": "read", "name": name},
        )
    except Exception as e:
        log.exception("invoke_skill: bus.request(skill_index) raised")
        return {"ok": False, "error": f"skill_index request failed: {e}"}

    if not isinstance(result, dict) or not result.get("ok"):
        err = (result or {}).get("error", "skill_index read failed") \
            if isinstance(result, dict) else "skill_index returned non-dict"
        return {"ok": False, "error": err}

    return {
        "ok": True,
        "name": result.get("name", name),
        "kind": result.get("kind"),
        "location": result.get("location"),
        "frontmatter": result.get("frontmatter", {}),
        "body": result.get("body", ""),
    }
