---
driver: python
run_mode: persistent
scope: system
edit_warning: true
depends_on: [gateway, llm_driver]
optional_deps: [approval_queue, performance_ranker]
ready_timeout: 5
---
# reflection_agent

Hermes-inspired iteration / exploration agent. Given a user need and access
to past session transcripts, it generates several candidate readings of "what
worked" in parallel, ranks/merges them, and proposes memory edits as **drafts
that the user must approve** before they hit live memory.

```
/reflect <need>
  ├─ load sources (default: <project>/data/sessions/**/*.md, or --sources arg)
  ├─ N parallel hypotheses (different framings: pattern / anti-pattern / synthesizer)
  ├─ LLM-rank + merge → chosen edit list
  ├─ stage drafts at <memory_root>/_drafts/reflection_<run_id>_<n>.md
  └─ enqueue each via approval_queue (best-effort, optional dep)
```

## Why iterate before write?

Hermes commits memory directly on session-end (no proposal stage).
reflection_agent intentionally diverges: it proposes, never auto-commits,
because the **explore-N-then-rank** flow has more failure modes (LLM
overconfidence, hypothesis drift, cross-session contamination). Drafts on
disk give the user a recoverable inspection point.

The Hermes traits we DO keep: atomic temp-then-rename writes, char-cap per
draft (default 4 KB), dedup by content hash, `_drafts/` is opaque to other
agents (filename prefix `_` keeps loader / picker scans clean).

## Operations

| op | payload | 返回 |
|---|---|---|
| `reflect` | `{need, sources?, memory_root?, n_hypotheses?=3, model?, pool?}` | `{ok, run_id, hypotheses, chosen, drafts, approval_ids, warnings}` |
| `reflect` (auto) | `{auto: true, ...}` | same shape, `need` synthesized from performance_ranker |

`hypotheses` is the raw N-way exploration. `chosen` is the post-rank merged
edit set. `drafts` is the list of `<memory_root>/_drafts/reflection_*.md`
paths written to disk. `approval_ids` is the list of approval_queue item ids
(empty if approval_queue isn't running — drafts stay on disk).

## Slash command

- `/reflect <need>` — equivalent to
  `bus.request("reflection_agent", {op: "reflect", need: <args>})`.
- `/reflect auto` — ask `performance_ranker` for the worst-performing agent
  (most recent `.error` + `approval_queue.rejected` signals in the window)
  and synthesize a `need` focused on that agent. Fails gracefully with
  `stage=auto_target` if ranker isn't running or has no candidates.

## Why this is an agent

Persistent gateway subscriber + holds a per-run `_drafts/` workspace. The
multi-hypothesis orchestration could be a skill long-term, but it has its
own state (run_id sequencing, approval_queue interaction) that justifies
agent status. Pure LLM steps (extract / propose) may later be extracted to
skills once a second consumer appears.

## v0 limits

- `add` and `update` proposed actions only — no `remove`.
- LLM-based ranker; no deterministic scoring.
- Per-hypothesis temperature fixed at 0.5 (framing diversity carries it).
- Sources read as plain markdown; no chunking past a soft size cap (8 KB
  per source, configurable via `MAX_SOURCE_BYTES`).
- approval_queue interaction is fire-and-forget; failures are warnings.
