---
name: invoke_skill
version: "0.1.0"
author: yuxu
license: MIT
description: LLM-facing tool that loads the full SKILL.md body for a catalog entry. Companion to skill_index — L1 (the `<available_skills>` catalog) tells the model what exists, `invoke_skill` (this) fetches the chosen entry's body via a single `{"name": "<skill>"}` tool call. Not a Python API; for in-process catalog reads use `skill_index` with `{"op": "read", "name": ...}` directly.
triggers: [load skill, invoke skill, fetch skill body]
parameters:
  type: object
  required: [name]
  properties:
    name:
      type: string
      description: "Exact skill/agent name from the `<available_skills>` catalog, e.g. `memory`, `session_compressor`."
---
# invoke_skill

LLM-only wrapper around `skill_index.read`. Exists so the LLM can be
given a single, flat-argument `invoke_skill({"name": "..."})` tool
instead of constructing the nested `{"op": "read", ...}` envelope that
`skill_index` itself requires.

## Division of labor with `skill_index`

| Layer | Caller | Tool | Input |
|---|---|---|---|
| L1 catalog | agent code (Python) | `skill_index.list` | `{"op": "list"}` |
| L2 fetch (programmatic) | agent code (Python) | `skill_index.read` | `{"op": "read", "name": ...}` |
| L2 fetch (LLM) | LLM tool-use | `invoke_skill` | `{"name": ...}` |

If you are writing Python code and already hold `ctx.bus`, call
`skill_index` directly — going through `invoke_skill` just adds a
redundant bus hop.

## Response shape

```json
{"ok": true,
 "name": "memory",
 "kind": "skill",
 "location": "bundled/memory/SKILL.md",
 "frontmatter": {...},
 "body": "..."}
```

On error (missing field / bad name / downstream failure):
```json
{"ok": false, "error": "<reason>"}
```

## How the LLM sees this

Inject the pair into `llm_driver.run_turn`:

```python
xml = await bus.request("skill_index", {"op": "list"})
directive = build_directive(xml["xml_block"])
result = await bus.request("llm_driver", {
    "op": "run_turn",
    "system_prompt": "...",
    "messages": [...],
    "attachments": [directive],
    "tools": [TOOL_SCHEMA],            # from invoke_skill
    "tool_dispatch": {"invoke_skill": "invoke_skill"},
    "pool": "minimax", "model": "minimax-m2",
})
```

The `attachments` list surfaces the catalog as a per-turn
`<system-reminder>` (Claude-Code-style), keeping the static
`system_prompt` cache-friendly. The body for the chosen skill flows
back through the standard tool_result → next-iteration path in
`llm_driver`, so `invoke_skill` itself needs no special plumbing.

## Why a skill not an agent

Stateless. No lifecycle, no bus subscribes, no persistent state. Every
call does one bus hop and returns.

## Prompt provenance (do not soften)

`TOOL_SCHEMA["description"]` is ported verbatim from Claude Code 2.1.88
`tools/SkillTool/prompt.ts:173-195` with three adaptations documented
inline in `handler.py`:

- `"Skill tool" / "this tool"` → `"invoke_skill tool"`
- example skill names → yuxu bundled skills
- `<COMMAND_NAME_TAG>` sentinel → "a previous invoke_skill tool call in
  this turn already returned a SKILL.md body" (yuxu's equivalent entry
  point for an already-loaded skill is the tool_result stream, not a
  slash-command wrapper in the user message)

The four anti-fishing anchors are tested by
`tests/test_skill_invoke_skill.py::test_description_carries_cc_anti_fishing_anchors`.
Do not reword these without re-porting from the CC source — LLM
behaviour on MiniMax M2.7 shifted measurably when they were absent
(2026-04-24 observation; see `reference_cc_skilltool_prompt.md`).
