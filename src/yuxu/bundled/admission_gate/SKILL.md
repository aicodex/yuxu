---
name: admission_gate
version: "0.1.0"
author: yuxu
license: MIT
description: "Three-stage quality gate run on memory writes (write-admission, per I6). `surface_check` uses an LLM judge to decide if the entry is an actionable rule vs verbose-obvious/opinion/mis-typed content; `golden_replay` verifies that any `originSessionId` citation points to a session archive that actually exists; `noop_baseline` flags near-duplicates of existing entries. Any stage fail → pass false; caller (today, `approval_applier`) archives the draft instead of writing it."
triggers: [admission gate, memory gate, check memory entry]
parameters:
  type: object
  required: [op, entry_body]
  properties:
    op:
      type: string
      enum: [check]
      description: Only `check` today.
    entry_body:
      type: string
      description: Full memory entry text — inner frontmatter + body. Same text that would be written to disk on approval.
    memory_root:
      type: string
      description: Absolute path of the target memory dir. Used by `noop_baseline` to list existing entries. Missing/unresolvable root → stage skipped with a pass note.
    target_path:
      type: string
      description: Path (relative to `memory_root` or absolute) of the entry being written. Excluded from `noop_baseline` dedup scan so self-updates don't compare against themselves. Optional.
    session_root:
      type: string
      description: Override for session JSONL archive root. Default walks up from `memory_root` to find `docs/experiences/sessions_raw/`; if that path doesn't exist the gate skips `golden_replay` for entries without an originSessionId, and fails it for entries that cite one.
    pool:
      type: string
      description: llm_driver pool for `surface_check`. Defaults to `ADMISSION_GATE_POOL` env, else llm_driver's own default.
    model:
      type: string
      description: llm_driver model for `surface_check`. Defaults to `ADMISSION_GATE_MODEL` env.
    dedup_threshold:
      type: number
      description: Jaccard threshold for `noop_baseline` (char-trigram over name+description). Default 0.6. Override for tests.
---
# admission_gate

Gate run immediately before a memory entry is written. Returns a
verdict composed of three independent stages; any `pass=false` is a
hard block for the caller.

## Stages

### 1. `surface_check` — LLM semantic check
Ask the LLM: *is this entry a real, actionable observation / rule, or
is it verbose-obvious / opinion-only / mis-typed?* Prompt is short,
JSON-mode, single turn.

- `pass=true` when the LLM returns a usable verdict accepting the entry
- `pass=false` when the verdict rejects
- `pass=true, skipped=<reason>` when llm_driver is unavailable or the
  response can't be parsed — infrastructure gaps don't block writes

### 2. `golden_replay` — citation honesty
If the entry's frontmatter carries `originSessionId`, the corresponding
session JSONL archive file must exist. Current check is **file existence
only**; content-overlap verification is a future upgrade.

- No `originSessionId` → `pass=true`, note "no session cited"
- `originSessionId` set + session file present → `pass=true`
- `originSessionId` set + file missing → `pass=false` (hallucinated citation)
- `session_root` unresolvable + citation present → `pass=false`

### 3. `noop_baseline` — duplicate detection
Scan the target memory_root for existing entries; if any non-target
entry has the same frontmatter `name` OR char-trigram Jaccard over
`name + description` ≥ `dedup_threshold`, the new entry is
redundant and the gate fails.

- No memory_root or empty directory → `pass=true`, note "no prior entries"
- Exact name collision → `pass=false` with `match_path`
- High-similarity description → `pass=false` with `match_path`
- Otherwise → `pass=true`

## Output

```json
{
  "ok": true,
  "pass": true|false,
  "stages": {
    "surface_check": {"pass": bool, "reason": str, "skipped": str?},
    "golden_replay": {"pass": bool, "reason": str},
    "noop_baseline": {"pass": bool, "reason": str, "match_path": str?}
  },
  "verdict": "<human-readable one-liner>"
}
```

Caller decides policy on failure. `approval_applier` archives the
draft under `_archive/gated/` and emits `approval_applier.gated`
rather than writing to the target path.
