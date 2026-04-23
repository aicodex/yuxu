"""Creation-time principles loader.

Agent-creation skills (`generate_agent_md`, `classify_intent`) and the
`harness_pro_max` agent inject these into their system prompts so that every
new agent is built with yuxu's core mental model in scope. Ordinary runtime
code should **not** pull this in — the principles are deliberately scoped to
creation context to avoid prompt bloat.

Two sources of truth:
- `docs/ARCHITECTURE.md` — invariants (I1–I8), product principles, scope.
- `docs/AGENT_GUIDE.md` — the operational "Principles (read before creating)"
  section.

If either file is missing (e.g. user's yuxu install is older or partial),
the loader returns the empty string for that piece rather than raising —
creation still works, just without the injection. A warning is logged.

Lazy-loaded + cached on first call. Files are small (~5KB combined), so
reading is cheap; cache avoids re-reading per LLM request.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_ARCH_CACHE: Optional[str] = None
_GUIDE_PRINCIPLES_CACHE: Optional[str] = None

# Resolve docs relative to the installed package. yuxu layout:
#   src/yuxu/core/principles.py        <- this file
#   docs/ARCHITECTURE.md               <- ../../../docs
_PKG_ROOT = Path(__file__).resolve().parents[3]
_ARCH_PATH = _PKG_ROOT / "docs" / "ARCHITECTURE.md"
_GUIDE_PATH = _PKG_ROOT / "docs" / "AGENT_GUIDE.md"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as e:
        log.warning("principles: cannot read %s: %s", path, e)
        return ""


def _extract_section(text: str, heading: str) -> str:
    """Return the body of a markdown section starting with `## {heading}`.

    Section ends at the next `## ` (same-level) heading or end of file.
    Returns empty string if heading not found.
    """
    marker = f"## {heading}"
    start = text.find(marker)
    if start == -1:
        return ""
    # include the heading itself so injected text self-labels
    nxt = text.find("\n## ", start + len(marker))
    if nxt == -1:
        return text[start:].rstrip()
    return text[start:nxt].rstrip()


def load_architecture() -> str:
    """Return the full ARCHITECTURE.md text (cached)."""
    global _ARCH_CACHE
    if _ARCH_CACHE is None:
        _ARCH_CACHE = _read(_ARCH_PATH)
    return _ARCH_CACHE


def load_guide_principles() -> str:
    """Return the 'Principles (read before creating)' section from
    AGENT_GUIDE.md (cached)."""
    global _GUIDE_PRINCIPLES_CACHE
    if _GUIDE_PRINCIPLES_CACHE is None:
        full = _read(_GUIDE_PATH)
        _GUIDE_PRINCIPLES_CACHE = _extract_section(
            full, "Principles (read before creating)",
        )
    return _GUIDE_PRINCIPLES_CACHE


def load_creation_context() -> str:
    """Return combined text to inject into a creation-time system prompt.

    Format:
      # yuxu Creation Context
      [ARCHITECTURE.md]
      ---
      [AGENT_GUIDE.md § Principles]

    Returns empty string if neither source file is available. Callers
    should defensively check and decide whether to inject the leading
    separator ("Follow these while creating:" or similar header).
    """
    arch = load_architecture()
    guide = load_guide_principles()
    if not arch and not guide:
        return ""
    parts = []
    if arch:
        parts.append(arch.rstrip())
    if guide:
        parts.append(guide.rstrip())
    return "\n\n---\n\n".join(parts)


def _clear_cache() -> None:
    """Test hook. Not public API."""
    global _ARCH_CACHE, _GUIDE_PRINCIPLES_CACHE
    _ARCH_CACHE = None
    _GUIDE_PRINCIPLES_CACHE = None
