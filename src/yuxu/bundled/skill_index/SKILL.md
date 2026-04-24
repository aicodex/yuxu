---
name: skill_index
version: "0.1.0"
author: yuxu
license: MIT
description: Progressive-disclosure over yuxu's skills and agents catalog. `stats` returns counts (L0, scale-independent), `list` returns an OpenClaw-style `<available_skills>` XML block for system-prompt injection (L1), `read` returns the full SKILL.md / AGENT.md body of one entry (L2). Parallel to the memory skill's L0/L1/L2 contract — progressive disclosure is a cross-cutting yuxu principle, and skills catalog is its second consumer.
triggers: [list skills, available skills, scan skills, read skill doc]
parameters:
  type: object
  required: [op]
  properties:
    op:
      type: string
      enum: [stats, list, read]
      description: "`stats` — L0 counts by kind/scope. `list` — L1 XML block + structured entries. `read` — L2 one entry's full body."
    kind:
      type: string
      enum: [skill, agent, all]
      description: "`list` / `stats` filter. Default `all`. `skill` = SKILL.md with no __init__.py; `agent` = AGENT.md with __init__.py."
    scope:
      type: string
      description: "`list` / `stats` filter by scope frontmatter value (e.g. `system`, `user`). Omit for all."
    include_self:
      type: boolean
      description: "`list` — include `skill_index` itself in the block. Default true (LLM is allowed to discover the discovery tool)."
    char_budget:
      type: integer
      description: "`list` — cap the rendered block to this many chars. Default 18000 (OpenClaw convention). Overflow triggers full → compact → truncation fallback ladder."
    name:
      type: string
      description: "`read` — the skill/agent name to fetch."
---
# skill_index

Catalog of yuxu skills and agents, progressively disclosed.

## Ops

### L0 — `stats`
```json
{"op": "stats"}
→ {"ok": true,
   "total": 28,
   "by_kind": {"skill": 8, "agent": 20},
   "by_scope": {"system": 15, "user": 13},
   "by_source": {"bundled": 28, "project": 0}}
```
Payload size is independent of catalog size. Use as the probe op.

### L1 — `list`
```json
{"op": "list", "kind": "skill", "char_budget": 18000}
→ {"ok": true,
   "xml_block": "<available_skills>...</available_skills>",
   "entries": [{"name", "kind", "description", "location", "scope", "source"}],
   "rendered_chars": 4823,
   "compact_used": false,
   "omitted": 0}
```

Per-entry XML shape (OpenClaw-compatible plus a `<kind>` tag so the
LLM knows whether a bus.request target is a stateless skill or a
stateful agent):

```xml
<skill>
  <name>memory</name>
  <kind>skill</kind>
  <description>Progressive disclosure over yuxu memory...</description>
  <location>bundled/memory/SKILL.md</location>
</skill>
```

Budget fallback ladder (ported from OpenClaw `workspace.ts:124-157`):
1. Full format with descriptions (target)
2. Compact format: name + kind + location only, no descriptions
3. Binary-search truncate to the largest fitting prefix, append
   `[+N entries omitted]` note

### L2 — `read`
```json
{"op": "read", "name": "memory"}
→ {"ok": true, "name": "memory", "kind": "skill",
   "location": "bundled/memory/SKILL.md",
   "frontmatter": {...},
   "body": "...",
   "bytes": 4312}
```

## Recommended directive (for callers injecting into system prompt)

Ported from OpenClaw `system-prompt.ts:156-172`. Use the
`build_directive(xml_block)` helper in handler.py or copy verbatim:

```
## Available Skills (mandatory)
Before replying: scan <available_skills> <description> entries below.
- If exactly one skill clearly applies: call `skill_index` with
  `{"op": "read", "name": "<skill>"}` to load its full SKILL.md, then follow it.
- If multiple could apply: choose the most specific one, then read/follow it.
- If none clearly apply: do not read any SKILL.md.
Constraints: never read more than one skill up front; only read after
selecting.

<available_skills>
  ...
</available_skills>
```

## Discovery

- Bundled: `yuxu/src/yuxu/bundled/*` — scanned via the Loader spec
  registry when available (`ctx.loader.specs`); falls back to a
  filesystem scan when it's not.
- Project: `<project>/agents/*`, `<project>/skills/*` — same dual
  strategy.
- Global: `~/.yuxu/skills/*` — filesystem scan.

Entries missing `name` or `description` in frontmatter are skipped
(mirrors the memory skill's indexer contract).

## Why a skill not an agent

Stateless — every call re-scans. No lifecycle, no bus subscribes, no
persistent counters. Same shape as memory / admission_gate /
llm_judge / context_compressor.
