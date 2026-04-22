# Yuxu Skill Format + Cross-Ecosystem Compatibility

Yuxu skills live in one of three scope roots (`src/yuxu/bundled/`,
`<project>/agents/` or `<project>/skills/`, `<agent_dir>/skills/`) as
**folders** containing:

```
{skill_name}/
├── SKILL.md      # YAML frontmatter + markdown body
└── handler.py    # async def execute(input: dict, ctx) -> dict
# note: NO __init__.py — that's the marker distinguishing skills from agents
```

The unified `Loader` (`core/loader.py`) scans every scope root, classifies
each folder by the presence of `__init__.py` (agent) vs. handler.py + no
`__init__.py` (skill), and dispatches both kinds via `bus.request("{name}", ...)`.
No separate skill registry.

## Frontmatter Fields

| Field | Type | Yuxu Meaning | OpenClaw | Claude Code |
|---|---|---|---|---|
| `name` | str | skill identifier (folder name wins on conflict) | ✓ | ✓ |
| `description` | str | one-paragraph summary shown to callers | ✓ | ✓ (multi-line via `\|` allowed) |
| `triggers` | list[str] | natural-language phrases hinting when to pick | — | ✓ |
| `parameters` | JSON Schema | OpenAI function-call-style input schema | — | — |
| `depends_on` | list[str] | bus addresses this skill needs running | — | — |
| `rate_limit_pool` | str | yuxu rate-limit pool name | — | — |
| `edit_warning` | bool | user must explicitly approve edits to this file | — | — |
| `version` | str | SemVer or free-form | ✓ | ✓ |
| `author` | str | credit | ✓ | — |
| `license` | str | SPDX id or free text | ✓ | — |
| `tags` | list[str] | cataloging hints | ✓ | — |
| `homepage` | str | URL | ✓ | — |
| `handler` | str | override default `handler.py` filename | — | — |
| `allowed_tools` / `allowed-tools` | list[str] | tool allowlist (CC compat; yuxu no-op today) | — | ✓ |
| `model` | str | LLM model hint (CC compat; yuxu executor may honor) | — | ✓ |
| `context` | `"inline"` \| `"fork"` | CC context mode hint | — | ✓ |
| `preamble-tier` | int | CC-specific priority; preserved verbatim | — | ✓ |

**Keys not in the table** land in `spec.frontmatter` (dict) unchanged — no
schema gatekeeping, no crashes. That's intentional: a future `skill_converter`
agent can read a OpenClaw / Claude Code skill, hand it to yuxu as-is, and the
registry will preserve every byte of metadata for round-trip fidelity.

## Runtime Behavior Matrix

| Behavior | Yuxu today | Plan |
|---|---|---|
| `triggers` surfaced via `loader.filter(surface=...)` | ✓ (in `spec.frontmatter`) | intent_router skill |
| `parameters` surfaced to LLM as tool schema | partially (read, not dispatched) | llm_driver tool-binding |
| `depends_on` resolved via Loader `ensure_running` | ✓ (recursive) | — |
| `allowed_tools` enforced at bus dispatch | — | v0.2 security gating |
| `model` / `context` honored by LLM call wrapper | `context: inline` → gateway.inline_expander | fork mode later |
| `surface: [menu, command]` exposes unit to gateway UI | ✓ (`loader.filter(surface=...)`) | — |
| Handler file (anything other than `handler.py`) | via `handler:` frontmatter | — |

## Handler Conventions (for Python-backed skills)

Yuxu-native handler:
```python
# handler.py
async def execute(input: dict, ctx) -> dict:
    return {"ok": True, ...}
```

OpenClaw skills ship arbitrary-named modules (`self_improving.py`) with a
class. A converter agent can write a thin `handler.py` shim:
```python
from .self_improving import SelfImprovingAgent
async def execute(input, ctx):
    sia = SelfImprovingAgent()
    return {"ok": True, "result": sia.log_improvement(input["insight"])}
```

Or set `handler: self_improving.py` in frontmatter if the OpenClaw file
already exposes an `execute` function (rare).

## Compatibility Discipline

Yuxu's guarantees for foreign skills:

1. **No crash on unknown fields.** Every skill frontmatter can contain
   ecosystem-specific keys; yuxu preserves them in `spec.frontmatter`.
2. **Case-insensitive to kebab-vs-snake** for dual-ecosystem fields
   (`allowed-tools` / `allowed_tools`). CC-conventions win when both appear.
3. **SKILL.md filename is fixed** (`SKILL.md`). Claude Code uses the same.
   OpenClaw also. Uppercase-insensitive match is NOT supported; converter
   should rename.
4. **Folder name is the skill's identity.** Frontmatter `name` can drift;
   yuxu logs a WARNING and keeps the folder name.

## Where compatibility ends

Yuxu does NOT:
- Execute CC skills' `!command` preambles **at bus dispatch time**. Inline
  preambles (both `!\`cmd\`` and ` ```! ... ``` ` fenced form) are expanded
  only when a skill is rendered via `gateway.inline_expander.expand_inline_skill`
  (i.e. `context: inline` skills used as LLM prompt templates).
- Load OpenClaw skills' non-`handler.py` Python code automatically. The
  `handler:` frontmatter override points Loader to the file, but the module
  must still expose an `execute(input, ctx)` function matching yuxu's signature.
- Honor `allowed_tools` as a permission enforcement today — it's metadata only.

A future `skill_converter` agent should:
1. Read foreign SKILL.md / skill.yaml
2. Rewrite frontmatter with yuxu's field names where needed (rename
   `allowed-tools` → `allowed_tools`, add `handler:` if the python file isn't
   named `handler.py`)
3. Generate a `handler.py` shim if the foreign skill's Python API doesn't
   match yuxu's `execute(input, ctx)` shape
4. Drop the result under the target scope and emit a conversion-log entry
