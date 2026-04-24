"""skill_index — progressive-disclosure catalog over yuxu skills + agents.

Design heritage (per reference_subagent_context_inheritance.md + deep-dive
of CC/OC skill disclosure):

- Shape: stats / list / read — parallel to `memory` skill's L0/L1/L2.
  Progressive disclosure is a cross-cutting yuxu principle.
- L1 format: OpenClaw XML block `<available_skills><skill>...</skill>
  </available_skills>` + `<kind>` tag added (yuxu distinguishes skills
  from agents; OC doesn't need to).
- Budget fallback: full → compact → truncate (OC workspace.ts:124-157
  pattern, adapted to yuxu's smaller catalog).
- L1→L2 directive: ported verbatim in the `build_directive()` helper
  and SKILL.md doc block.
- Discovery: prefer `ctx.loader.specs` (already parsed) with filesystem
  fallback. No separate manifest cache — Loader.scan() is the cache.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

from yuxu.core.frontmatter import parse_frontmatter

log = logging.getLogger(__name__)

NAME = "skill_index"

# OpenClaw convention — max rendered XML block size before the fallback
# ladder kicks in.
DEFAULT_CHAR_BUDGET = 18_000

SKIP_DIRS = {"__pycache__"}


# -- discovery helpers ----------------------------------------


def _iter_dir(root: Path) -> list[Path]:
    if not root.exists() or not root.is_dir():
        return []
    out: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if child.name in SKIP_DIRS or child.name.startswith((".", "_")):
            continue
        out.append(child)
    return out


def _extract_body_description(body: str) -> str:
    """Pull a one-line description from the body when frontmatter lacks one.
    Takes the first non-header non-empty line after the `# title` heading,
    truncated to 250 chars (matches CC's MAX_LISTING_DESC_CHARS)."""
    if not body:
        return ""
    lines = body.splitlines()
    # Skip leading blank lines and a single H1
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and lines[i].lstrip().startswith("#"):
        i += 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines):
        return ""
    # Join consecutive non-blank lines as the first paragraph.
    para: list[str] = []
    while i < len(lines) and lines[i].strip() and not lines[i].lstrip().startswith("#"):
        para.append(lines[i].strip())
        i += 1
    return " ".join(para)[:250]


def _read_spec_dir(path: Path) -> Optional[dict]:
    """Return a normalized entry dict for one agent/skill dir, or None
    if the dir doesn't carry a valid SKILL.md or AGENT.md.

    Tolerates yuxu's current convention where AGENT.md files omit
    `name` / `description` from frontmatter (they live in the body).
    Falls back to path.name and first-paragraph extraction so agents
    are catalogued alongside skills."""
    has_init = (path / "__init__.py").exists()
    skill_md = path / "SKILL.md"
    agent_md = path / "AGENT.md"

    if has_init:
        kind = "agent"
        md_path = agent_md if agent_md.exists() else skill_md
    elif skill_md.exists():
        kind = "skill"
        md_path = skill_md
    elif agent_md.exists():
        kind = "agent"  # LLM-only agent
        md_path = agent_md
    else:
        return None

    if not md_path.exists():
        return None

    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError:
        return None
    fm, body = parse_frontmatter(text)
    if not isinstance(fm, dict) or not fm:
        # No usable frontmatter at all (malformed or missing) — skip.
        # Valid skills have `name`/`description`; valid agents still have
        # `driver`/`run_mode`/etc even without description. An empty fm
        # means the file is broken.
        return None
    # Name: frontmatter first, then dir name
    name = fm.get("name") if isinstance(fm.get("name"), str) else path.name
    # Description: frontmatter first, then first body paragraph
    description = fm.get("description")
    if not isinstance(description, str) or not description.strip():
        description = _extract_body_description(body)
    if not description.strip():
        # Still nothing — not useful for LLM disclosure, skip
        return None
    return {
        "name": name,
        "kind": kind,
        "description": description,
        "location": str(md_path),
        "scope": fm.get("scope"),
        "source": None,  # filled by caller
    }


def _discover_from_fs(ctx) -> list[dict]:
    """Filesystem-level catalog walk. Used when ctx.loader.specs isn't
    available (bare test harness, standalone invocation)."""
    entries: list[dict] = []
    seen: set[str] = set()

    # Bundled (importable location)
    try:
        import yuxu.bundled as _b
        bundled_root = Path(_b.__file__).parent
    except Exception:
        bundled_root = None
    if bundled_root:
        for d in _iter_dir(bundled_root):
            e = _read_spec_dir(d)
            if e and e["name"] not in seen:
                e["source"] = "bundled"
                entries.append(e)
                seen.add(e["name"])

    # Project — walk up from agent_dir to find yuxu.json
    start = Path(getattr(ctx, "agent_dir", ".")).resolve()
    project_root: Optional[Path] = None
    for cand in (start, *start.parents):
        if (cand / "yuxu.json").exists():
            project_root = cand
            break
    if project_root:
        for sub in ("agents", "skills", "_system"):
            for d in _iter_dir(project_root / sub):
                e = _read_spec_dir(d)
                if e and e["name"] not in seen:
                    e["source"] = "project"
                    entries.append(e)
                    seen.add(e["name"])

    # Global ~/.yuxu/skills
    home = os.environ.get("YUXU_HOME")
    global_root = (Path(home).expanduser() if home
                    else Path.home() / ".yuxu") / "skills"
    for d in _iter_dir(global_root):
        e = _read_spec_dir(d)
        if e and e["name"] not in seen:
            e["source"] = "global"
            entries.append(e)
            seen.add(e["name"])

    return entries


def _discover_from_loader(ctx) -> Optional[list[dict]]:
    """Fast path: Loader has already scanned + parsed frontmatter.
    Returns None if ctx has no loader or its specs aren't usable."""
    loader = getattr(ctx, "loader", None)
    if loader is None:
        return None
    specs = getattr(loader, "specs", None)
    if not isinstance(specs, dict):
        return None
    entries: list[dict] = []
    for name, spec in specs.items():
        path = getattr(spec, "path", None)
        if path is None:
            continue
        # Delegate to the same fs parser for consistent fallback behavior
        # (handles agents whose AGENT.md has no description frontmatter).
        entry = _read_spec_dir(Path(path))
        if entry is None:
            continue
        # Prefer the spec's registered name (handles name != dirname).
        entry["name"] = name
        entry["source"] = ("bundled" if "bundled" in str(path)
                            else "project")
        # Spec's `scope` overrides frontmatter when present.
        spec_scope = getattr(spec, "scope", None)
        if spec_scope:
            entry["scope"] = spec_scope
        entries.append(entry)
    return sorted(entries, key=lambda e: e["name"])


def _discover(ctx) -> list[dict]:
    out = _discover_from_loader(ctx)
    if out is not None and len(out) > 0:
        return out
    return _discover_from_fs(ctx)


# -- XML rendering --------------------------------------------


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
             .replace("'", "&apos;"))


def _render_full(entries: list[dict]) -> str:
    lines = ["<available_skills>"]
    for e in entries:
        lines.append("  <skill>")
        lines.append(f"    <name>{_xml_escape(e['name'])}</name>")
        lines.append(f"    <kind>{_xml_escape(e['kind'])}</kind>")
        lines.append(f"    <description>{_xml_escape(e['description'])}</description>")
        lines.append(f"    <location>{_xml_escape(e['location'])}</location>")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    return "\n".join(lines)


def _render_compact(entries: list[dict]) -> str:
    """OC-style compact fallback — drop descriptions when full block
    exceeds budget. Keeps skill discoverability when space is tight."""
    lines = ["<available_skills>"]
    for e in entries:
        lines.append(
            f"  <skill><name>{_xml_escape(e['name'])}</name>"
            f"<kind>{_xml_escape(e['kind'])}</kind>"
            f"<location>{_xml_escape(e['location'])}</location></skill>"
        )
    lines.append("</available_skills>")
    return "\n".join(lines)


def _render_truncated(entries: list[dict], char_budget: int,
                        compact: bool) -> tuple[str, int]:
    """Binary-search for the largest prefix that fits `char_budget`.
    Returns (rendered_xml, omitted_count)."""
    render = _render_compact if compact else _render_full
    n = len(entries)
    # Quick bounds
    if n == 0:
        return render([]), 0
    lo, hi = 0, n
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        subset = entries[:mid]
        rendered = render(subset)
        note = (f"\n<!-- {n - mid} entries omitted for char budget -->"
                 if mid < n else "")
        if len(rendered) + len(note) <= char_budget:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    subset = entries[:best]
    rendered = render(subset)
    omitted = n - best
    if omitted > 0:
        rendered += f"\n<!-- {omitted} entries omitted for char budget -->"
    return rendered, omitted


def _render_with_budget(entries: list[dict],
                          char_budget: int
                          ) -> tuple[str, bool, int]:
    """Return (xml, compact_used, omitted_count)."""
    full = _render_full(entries)
    if len(full) <= char_budget:
        return full, False, 0
    compact = _render_compact(entries)
    if len(compact) <= char_budget:
        return compact, True, 0
    # Compact also too big — binary-search truncate within compact.
    rendered, omitted = _render_truncated(entries, char_budget, compact=True)
    return rendered, True, omitted


# -- directive helper (importable) ----------------------------


DIRECTIVE_TEMPLATE = """## Available Skills (mandatory)
Before replying: scan <available_skills> <description> entries below.
- If exactly one skill clearly applies: call the `invoke_skill` tool \
with `{{"name": "<skill>"}}` to load its full SKILL.md, then follow it.
- If multiple could apply: choose the most specific one, then invoke it.
- If none clearly apply: do not invoke any skill.
Constraints: never invoke more than one skill up front; only invoke after \
selecting.

{xml_block}
"""


def build_directive(xml_block: str) -> str:
    """Standard system-prompt section that introduces the catalog and
    tells the LLM how to use it. Callers can copy the template manually
    or import and call this. Consistent wording is the point."""
    return DIRECTIVE_TEMPLATE.format(xml_block=xml_block)


# -- op handlers ----------------------------------------------


def _filter_entries(entries: list[dict],
                      kind: Optional[str],
                      scope: Optional[str],
                      include_self: bool) -> list[dict]:
    out = []
    for e in entries:
        if not include_self and e["name"] == NAME:
            continue
        if kind and kind != "all" and e["kind"] != kind:
            continue
        if scope and (e.get("scope") != scope):
            continue
        out.append(e)
    return out


async def _op_stats(input: dict, ctx) -> dict:
    entries = _discover(ctx)
    kind = input.get("kind")
    scope = input.get("scope")
    filtered = _filter_entries(entries, kind, scope, include_self=True)
    by_kind: dict[str, int] = {}
    by_scope: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for e in filtered:
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
        by_scope[e.get("scope") or "unspecified"] = (
            by_scope.get(e.get("scope") or "unspecified", 0) + 1)
        by_source[e.get("source") or "unknown"] = (
            by_source.get(e.get("source") or "unknown", 0) + 1)
    return {
        "ok": True,
        "total": len(filtered),
        "by_kind": by_kind,
        "by_scope": by_scope,
        "by_source": by_source,
    }


async def _op_list(input: dict, ctx) -> dict:
    entries = _discover(ctx)
    kind = input.get("kind")
    scope = input.get("scope")
    include_self = bool(input.get("include_self", True))
    try:
        char_budget = int(input.get("char_budget")
                           or DEFAULT_CHAR_BUDGET)
    except (TypeError, ValueError):
        char_budget = DEFAULT_CHAR_BUDGET

    filtered = _filter_entries(entries, kind, scope, include_self)
    xml, compact_used, omitted = _render_with_budget(filtered, char_budget)
    return {
        "ok": True,
        "xml_block": xml,
        "entries": filtered,
        "rendered_chars": len(xml),
        "compact_used": compact_used,
        "omitted": omitted,
        "char_budget": char_budget,
    }


async def _op_read(input: dict, ctx) -> dict:
    target = input.get("name")
    if not isinstance(target, str) or not target.strip():
        return {"ok": False, "error": "missing field: name"}
    entries = _discover(ctx)
    match: Optional[dict] = None
    for e in entries:
        if e["name"] == target:
            match = e
            break
    if match is None:
        return {"ok": False,
                 "error": f"not found: {target!r}",
                 "available_count": len(entries)}
    path = Path(match["location"])
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return {"ok": False,
                 "error": f"read {path}: {e}"}
    fm, body = parse_frontmatter(text)
    return {
        "ok": True,
        "name": match["name"],
        "kind": match["kind"],
        "location": str(path),
        "frontmatter": fm if isinstance(fm, dict) else {},
        "body": body,
        "bytes": len(text.encode("utf-8", errors="replace")),
    }


# -- entry ---------------------------------------------------


async def execute(input: dict, ctx) -> dict:
    op = (input or {}).get("op")
    if op == "stats":
        return await _op_stats(input, ctx)
    if op == "list":
        return await _op_list(input, ctx)
    if op == "read":
        return await _op_read(input, ctx)
    return {"ok": False, "error": f"unknown op: {op!r}"}
