"""memory skill — two-layer progressive disclosure over <project>/data/memory.

Reads only. Writes still flow through memory_curator → approval_queue →
approval_applier per I6 (scope-crossing writes gated by approval).

`list`: scan memory_root, parse frontmatter ONLY, return {name, description,
        type, path, bytes}. Index size is O(Nfiles) regardless of body length.
`get`:  load a specific entry's full body + parsed frontmatter.

Mirrors Claude Code skill loading (name + description up front, body on demand)
and OpenClaw's 2-layer memory convention.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable, Optional

from yuxu.core.frontmatter import parse_frontmatter

log = logging.getLogger(__name__)

# Hidden from the index — curator staging area and the append-only log aren't
# "entries" callers should reason about.
SKIP_DIRS = {"_drafts"}
SKIP_FILES = {"_improvement_log.md"}


def _resolve_memory_root(override: Optional[str], ctx) -> Path:
    if override:
        return Path(override).expanduser().resolve()
    start = Path(getattr(ctx, "agent_dir", ".")).resolve()
    for cand in (start, *start.parents):
        if (cand / "yuxu.json").exists():
            return cand / "data" / "memory"
    return Path.cwd() / "data" / "memory"


def _iter_memory_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return
    for p in sorted(root.rglob("*.md")):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        if p.name in SKIP_FILES:
            continue
        if p.name.startswith("."):
            continue
        yield p


def _read_entry_summary(path: Path, root: Path) -> Optional[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("memory: could not read %s: %s", path, e)
        return None
    fm, _ = parse_frontmatter(text)
    name = fm.get("name")
    description = fm.get("description")
    if not isinstance(name, str) or not isinstance(description, str):
        # Un-frontmattered files aren't first-class memory entries.
        return None
    return {
        "path": str(path.relative_to(root)),
        "name": name,
        "description": description,
        "type": fm.get("type"),
        "bytes": len(text.encode("utf-8", errors="replace")),
    }


async def _op_list(input: dict, ctx) -> dict:
    root = _resolve_memory_root(input.get("memory_root"), ctx)
    if not root.exists():
        return {"ok": True, "memory_root": str(root), "entries": []}
    types_filter = input.get("types")
    if types_filter is not None and not isinstance(types_filter, list):
        return {"ok": False, "error": "types must be a list of strings"}
    wanted = set(types_filter) if types_filter else None

    entries: list[dict] = []
    for p in _iter_memory_files(root):
        summary = _read_entry_summary(p, root)
        if summary is None:
            continue
        if wanted is not None and summary.get("type") not in wanted:
            continue
        entries.append(summary)
    return {"ok": True, "memory_root": str(root), "entries": entries}


async def _op_get(input: dict, ctx) -> dict:
    raw_path = input.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return {"ok": False, "error": "missing or empty field: path"}
    root = _resolve_memory_root(input.get("memory_root"), ctx)
    p = Path(raw_path).expanduser()
    if not p.is_absolute():
        p = (root / p).resolve()
    else:
        p = p.resolve()
    try:
        p.relative_to(root.resolve())
    except ValueError:
        return {"ok": False, "error": f"path escapes memory_root: {p}"}
    if not p.is_file():
        return {"ok": False, "error": f"not a file: {p}"}
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": f"read failed: {e}"}
    fm, body = parse_frontmatter(text)
    return {
        "ok": True,
        "path": str(p.relative_to(root.resolve())),
        "frontmatter": fm,
        "body": body,
        "bytes": len(text.encode("utf-8", errors="replace")),
    }


async def execute(input: dict, ctx) -> dict:
    op = (input or {}).get("op")
    if op == "list":
        return await _op_list(input, ctx)
    if op == "get":
        return await _op_get(input, ctx)
    return {"ok": False, "error": f"unknown op: {op!r}"}
