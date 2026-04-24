"""memory skill — progressive disclosure over <project>/data/memory.

Reads only. Writes still flow through memory_curator → approval_queue →
approval_applier per I6 (scope-crossing writes gated by approval).

Ops:
- `stats`:  L0 — counts by type / scope / status / evidence_level; independent
            of total entry count, so cheap at any scale.
- `list`:   L1 — filtered index (frontmatter only). Accepts `mode` + explicit
            filter params; mode sets defaults, explicit params override.
- `get`:    L2 — full body + parsed frontmatter for one entry. Optional
            `section=<label>` returns only the inline-bold-labeled paragraph
            (CC port: `**Why:**` / `**How to apply:**` + yuxu `**Evidence:**`
            / `**Score:**` / `**Source:**`; see `project_memory_section_convention`).
- `search`: cross-cut — keyword match on name + description + body, ranked
            top-K. Body match has lower weight than metadata; a snippet of
            body context around the first hit is returned so the caller can
            eyeball why each entry ranked.

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
import re
from pathlib import Path
from typing import Any, Iterable, Optional

from yuxu.core.frontmatter import parse_frontmatter

log = logging.getLogger(__name__)

SKIP_DIRS = {"_drafts"}
SKIP_FILES = {"_improvement_log.md"}

DEFAULT_MODE = "execute"

# Topic for retrieval signal events. Phase 4 scoring infrastructure
# (performance_ranker extension, consumed by future iteration_agent) will
# subscribe here to credit / debit retrieved memory entries per session
# outcome. Publishing is best-effort — skill stays functional if the bus
# can't dispatch (no subscribers, missing attr, etc.).
RETRIEVED_TOPIC = "memory.retrieved"

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


async def _publish_retrieved(ctx, op: str, paths: list[str],
                              extras: Optional[dict] = None) -> None:
    """Best-effort emission of memory.retrieved. Phase 4 foundation — no
    hard consumer today; iteration_agent / performance_ranker will credit
    retrieved entries against session outcome signals once they exist.
    """
    bus = getattr(ctx, "bus", None)
    if bus is None or not paths:
        return
    payload: dict[str, Any] = {"op": op, "paths": list(paths)}
    if extras:
        payload.update(extras)
    try:
        await bus.publish(RETRIEVED_TOPIC, payload)
    except Exception:
        log.exception("memory: publish %s raised", RETRIEVED_TOPIC)


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
    await _publish_retrieved(ctx, "list",
                              [e["path"] for e in entries],
                              extras={"mode": mode,
                                      "memory_root": str(root)})
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


def _match_score(entry: dict, query_lower: str,
                    body_lower: str = "") -> int:
    """Keyword relevance: name match weighs higher than description, body
    lowest. Body is optional — if empty, score matches the old name+desc
    behaviour exactly (regression-safe).

    Phrase hit: name +10 / desc +3 / body +2.
    Per-token hit (whitespace split): name +2 / desc +1 / body per-hit +1
    capped at 3 (prevents a memory that accidentally repeats a common token
    many times from dominating rankings).
    """
    name = (entry.get("name") or "").lower()
    desc = (entry.get("description") or "").lower()
    score = 0
    if query_lower in name:
        score += 10
    if query_lower in desc:
        score += 3
    if body_lower and query_lower in body_lower:
        score += 2
    for token in query_lower.split():
        if not token:
            continue
        if token in name:
            score += 2
        if token in desc:
            score += 1
        if body_lower:
            hits = body_lower.count(token)
            if hits > 0:
                score += min(hits, 3)
    return score


def _body_snippet(body: str, query_lower: str,
                    ctx_chars: int = 180) -> Optional[str]:
    """Return a short excerpt of `body` around the first query hit.

    Tries the whole phrase first, then falls back to the longest query
    token (≥3 chars). Returns None if nothing hits. Ellipses mark
    truncation on either end; the whole result is single-line.
    """
    if not body:
        return None
    body_lower = body.lower()
    idx = body_lower.find(query_lower)
    if idx < 0:
        tokens = [t for t in query_lower.split() if len(t) >= 3]
        tokens.sort(key=len, reverse=True)
        for t in tokens:
            idx = body_lower.find(t)
            if idx >= 0:
                break
        else:
            return None
    half = max(20, ctx_chars // 2)
    start = max(0, idx - half)
    end = min(len(body), idx + len(query_lower) + half)
    snip = body[start:end].replace("\n", " ").strip()
    if start > 0:
        snip = "…" + snip
    if end < len(body):
        snip = snip + "…"
    return snip


# Matches the CC + yuxu inline-bold-label convention:
# `**Why:**`, `**How to apply:**`, `**Evidence:**`, `**Score:**`, `**Source:**`.
# A label caps the paragraph that follows until the next label or end-of-body.
_SECTION_LABEL_RE = re.compile(r'\*\*([^:*\n]+?):\*\*')


def _extract_section(body: str, section: str) -> Optional[str]:
    """Return the paragraph that follows `**<section>:**` in body, or None
    if no such label exists. Case-insensitive; accepts underscored
    (`how_to_apply`) or spaced (`how to apply`) form for the same label.
    """
    if not body or not section:
        return None
    target_variants = {
        section.lower(),
        section.lower().replace("_", " "),
        section.lower().replace(" ", "_"),
        section.lower().replace("-", " "),
    }
    matches = list(_SECTION_LABEL_RE.finditer(body))
    if not matches:
        return None
    for i, m in enumerate(matches):
        label = m.group(1).strip().lower()
        label_variants = {
            label,
            label.replace(" ", "_"),
            label.replace("_", " "),
        }
        if label_variants & target_variants:
            content_start = m.end()
            content_end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            return body[content_start:content_end].strip()
    return None


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
    search_body = bool(input.get("search_body", True))
    scored: list[tuple[int, dict]] = []
    for p in _iter_memory_files(root):
        summary = _read_entry_summary(p, root)
        if summary is None:
            continue
        if not _entry_passes(summary, mode=mode, user_filters=filters):
            continue
        body_text = ""
        if search_body:
            try:
                text = p.read_text(encoding="utf-8")
                _, body_text = parse_frontmatter(text)
            except OSError as e:
                log.warning("memory: could not read body %s: %s", p, e)
                body_text = ""
        body_lower = body_text.lower() if body_text else ""
        s = _match_score(summary, query_lower, body_lower)
        if s > 0:
            snippet = _body_snippet(body_text, query_lower) if body_text else None
            entry = dict(summary)
            if snippet:
                entry["body_snippet"] = snippet
            scored.append((s, entry))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = [entry for _, entry in scored[:limit]]
    await _publish_retrieved(ctx, "search",
                              [e["path"] for e in top],
                              extras={"mode": mode, "query": query,
                                      "memory_root": str(root)})
    return {"ok": True, "memory_root": str(root), "query": query,
            "mode": mode, "entries": top}


async def _op_get(input: dict, ctx) -> dict:
    raw_path = input.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return {"ok": False, "error": "missing or empty field: path"}
    section = input.get("section")
    if section is not None and (not isinstance(section, str) or not section.strip()):
        return {"ok": False, "error": "section must be a non-empty string"}
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
    rel_path = str(p.relative_to(root.resolve()))
    await _publish_retrieved(ctx, "get", [rel_path],
                              extras={"memory_root": str(root),
                                       "section": section})

    result = {
        "ok": True,
        "path": rel_path,
        "frontmatter": fm,
        "body": body,
        "bytes": len(text.encode("utf-8", errors="replace")),
    }
    if section is not None:
        section_body = _extract_section(body, section.strip())
        if section_body is None:
            # Caller asked for a specific section that doesn't exist — still
            # return ok=True with full body so they can decide, but surface
            # the miss via `section_body=None` + available_sections list.
            available = [m.group(1).strip() for m in _SECTION_LABEL_RE.finditer(body)]
            result["section"] = section
            result["section_body"] = None
            result["available_sections"] = available
        else:
            result["section"] = section
            result["section_body"] = section_body
    return result


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
