"""Per-agent transcript log — append-only JSONL.

Writes lifecycle events (from loader) and message turns (from llm_driver) to
`<project_root>/data/sessions/<agent>.jsonl`, one JSON object per line. The
file persists across restarts; lifecycle lines (`event: "lifecycle"`) serve
as run separators so consumers (e.g. memory_curator) can split runs.

Schema (all entries):
  {ts: float, event: "lifecycle"|"message", ...extras}

Lifecycle extras: state (one of loader's STATES), optional reason.
Message extras: role (user|assistant|tool), content, optional tool_calls,
                optional tool_call_id, optional iteration.

Design notes:
- No-op when no yuxu.json found walking up from agent_dir. Keeps tests that
  don't set up a project free from accidental writes.
- Per-path asyncio.Lock to serialize appends within one process (multiple
  coroutines writing to the same file). Cross-process isn't needed because
  yuxu runs one daemon per project.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_path_locks: dict[str, asyncio.Lock] = {}


def find_project_root(start: Path) -> Optional[Path]:
    """Walk up from `start` looking for yuxu.json. Returns None if not found."""
    start = Path(start).resolve()
    for cand in (start, *start.parents):
        if (cand / "yuxu.json").exists():
            return cand
    return None


def transcript_path_for(project_root: Path, agent: str) -> Path:
    return project_root / "data" / "sessions" / f"{agent}.jsonl"


def resolve_transcript_path(agent_dir: Path, agent: str) -> Optional[Path]:
    """Return the transcript path for `agent`, or None if no project root found."""
    root = find_project_root(agent_dir)
    if root is None:
        return None
    return transcript_path_for(root, agent)


async def append(agent_dir: Path, agent: str, entry: dict) -> Optional[Path]:
    """Atomically append a JSONL line. Returns path written, or None if no-op.

    `entry` is merged with a fresh `ts` (float seconds). Caller-supplied `ts`
    is overwritten to keep the timeline monotonic and trust-worthy.
    """
    path = resolve_transcript_path(agent_dir, agent)
    if path is None:
        return None
    line_obj: dict[str, Any] = {"ts": time.time(), **entry}
    line = json.dumps(line_obj, ensure_ascii=False) + "\n"
    lock = _path_locks.setdefault(str(path), asyncio.Lock())
    async with lock:
        await asyncio.to_thread(_sync_append, path, line)
    return path


def _sync_append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


# -- transcript reader / formatter --------------------------------

# Cap a single entry's rendered body so one runaway message (e.g. a huge
# tool output) can't starve the rest of the transcript when the formatter
# budgets total size.
_ENTRY_BODY_CAP_CHARS = 2_000


def _fmt_ts(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%SZ",
        )
    except (TypeError, ValueError):
        return "?"


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[:n] + f"\n...(+{len(s) - n} chars truncated)"


def _render_entry(entry: dict) -> str:
    event = entry.get("event")
    ts = _fmt_ts(entry.get("ts"))
    if event == "lifecycle":
        state = entry.get("state", "?")
        reason = entry.get("reason")
        tail = f" — {reason}" if reason else ""
        return f"--- [{ts}] lifecycle: {state}{tail} ---"
    if event == "message":
        role = entry.get("role", "?")
        kind = entry.get("kind")
        iteration = entry.get("iteration")
        iter_tag = f" iter={iteration}" if iteration is not None else ""
        tool_name = entry.get("tool_name")
        tool_id = entry.get("tool_call_id")
        body = entry.get("content") or ""
        if isinstance(body, list):
            # Anthropic-style content blocks — flatten to text.
            body = "".join(
                (b.get("text") or b.get("thinking") or "") if isinstance(b, dict) else str(b)
                for b in body
            )
        body = _truncate(str(body).strip(), _ENTRY_BODY_CAP_CHARS)

        if kind == "reasoning":
            header = f"[{ts}] ASSISTANT reasoning{iter_tag}"
        elif role == "assistant":
            tool_calls = entry.get("tool_calls") or []
            if tool_calls:
                tc_desc = ", ".join(
                    f"{(tc.get('function') or {}).get('name', '?')}"
                    for tc in tool_calls
                )
                header = f"[{ts}] ASSISTANT tool_use{iter_tag} → {tc_desc}"
            else:
                header = f"[{ts}] ASSISTANT{iter_tag}"
        elif role == "tool":
            label = tool_name or tool_id or "?"
            header = f"[{ts}] TOOL_RESULT {label}{iter_tag}"
        elif role == "user":
            header = f"[{ts}] USER{iter_tag}"
        else:
            header = f"[{ts}] {role.upper() if isinstance(role, str) else role}{iter_tag}"
        return f"{header}\n{body}" if body else header
    # Unknown event: still surface it so investigators can see.
    return f"[{ts}] {event or '?'}: {json.dumps(entry, ensure_ascii=False)}"


def format_jsonl_transcript(path: Path | str, *,
                             max_chars: Optional[int] = None) -> str:
    """Render a session JSONL transcript as human/LLM-readable text.

    Each entry becomes one or two lines (header + optional body). Lifecycle
    markers are horizontal rules so a multi-run transcript reads as
    discrete sessions. If `max_chars` is set, emits the TAIL of the
    rendered text (most recent runs), since curation cares most about the
    latest session.

    Malformed lines are skipped with a warning rather than raising —
    transcripts are append-only and a partial write shouldn't brick reads.
    """
    path = Path(path)
    if not path.exists():
        return ""
    pieces: list[str] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("session_log: cannot read %s: %s", path, e)
        return ""
    for i, line in enumerate(raw.splitlines()):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            log.warning("session_log: %s line %d not JSON, skipping", path, i + 1)
            continue
        if not isinstance(entry, dict):
            continue
        pieces.append(_render_entry(entry))

    rendered = "\n\n".join(pieces)
    if max_chars is not None and len(rendered) > max_chars:
        # Keep the tail; prepend a truncation marker so the LLM knows.
        cut = rendered[-max_chars:]
        # Snap to the next line break for clean chunking.
        nl = cut.find("\n")
        if 0 <= nl < 200:
            cut = cut[nl + 1:]
        rendered = (f"[...earlier session history truncated; last "
                    f"{len(cut)} chars shown...]\n\n{cut}")
    return rendered
