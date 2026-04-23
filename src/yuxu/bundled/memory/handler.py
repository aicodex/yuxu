"""memory skill — progressive disclosure over <project>/data/memory.

Reads only. Writes still flow through memory_curator → approval_queue →
approval_applier per I6 (scope-crossing writes gated by approval).

Ops:
- `stats`:  L0 — counts by type / scope / status / evidence_level; independent
            of total entry count, so cheap at any scale.
- `list`:   L1 — filtered index (frontmatter only). Accepts `mode` + explicit
            filter params; mode sets defaults, explicit params override.
- `get`:    L2 — full body + parsed frontmatter for one entry.
- `search`: cross-cut — keyword match on name + description, ranked top-K.

Modes (per I6 Memory access discipline):
- `blank`   → only entries tagged `mandatory` (I6 reserved tag)
- `explore` → same as `blank` (mandatory-only)
- `execute` → evidence_level ∈ {validated, consensus, observed}, status=current,
              probation excluded — the operational default
- `reflect` → no restrictions (includes archived + probation)
- `debug`   → evidence_level=observed, status=archived

Mirrors Claude Code skill loading and OpenClaw's 2-layer memory convention.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable, Optional

from yuxu.core.frontmatter import parse_frontmatter

log = logging.getLogger(__name__)

SKIP_DIRS = {"_drafts"}
SKIP_FILES = {"_improvement_log.md"}

DEFAULT_MODE = "execute"

# Mode → default filter policy. User-provided filters override the mode's
# corresponding field; mode fills the gaps.
MODE_POLICIES: dict[str, dict[str, Any]] = {
    "blank":   {"only_mandatory": True},
    "explore": {"only_mandatory": True},
    "execute": {
        "evidence_levels": {"validated", "consensus", "observed"},
        "statuses": {"current"},
        "exclude_probation": True,
    },
    "reflect": {},
    "debug": {
        "evidence_levels": {"observed"},
        "statuses": {"archived"},
    },
}


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
        return None
    tags = fm.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    updated = fm.get("updated")
    return {
        "path": str(path.relative_to(root)),
        "name": name,
        "description": description,
        "type": fm.get("type"),
        "scope": fm.get("scope"),
        "evidence_level": fm.get("evidence_level"),
        "status": fm.get("status"),
        "tags": [str(t) for t in tags],
        "probation": bool(fm.get("probation", False)),
        "updated": str(updated) if updated is not None else None,
        "bytes": len(text.encode("utf-8", errors="replace")),
    }


def _as_set(v) -> Optional[set]:
    if v is None:
        return None
    if isinstance(v, str):
        return {v}
    if isinstance(v, (list, tuple, set)):
        return set(v)
    return None


def _entry_passes(entry: dict, *, mode: str, user_filters: dict) -> bool:
    policy = MODE_POLICIES.get(mode, MODE_POLICIES[DEFAULT_MODE])
    tags = set(entry.get("tags") or [])

    # blank / explore modes: ONLY mandatory-tagged entries, nothing else matters
    if policy.get("only_mandatory"):
        return "mandatory" in tags

    # Type filter (user-only — no mode default)
    type_set = user_filters.get("types")
    if type_set is not None and entry.get("type") not in type_set:
        return False

    # Scope filter (user-only)
    scope_set = user_filters.get("scopes")
    if scope_set is not None and entry.get("scope") not in scope_set:
        return False

    # Tags — if user asked for tags, entry must have ALL of them
    req_tags = user_filters.get("tags")
    if req_tags is not None and not req_tags.issubset(tags):
        return False

    # Evidence level: user > mode default
    el_set = user_filters.get("evidence_levels")
    if el_set is None:
        el_set = policy.get("evidence_levels")
    if el_set is not None:
        # Entries without a level default to `observed` (Phase 1 seeding norm)
        lvl = entry.get("evidence_level") or "observed"
        if lvl not in el_set:
            return False

    # Status: user > mode default
    st_set = user_filters.get("statuses")
    if st_set is None:
        st_set = policy.get("statuses")
    if st_set is not None:
        status = entry.get("status") or "current"
        if status not in st_set:
            return False

    # Probation: user override > mode default
    if user_filters.get("include_probation"):
        pass  # explicitly allowed
    elif policy.get("exclude_probation") and entry.get("probation"):
        return False

    return True


def _collect_user_filters(input: dict) -> tuple[Optional[dict], Optional[str]]:
    """Parse user filter params; return (filters_dict, error_message)."""
    # Accept both `types` (list) and `type` (str) for backward compat
    types = input.get("types")
    if types is None and "type" in input:
        types = input["type"]
    if types is not None and not isinstance(types, (list, tuple, str)):
        return None, "types must be a list or string"

    filters = {
        "types": _as_set(types),
        "scopes": _as_set(input.get("scope")),
        "evidence_levels": _as_set(input.get("evidence_level")),
        "statuses": _as_set(input.get("status")),
        "tags": _as_set(input.get("require_tags") or input.get("tags")),
        "include_probation": bool(input.get("include_probation")),
    }
    return filters, None


async def _op_list(input: dict, ctx) -> dict:
    root = _resolve_memory_root(input.get("memory_root"), ctx)
    mode = input.get("mode") or DEFAULT_MODE
    if mode not in MODE_POLICIES:
        return {"ok": False, "error": f"unknown mode: {mode!r}"}
    filters, err = _collect_user_filters(input)
    if err:
        return {"ok": False, "error": err}

    if not root.exists():
        return {"ok": True, "memory_root": str(root), "mode": mode, "entries": []}

    entries: list[dict] = []
    for p in _iter_memory_files(root):
        summary = _read_entry_summary(p, root)
        if summary is None:
            continue
        if not _entry_passes(summary, mode=mode, user_filters=filters):
            continue
        entries.append(summary)
    return {"ok": True, "memory_root": str(root), "mode": mode, "entries": entries}


async def _op_stats(input: dict, ctx) -> dict:
    root = _resolve_memory_root(input.get("memory_root"), ctx)
    result: dict[str, Any] = {
        "ok": True,
        "memory_root": str(root),
        "total": 0,
        "by_type": {},
        "by_scope": {},
        "by_status": {},
        "by_evidence_level": {},
        "probation_count": 0,
        "mandatory_count": 0,
    }
    if not root.exists():
        return result

    def _bump(bucket: dict, key: str) -> None:
        bucket[key] = bucket.get(key, 0) + 1

    for p in _iter_memory_files(root):
        summary = _read_entry_summary(p, root)
        if summary is None:
            continue
        result["total"] += 1
        _bump(result["by_type"], summary.get("type") or "unknown")
        _bump(result["by_scope"], summary.get("scope") or "unspecified")
        _bump(result["by_status"], summary.get("status") or "current")
        _bump(result["by_evidence_level"],
              summary.get("evidence_level") or "unspecified")
        if summary.get("probation"):
            result["probation_count"] += 1
        if "mandatory" in (summary.get("tags") or []):
            result["mandatory_count"] += 1
    return result


def _match_score(entry: dict, query_lower: str) -> int:
    """Keyword relevance: name match weighs higher than description.

    Phrase hit: name +10 / desc +3. Per-token hit (lowercased whitespace
    split): name +2 / desc +1. Deterministic, no external index.
    """
    name = (entry.get("name") or "").lower()
    desc = (entry.get("description") or "").lower()
    score = 0
    if query_lower in name:
        score += 10
    if query_lower in desc:
        score += 3
    for token in query_lower.split():
        if not token:
            continue
        if token in name:
            score += 2
        if token in desc:
            score += 1
    return score


async def _op_search(input: dict, ctx) -> dict:
    query = input.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"ok": False, "error": "missing or empty field: query"}
    limit = input.get("limit", 10)
    if not isinstance(limit, int) or limit <= 0:
        return {"ok": False, "error": "limit must be positive integer"}
    mode = input.get("mode") or DEFAULT_MODE
    if mode not in MODE_POLICIES:
        return {"ok": False, "error": f"unknown mode: {mode!r}"}
    filters, err = _collect_user_filters(input)
    if err:
        return {"ok": False, "error": err}

    root = _resolve_memory_root(input.get("memory_root"), ctx)
    if not root.exists():
        return {"ok": True, "memory_root": str(root), "query": query,
                "mode": mode, "entries": []}

    query_lower = query.lower().strip()
    scored: list[tuple[int, dict]] = []
    for p in _iter_memory_files(root):
        summary = _read_entry_summary(p, root)
        if summary is None:
            continue
        if not _entry_passes(summary, mode=mode, user_filters=filters):
            continue
        s = _match_score(summary, query_lower)
        if s > 0:
            scored.append((s, summary))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = [entry for _, entry in scored[:limit]]
    return {"ok": True, "memory_root": str(root), "query": query,
            "mode": mode, "entries": top}


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
    if op == "stats":
        return await _op_stats(input, ctx)
    if op == "search":
        return await _op_search(input, ctx)
    return {"ok": False, "error": f"unknown op: {op!r}"}
