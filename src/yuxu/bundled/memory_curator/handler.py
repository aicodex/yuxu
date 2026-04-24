"""MemoryCurator — Hermes-inspired single-pass memory curation.

Pairs with:
- reflection_agent (user-directed deep exploration, multi-hypothesis)
- approval_applier (decides to actually write memory files)

Where Hermes writes memory directly on session-end, curator stages:
- `improvements` → append-only `_improvement_log.md` (Hermes char-cap + dedup)
- `memory_edits` → `_drafts/` + approval_queue (yuxu proposal discipline)

Cross-agent imports from reflection_agent.handler for small helpers; both
live in `bundled/` so the coupling is intentional and survives. If a third
memory-domain agent appears, extract those helpers to a `_memory_tools`
module.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from yuxu.bundled.reflection_agent.handler import (
    DEFAULT_COMPRESS_TARGET_TOKENS,
    _compress_sources,
    _content_hash,
    _extract_json,
    _format_sources,
    _load_existing_draft_hashes,
    _load_sources,
    _slugify,
    _truncate_bytes,
)
from yuxu.core.frontmatter import parse_frontmatter
from yuxu.core.session_log import format_jsonl_transcript

log = logging.getLogger(__name__)

COMMAND = "/curate"
COMMAND_HELP = (
    "Extract improvements + propose memory edits from a session transcript. "
    "Usage: `/curate [hint]` — reads configured sources; "
    "`/curate auto` — focuses on the worst-performing agent via performance_ranker."
)
AUTO_KEYWORD = "auto"

# Thresholds --------------------------------------------------

MIN_SOURCE_CHARS = 200         # skip floor: transcripts shorter than this → no-op
MAX_LOG_BYTES = 10_240         # improvement_log.md cap; roll-trim when over
MAX_DRAFT_BYTES = 4_096        # per proposed memory entry body
MAX_IMPROVEMENTS = 5           # per curate call
MAX_MEMORY_EDITS = 3           # per curate call
MAX_TRANSCRIPT_CHARS = 32_768  # per transcript file, after JSONL → readable render
SESSION_ENDED_TOPIC = "session.ended"

# --------------------------------------------------------------

EXTRACTOR_SYSTEM = """You are a memory curator for the yuxu agent framework (Hermes-inspired).

A session just ended (or the user manually requested curation). Read the
transcript and output TWO lists in STRICT JSON:

{{
  "improvements": [
    "<one-line terse observation about what was learned this session>"
  ],
  "memory_edits": [
    {{
      "action": "add" | "update",
      "target": "<relative path under memory_root, e.g. feedback_terse.md>",
      "title": "<short human-readable title>",
      "memory_type": "user|feedback|project|reference",
      "body": "<complete markdown ready to drop on disk, with its own inner frontmatter>",
      "rationale": "<one sentence citing the transcript>"
    }}
  ],
  "summary": "<one sentence>"
}}

RULES
- `improvements`: terse (<120 chars each), <= {max_imp} items. Meant for an
  append-only log — think "what would be useful to remember if I read this
  line 3 months from now". Skip obvious / boring / already-known things.
- `memory_edits`: ONLY for insights stable enough to deserve a whole
  memory file. Higher bar than improvements. <= {max_mem} items. Each `body`
  must be a complete file (---frontmatter--- + markdown), under {max_body}
  characters.
- For `update`, `target` MUST reference a file plausibly already in memory.
  For `add`, pick a new snake_case filename.
- Each `body`'s inner frontmatter MUST include `name`, `description`, `type`
  (one of user/feedback/project/reference). SHOULD include
  `evidence_level: observed` (new observations start here — curator is
  empirical, not validated), `status: current`, and `updated: YYYY-MM-DD`.
  MAY include `tags: [...]`. If you omit evidence_level / status / updated,
  the curator will inject defaults post-hoc.
- If nothing in this session is worth preserving, return empty lists. Do not
  invent.
- STRICT JSON only — no prose, no markdown fences.
{context_hint_block}"""


def _read_or_empty(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""


def _append_improvements(log_path: Path, entries: list[str],
                          *, max_bytes: int = MAX_LOG_BYTES) -> tuple[int, int]:
    """Append unseen entries to improvement_log.md. Dedup by content hash of
    the entry line. Roll-trim oldest section when file grows past max_bytes.

    Returns (appended_count, duplicated_count).
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_or_empty(log_path)
    # Record an index of every hash already in the file to cheaply dedup.
    seen: set[str] = set()
    for line in existing.splitlines():
        if line.startswith("- "):
            seen.add(_content_hash(line[2:].strip()))

    to_append: list[str] = []
    dupes = 0
    for e in entries:
        e = (e or "").strip()
        if not e:
            continue
        h = _content_hash(e)
        if h in seen:
            dupes += 1
            continue
        seen.add(h)
        to_append.append(e)

    if not to_append:
        return 0, dupes

    ts = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    block = f"\n## [{ts}]\n" + "\n".join(f"- {line}" for line in to_append) + "\n"
    combined = existing + block

    # Roll-trim: keep tail under max_bytes by dropping whole `## [...]` sections
    # from the top until we fit.
    b = combined.encode("utf-8", errors="replace")
    if len(b) > max_bytes:
        # split on section markers
        parts = combined.split("\n## ")
        # first part is preamble (or empty); subsequent parts are sections
        # reassemble tail-first until budget
        head = parts[0]
        sections = ["## " + p for p in parts[1:]]
        kept: list[str] = []
        total = len(block.encode("utf-8", errors="replace"))
        for sec in reversed(sections):
            sb = len(sec.encode("utf-8", errors="replace"))
            if total + sb > max_bytes:
                break
            kept.insert(0, sec)
            total += sb
        combined = ("\n".join(kept)) if kept else block.lstrip("\n")

    tmp = log_path.with_suffix(log_path.suffix + ".tmp")
    tmp.write_text(combined, encoding="utf-8")
    os.replace(tmp, log_path)
    return len(to_append), dupes


def _ensure_inner_frontmatter_defaults(body: str,
                                          session_id: Optional[str] = None) -> str:
    """Inject I6 default fields into a memory entry's inner frontmatter.

    Curator-produced entries land at evidence_level `observed` (single real
    observation, not yet validated) with status `current` and today's
    `updated` date. If the LLM already provided these fields we leave them
    alone. If the body has no frontmatter at all, pass through untouched
    (approval_applier will reject it downstream).

    `session_id` populates `originSessionId` when the LLM didn't already
    cite one. admission_gate's golden_replay stage requires this field to
    resolve a real session JSONL archive; without it the gate soft-passes,
    effectively disabling that stage for curator-produced memories.
    yuxu's `session_id` is typically the agent name whose transcript
    sourced the curate call (derived by caller from the first source's
    basename).
    """
    fm, rest = parse_frontmatter(body or "")
    if not isinstance(fm, dict) or not fm:
        return body
    changed = False
    if "evidence_level" not in fm:
        fm["evidence_level"] = "observed"
        changed = True
    if "status" not in fm:
        fm["status"] = "current"
        changed = True
    if "updated" not in fm:
        fm["updated"] = time.strftime("%Y-%m-%d", time.localtime())
        changed = True
    if session_id and "originSessionId" not in fm:
        fm["originSessionId"] = session_id
        changed = True
    if not changed:
        return body
    # Serialize back. Prefer json.dumps for strings/numbers/lists to preserve
    # non-ASCII and escape cleanly; this matches the outer-frontmatter style
    # already used by _stage_edit_draft below.
    lines = ["---"]
    for k, v in fm.items():
        if isinstance(v, (dict, list)):
            lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        elif v is None:
            lines.append(f"{k}: null")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    head = "\n".join(lines)
    tail = rest if rest.startswith("\n") else ("\n" + rest)
    return head + tail


def _stage_edit_draft(*, drafts_dir: Path, run_id: str, edit: dict,
                      score: Optional[float] = None,
                      body: Optional[str] = None,
                      session_id: Optional[str] = None) -> dict:
    """Mirror reflection_agent._stage_draft format so approval_applier
    handles curator drafts identically. Curator omits ranker metadata.

    `session_id` is forwarded to _ensure_inner_frontmatter_defaults so the
    inner frontmatter gets `originSessionId` populated — admission_gate's
    golden_replay needs it to resolve to a real session archive. Pass None
    when the caller doesn't know the session (golden_replay will soft-pass).
    """
    drafts_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    body = _truncate_bytes(body or edit.get("body", ""), MAX_DRAFT_BYTES)
    body = _ensure_inner_frontmatter_defaults(body, session_id=session_id)
    body_hash = _content_hash(body)[:8]
    slug = _slugify(edit.get("target", "") or edit.get("title", ""))
    fname = f"curator_{ts}_{run_id}_{slug}_{body_hash}.md"
    dest = drafts_dir / fname
    meta = {
        "status": "draft",
        "source": "memory_curator",
        "proposed_action": edit.get("action"),
        "proposed_target": edit.get("target"),
        "proposed_title": edit.get("title", ""),
        "memory_type": edit.get("memory_type", ""),
        "curator_run_id": run_id,
        "score": score,
        "rationale": edit.get("rationale", ""),
        "proposed_at": ts,
    }
    text = ("---\n"
            + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}"
                        for k, v in meta.items())
            + "\n---\n" + body + "\n")
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, dest)
    return {"path": str(dest), "action": edit.get("action"),
            "target": edit.get("target"), "title": edit.get("title", ""),
            "score": score}


# -- main class --------------------------------------------------


class MemoryCurator:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self._registered_command = False
        self._log_lock = asyncio.Lock()   # serialize log appends on this instance

    async def install(self) -> None:
        self.ctx.bus.subscribe(SESSION_ENDED_TOPIC, self._on_session_ended)
        # gateway slash-command — best-effort (gateway may not be up in tests)
        try:
            r = await self.ctx.bus.request("gateway", {
                "op": "register_command",
                "command": COMMAND,
                "agent": "memory_curator",
                "help": COMMAND_HELP,
            }, timeout=2.0)
            if isinstance(r, dict) and r.get("ok"):
                self._registered_command = True
            else:
                log.info("memory_curator: register_command unavailable: %s", r)
        except Exception:
            log.info("memory_curator: gateway not reachable; skip /curate registration")
        self.ctx.bus.subscribe("gateway.command_invoked", self._on_command)

    async def uninstall(self) -> None:
        try:
            self.ctx.bus.unsubscribe(SESSION_ENDED_TOPIC, self._on_session_ended)
            self.ctx.bus.unsubscribe("gateway.command_invoked", self._on_command)
        except Exception:
            pass
        if self._registered_command:
            try:
                await self.ctx.bus.request("gateway", {
                    "op": "unregister_command", "command": COMMAND,
                }, timeout=2.0)
            except Exception:
                pass
            self._registered_command = False

    # -- event handlers -------------------------------------------

    async def _on_session_ended(self, event: dict) -> None:
        payload = event.get("payload") if isinstance(event, dict) else None
        if not isinstance(payload, dict):
            return
        transcript = payload.get("transcript")
        transcript_path = payload.get("transcript_path")
        sources = None
        # Prefer rendering the JSONL transcript into readable text at the
        # boundary so the LLM doesn't have to parse JSON lines. `.jsonl`
        # paths that don't exist yet (race condition: curator called before
        # the last append lands) gracefully fall through as empty.
        if not transcript and transcript_path:
            try:
                if str(transcript_path).endswith(".jsonl"):
                    transcript = format_jsonl_transcript(
                        transcript_path, max_chars=MAX_TRANSCRIPT_CHARS,
                    ) or None
                else:
                    sources = [transcript_path]
            except Exception:
                log.exception(
                    "memory_curator: format_jsonl_transcript(%s) failed",
                    transcript_path,
                )
                sources = [transcript_path]  # fall back to raw read
        context_hint = payload.get("context_hint") or \
            f"auto-curated on {SESSION_ENDED_TOPIC}"
        result = await self.curate(
            sources=sources, transcript=transcript,
            context_hint=context_hint,
        )
        if not result.get("ok"):
            log.info("memory_curator: session-end curate skipped: %s",
                     result.get("reason") or result.get("error"))

    async def _on_command(self, event: dict) -> None:
        payload = event.get("payload") if isinstance(event, dict) else None
        if not isinstance(payload, dict) or payload.get("command") != COMMAND:
            return
        session_key = payload.get("session_key", "")
        args = (payload.get("args") or "").strip()
        if args.lower() == AUTO_KEYWORD:
            result = await self.curate(auto=True)
            quote = f"{COMMAND} {AUTO_KEYWORD}"
        else:
            hint = args or None
            result = await self.curate(context_hint=hint)
            quote = f"{COMMAND} {args}".strip() if args else COMMAND
        await self._reply(session_key, result, quote_text=quote)

    # -- core flow -------------------------------------------------

    def _resolve_memory_root(self, override: Optional[Path | str]) -> Path:
        if override is not None:
            return Path(override).expanduser().resolve()
        agent_dir = Path(self.ctx.agent_dir).resolve()
        for cand in (agent_dir, *agent_dir.parents):
            if (cand / "yuxu.json").exists():
                return cand / "data" / "memory"
        return Path.cwd() / "data" / "memory"

    def _default_session_root(self) -> Path:
        agent_dir = Path(self.ctx.agent_dir).resolve()
        for cand in (agent_dir, *agent_dir.parents):
            if (cand / "yuxu.json").exists():
                return cand / "data" / "sessions"
        return Path.cwd() / "data" / "sessions"

    async def _resolve_auto_hint(self) -> tuple[Optional[str], Optional[dict]]:
        """Ask performance_ranker for the worst agent and return
        (hint_string, ranker_row), or (None, None) if unavailable."""
        try:
            r = await self.ctx.bus.request(
                "performance_ranker", {"op": "rank", "limit": 1},
                timeout=2.0,
            )
        except LookupError:
            return None, None
        except Exception:
            log.exception("memory_curator: performance_ranker request raised")
            return None, None
        if not isinstance(r, dict) or not r.get("ok"):
            return None, None
        ranked = r.get("ranked") or []
        if not ranked:
            return None, None
        top = ranked[0]
        window = r.get("window_hours", 24)
        hint = (
            f"Focus curation on agent `{top['agent']}` — it has accumulated "
            f"{top['errors']} error(s) and {top['rejections']} rejection(s) "
            f"in the last {window}h (score={top['score']:.1f}). "
            "Prefer improvements / memory edits that would help this agent."
        )
        return hint, top

    async def curate(self, *,
                     sources: Optional[list[str]] = None,
                     transcript: Optional[str] = None,
                     context_hint: Optional[str] = None,
                     auto: bool = False,
                     memory_root: Optional[Path | str] = None,
                     pool: Optional[str] = None,
                     model: Optional[str] = None) -> dict:
        run_id = uuid.uuid4().hex[:8]
        warnings: list[str] = []
        auto_target: Optional[dict] = None
        # Auto mode: query ranker and merge its hint with caller's (caller's
        # hint wins position, ranker's appends). Ranker unavailable → warn
        # and proceed without auto hint (soft failure, unlike reflection).
        if auto:
            auto_hint, auto_target = await self._resolve_auto_hint()
            if auto_hint is None:
                warnings.append("auto mode: performance_ranker unavailable "
                                "or no struggling agents; proceeding without "
                                "auto hint")
            else:
                context_hint = (f"{context_hint}\n\n{auto_hint}"
                                 if context_hint else auto_hint)
        mem_root = self._resolve_memory_root(memory_root)
        drafts_dir = mem_root / "_drafts"
        log_path = mem_root / "_improvement_log.md"

        # Load content
        # Resolve pool/model first — compress call below shares them.
        pool = pool or os.environ.get("CURATOR_POOL") \
            or os.environ.get("NEWSFEED_POOL") or "openai"
        model = model or os.environ.get("CURATOR_MODEL") \
            or os.environ.get("TFE_MODEL") or "gpt-4o-mini"

        if transcript:
            body_text = transcript
            loaded = [{"path": "<inline>", "text": transcript}]
        else:
            loaded, load_warnings = _load_sources(
                sources, self._default_session_root(),
            )
            warnings.extend(load_warnings)
            if not loaded:
                await self._publish_skip(run_id, "no readable sources")
                return {"ok": False, "run_id": run_id,
                        "reason": "no readable sources", "warnings": warnings}
            # Compress loaded sources before feeding the LLM. Short-circuits
            # on small inputs; falls back to raw format when context_compressor
            # is absent.
            body_text, compress_warnings = await _compress_sources(
                self.ctx.bus, loaded,
                task=f"curate improvements and memory edits ({context_hint or 'general'})",
                target_tokens=DEFAULT_COMPRESS_TARGET_TOKENS,
                pool=pool, model=model,
            )
            warnings.extend(compress_warnings)

        # Floor: skip trivially short content
        if len(body_text.strip()) < MIN_SOURCE_CHARS:
            await self._publish_skip(run_id,
                                     f"transcript < {MIN_SOURCE_CHARS} chars")
            return {"ok": False, "run_id": run_id,
                    "reason": f"transcript too short ({len(body_text)} chars)",
                    "warnings": warnings}

        ctx_block = (f"\nContext hint: {context_hint.strip()}\n"
                     if context_hint else "")
        prompt = EXTRACTOR_SYSTEM.format(
            max_imp=MAX_IMPROVEMENTS, max_mem=MAX_MEMORY_EDITS,
            max_body=MAX_DRAFT_BYTES, context_hint_block=ctx_block,
        )
        try:
            resp = await self.ctx.bus.request("llm_driver", {
                "op": "run_turn",
                "system_prompt": prompt,
                "messages": [{"role": "user", "content":
                              f"Transcript:\n{body_text}"}],
                "pool": pool, "model": model,
                "temperature": 0.2, "json_mode": True,
                "max_iterations": 1,
                "strip_thinking_blocks": True,
                "llm_timeout": 90.0,
            }, timeout=120.0)
        except Exception as e:
            log.exception("memory_curator: bus.request failed")
            return {"ok": False, "run_id": run_id,
                    "error": f"bus.request: {e}", "warnings": warnings}
        if not resp.get("ok"):
            return {"ok": False, "run_id": run_id,
                    "error": resp.get("error"), "raw": resp.get("content"),
                    "warnings": warnings}

        obj = _extract_json(resp.get("content") or "")
        if not isinstance(obj, dict):
            return {"ok": False, "run_id": run_id,
                    "error": "no JSON in LLM output",
                    "raw": resp.get("content"), "warnings": warnings}

        improvements = [str(x) for x in (obj.get("improvements") or [])
                        if isinstance(x, str) and x.strip()][:MAX_IMPROVEMENTS]
        raw_edits = obj.get("memory_edits") or []
        memory_edits: list[dict] = []
        for e in raw_edits:
            if not isinstance(e, dict):
                continue
            if e.get("action") not in ("add", "update"):
                continue
            if not e.get("target") or not e.get("body"):
                continue
            memory_edits.append(e)
            if len(memory_edits) >= MAX_MEMORY_EDITS:
                break

        # Append improvements (atomic + dedup + roll)
        appended = 0
        dupes = 0
        if improvements:
            async with self._log_lock:
                appended, dupes = _append_improvements(log_path, improvements)

        # Stage edits as drafts. Per-run dedup (this call's edits) + cross-run
        # dedup (drafts already pending under _drafts/ from earlier curator or
        # reflection runs). Cross-run key is the 8-char hash prefix encoded
        # in the draft filename (`*_<hash8>.md`); curator's _content_hash
        # returns 12 chars so we slice to align. Without cross-run dedup,
        # repeated `/curate` on similar sources flooded approval_queue and
        # admission_gate's noop_baseline was the only backstop.
        drafts: list[dict] = []
        seen_hashes: set[str] = {h for h in _load_existing_draft_hashes(drafts_dir)}
        cross_run_skipped = 0
        # Derive originSessionId from the first source's basename (e.g.
        # `data/sessions/harness_pro_max.jsonl` → `harness_pro_max`). In
        # yuxu a "session" is an agent's transcript, so the agent name is
        # the canonical citation. admission_gate's golden_replay prefix-
        # matches this against `*.jsonl` filenames under session_root.
        session_id: Optional[str] = None
        if loaded:
            try:
                first_path = loaded[0].get("path") or ""
                if first_path and first_path != "<inline>":
                    session_id = Path(first_path).stem or None
            except Exception:
                session_id = None
        for e in memory_edits:
            body = _truncate_bytes(e.get("body", ""), MAX_DRAFT_BYTES)
            h8 = _content_hash(body)[:8]
            if h8 in seen_hashes:
                cross_run_skipped += 1
                continue
            seen_hashes.add(h8)
            drafts.append(_stage_edit_draft(
                drafts_dir=drafts_dir, run_id=run_id, edit=e, body=body,
                session_id=session_id,
            ))

        if cross_run_skipped:
            warnings.append(f"cross-run dedup skipped {cross_run_skipped} "
                            "draft(s) already staged in _drafts/")

        approval_ids = await self._enqueue_approvals(drafts, run_id, context_hint)

        # Surface LLM timing so the gateway reply footer can show stats
        llm_usage = resp.get("usage") or {}
        llm_elapsed_ms = float(resp.get("elapsed_ms") or 0)
        llm_tps = resp.get("output_tps")
        summary = str(obj.get("summary") or "")
        out = {
            "ok": True, "run_id": run_id,
            "log_entries": appended,
            "log_dupes_dropped": dupes,
            "drafts": drafts,
            "approval_ids": approval_ids,
            "summary": summary,
            "warnings": warnings,
            "memory_root": str(mem_root),
            **({"auto_target": auto_target} if auto_target else {}),
            "llm_stats": {
                "n_calls": 1,
                "elapsed_ms": round(llm_elapsed_ms, 2),
                "output_tps": llm_tps,
                "prompt_tokens": int(llm_usage.get("prompt_tokens") or 0),
                "completion_tokens": int(llm_usage.get("completion_tokens") or 0),
            },
        }
        await self.ctx.bus.publish("memory_curator.curated", {
            "run_id": run_id, "log_entries": appended,
            "drafts": drafts, "approval_ids": approval_ids,
            "summary": summary,
        })
        return out

    async def _enqueue_approvals(self, drafts: list[dict], run_id: str,
                                  context_hint: Optional[str]) -> list[str]:
        ids: list[str] = []
        for d in drafts:
            try:
                r = await self.ctx.bus.request("approval_queue", {
                    "op": "enqueue",
                    "action": "memory_edit",
                    "detail": {
                        "run_id": run_id,
                        "context_hint": context_hint or "",
                        "draft_path": d["path"],
                        "proposed_target": d["target"],
                        "proposed_action": d["action"],
                        "title": d["title"],
                    },
                    "requester": "memory_curator",
                }, timeout=2.0)
            except Exception:
                continue
            if isinstance(r, dict) and r.get("ok") and r.get("approval_id"):
                ids.append(r["approval_id"])
        return ids

    async def _publish_skip(self, run_id: str, reason: str) -> None:
        await self.ctx.bus.publish("memory_curator.skipped", {
            "run_id": run_id, "reason": reason,
        })

    # -- bus surface ----------------------------------------------

    async def handle(self, msg) -> dict:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        op = payload.get("op", "curate")
        if op == "status":
            mem_root = self._resolve_memory_root(payload.get("memory_root"))
            log_path = mem_root / "_improvement_log.md"
            if log_path.exists():
                text = _read_or_empty(log_path)
                count = sum(1 for line in text.splitlines()
                            if line.startswith("- "))
                return {"ok": True, "log_path": str(log_path),
                        "log_bytes": len(text.encode("utf-8", errors="replace")),
                        "improvements_total": count}
            return {"ok": True, "log_path": str(log_path),
                    "log_bytes": 0, "improvements_total": 0}
        if op != "curate":
            return {"ok": False, "error": f"unknown op: {op!r}"}
        return await self.curate(
            sources=payload.get("sources"),
            transcript=payload.get("transcript"),
            context_hint=payload.get("context_hint"),
            auto=bool(payload.get("auto", False)),
            memory_root=payload.get("memory_root"),
            pool=payload.get("pool"), model=payload.get("model"),
        )

    # -- reply formatting -----------------------------------------

    def _format_reply_parts(self, result: dict) -> tuple[str, list[tuple[str, str]]]:
        """Split curate result into (content, footer_meta)."""
        from yuxu.bundled.gateway.reply_helpers import format_llm_stats_footer

        footer = format_llm_stats_footer(result.get("llm_stats"))

        if not result.get("ok"):
            r = result.get("reason") or result.get("error") or "(no message)"
            content = f"⏭  curate skipped: {r}"
            footer.insert(0, ("status", "skipped"))
            return content, footer

        drafts = result.get("drafts") or []
        approvals = result.get("approval_ids") or []
        lines = [f"✅ curate run `{result['run_id']}`"]
        if drafts:
            lines.append(f"\n**{len(drafts)} draft(s) staged**:")
            for d in drafts:
                lines.append(f"- `{d['action']}` → `{d['target']}` "
                             f"— {d.get('title', '')}")
        if result.get("summary"):
            lines.append(f"\n{result['summary']}")

        footer.insert(0, ("log+", str(result.get("log_entries", 0))))
        if result.get("log_dupes_dropped"):
            footer.insert(1, ("log dupes", str(result["log_dupes_dropped"])))
        footer.insert(2 if result.get("log_dupes_dropped") else 1,
                       ("drafts", str(len(drafts))))
        footer.insert(3 if result.get("log_dupes_dropped") else 2,
                       ("approvals",
                        f"{len(approvals)} queued" if approvals else "disk-only"))
        return "\n".join(lines), footer

    def _format_reply(self, result: dict) -> str:
        from yuxu.bundled.gateway.reply_helpers import compose_fallback_text
        content, footer = self._format_reply_parts(result)
        return compose_fallback_text(content, footer)

    async def _reply(self, session_key: str, result: dict,
                     *, quote_user: Optional[str] = None,
                     quote_text: Optional[str] = None) -> None:
        from yuxu.bundled.gateway.reply_helpers import reply_via_gateway
        content, footer = self._format_reply_parts(result)
        await reply_via_gateway(
            self.ctx.bus, session_key,
            content=content, footer_meta=footer,
            quote_user=quote_user, quote_text=quote_text,
            agent_name="memory_curator",
        )
