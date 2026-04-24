---
name: memory
version: "0.2.0"
author: yuxu
license: MIT
description: Progressive disclosure over yuxu memory. `stats` returns counts (L0, scale-independent), `list` returns a filtered index (L1, frontmatter only), `get` returns a full entry (L2), `search` ranks name+description matches. Writes still flow through memory_curator / approval_queue.
triggers: [list memory, read memory, memory stats, memory search, recall memory]
parameters:
  type: object
  required: [op]
  properties:
    op:
      type: string
      enum: [stats, list, get, search]
      description: "`stats` — L0 counts by type/scope/status/evidence_level; `list` — L1 filtered index; `get` — L2 one entry body; `search` — keyword match ranked top-K."
    memory_root:
      type: string
      description: Override memory root. Defaults to `<project>/data/memory` resolved via yuxu.json walk-up, falling back to cwd.
    mode:
      type: string
      enum: [blank, explore, execute, reflect, debug]
      description: "Default filter policy per I6. `execute` (default) = {validated, consensus, observed} + current, excludes probation. `blank`/`explore` = only mandatory-tagged. `reflect` = no restrictions. `debug` = observed + archived. User-provided filters below override the mode's corresponding axis."
    path:
      type: string
      description: For `get` — path to memory file, absolute or relative to memory_root.
    query:
      type: string
      description: For `search` — keyword(s) to match on name + description.
    limit:
      type: integer
      description: For `search` — top-K cap (default 10).
    type:
      type: string
      description: Filter by frontmatter type (alias for `types` with a single value).
    types:
      type: array
      items: {type: string}
      description: Filter by frontmatter type (e.g. `["feedback", "project"]`).
    scope:
      type: [string, array]
      description: Filter by scope (semantic — where the rule applies, not where stored).
    evidence_level:
      type: [string, array]
      description: Filter by evidence level (validated | consensus | observed | speculative). Overrides mode default.
    status:
      type: [string, array]
      description: Filter by status (current | archived). Overrides mode default.
    tags:
      type: array
      items: {type: string}
      description: Entry must carry ALL given tags (AND semantics).
    include_probation:
      type: boolean
      description: Override mode's probation exclusion (default false in execute mode).
---
# memory

Progressive disclosure over `<project>/data/memory/*.md`, following I6's
Memory access discipline: lazy, tool-mediated, never auto-injected.

- **L0 — stats**: `{op: "stats"}` → counts by type / scope / status /
  evidence_level + probation/mandatory totals. Payload size independent of
  total entry count.
- **L1 — list**: `{op: "list", mode?, type?, scope?, evidence_level?, status?, tags?}`
  → filtered frontmatter-only index. Mode sets defaults; explicit filters
  override per-axis.
- **L2 — get**: `{op: "get", path}` → full body + parsed frontmatter.
- **Cross-cut — search**: `{op: "search", query, limit?, mode?}` → top-K
  ranked by name + description match.

Modes (per I6):

| mode | default filter |
|---|---|
| `blank`   | only entries tagged `mandatory` |
| `explore` | only entries tagged `mandatory` |
| `execute` | evidence_level ∈ {validated, consensus, observed}, status=current, probation excluded (**default**) |
| `reflect` | no restrictions (includes archived + probation) |
| `debug`   | evidence_level=observed, status=archived |

Skipped from all ops: `_drafts/`, `_improvement_log.md`, dotfiles, entries
missing frontmatter `name` / `description`.

Entries without `evidence_level` default to `observed` for filter purposes;
without `status` default to `current`. Keeps Phase 1 ungraded entries
addressable under execute mode.

## When to call (LLM-facing guidance)

**Default to `execute` mode** — it returns the validated/consensus/observed
entries the caller actually needs to act on, minus probation. Switch modes
only when you need a different slice:

- **`execute`** (default) — executing a task, need proven rules only
- **`explore`** — starting a new task, want the minimal mandatory rules
- **`reflect`** — post-mortem / learning from past runs, include archived
  and probation entries so you see the whole history
- **`blank`** — you want to verify behavior without memory bias (test-time,
  debugging a prompt)
- **`debug`** — archaeology: show me what was observed then archived

**Op selection:**

- **Start with `stats`** when unsure of scope. `{op: "stats"}` is O(1)-payload
  and tells you how many entries exist, by type/scope/evidence. Cheap probe.
- **`list`** when you need the filtered index — never dumps full bodies, just
  the frontmatter. Use `type` / `scope` / `tags` / `evidence_level` filters
  to narrow. If the expected result is > 30 entries, add filters or switch
  to `search`.
- **`search`** when you have a keyword and want ranked top-K. Ranks name
  hits heavier than description hits. Cheap ranked fuzzy match; not
  semantic.
- **`get`** when you already have a path (from `list` or `search`) and need
  the full body. One `get` per entry — don't bulk-fetch.

**Do NOT:**

- Do NOT read memory files with `rglob` / filesystem scans — violates I6's
  lazy-access contract; the skill exists precisely to mediate this.
- Do NOT auto-inject all memory into every prompt — retrieval is opt-in per
  turn, stays tool-mediated.
- Do NOT write memory through this skill. All writes flow through
  `memory_curator` → `approval_queue` → `approval_applier` → `admission_gate`.
  This skill is read-only.
- Do NOT bypass `mode` when the caller context suggests it matters; e.g.
  don't pass `include_probation=true` from an `execute`-mode agent unless
  you're explicitly doing post-mortem analysis.
- Do NOT use memory for temporary TODO state, session progress, or
  completed-work logs — those belong to session transcripts, not memory.
  Memory is for durable cross-session facts.
