"""Shared helpers for agents that reply via the gateway.

Reply pattern (upgraded from plain `op:send`):
1. Agent produces `(content: str, footer_meta: list[tuple[str, str]])`.
2. `reply_via_gateway(...)` opens a draft (`op: open_draft`) with content +
   footer + optional quote, then closes it (`op: close_draft`). This gets
   platform-native rendering on Telegram / Feishu (italic key:value footer,
   threaded quote block, etc.).
3. If the draft path fails (unknown session, no adapter, transient bus
   error), falls back to plain `op: send` with the footer inlined as
   italic markdown so the user still sees the stats.

All three draft-pattern agents (reflection_agent / memory_curator /
harness_pro_max) use this helper so they share a single code path for
replying, and so adding a new field (e.g. budget stats) to all replies is
a single edit here.

Helpers that build `footer_meta` from standard shapes:
- `format_llm_stats_footer(stats)` — convert llm_driver aggregate stats dict
  to a list of footer rows.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)


def format_llm_stats_footer(stats: Optional[dict]) -> list[tuple[str, str]]:
    """Turn `{n_calls, elapsed_ms, output_tps, prompt_tokens,
    completion_tokens}` into footer rows. Empty list if `stats` is empty or
    `n_calls` is 0 (agent didn't make an LLM call this run).
    """
    if not stats or not stats.get("n_calls"):
        return []
    rows: list[tuple[str, str]] = [("calls", str(stats["n_calls"]))]
    elapsed_ms = stats.get("elapsed_ms") or 0
    rows.append(("elapsed", f"{elapsed_ms / 1000.0:.1f}s"))
    tps = stats.get("output_tps")
    if tps:
        rows.append(("tok/s", f"{tps}"))
    rows.append((
        "tokens",
        f"{stats.get('prompt_tokens') or 0} in / "
        f"{stats.get('completion_tokens') or 0} out",
    ))
    return rows


def format_footer_inline(footer: list[tuple[str, str]]) -> str:
    """Collapse footer rows to an italic one-liner for the plain-text fallback
    (and for console / test display)."""
    if not footer:
        return ""
    return "_" + " | ".join(f"{k}: {v}" for k, v in footer) + "_"


def compose_fallback_text(content: str,
                          footer: list[tuple[str, str]]) -> str:
    """Content + inline italic footer, joined by a blank line."""
    line = format_footer_inline(footer)
    if not line:
        return content
    return f"{content}\n\n{line}"


async def reply_via_gateway(bus, session_key: str, *,
                             content: str,
                             footer_meta: list[tuple[str, str]],
                             quote_user: Optional[str] = None,
                             quote_text: Optional[str] = None,
                             agent_name: str = "agent",
                             timeout: float = 5.0) -> None:
    """Post a reply to the gateway using the structured draft path.

    Args:
        bus: the Bus instance (typically `ctx.bus`)
        session_key: the recipient session (from gateway.command_invoked)
        content: main body markdown
        footer_meta: structured metrics; gateway adapters render as italic
                     `key: value | ...` footer (or platform equivalent)
        quote_user / quote_text: optional — shows the user's original message
                                 as a threaded quote above the reply
        agent_name: for log lines so multi-agent crashes are disambiguable
        timeout: bus.request timeout per call
    """
    if not session_key:
        return
    quote_payload: dict = {}
    if quote_text:
        if quote_user:
            quote_payload["user"] = quote_user
        quote_payload["text"] = quote_text

    try:
        r = await bus.request("gateway", {
            "op": "open_draft",
            "session_key": session_key,
            "content": content,
            "footer_meta": [list(t) for t in footer_meta],
            "quote": quote_payload,
        }, timeout=timeout)
    except Exception:
        log.exception("%s: gateway open_draft raised", agent_name)
        r = {"ok": False}

    if isinstance(r, dict) and r.get("ok") and r.get("draft_id"):
        try:
            await bus.request("gateway", {
                "op": "close_draft", "draft_id": r["draft_id"],
            }, timeout=timeout)
        except Exception:
            log.exception("%s: gateway close_draft raised", agent_name)
        return

    # Fallback — plain text with footer inlined, so user still sees everything.
    try:
        await bus.request("gateway", {
            "op": "send", "session_key": session_key,
            "text": compose_fallback_text(content, footer_meta),
        }, timeout=timeout)
    except Exception:
        log.exception("%s: gateway send fallback failed", agent_name)
