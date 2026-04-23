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
