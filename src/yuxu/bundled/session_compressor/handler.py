"""session_compressor — raw session JSONL → compressed memory entry.

Writes `<project>/data/memory/sessions/<YYYY-MM-DD>-<uuid8>.md` so the
memory skill's L0/L1/L2 progressive disclosure can surface it just like
any other entry. Closes the loop on the permanent-memory + dynamic-
compression pipeline:

    raw JSONL (ephemeral, sessions_raw/)
        ↓ session_compressor (this agent)
    compressed memory entry (permanent, data/memory/sessions/)
        ↓ memory skill L0/L1/L2
    LLM lazy retrieval

Design notes:
- Depends on context_compressor. Without it, falls back to head/tail
  byte truncation (same degraded-mode contract).
- Idempotent by `originSessionId`: re-compressing a session overwrites
  the same memory entry path.
- Writes .md with full frontmatter (name / description / type=session /
  scope / evidence_level / status / tags / originSessionId /
  source_path / source_bytes / compressed_bytes / compression_ratio /
  updated) so memory.search / memory.list filters work immediately.
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import date as _date
from datetime import datetime as _datetime
from pathlib import Path
from typing import Optional

from yuxu.bundled._shared import dump_frontmatter
from yuxu.core.session_log import format_jsonl_transcript

log = logging.getLogger(__name__)

NAME = "session_compressor"

ARCHIVED_TOPIC = "session.archived"
COMPRESSED_TOPIC = "session.compressed"

DEFAULT_TARGET_RATIO = 0.10
DEFAULT_MIN_TOKENS = 5_000
DEFAULT_MAX_TOKENS = 50_000
MAX_DESCRIPTION_CHARS = 200
# Per-map-call byte cap for the downstream context_compressor. Sessions
# are one giant doc; we deliberately pass a tighter value than the
# compressor's default (500KB) so a single MiniMax call stays well inside
# provider limits. Leaves headroom for prompt + JSON overhead.
MAP_BYTE_CAP = 150_000

# Session entries live under a dedicated subdir so memory.list filters
# and archival sweeps can address them as a group.
SESSIONS_SUBDIR = "sessions"


# -- utilities --------------------------------------------------


_UUID_RE = re.compile(
    r"([0-9a-f]{8})-([0-9a-f]{4})-([0-9a-f]{4})-([0-9a-f]{4})-([0-9a-f]{12})",
    re.IGNORECASE,
)


def _extract_session_id(path: Path) -> Optional[str]:
    """Pull a UUID out of the filename. Tolerates both
    `YYYY-MM-DD-<uuid8>.jsonl` and `<full-uuid>.jsonl` shapes."""
    m = _UUID_RE.search(path.stem)
    if m:
        return "-".join(m.groups())
    # 8-char prefix fallback (from archive_session.sh convention)
    m8 = re.search(r"([0-9a-f]{8})", path.stem, re.IGNORECASE)
    if m8:
        return m8.group(1)
    return None


def _pick_date(path: Path, jsonl_text: str) -> _date:
    """Date for the filename. Prefer the archive-script stem's date;
    else mtime; else today."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", path.stem)
    if m:
        try:
            return _date.fromisoformat("-".join(m.groups()))
        except ValueError:
            pass
    try:
        return _datetime.fromtimestamp(path.stat().st_mtime).date()
    except OSError:
        pass
    return _date.today()


def _short_id(session_id: Optional[str]) -> str:
    if not session_id:
        return "unknown"
    return session_id.split("-")[0][:8]


def _estimate_tokens(text: str) -> int:
    return max(1, len(text.encode("utf-8", errors="replace")) // 4)


def _derive_target_tokens(estimated: int, *,
                           ratio: float = DEFAULT_TARGET_RATIO,
                           floor: int = DEFAULT_MIN_TOKENS,
                           ceiling: int = DEFAULT_MAX_TOKENS) -> int:
    raw = int(estimated * ratio)
    return max(floor, min(ceiling, raw))


# Look for CC's "## Primary Request and Intent" or numbered "1. Primary
# Request and Intent:" — matches both the 9-section plain and markdown
# variants the compressor may produce.
_PRIMARY_RE = re.compile(
    r"(?:^|\n)\s*(?:#{1,3}\s*)?(?:1\.\s*)?Primary Request and Intent:?\s*\n+"
    r"([\s\S]+?)(?=\n\s*(?:#{1,3}\s*)?(?:2\.\s*)?Key Technical Concepts|\Z)",
    re.IGNORECASE,
)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。.!?！？])\s+")


def _sanitize_description(s: str) -> str:
    """Strip YAML-unsafe / raw-JSONL garbage from a candidate description.
    Removes structural chars that make YAML bare-scalar parsing brittle
    even after quoting, and collapses whitespace."""
    if not s:
        return ""
    # Drop braces, quotes, backslashes — frequent in fallback (raw JSONL
    # lines leaked through the compressor).
    s = re.sub(r"[{}\\\"']", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _extract_description(body: str) -> str:
    """Pull a one-line description from the compressed body — first
    sentence of 'Primary Request and Intent' section, or first non-empty
    non-JSON-looking line as fallback. Truncated to MAX_DESCRIPTION_CHARS."""
    if not body:
        return ""
    m = _PRIMARY_RE.search(body)
    if m:
        chunk = m.group(1).strip()
        parts = _SENTENCE_SPLIT_RE.split(chunk, maxsplit=1)
        first = parts[0] if parts else chunk
        first = re.sub(r"^[\s\-\*•]+", "", first).strip()
        first = _sanitize_description(first)
        if first:
            return first[:MAX_DESCRIPTION_CHARS]
    # Fallback: find first non-header non-JSON-looking line.
    for line in body.splitlines():
        s = re.sub(r"^[#\-\*\s\d\.]+", "", line).strip()
        # Skip lines that look like raw JSON/JSONL (start with `{` / `[`
        # or contain typical JSON keys like `"type":`). Those are the
        # telltale of fallback passing through raw content.
        if not s or s.startswith(("{", "[")) or '"type":' in s:
            continue
        s = _sanitize_description(s)
        if s:
            return s[:MAX_DESCRIPTION_CHARS]
    return ""


def _resolve_memory_root(override: Optional[str], ctx) -> Path:
    """Walk up from ctx.agent_dir looking for yuxu.json; fall back to
    $cwd/data/memory. Same convention as the memory skill."""
    if override:
        return Path(override).expanduser().resolve()
    start = Path(getattr(ctx, "agent_dir", ".")).resolve()
    for cand in (start, *start.parents):
        if (cand / "yuxu.json").exists():
            return cand / "data" / "memory"
    return Path.cwd() / "data" / "memory"


def _safe_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# -- agent class ------------------------------------------------


class SessionCompressor:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.target_ratio = float(
            os.environ.get("SESSION_COMPRESSOR_TARGET_RATIO")
            or DEFAULT_TARGET_RATIO
        )
        self.min_target_tokens = int(
            os.environ.get("SESSION_COMPRESSOR_MIN_TOKENS")
            or DEFAULT_MIN_TOKENS
        )
        self.max_target_tokens = int(
            os.environ.get("SESSION_COMPRESSOR_MAX_TOKENS")
            or DEFAULT_MAX_TOKENS
        )

    # -- lifecycle --------------------------------------------

    async def install(self) -> None:
        self.ctx.bus.subscribe(ARCHIVED_TOPIC, self._on_archived)

    async def uninstall(self) -> None:
        try:
            self.ctx.bus.unsubscribe(ARCHIVED_TOPIC, self._on_archived)
        except Exception:
            pass

    async def _on_archived(self, event: dict) -> None:
        payload = (event or {}).get("payload") or {}
        if not isinstance(payload, dict):
            return
        jsonl_path = payload.get("jsonl_path")
        if not isinstance(jsonl_path, str) or not jsonl_path.strip():
            return
        try:
            await self._compress_and_write(Path(jsonl_path).expanduser(),
                                            memory_root=None,
                                            target_tokens=None,
                                            pool=payload.get("pool"),
                                            model=payload.get("model"))
        except Exception:
            log.exception("session_compressor: auto-trigger failed for %s",
                          jsonl_path)

    # -- core -------------------------------------------------

    async def _compress_and_write(self, jsonl_path: Path, *,
                                    memory_root: Optional[str],
                                    target_tokens: Optional[int],
                                    pool: Optional[str],
                                    model: Optional[str]) -> dict:
        if not jsonl_path.exists():
            return {"ok": False,
                     "error": f"jsonl not found: {jsonl_path}"}
        source_bytes = jsonl_path.stat().st_size
        rendered = format_jsonl_transcript(jsonl_path)
        if not rendered.strip():
            return {"ok": False,
                     "error": f"empty transcript rendering: {jsonl_path}"}

        rendered_tokens = _estimate_tokens(rendered)
        if target_tokens is None:
            target_tokens = _derive_target_tokens(
                rendered_tokens,
                ratio=self.target_ratio,
                floor=self.min_target_tokens,
                ceiling=self.max_target_tokens,
            )

        session_id = _extract_session_id(jsonl_path) or "unknown"
        short_id = _short_id(session_id)
        date = _pick_date(jsonl_path, rendered)

        t0 = time.monotonic()
        r = await self.ctx.bus.request("context_compressor", {
            "op": "summarize",
            "documents": [{"id": jsonl_path.name, "body": rendered}],
            "task": (f"Archive this CC session as a permanent memory entry. "
                     f"Preserve architectural decisions, design critiques "
                     f"from the user, open TODOs, file paths, commit hashes, "
                     f"and I-invariant references. Output the CC 9-section "
                     f"structure; a future agent will retrieve this entry "
                     f"via memory.get."),
            "target_tokens": target_tokens,
            "max_bytes_per_map": MAP_BYTE_CAP,
            "pool": pool, "model": model,
            "custom_instructions": (
                "Prioritize: user pushbacks and corrections, architectural "
                "decisions, specific file paths and commit hashes, open "
                "tasks and deferred items. De-prioritize: routine tool "
                "results, verbose success logs."
            ),
        }, timeout=600.0)
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        if not isinstance(r, dict) or not r.get("ok"):
            err = r.get("error") if isinstance(r, dict) else "non-dict"
            return {"ok": False,
                     "error": f"context_compressor: {err}",
                     "elapsed_ms": elapsed_ms}
        body = (r.get("merged_summary") or "").strip()
        if not body:
            return {"ok": False,
                     "error": "compressor returned empty body",
                     "elapsed_ms": elapsed_ms}

        fallback_used = bool(r.get("fallback_used"))
        description = _extract_description(body) or f"session {short_id}"

        mem_root = _resolve_memory_root(memory_root, self.ctx)
        entry_path = mem_root / SESSIONS_SUBDIR / f"{date.isoformat()}-{short_id}.md"

        fm = {
            "name": f"Session {date.isoformat()} {short_id} — {description[:60]}",
            "description": description,
            "type": "session",
            "scope": "project",
            "evidence_level": "observed",
            "status": "current",
            "tags": ["session"],
            "originSessionId": session_id,
            "source_path": str(jsonl_path),
            "source_bytes": source_bytes,
            "compressed_bytes": len(body.encode("utf-8", errors="replace")),
            "compression_ratio": round(
                1.0 - (len(body.encode("utf-8", errors="replace"))
                        / max(source_bytes, 1)),
                4,
            ),
            "fallback_used": fallback_used,
            "updated": date.isoformat(),
        }
        head = dump_frontmatter(fm)
        text = head + "\n\n" + body + "\n"
        _safe_write(entry_path, text)

        result = {
            "ok": True,
            "memory_entry_path": str(entry_path),
            "originSessionId": session_id,
            "source_bytes": source_bytes,
            "compressed_bytes": fm["compressed_bytes"],
            "compression_ratio": fm["compression_ratio"],
            "target_tokens": target_tokens,
            "fallback_used": fallback_used,
            "elapsed_ms": elapsed_ms,
        }
        try:
            await self.ctx.bus.publish(COMPRESSED_TOPIC, {
                "originSessionId": session_id,
                "memory_entry_path": str(entry_path),
                "source_bytes": source_bytes,
                "compressed_bytes": fm["compressed_bytes"],
                "compression_ratio": fm["compression_ratio"],
                "elapsed_ms": elapsed_ms,
            })
        except Exception:
            log.exception("session_compressor: publish %s raised",
                          COMPRESSED_TOPIC)
        return result

    # -- bus surface ------------------------------------------

    async def handle(self, msg) -> dict:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        op = payload.get("op", "compress_jsonl")
        if op != "compress_jsonl":
            return {"ok": False, "error": f"unknown op: {op!r}"}
        raw_path = payload.get("jsonl_path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return {"ok": False, "error": "missing jsonl_path"}
        try:
            tgt = payload.get("target_tokens")
            target_tokens = int(tgt) if tgt is not None else None
        except (TypeError, ValueError):
            return {"ok": False, "error": "target_tokens must be int"}
        return await self._compress_and_write(
            Path(raw_path).expanduser(),
            memory_root=payload.get("memory_root"),
            target_tokens=target_tokens,
            pool=payload.get("pool"),
            model=payload.get("model"),
        )
