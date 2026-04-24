---
name: compactor
version: "0.1.0"
author: yuxu
license: MIT
description: Stateless conversation compaction — port of Claude Code 2.1.88's microcompact (clear old tool_results, keep last N) + full_compact (LLM-summarise all-but-last-N-turns using CC's verbatim 9-section prompt, insert compact_boundary marker). Mechanism only — no automatic triggers; callers invoke when a byte/turn threshold is hit. See `reference_cc_agent_protocol.md` for the upstream semantics.
triggers: [compact context, microcompact, full compact, clear tool results, summarise conversation]
parameters:
  type: object
  required: [op]
  properties:
    op:
      type: string
      enum: [microcompact, full_compact]
      description: "`microcompact` — pure list manipulation, no LLM. `full_compact` — LLM summarise."
    messages:
      type: array
      description: "OpenAI-style messages list; both ops take + return this shape."
    keep_recent:
      type: integer
      description: "microcompact only: how many recent tool_results to preserve. Default 5 (matches CC `tengu_slate_heron.keepRecent`)."
    keep_recent_turns:
      type: integer
      description: "full_compact only: how many recent user-initiated turns to preserve. Default 5."
    pool:
      type: string
      description: "full_compact only: LLM pool (e.g. `minimax`)."
    model:
      type: string
      description: "full_compact only: model name."
    llm_timeout:
      type: number
      description: "full_compact only: LLM call timeout in seconds. Default 120."
    max_tokens:
      type: integer
      description: "full_compact only: response cap. Default provider-side."
---
# compactor

Port of Claude Code 2.1.88's context compaction. Two ops, both stateless.

## `microcompact`

Cheap. No LLM call.

```json
{"op": "microcompact", "messages": [...], "keep_recent": 5}
→ {"ok": true, "messages": [...], "cleared_count": N, "tool_count": M}
```

Behaviour:
- Identifies all messages with `role: "tool"`.
- If there are ≤ `keep_recent` tool messages, returns `messages` unchanged
  (cleared_count=0).
- Otherwise: for every tool message EXCEPT the last `keep_recent`, replaces
  `content` with `"[Old tool result content cleared]"` (verbatim CC marker).
  `role` and `tool_call_id` are preserved so the message list stays valid.
- Input list is NOT mutated — a new list is returned.

Port notes:
- CC uses `cache_edits` to mark old tool_results as deleted at the
  Anthropic API layer so the prompt cache stays warm. yuxu runs on MiniMax
  which has no equivalent; we rewrite the list directly. Loses the "cache
  byte reuse" benefit, keeps the tokens-saved benefit.
- Default `keep_recent = 5` = CC's `tengu_slate_heron.keepRecent` GrowthBook
  default (`timeBasedMCConfig.ts:30-34`).

## `full_compact`

Expensive. One LLM call per invocation.

```json
{"op": "full_compact",
 "messages": [...],
 "pool": "minimax", "model": "MiniMax-M2.7-highspeed",
 "keep_recent_turns": 5}
→ {"ok": true,
   "messages": [summary_user_msg, compact_boundary_marker, *last_N_turns],
   "summary": "...",
   "cleared_count": N,
   "usage": {...},
   "elapsed_ms": ...}
```

Behaviour:
- Splits `messages` at the Nth-from-last user message (each user message
  starts a turn).
- Renders the prefix into a flattened prose stream (tool_results > 500
  chars get tail-clipped — the whole point is not to carry them through
  the summariser).
- Calls `llm_driver.run_turn` with CC's verbatim 9-section prompt:
  Primary Request / Key Technical Concepts / Files and Code Sections /
  Errors and fixes / Problem Solving / All user messages / Pending Tasks /
  Current Work / Optional Next Step.
- Returns a new list: `[summary_msg, compact_boundary, *tail]`.
  `compact_boundary` is a system-role marker so inspection tools can see
  where compaction cut.

Edge cases (all return unchanged messages + a `skipped` note, NOT an error):
- Empty prefix (all turns already within keep_recent_turns).
- Not enough turns (total turns ≤ keep_recent_turns).

Hard failures (return `ok: false`):
- Empty messages list.
- LLM returned empty summary / bus error.

## Why no automatic triggers?

User directive 2026-04-24: "微压缩加上 但是先不加触发器" (add the mechanism,
hold the trigger). The reasons:

- **llm_driver.run_turn**: auto-compacting inside a tool-call loop is
  high-stakes — a wrong cut could break tool_call_id correspondence.
  Adding the trigger needs integration testing against real tool-heavy
  agents. Until then: caller opts in explicitly.
- **gateway**: gateway doesn't yet store conversation history (`SessionEntry`
  is routing-only). When it does, that's the natural auto-trigger site
  — TODO pinned in `project_pending_todos.md`.
- **Long-running agents**: configuration knob lives with each agent
  (whether to invoke compactor at N-byte / N-turn / N-minute thresholds).
  Blanket automation in the driver would fight agent-specific policy.

## Calling from Python (bypass bus)

```python
from yuxu.bundled.compactor.handler import microcompact, full_compact

# Cheap path
result = microcompact(messages, keep_recent=5)
messages = result["messages"]

# Expensive path (needs bus for llm_driver)
result = await full_compact(messages, bus=ctx.bus,
                             pool="minimax", model="MiniMax-M2.7-highspeed",
                             keep_recent_turns=5)
if result["ok"]:
    messages = result["messages"]
```

## Reference

- CC source: `services/compact/microCompact.ts`, `autoCompact.ts`,
  `compact.ts`, `prompt.ts`, `messages.ts:4530-4555`
- yuxu memory: `reference_cc_agent_protocol.md` (agent protocol overall),
  `reference_memory_designs_oc_hermes.md` (OC memory search),
  `project_pending_todos.md` under "🔧 compaction triggers"

## Why a skill, not an agent

Stateless. Every call is independent — no lifecycle, no bus subscribes,
no persistent state. Same shape as `memory` / `admission_gate` /
`llm_judge` / `context_compressor`.
