"""compactor — stateless conversation compaction helpers.

Port of Claude Code 2.1.88's context compaction machinery, two ops:

- `microcompact` (L1-equivalent):
    Keep the last N tool_result messages verbatim; replace the content of
    older tool_results with `[Old tool result content cleared]`. Pure
    string/list manipulation, no LLM call. CC's cache_edits trick lets
    its equivalent reuse prompt cache bytes; yuxu runs on MiniMax which
    has no Anthropic-style prompt cache, so we do the cheaper variant:
    rewrite the list in place. Callers invoke this when a tool-heavy
    iteration pushes conversation past a byte / iteration threshold.

- `full_compact` (L4-equivalent):
    Summarise all-but-the-last-N turns via an LLM call, emit
    `[summary_user_msg, compact_boundary_marker, *recent_turns]`. Ported
    verbatim from CC's 9-section structured prompt (Primary Request /
    Key Technical Concepts / Files and Code Sections / Errors and Fixes
    / Problem Solving / All User Messages / Pending Tasks / Current
    Work / Optional Next Step). CC does this when the context window
    is near-full (<13k remaining) OR on manual trigger; this skill is
    the mechanism — triggering lives with the caller.

Neither op auto-runs anywhere yet. `llm_driver.run_turn` does NOT call
this — per the design note the user gave 2026-04-24: "加上但先不加触发器"
(add the mechanism, not the automatic trigger). Triggers are TODOs
pinned in `project_pending_todos.md` under 🔧 compaction.

Shape (matches OpenAI-style messages list that llm_driver already uses):
    [{role: "system"|"user"|"assistant"|"tool", content: str|list,
      tool_call_id?: str, tool_calls?: list}, ...]
"""
from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

NAME = "compactor"

# Verbatim from CC `services/compact/microCompact.ts:36`.
CLEARED_MARKER = "[Old tool result content cleared]"

# Default: keep the last 5 tool results — matches CC's
# `tengu_slate_heron.keepRecent=5` GrowthBook config
# (`timeBasedMCConfig.ts:30-34`). Caller can override.
DEFAULT_KEEP_RECENT = 5

# Default: keep the last 5 turns (user+assistant round-trips) when full-
# compacting. CC's `POST_COMPACT_MAX_FILES_TO_RESTORE=5` is about file
# attachments, not turns — yuxu's equivalent is simpler so we reuse 5
# as a reasonable start. Tune after dogfood.
DEFAULT_KEEP_RECENT_TURNS = 5

# Verbatim from CC `services/compact/messages.ts:4530-4555` —
# subtype inserted between the summary message and the preserved turns
# so future inspectors can spot where compaction cut.
COMPACT_BOUNDARY_MARKER = {
    "role": "system",
    "content": "[compact_boundary] conversation summarised above; full history cleared.",
}

# Verbatim port of CC's 9-section summary prompt — do not reword. CC
# iterated this over many releases; the exact section names are what the
# model is trained to honour. (`services/compact/prompt.ts:61-131`.)
FULL_COMPACT_SYSTEM_PROMPT = """Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.
This summary should be thorough in capturing technical details, code patterns, and architectural decisions that would be essential for continuing development work without losing context.

Your summary should include the following sections:

1. Primary Request and Intent: Capture all of the user's explicit requests and intents in detail.
2. Key Technical Concepts: List all important technical concepts, technologies, and frameworks discussed.
3. Files and Code Sections: Enumerate specific files and code sections examined, modified, or created. Pay special attention to the most recent messages and include full code snippets where applicable and include a summary of why this file read or edit is important.
4. Errors and fixes: List all errors that you ran into, and how you fixed them. Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
5. Problem Solving: Document problems solved and any ongoing troubleshooting efforts.
6. All user messages: List ALL user messages that are not tool results. These are critical for understanding the users' feedback and changing intent.
7. Pending Tasks: Outline any pending tasks that you have explicitly been asked to work on.
8. Current Work: Describe in detail precisely what was being worked on immediately before this summary request, paying special attention to the most recent messages from both user and assistant. Include file names and code snippets where applicable.
9. Optional Next Step: List the next step that you will take that is related to the most recent work you were doing. IMPORTANT: ensure that this step is DIRECTLY in line with the user's most recent explicit requests, and the task you were working on immediately before this summary request.

Respond with the summary directly — no preamble, no `<analysis>` block, just the nine numbered sections in order.
"""


# -- microcompact --------------------------------------------------


def microcompact(messages: list[dict], *,
                   keep_recent: int = DEFAULT_KEEP_RECENT) -> dict:
    """Replace the content of older tool messages with CLEARED_MARKER.

    Keeps the last `keep_recent` tool messages verbatim. Non-tool
    messages (system / user / assistant) pass through unchanged. Returns
    a NEW list; input is not mutated.

    Returns {messages: [...], cleared_count: N, tool_count: M}.
    """
    if not isinstance(messages, list):
        return {"messages": messages, "cleared_count": 0, "tool_count": 0}
    if keep_recent < 0:
        keep_recent = 0

    tool_indices = [i for i, m in enumerate(messages)
                    if isinstance(m, dict) and m.get("role") == "tool"]
    tool_count = len(tool_indices)
    if tool_count <= keep_recent:
        return {"messages": list(messages), "cleared_count": 0,
                "tool_count": tool_count}

    # Older tools = all except the last keep_recent.
    clear_set = set(tool_indices[:-keep_recent]) if keep_recent > 0 \
                 else set(tool_indices)
    cleared = 0
    out: list[dict] = []
    for i, m in enumerate(messages):
        if i in clear_set and isinstance(m, dict):
            # Preserve role + tool_call_id so the message list stays a
            # valid conversation; only the content (likely huge stdout /
            # file contents) gets stripped.
            new_msg = {**m, "content": CLEARED_MARKER}
            out.append(new_msg)
            cleared += 1
        else:
            out.append(m)
    return {"messages": out, "cleared_count": cleared, "tool_count": tool_count}


# -- full_compact --------------------------------------------------


def _turn_index(messages: list[dict]) -> list[int]:
    """Return indices of user messages — each one begins a turn. We
    count from the user message (not the assistant reply) because a
    "turn" conventionally starts when the user speaks."""
    return [i for i, m in enumerate(messages)
            if isinstance(m, dict) and m.get("role") == "user"]


def _stringify(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Anthropic-style content blocks: join text blocks.
        parts = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("text") or block.get("content")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    return str(content)


def _render_for_summary(messages: list[dict]) -> str:
    """Flatten messages into a prose stream for the summariser. Tool
    results show only their first 500 chars — the whole point is to
    compress them out."""
    lines: list[str] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "?")
        if role == "tool":
            body = _stringify(m.get("content"))
            if len(body) > 500:
                body = body[:500] + f"…[+{len(body) - 500} chars]"
            lines.append(f"[tool_result]\n{body}")
        elif role == "assistant":
            body = _stringify(m.get("content"))
            tcs = m.get("tool_calls") or []
            if tcs:
                fns = ", ".join(
                    (tc.get("function") or {}).get("name", "?")
                    for tc in tcs if isinstance(tc, dict)
                )
                lines.append(f"[assistant — tool_calls: {fns}]\n{body}")
            else:
                lines.append(f"[assistant]\n{body}")
        elif role == "user":
            lines.append(f"[user]\n{_stringify(m.get('content'))}")
        elif role == "system":
            continue  # system prompt reconstructed by caller, not needed here
    return "\n\n".join(lines)


async def full_compact(messages: list[dict], *,
                          bus,
                          pool: str,
                          model: str,
                          keep_recent_turns: int = DEFAULT_KEEP_RECENT_TURNS,
                          llm_timeout: float = 120.0,
                          max_tokens: Optional[int] = None) -> dict:
    """Summarise the prefix, keep the last `keep_recent_turns` turns.

    Returns {ok, messages, summary, cleared_count, usage} on success,
    {ok: false, error} on failure. On failure, caller should fall back
    to microcompact or accept the unchanged messages — full_compact
    must never silently corrupt the list.
    """
    if not isinstance(messages, list) or not messages:
        return {"ok": False, "error": "messages must be a non-empty list"}
    if keep_recent_turns < 0:
        keep_recent_turns = 0

    turn_indices = _turn_index(messages)
    if len(turn_indices) <= keep_recent_turns:
        return {"ok": True, "messages": list(messages), "summary": "",
                "cleared_count": 0, "usage": {}, "skipped": "not enough turns"}

    # cut = first index that belongs to the preserved tail
    cut = turn_indices[-keep_recent_turns] if keep_recent_turns > 0 else len(messages)
    prefix = messages[:cut]
    tail = messages[cut:]

    rendered = _render_for_summary(prefix)
    if not rendered.strip():
        return {"ok": True, "messages": list(messages), "summary": "",
                "cleared_count": 0, "usage": {}, "skipped": "empty prefix"}

    req: dict = {
        "op": "run_turn",
        "system_prompt": FULL_COMPACT_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": rendered}],
        "pool": pool, "model": model,
        "temperature": 0.2,
        "max_iterations": 1,
        "strip_thinking_blocks": True,
        "llm_timeout": llm_timeout,
    }
    if max_tokens is not None:
        req["max_tokens"] = max_tokens
    try:
        resp = await bus.request("llm_driver", req, timeout=llm_timeout + 30.0)
    except Exception as e:  # noqa: BLE001
        log.exception("compactor.full_compact: bus.request raised")
        return {"ok": False, "error": f"llm_driver: {e}"}
    if not isinstance(resp, dict) or not resp.get("ok"):
        return {"ok": False,
                "error": (resp or {}).get("error", "llm_driver returned not-ok"),
                "raw": resp}
    summary = resp.get("content") or ""
    if not summary.strip():
        return {"ok": False, "error": "llm returned empty summary"}

    summary_msg = {
        "role": "user",
        "content": (
            "The following is a summary of the prior conversation — "
            "it replaces the raw history to save context. Continue from "
            "here.\n\n" + summary
        ),
    }
    new_messages = [summary_msg, dict(COMPACT_BOUNDARY_MARKER), *tail]
    return {
        "ok": True,
        "messages": new_messages,
        "summary": summary,
        "cleared_count": len(prefix),
        "usage": resp.get("usage") or {},
        "elapsed_ms": resp.get("elapsed_ms"),
    }


# -- op dispatcher -------------------------------------------------


async def execute(input: dict, ctx) -> dict:
    if not isinstance(input, dict):
        return {"ok": False, "error": "input must be a dict"}
    # Tolerate llm_driver's tool-call envelope {"op":"execute","input":...}
    if input.get("op") == "execute" and isinstance(input.get("input"), dict):
        input = input["input"]
    op = input.get("op")
    if op == "microcompact":
        result = microcompact(
            input.get("messages") or [],
            keep_recent=int(input.get("keep_recent", DEFAULT_KEEP_RECENT)),
        )
        return {"ok": True, **result}
    if op == "full_compact":
        pool = input.get("pool")
        model = input.get("model")
        if not pool or not model:
            return {"ok": False, "error": "full_compact requires pool + model"}
        return await full_compact(
            input.get("messages") or [],
            bus=ctx.bus,
            pool=pool, model=model,
            keep_recent_turns=int(
                input.get("keep_recent_turns", DEFAULT_KEEP_RECENT_TURNS)
            ),
            llm_timeout=float(input.get("llm_timeout", 120.0)),
            max_tokens=input.get("max_tokens"),
        )
    return {"ok": False, "error": f"unknown op: {op!r}"}
