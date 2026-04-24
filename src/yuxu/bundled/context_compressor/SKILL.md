---
name: context_compressor
version: "0.1.0"
author: yuxu
license: MIT
description: Map-reduce LLM summarization for large inputs (session JSONL transcripts, multi-document corpora). Builds on Claude Code's 9-section BASE_COMPACT_PROMPT + `<analysis>` scratchpad pattern, with OpenClaw's IDENTIFIER_PRESERVATION injected. `summarize` is the primary op; single document → one LLM call, multi-document → map-reduce. Falls back to head+tail byte truncation when the LLM is unavailable or returns unparseable output (with `fallback_used=true, confidence` implicit in whether summaries exist).
triggers: [compress context, summarize transcripts, shrink input, shorten prompt]
parameters:
  type: object
  required: [op]
  properties:
    op:
      type: string
      enum: [summarize]
      description: v0 — `summarize`. Future `compact_conversation` op is reserved for in-run chat compaction when a real conversational agent lands.
    documents:
      type: array
      description: One or more `{id, body}` objects. `id` is a free-form label (e.g. filename or session id) that appears in the result; `body` is the raw text.
      items: {type: object}
    task:
      type: string
      description: What the downstream agent will do with this summary. Injected into the prompt so the summarizer can prioritize relevant parts. Strongly recommended — absent task → generic summary.
    target_tokens:
      type: integer
      description: Desired output token budget across all summaries. Default 2000. If the total input is already smaller, compression is skipped and originals are returned concatenated (`fallback_used=false`, just a pass-through).
    custom_instructions:
      type: string
      description: Extra rules appended to the compression prompt — same slot as Claude Code's `## Compact Instructions`. Use for task-specific constraints ("focus on error/fix pairs", "preserve code snippets verbatim").
    pool:
      type: string
      description: llm_driver pool override. Defaults to `CONTEXT_COMPRESSOR_POOL` env, else llm_driver's default.
    model:
      type: string
      description: llm_driver model override. Defaults to `CONTEXT_COMPRESSOR_MODEL` env.
    max_bytes_per_map:
      type: integer
      description: Safety cap per single map-phase LLM call (in bytes). Documents larger than this are byte-truncated head+tail before hitting the LLM. Default 60_000.
    fallback_enabled:
      type: boolean
      description: Allow head-and-tail byte-truncation fallback when the LLM path fails. Default true; disable for pure-LLM tests.
---
# context_compressor

Map-reduce summarization skill.

## Op `summarize`

```
input:  documents=[{id, body}, ...], task?, target_tokens?,
        custom_instructions?, pool?, model?
output: {
  ok: true,
  merged_summary: str,           # what the caller feeds downstream
  per_document: [
    {id, original_tokens, summary_tokens, summary, fallback_used}
  ],
  total_tokens_before: int,
  total_tokens_after: int,
  savings_ratio: float,          # (before - after) / before
  fallback_used: bool,            # any document hit fallback
  skipped: bool                   # input already under budget → no-op
}
```

## Flow

1. Token estimate (bytes / 4 — no tokenizer dependency).
2. If total ≤ `target_tokens`: return originals concatenated, `skipped=true`.
3. **Map phase** — per document, call llm_driver with CC-style
   `BASE_COMPACT_PROMPT` adapted: 9 sections with "omit sections that
   don't apply" directive. Input larger than `max_bytes_per_map` is
   head+tail-truncated before the call.
4. **Reduce phase** (only when `len(documents) > 1`) — merge all
   partial summaries with the OpenClaw-style concise `MUST PRESERVE`
   prompt.
5. If LLM fails on any stage: head+tail byte-truncate that document,
   mark `fallback_used=true`.
6. Publish `context.compressed` event.

## Prompt design (what we borrowed vs added)

From Claude Code:
- 9-section output structure (Primary Request / Key Concepts / Files
  & Code / Errors & Fixes / Problem Solving / All User Messages /
  Pending Tasks / Current Work / Optional Next Step)
- `<analysis>` scratchpad pattern — LLM drafts in `<analysis>`, writer
  strips it before returning, CoT quality without context pollution
- `NO_TOOLS` preamble + trailer double guard — defense in depth
- Custom instruction insertion slot
- Direct-quote mandate for Current Work + Next Step (anti-drift)

From OpenClaw:
- `IDENTIFIER_PRESERVATION_INSTRUCTIONS` — UUIDs, hashes, URLs, paths
  kept verbatim (the one truly universal compression directive)
- `MERGE_SUMMARIES_INSTRUCTIONS` concise style for reduce phase

Explicitly NOT ported (from Hermes):
- Field structure assuming RL-style state (Active Task / In Progress /
  Blocked) — doesn't match yuxu inputs
- Anti-thrashing guard — needed only for repeated in-run compaction,
  which we're deferring
- Tool result MD5 dedup — our inputs aren't tool-call-shaped

## Events

`context.compressed` on every `summarize` call with:
`{op, document_count, total_tokens_before, total_tokens_after,
  savings_ratio, fallback_used, skipped}`.

Future iteration_agent uses this to score the compressor's own quality
(I10).

## v0 out of scope

- `compact_conversation` (in-run chat compaction). Reserved — will add
  when a real conversational agent needs it.
- Streaming / incremental summarization.
- Structured-output per-section parsing (we rely on the downstream LLM
  re-reading the 9-section text naturally).
- Anthropic prompt caching. MiniMax ignores `cache_control`; revisit
  when a Claude pool lands.
