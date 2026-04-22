# Yuxu Skill Format + Cross-Ecosystem Compatibility

Yuxu skills live in one of three scope roots (`src/yuxu/skills_bundled/`,
`<project>/skills/`, `<agent_dir>/skills/`) as **folders** containing:

```
{skill_name}/
├── SKILL.md      # YAML frontmatter + markdown body
└── handler.py    # optional; async def execute(input: dict, ctx) -> dict
```

The `SkillRegistry` in `bundled/skill_picker/registry.py` scans these roots,
reads the frontmatter, and surfaces everything via `catalog` / `load` ops.

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
| `triggers` used by `skill_picker.catalog(triggers_any=...)` | ✓ | — |
| `parameters` surfaced to LLM as tool schema | partially (read, not dispatched) | skill executor agent (TBD) |
| `depends_on` resolved via Loader ensure_running | — | skill executor |
| `allowed_tools` enforced at bus dispatch | — | v0.2 security gating |
| `model` / `context` honored by LLM call wrapper | — | when skill executor runs LLM-only skills |
| `version` displayed in catalog | ✓ | — |
| Handler file (anything other than `handler.py`) | via `handler:` field | — |

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
- Execute CC skills' `!command` preambles (that's a CC Bun runtime feature).
- Load OpenClaw skills' non-`handler.py` Python code automatically. The
  `handler:` frontmatter override points registry to the file, but a skill
  executor agent still has to import + call it with yuxu's `execute(input, ctx)`
  signature.
- Honor `allowed_tools` as a permission enforcement today — it's metadata only.

A future `skill_converter` agent should:
1. Read foreign SKILL.md / skill.yaml
2. Rewrite frontmatter with yuxu's field names where needed (rename
   `allowed-tools` → `allowed_tools`, add `handler:` if the python file isn't
   named `handler.py`)
3. Generate a `handler.py` shim if the foreign skill's Python API doesn't
   match yuxu's `execute(input, ctx)` shape
4. Drop the result under the target scope and emit a conversion-log entry
