"""context_compressor — map-reduce LLM summarization for large inputs.

Design heritage:
- Claude Code `services/compact/prompt.ts` — 9-section output structure,
  `<analysis>` scratchpad pattern, NO_TOOLS double guard, custom
  instructions slot, direct-quote anti-drift, continuation handling
- OpenClaw `compaction.ts` — IDENTIFIER_PRESERVATION (universal),
  MERGE_SUMMARIES_INSTRUCTIONS concise style for reduce
- NOT Hermes — field structure assumed RL training state that doesn't
  fit yuxu inputs; skipped per project memory

yuxu additions:
- Skill form (stateless `execute(input, ctx)`) per I1/I4
- Framework provides compression mechanism; caller supplies task +
  custom_instructions + target_tokens per I2
- `context.compressed` event on every call, for future
  iteration_agent to score the compressor against outcomes (I10)
- Head+tail byte-truncation fallback when LLM unavailable/unparseable
- Skip-on-small-input short-circuit
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

log = logging.getLogger(__name__)

NAME = "context_compressor"

COMPRESSED_TOPIC = "context.compressed"

DEFAULT_TARGET_TOKENS = 2000
# 500KB per map call — MiniMax 2.7 has 256k context (~1MB bytes), leaves
# plenty of room for system prompt + LLM output. Raised from initial 60KB
# after reference_compressor_measured_quality.md showed the old cap dropped
# 99%+ of large session JSONLs. Caller can still override per call.
DEFAULT_MAX_BYTES_PER_MAP = 500_000
BYTES_PER_TOKEN_ESTIMATE = 4  # rough — no tokenizer dependency

# Shared directive injected into every compression prompt. Ported from
# OpenClaw `compaction.ts:38-40`. Universally useful: we never want the
# summarizer to shorten a UUID or guess at a URL.
IDENTIFIER_PRESERVATION = """Preserve all opaque identifiers exactly as written \
(no shortening or reconstruction), including UUIDs, hashes, IDs, hostnames, \
IPs, ports, URLs, and file names."""


# NO_TOOLS preamble — ported from Claude Code `prompt.ts:19-26`.
# The CC notes called out a real empirical problem: Sonnet 4.6+ sometimes
# attempts a tool call during compaction (2.79% vs 0.01% on 4.5) and burns
# its only turn. Even though we don't pass tools in this skill's llm_driver
# call today, this preamble is cheap and hardens us if a caller ever wires
# tools through.
NO_TOOLS_PREAMBLE = """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

- You already have all the context you need in the input below.
- Tool calls will be REJECTED and will waste your only turn — you will fail the task.
- Your entire response must be plain text: an <analysis> block followed by a <summary> block.

"""


NO_TOOLS_TRAILER = (
    "\n\nREMINDER: Do NOT call any tools. Respond with plain text only — "
    "an <analysis> block followed by a <summary> block."
)


# Map-phase compression prompt. Adapted from CC `BASE_COMPACT_PROMPT`
# (`prompt.ts:61-143`). The key CC innovations preserved:
# - <analysis> drafting scratchpad (CoT without polluting output)
# - 9-section output structure with a full <example>
# - "include direct quotes ... verbatim to ensure there's no drift"
# yuxu divergence: added an "omit sections that don't apply" directive
# since our inputs aren't always conversations (may be docs, code, etc.)
MAP_COMPRESS_BASE = """Your task is to create a detailed summary of the input below, paying close attention to the user's explicit requests and the actions taken. This summary should capture technical details, code patterns, and architectural decisions that would be essential for continuing work without losing context.

Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts and ensure you've covered all necessary points. In your analysis:

1. Chronologically walk through each message or section of the input. For each section thoroughly identify:
   - The user's explicit requests and intents
   - The assistant's approach to addressing them
   - Key decisions, technical concepts, and code patterns
   - Specific details like file names, code snippets, function signatures, file edits
   - Errors encountered and how they were fixed
   - User feedback, especially when the user corrected direction
2. Double-check for technical accuracy and completeness.

Your summary should include the following sections. **Omit any section that does not apply to this input** (e.g. if there is no code, skip "Files and Code Sections"; if there is no conversation, skip "All User Messages"):

1. Primary Request and Intent
2. Key Technical Concepts
3. Files and Code Sections (with important code snippets verbatim)
4. Errors and Fixes
5. Problem Solving
6. All User Messages (non-tool-result user turns, if any)
7. Pending Tasks
8. Current Work (precise, with file names and snippets)
9. Optional Next Step — if a next step is clearly implied by the most recent content, include direct quotes from it verbatim to prevent drift in task interpretation. Do not invent tangential next steps.

{identifier_preservation}

Here's an example of how your output should be structured:

<example>
<analysis>
[Your thought process, ensuring all applicable points are covered thoroughly]
</analysis>

<summary>
1. Primary Request and Intent:
   [Detailed description]

2. Key Technical Concepts:
   - [Concept 1]
   - [Concept 2]

3. Files and Code Sections:
   - [File Name]
      - [Why important]
      - [Important code snippet]

4. Errors and Fixes:
    - [Error]:
      - [Fix]

5. Problem Solving:
   [Description]

6. All User Messages:
    - [Non-tool-result user turn]

7. Pending Tasks:
   - [Task]

8. Current Work:
   [Precise description]

9. Optional Next Step:
   [Next step + direct quote]
</summary>
</example>

Please provide your summary based on the input below, following this structure with precision and thoroughness. If `## Compact Instructions` are included with the input, honor them.
"""


# Reduce-phase prompt — from OpenClaw `compaction.ts:24-40`, adapted to
# merge N partial summaries produced by the map phase above.
REDUCE_MERGE_BASE = """Merge these partial summaries into a single cohesive summary.

MUST PRESERVE:
- Active tasks and their current status (in-progress, blocked, pending)
- Batch operation progress (e.g. "5/17 items completed")
- The last thing the user requested and what was being done about it
- Decisions made and their rationale
- TODOs, open questions, and constraints
- Any commitments or follow-ups promised
- File names, function names, identifiers exactly as in the partials

PRIORITIZE recent context over older history. The downstream agent needs to know what was being done, not just what was discussed.

{identifier_preservation}

Before providing your merged summary, wrap your analysis in <analysis> tags. Inside <analysis>, reconcile conflicts between partials (later partial wins unless explicitly noted) and drop duplicates. Then produce the final summary inside <summary> tags using the same 9-section structure as the partials (omit sections that don't apply).
"""


# -- utilities -----------------------------------------------


_ANALYSIS_RE = re.compile(r"<analysis>[\s\S]*?</analysis>", re.IGNORECASE)
_SUMMARY_RE = re.compile(r"<summary>([\s\S]*?)</summary>", re.IGNORECASE)


def _estimate_tokens(text: str) -> int:
    """Bytes-per-token estimate. Good enough for budget checking —
    the actual tokenizer depends on the provider and isn't worth a
    dependency here."""
    if not text:
        return 0
    return max(1, len(text.encode("utf-8", errors="replace"))
                   // BYTES_PER_TOKEN_ESTIMATE)


def _head_tail_truncate(text: str, max_bytes: int) -> tuple[str, bool]:
    """Keep head + tail, drop middle, insert marker. Returns (text, truncated)."""
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return text, False
    # Reserve ~200 bytes for the marker; split remainder 50/50.
    marker = b"\n\n[... middle elided for length ...]\n\n"
    budget = max_bytes - len(marker)
    half = budget // 2
    head = raw[:half]
    tail = raw[-half:]
    # Decode back, ignoring partial char errors at boundaries.
    return (head.decode("utf-8", errors="ignore")
             + marker.decode("utf-8")
             + tail.decode("utf-8", errors="ignore"),
            True)


def _format_summary(raw: str) -> str:
    """Strip <analysis> scratchpad, extract <summary> body, clean up.

    Ported from CC `prompt.ts:311-335`. The analysis tag is a drafting
    scratchpad — it improves summary quality but has no informational
    value once the summary is written, so we remove it before handing
    the text downstream.
    """
    if not raw:
        return ""
    text = _ANALYSIS_RE.sub("", raw)
    m = _SUMMARY_RE.search(text)
    if m:
        return m.group(1).strip()
    # Summary tag not found — return cleaned text as-is, caller still
    # gets something usable.
    return text.strip()


def _build_map_prompt(custom_instructions: Optional[str]) -> str:
    prompt = NO_TOOLS_PREAMBLE + MAP_COMPRESS_BASE.format(
        identifier_preservation=IDENTIFIER_PRESERVATION)
    if custom_instructions and custom_instructions.strip():
        prompt += f"\n\nAdditional Instructions:\n{custom_instructions.strip()}"
    prompt += NO_TOOLS_TRAILER
    return prompt


def _build_reduce_prompt(custom_instructions: Optional[str]) -> str:
    prompt = NO_TOOLS_PREAMBLE + REDUCE_MERGE_BASE.format(
        identifier_preservation=IDENTIFIER_PRESERVATION)
    if custom_instructions and custom_instructions.strip():
        prompt += f"\n\nAdditional Instructions:\n{custom_instructions.strip()}"
    prompt += NO_TOOLS_TRAILER
    return prompt


def _build_map_user_message(doc_id: str, body: str, task: str) -> str:
    parts = [
        f"DOCUMENT ID: {doc_id}",
        f"TASK (what downstream will do): {task or 'not specified'}",
        "",
        "INPUT TO SUMMARIZE:",
        body,
    ]
    return "\n".join(parts)


def _build_reduce_user_message(partials: list[dict], task: str) -> str:
    parts = [f"TASK (what downstream will do): {task or 'not specified'}",
              ""]
    for p in partials:
        parts.append(f"### Partial from {p['id']}")
        parts.append(p["summary"])
        parts.append("")
    return "\n".join(parts)


# -- LLM call wrapper ----------------------------------------


async def _call_llm(ctx, *, system_prompt: str, user: str,
                      pool: Optional[str], model: Optional[str],
                      max_tokens: Optional[int] = None) -> Optional[str]:
    """Returns the raw content string on success, None on failure.
    Failure modes: LookupError (llm_driver not loaded), exception,
    ok=false, empty content. All reduce to None so the caller can
    decide fallback policy per document."""
    payload = {
        "op": "run_turn",
        "system_prompt": system_prompt,
        "messages": [{"role": "user", "content": user}],
        "pool": pool, "model": model,
        "temperature": 0.2,          # low but not zero — summaries are generative
        "json_mode": False,          # we parse <summary> tags, not JSON
        "max_iterations": 1,
        "strip_thinking_blocks": True,
        "llm_timeout": 120.0,
    }
    if max_tokens is not None:
        payload["max_tokens"] = int(max_tokens)
    try:
        resp = await ctx.bus.request("llm_driver", payload, timeout=180.0)
    except LookupError:
        log.info("context_compressor: llm_driver not loaded — fallback")
        return None
    except Exception as e:
        log.warning("context_compressor: llm_driver raised: %s", e)
        return None
    if not isinstance(resp, dict) or not resp.get("ok"):
        log.warning("context_compressor: llm_driver not ok: %s",
                    resp if not isinstance(resp, dict)
                    else resp.get("error"))
        return None
    content = resp.get("content") or ""
    if not content.strip():
        return None
    return content


# -- map / reduce --------------------------------------------


async def _map_one(ctx, doc: dict, task: str, system_prompt: str,
                     max_bytes: int,
                     pool: Optional[str], model: Optional[str],
                     fallback_enabled: bool) -> dict:
    """Summarize one document. Returns a normalized result dict."""
    doc_id = str(doc.get("id") or "doc")
    body = str(doc.get("body") or "")
    original_tokens = _estimate_tokens(body)
    if not body.strip():
        return {"id": doc_id, "summary": "",
                 "original_tokens": 0, "summary_tokens": 0,
                 "fallback_used": False, "note": "empty input"}

    trimmed, pre_truncated = _head_tail_truncate(body, max_bytes)
    user = _build_map_user_message(doc_id, trimmed, task)
    raw = await _call_llm(ctx, system_prompt=system_prompt, user=user,
                           pool=pool, model=model)
    if raw is not None:
        summary = _format_summary(raw)
        if summary:
            return {
                "id": doc_id,
                "summary": summary,
                "original_tokens": original_tokens,
                "summary_tokens": _estimate_tokens(summary),
                "fallback_used": False,
                "pre_truncated": pre_truncated,
            }

    # Fallback — head+tail byte trim to roughly 1/4 the input.
    if not fallback_enabled:
        return {"id": doc_id, "summary": "",
                 "original_tokens": original_tokens, "summary_tokens": 0,
                 "fallback_used": True, "note": "llm failed; fallback disabled"}
    fb_budget = max(1024, min(max_bytes // 4, 8192))
    fb, _ = _head_tail_truncate(body, fb_budget)
    return {
        "id": doc_id,
        "summary": fb,
        "original_tokens": original_tokens,
        "summary_tokens": _estimate_tokens(fb),
        "fallback_used": True,
    }


async def _reduce(ctx, partials: list[dict], task: str,
                   system_prompt: str,
                   pool: Optional[str], model: Optional[str],
                   fallback_enabled: bool) -> tuple[str, bool]:
    """Merge partials into one. Returns (merged_summary, fallback_used)."""
    # Filter out empty summaries — they don't contribute.
    usable = [p for p in partials if p.get("summary", "").strip()]
    if not usable:
        return "", False
    if len(usable) == 1:
        return usable[0]["summary"], usable[0].get("fallback_used", False)

    user = _build_reduce_user_message(usable, task)
    raw = await _call_llm(ctx, system_prompt=system_prompt, user=user,
                           pool=pool, model=model)
    if raw is not None:
        merged = _format_summary(raw)
        if merged:
            return merged, False

    if not fallback_enabled:
        return "\n\n".join(
            f"## {p['id']}\n{p['summary']}" for p in usable), True

    # Fallback merge: concatenate with section headers. Crude but preserves
    # every partial so the downstream has everything.
    concat = "\n\n".join(
        f"## Partial from {p['id']}\n{p['summary']}" for p in usable)
    return concat, True


# -- event publishing ----------------------------------------


async def _publish_compressed(ctx, op: str, result: dict) -> None:
    bus = getattr(ctx, "bus", None)
    if bus is None:
        return
    payload = {
        "op": op,
        "document_count": len(result.get("per_document", [])),
        "total_tokens_before": result.get("total_tokens_before", 0),
        "total_tokens_after": result.get("total_tokens_after", 0),
        "savings_ratio": result.get("savings_ratio", 0.0),
        "fallback_used": result.get("fallback_used", False),
        "skipped": result.get("skipped", False),
    }
    try:
        await bus.publish(COMPRESSED_TOPIC, payload)
    except Exception:
        log.exception("context_compressor: publish %s raised",
                      COMPRESSED_TOPIC)


# -- op entry ------------------------------------------------


async def _op_summarize(input: dict, ctx) -> dict:
    documents = input.get("documents")
    if not isinstance(documents, list) or not documents:
        return {"ok": False, "error": "summarize requires non-empty documents list"}
    clean: list[dict] = []
    for i, d in enumerate(documents):
        if not isinstance(d, dict):
            return {"ok": False, "error": f"documents[{i}] not a dict"}
        if not isinstance(d.get("body"), str):
            return {"ok": False, "error": f"documents[{i}].body must be str"}
        did = d.get("id")
        clean.append({"id": str(did) if did is not None else f"doc{i}",
                       "body": d["body"]})

    task = str(input.get("task") or "")
    custom_instructions = input.get("custom_instructions")
    target_tokens = int(input.get("target_tokens") or DEFAULT_TARGET_TOKENS)
    max_bytes_per_map = int(input.get("max_bytes_per_map")
                             or DEFAULT_MAX_BYTES_PER_MAP)
    fallback_enabled = bool(input.get("fallback_enabled", True))
    pool = input.get("pool") or os.environ.get("CONTEXT_COMPRESSOR_POOL")
    model = input.get("model") or os.environ.get("CONTEXT_COMPRESSOR_MODEL")

    total_before = sum(_estimate_tokens(d["body"]) for d in clean)

    # Short-circuit: input already under budget → no-op pass-through.
    if total_before <= target_tokens:
        concat = "\n\n".join(
            f"## {d['id']}\n{d['body']}" for d in clean)
        result = {
            "ok": True,
            "merged_summary": concat,
            "per_document": [
                {"id": d["id"], "original_tokens": _estimate_tokens(d["body"]),
                 "summary_tokens": _estimate_tokens(d["body"]),
                 "summary": d["body"], "fallback_used": False}
                for d in clean
            ],
            "total_tokens_before": total_before,
            "total_tokens_after": total_before,
            "savings_ratio": 0.0,
            "fallback_used": False,
            "skipped": True,
        }
        await _publish_compressed(ctx, "summarize", result)
        return result

    map_prompt = _build_map_prompt(custom_instructions)
    reduce_prompt = _build_reduce_prompt(custom_instructions)

    # Map phase — serialized by default to stay gentle with MiniMax
    # concurrency limits. Parallelism is a future extension if needed.
    per_doc: list[dict] = []
    any_fallback = False
    for d in clean:
        r = await _map_one(ctx, d, task, map_prompt, max_bytes_per_map,
                             pool, model, fallback_enabled)
        per_doc.append(r)
        if r.get("fallback_used"):
            any_fallback = True

    # Reduce phase (if needed).
    if len(per_doc) == 1:
        merged = per_doc[0]["summary"]
        reduce_fallback = per_doc[0].get("fallback_used", False)
    else:
        merged, reduce_fallback = await _reduce(
            ctx, per_doc, task, reduce_prompt, pool, model, fallback_enabled)
    if reduce_fallback:
        any_fallback = True

    total_after = _estimate_tokens(merged)
    savings = ((total_before - total_after) / total_before
                if total_before > 0 else 0.0)
    result = {
        "ok": True,
        "merged_summary": merged,
        "per_document": [
            {"id": r["id"], "original_tokens": r["original_tokens"],
             "summary_tokens": r["summary_tokens"],
             "summary": r["summary"],
             "fallback_used": r.get("fallback_used", False)}
            for r in per_doc
        ],
        "total_tokens_before": total_before,
        "total_tokens_after": total_after,
        "savings_ratio": round(savings, 3),
        "fallback_used": any_fallback,
        "skipped": False,
    }
    await _publish_compressed(ctx, "summarize", result)
    return result


async def execute(input: dict, ctx) -> dict:
    op = (input or {}).get("op")
    if op == "summarize":
        return await _op_summarize(input, ctx)
    return {"ok": False, "error": f"unknown op: {op!r}"}
