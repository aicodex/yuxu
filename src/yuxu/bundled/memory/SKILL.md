---
name: memory
version: "0.1.0"
author: yuxu
license: MIT
description: Read yuxu memory with progressive disclosure — `list` returns a lightweight index (name + description + type, from frontmatter), `get` returns the full body for one entry. Writes still flow through memory_curator / approval_queue.
triggers: [list memory, read memory, memory index, recall memory]
parameters:
  type: object
  required: [op]
  properties:
    op:
      type: string
      enum: [list, get]
      description: "`list` — scan memory_root and return index entries (frontmatter only). `get` — read one entry fully."
    memory_root:
      type: string
      description: Override memory root. Defaults to `<project>/data/memory` resolved via yuxu.json walk-up, falling back to cwd.
    path:
      type: string
      description: For `get` — path to the memory file, either absolute or relative to memory_root.
    types:
      type: array
      items: {type: string}
      description: For `list` — optional filter (e.g. `["feedback", "project"]`). Entries without a matching `type` in frontmatter are excluded.
---
# memory

Two-layer progressive disclosure over `<project>/data/memory/*.md`:

- **Layer 1 (index)**: `{op: "list"}` → `{ok, memory_root, entries: [{path, name, description, type, bytes}]}`. Parses only frontmatter.
- **Layer 2 (body)**: `{op: "get", path}` → `{ok, path, frontmatter, body, bytes}`. Reads the full file.

Skipped from the index: `_drafts/`, `_improvement_log.md`, dotfiles, and files missing frontmatter `name` / `description`.

Callers decide which entries they need from the index, then fetch those bodies — mirroring Claude Code's skill loader and OpenClaw's 2-layer memory.
