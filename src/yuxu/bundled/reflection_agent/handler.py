"""ReflectionAgent — Hermes-inspired iterative exploration + memory proposal.

Multi-hypothesis exploration of past sessions (the part Hermes doesn't have)
followed by atomic-staged drafts and approval_queue submission (Hermes's good
parts: atomic file ops, dedup, char-cap). No memory file is touched without
explicit user approval.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from glob import glob
from pathlib import Path
from typing import Any, Optional

from yuxu.core.frontmatter import parse_frontmatter

log = logging.getLogger(__name__)

COMMAND = "/reflect"
COMMAND_HELP = (
    "Iteratively explore past sessions; stage memory edit proposals "
    "you can approve. "
    "Usage: `/reflect <need>` or `/reflect auto` "
    "(auto picks the worst-performing agent via performance_ranker)."
)
AUTO_KEYWORD = "auto"

DEFAULT_HYPOTHESES = 3
MAX_SOURCE_BYTES = 500_000     # per source file — safety cap; compressor handles budget
MAX_DRAFT_BYTES = 4_096        # per drafted memory entry body
MAX_TOTAL_SOURCE_BYTES = 5_000_000  # safety cap; context_compressor enforces real budget
DEFAULT_COMPRESS_TARGET_TOKENS = 10_000  # post-compression budget fed to LLM

# Three framings; each one is a different prompt persona that biases the
# extractor toward a complementary slice of the transcript.
FRAMINGS = (
    {
        "id": "pattern_extractor",
        "lens": ("Look for recurring SUCCESSFUL patterns the user keeps "
                 "rediscovering. Things that worked. Lock them in."),
    },
    {
        "id": "anti_pattern_spotter",
        "lens": ("Look for FAILED approaches the user kept circling back to. "
                 "Mistakes that wasted time. Surface them so they're not "
                 "repeated next session."),
    },
    {
        "id": "synthesizer",
        "lens": ("Look for CROSS-CUTTING insights — connections between "
                 "different topics in the transcript that the user hasn't "
                 "named yet but is implicitly relying on."),
    },
)

EXTRACTOR_SYSTEM = """You are a reflection assistant for the yuxu agent framework.

You will receive:
- A user need / focus area
- (optional) An index of existing memory entries already in the store
- One or more session transcripts (markdown)
- A specific lens to use

Your job: read the transcripts through the lens and propose 1-5 *memory edits*
that would help future sessions on this need. Output STRICT JSON, no prose:

{{
  "edits": [
    {{
      "action": "add" | "update",
      "target": "<relative path under memory_root, e.g. feedback_dialog_design.md>",
      "title": "<short human-readable title>",
      "memory_type": "user|feedback|project|reference",
      "body": "<full markdown body of the proposed memory entry, including frontmatter>",
      "rationale": "<one sentence: why this edit, citing the transcript>"
    }}
  ],
  "summary": "<one sentence summarizing this hypothesis>"
}}

Rules:
- The `body` field must be a complete file ready to drop on disk; it should
  start with `---` frontmatter (name / description / type), then the body.
- Keep each `body` under {max_body} characters.
- For `update`, `target` MUST match an existing entry's path from the
  "Existing memory" index if one was provided. For `add`, pick a new
  snake_case filename that does NOT collide with any existing entry.
- **Prefer `update` over `add`** when the existing memory index shows a
  related entry — amend the current entry instead of creating a parallel
  one. Only `add` when no existing entry covers the insight.
- Don't propose edits that contradict an explicit user preference visible in
  the transcripts.
- If you find nothing high-signal under this lens, return {{"edits": [], "summary": "..."}}.

Lens: {lens}"""

RANKER_SYSTEM = """You are a memory-edit reviewer for the yuxu agent framework.

You receive several hypothesis outputs from independent reviewers, each with
a list of proposed memory edits. Pick the **strongest non-redundant subset**
the user should be asked to approve. Bias toward FEW high-signal edits over
MANY low-signal ones.

Output STRICT JSON:

{{
  "chosen": [
    {{
      "framing_id": "<which hypothesis this came from>",
      "edit_index": <int, position in that hypothesis's edits list>,
      "score": <float 0-1, your confidence>,
      "reason": "<one sentence: why this edit beat the others>"
    }}
  ],
  "rejected_summary": "<one sentence on what you dropped and why>"
}}

Constraints:
- Pick at most {max_chosen} edits total.
- Drop near-duplicates by `target` and `title`. Prefer the higher-signal version.
- If a hypothesis returned no edits, just skip it."""


# -- helpers -----------------------------------------------------


def _truncate_bytes(text: str, limit: int) -> str:
    if not text:
        return ""
    b = text.encode("utf-8", errors="replace")
    if len(b) <= limit:
        return text
    return b[:limit].decode("utf-8", errors="ignore") + "\n[...truncated]"


def _format_memory_index(entries: list[dict], *, max_lines: int = 100) -> str:
    """Render memory index entries as a compact text block for LLM prompts.

    One line per entry: `- path (tier, type) — description`. Capped at
    max_lines to prevent prompt bloat on large memory stores; if truncated,
    the final line notes how many entries were dropped.
    """
    if not entries:
        return ""
    lines: list[str] = []
    for e in entries[:max_lines]:
        path = e.get("path") or "?"
        level = e.get("evidence_level") or "?"
        typ = e.get("type") or "?"
        desc = (e.get("description") or "").strip()
        lines.append(f"- {path} ({level}, {typ}) — {desc}")
    if len(entries) > max_lines:
        lines.append(f"- [+{len(entries) - max_lines} more entries omitted]")
    return "\n".join(lines)


def _load_sources(sources: Optional[list[str]],
                  default_root: Path) -> tuple[list[dict], list[str]]:
    """Return ([{path, text}], warnings). Empty list means no usable sources."""
    warnings: list[str] = []
    paths: list[Path] = []
    if sources:
        for s in sources:
            sp = Path(s).expanduser()
            if sp.is_file():
                paths.append(sp)
            elif sp.is_dir():
                paths.extend(p for p in sp.rglob("*.md") if p.is_file())
            else:
                # treat as glob
                for hit in sorted(glob(str(sp), recursive=True)):
                    p = Path(hit)
                    if p.is_file() and p.suffix == ".md":
                        paths.append(p)
    else:
        if default_root.exists():
            paths.extend(p for p in default_root.rglob("*.md") if p.is_file())
        else:
            warnings.append(f"default sources dir {default_root} does not exist")

    out: list[dict] = []
    total_bytes = 0
    for p in sorted(set(paths)):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as e:
            warnings.append(f"could not read {p}: {e}")
            continue
        text = _truncate_bytes(text, MAX_SOURCE_BYTES)
        if total_bytes + len(text.encode("utf-8", errors="replace")) > MAX_TOTAL_SOURCE_BYTES:
            warnings.append(f"hit MAX_TOTAL_SOURCE_BYTES; skipping {len(paths) - len(out)} more files")
            break
        out.append({"path": str(p), "text": text})
        total_bytes += len(text.encode("utf-8", errors="replace"))
    return out, warnings


def _format_sources(sources: list[dict]) -> str:
    parts = []
    for s in sources:
        parts.append(f"### Source: {s['path']}\n\n{s['text']}\n")
    return "\n---\n".join(parts)


async def _compress_sources(bus, sources: list[dict], *, task: str,
                              target_tokens: int = DEFAULT_COMPRESS_TARGET_TOKENS,
                              pool: Optional[str] = None,
                              model: Optional[str] = None
                              ) -> tuple[str, list[str]]:
    """Run loaded sources through context_compressor. Gracefully falls back
    to `_format_sources` when the skill isn't loaded or errors out, and
    when the compressor reports `skipped=true` (input under budget).
    Returns (sources_block, warnings).
    """
    if not sources:
        return "", []
    documents = [{"id": s.get("path") or "src", "body": s.get("text") or ""}
                  for s in sources]
    try:
        r = await bus.request("context_compressor", {
            "op": "summarize",
            "documents": documents,
            "task": task,
            "target_tokens": target_tokens,
            "pool": pool,
            "model": model,
        }, timeout=300.0)
    except LookupError:
        return _format_sources(sources), [
            "context_compressor not loaded; using raw sources"]
    except Exception as e:
        return _format_sources(sources), [
            f"context_compressor raised: {e}; using raw sources"]
    if not isinstance(r, dict) or not r.get("ok"):
        err = r.get("error") if isinstance(r, dict) else "non-dict"
        return _format_sources(sources), [
            f"context_compressor not ok ({err}); using raw sources"]
    if r.get("skipped"):
        # Already under budget — compressor returned concatenated originals.
        return r.get("merged_summary") or _format_sources(sources), []
    merged = r.get("merged_summary") or ""
    warns: list[str] = []
    if r.get("fallback_used"):
        warns.append("context_compressor used head+tail fallback (LLM unavailable)")
    if not merged.strip():
        return _format_sources(sources), (warns +
            ["context_compressor returned empty; using raw sources"])
    return merged, warns


def _extract_json(text: str) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]


def _slugify(s: str, max_len: int = 30) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")
    return (s[:max_len] or "edit")


# -- main class --------------------------------------------------


class ReflectionAgent:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self._registered_command = False
        # CC port: per-agent MEMORY.md observations are the persistent carry-over
        # between runs. Loaded at install() and cached — reflect() uses them to
        # bias hypotheses. Runs get appended at the end of each reflect().
        self._agent_memory_observations: str = ""

    async def install(self) -> None:
        self.ctx.bus.subscribe("gateway.command_invoked", self._on_command)
        # Load persistent observations once per process. If Loader didn't wire
        # agent_memory_path (no `memory:` in frontmatter, no project root, or
        # init error), stays empty string — reflection silently degrades to
        # the pre-port behaviour.
        self._agent_memory_observations = self._load_agent_memory_observations()
        try:
            r = await self.ctx.bus.request("gateway", {
                "op": "register_command",
                "command": COMMAND,
                "agent": "reflection_agent",
                "help": COMMAND_HELP,
            }, timeout=2.0)
            if isinstance(r, dict) and r.get("ok"):
                self._registered_command = True
            else:
                log.warning("reflection_agent: register_command failed: %s", r)
        except Exception:
            log.exception("reflection_agent: register_command raised")

    async def uninstall(self) -> None:
        try:
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

    # -- agent memory (CC AgentTool `memory:` port) ----------------

    def _load_agent_memory_observations(self) -> str:
        """Read ctx.agent_memory_path, return the body text under `## Observations`.

        Returns "" when no agent_memory_path is wired, the file hasn't been
        seeded yet, or the Observations heading is absent. The ## Runs log is
        never injected back into prompts — past run metadata would waste
        tokens without improving hypothesis quality.
        """
        path = getattr(self.ctx, "agent_memory_path", None)
        if path is None or not path.exists():
            return ""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            log.warning("reflection_agent: could not read agent_memory_path %s: %s",
                         path, e)
            return ""
        _, body = parse_frontmatter(text)
        # Strip the ## Observations section (up to next ## heading / EOF).
        lines = body.splitlines()
        out: list[str] = []
        inside = False
        for line in lines:
            if line.strip().startswith("## "):
                if inside:
                    break
                if line.strip().lower() == "## observations":
                    inside = True
                continue
            if inside:
                out.append(line)
        return "\n".join(out).strip()

    def _append_agent_memory_run(self, *, need: str, framings: list[dict],
                                    n_hyp_ok: int, n_drafts: int,
                                    run_id: str) -> None:
        """Append one bullet under `## Runs` in ctx.agent_memory_path. Creates
        the section if absent. Best-effort — any IO error is logged and
        swallowed; reflect() must not fail because of memory logging."""
        path = getattr(self.ctx, "agent_memory_path", None)
        if path is None:
            return
        ts = time.strftime("%Y-%m-%dT%H:%M")
        framing_ids = ",".join(f.get("id", "?") for f in framings)
        # Trim `need` on the log line so huge prompts don't bloat the file.
        need_short = need.strip().replace("\n", " ")
        if len(need_short) > 120:
            need_short = need_short[:119] + "\u2026"
        bullet = (f"- {ts} / run={run_id} / need=\"{need_short}\" / "
                   f"hypotheses={n_hyp_ok} / drafts={n_drafts} / "
                   f"framings=[{framing_ids}]")
        try:
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            if "\n## Runs" in text or text.startswith("## Runs"):
                # Append at the END of the file — simpler than inserting at
                # the end of the Runs section, and a ##-heading-ended file
                # reads the same either way.
                if not text.endswith("\n"):
                    text += "\n"
                text += bullet + "\n"
            else:
                if not text.endswith("\n"):
                    text += "\n"
                text += "\n## Runs\n\n" + bullet + "\n"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError as e:
            log.warning("reflection_agent: could not append run log to %s: %s",
                         path, e)

    # -- event handlers --------------------------------------------

    async def _on_command(self, event: dict) -> None:
        payload = event.get("payload") if isinstance(event, dict) else None
        if not isinstance(payload, dict) or payload.get("command") != COMMAND:
            return
        session_key = payload.get("session_key", "")
        args = (payload.get("args") or "").strip()
        if not args:
            usage = {"ok": False, "stage": "usage",
                     "error": f"Usage: `{COMMAND} <need>` or "
                              f"`{COMMAND} {AUTO_KEYWORD}`\n\n{COMMAND_HELP}",
                     "warnings": []}
            await self._reply(session_key, usage)
            return
        if args.lower() == AUTO_KEYWORD:
            result = await self.reflect(auto=True)
            await self._reply(session_key, result,
                              quote_text=f"{COMMAND} {AUTO_KEYWORD}")
            return
        result = await self.reflect(need=args)
        # Quote the user's /reflect invocation so the reply shows what
        # request the drafts / stats correspond to.
        await self._reply(session_key, result,
                          quote_text=f"{COMMAND} {args}")

    # -- core flow -------------------------------------------------

    def _resolve_paths(self, memory_root: Optional[Path | str]) -> tuple[Path, Path]:
        """Resolve (memory_root, drafts_dir). memory_root defaults from agent_dir."""
        if memory_root is not None:
            mr = Path(memory_root).expanduser().resolve()
        else:
            # walk up from agent_dir to find <project>/data/memory
            agent_dir = Path(self.ctx.agent_dir).resolve()
            mr = None
            for cand in (agent_dir, *agent_dir.parents):
                if (cand / "yuxu.json").exists():
                    mr = cand / "data" / "memory"
                    break
            if mr is None:
                mr = Path.cwd() / "data" / "memory"
        drafts = mr / "_drafts"
        return mr, drafts

    def _default_session_root(self) -> Path:
        agent_dir = Path(self.ctx.agent_dir).resolve()
        for cand in (agent_dir, *agent_dir.parents):
            if (cand / "yuxu.json").exists():
                return cand / "data" / "sessions"
        return Path.cwd() / "data" / "sessions"

    async def _fetch_memory_index(self, memory_root: Path
                                    ) -> tuple[list[dict], Optional[str]]:
        """Ask the `memory` skill for existing entries so the LLM can
        propose `update` over `add` when a related entry already exists.

        Per I6 discipline — do not rglob memory files directly. Returns
        ([], warning) when the skill is unavailable; reflection proceeds
        memory-unaware rather than failing hard.
        """
        try:
            r = await self.ctx.bus.request("memory", {
                "op": "list",
                "mode": "reflect",
                "memory_root": str(memory_root),
            }, timeout=5.0)
        except LookupError:
            return [], ("memory skill not loaded; reflection will "
                        "propose without memory-index awareness")
        except Exception as e:
            return [], f"memory.list raised: {e}"
        if not isinstance(r, dict) or not r.get("ok"):
            err = r.get("error") if isinstance(r, dict) else "non-dict response"
            return [], f"memory.list not ok: {err}"
        entries = r.get("entries") or []
        if not isinstance(entries, list):
            return [], "memory.list entries is not a list"
        return entries, None

    async def _resolve_auto_target(self) -> tuple[Optional[str], Optional[dict]]:
        """Query performance_ranker for the worst-performing agent and
        synthesize a `need` string focused on that agent. Returns
        (need_string, ranker_row) or (None, None) if unavailable.
        """
        try:
            r = await self.ctx.bus.request(
                "performance_ranker", {"op": "rank", "limit": 1},
                timeout=2.0,
            )
        except LookupError:
            return None, None  # ranker not loaded
        except Exception:
            log.exception("reflection_agent: performance_ranker request raised")
            return None, None
        if not isinstance(r, dict) or not r.get("ok"):
            return None, None
        ranked = r.get("ranked") or []
        if not ranked:
            return None, None
        top = ranked[0]
        window = r.get("window_hours", 24)
        need = (
            f"Review agent `{top['agent']}` which has accumulated "
            f"{top['errors']} error(s) and {top['rejections']} rejection(s) "
            f"in the last {window}h (score={top['score']:.1f}). "
            "Identify concrete improvements or anti-patterns — what is this "
            "agent getting wrong repeatedly, and what should future sessions "
            "do differently?"
        )
        return need, top

    async def reflect(self, *, need: Optional[str] = None,
                      auto: bool = False,
                      sources: Optional[list[str]] = None,
                      memory_root: Optional[Path | str] = None,
                      n_hypotheses: int = DEFAULT_HYPOTHESES,
                      pool: Optional[str] = None,
                      model: Optional[str] = None) -> dict:
        run_id = uuid.uuid4().hex[:8]
        warnings: list[str] = []
        auto_target: Optional[dict] = None
        # Auto-mode: synthesize `need` from performance_ranker's top worst.
        if auto or not (isinstance(need, str) and need.strip()):
            if not auto:
                # `need` was empty and `auto` wasn't set — preserve old
                # behavior: require a real need.
                return {"ok": False, "run_id": run_id,
                        "stage": "usage",
                        "error": "missing or empty field: need",
                        "warnings": warnings}
            resolved_need, auto_target = await self._resolve_auto_target()
            if resolved_need is None:
                return {"ok": False, "run_id": run_id,
                        "stage": "auto_target",
                        "error": ("performance_ranker unavailable or no "
                                  "struggling agents in the window"),
                        "warnings": warnings}
            need = resolved_need
        assert isinstance(need, str)
        memory_root_path, drafts_dir = self._resolve_paths(memory_root)
        loaded, load_warnings = _load_sources(sources, self._default_session_root())
        warnings.extend(load_warnings)
        if not loaded:
            return {"ok": False, "run_id": run_id, "stage": "load_sources",
                    "error": "no readable session sources",
                    "warnings": warnings,
                    **({"auto_target": auto_target} if auto_target else {})}

        # Memory-aware context: query the memory skill for existing entries
        # so hypotheses can prefer `update` over `add` when a related entry
        # exists. Best-effort — failure becomes a warning, not a hard stop.
        memory_entries, mem_warn = await self._fetch_memory_index(memory_root_path)
        if mem_warn:
            warnings.append(mem_warn)
        memory_index_block = _format_memory_index(memory_entries)

        n = max(1, min(n_hypotheses, len(FRAMINGS)))
        framings = list(FRAMINGS[:n])

        pool = pool or os.environ.get("REFLECTION_POOL") \
            or os.environ.get("NEWSFEED_POOL") or "openai"
        model = model or os.environ.get("REFLECTION_MODEL") \
            or os.environ.get("TFE_MODEL") or "gpt-4o-mini"

        # Phase 1: parallel hypotheses. Compress first — context_compressor
        # short-circuits when total input is under budget, so small inputs
        # pass through unchanged. When absent, falls back to raw format.
        sources_block, compress_warnings = await _compress_sources(
            self.ctx.bus, loaded, task=need,
            target_tokens=DEFAULT_COMPRESS_TARGET_TOKENS,
            pool=pool, model=model,
        )
        warnings.extend(compress_warnings)
        hyp_results = await asyncio.gather(*[
            self._explore(need=need, framing=fr, sources_block=sources_block,
                          memory_index_block=memory_index_block,
                          pool=pool, model=model)
            for fr in framings
        ], return_exceptions=True)
        hypotheses: list[dict] = []
        for fr, hr in zip(framings, hyp_results):
            if isinstance(hr, Exception):
                warnings.append(f"hypothesis {fr['id']} crashed: {hr}")
                hypotheses.append({"framing_id": fr["id"], "ok": False,
                                   "error": str(hr), "edits": []})
                continue
            hypotheses.append({"framing_id": fr["id"], **hr})

        all_edits = sum((h.get("edits") or [] for h in hypotheses if h.get("ok")), [])
        if not all_edits:
            return {"ok": False, "run_id": run_id, "stage": "hypothesize",
                    "error": "no usable edits from any hypothesis",
                    "hypotheses": hypotheses, "warnings": warnings,
                    **({"auto_target": auto_target} if auto_target else {})}

        # Phase 2: rank
        rank_resp = await self._rank(need=need, hypotheses=hypotheses,
                                     pool=pool, model=model,
                                     max_chosen=min(5, len(all_edits)))
        if not rank_resp.get("ok"):
            warnings.append(f"ranker failed: {rank_resp.get('error')}; "
                            "falling back to all edits unscored")
            chosen = [{"framing_id": h["framing_id"], "edit_index": i,
                       "score": 0.5, "reason": "ranker fallback"}
                      for h in hypotheses if h.get("ok")
                      for i in range(len(h.get("edits") or []))]
            rejected_summary = ""
        else:
            chosen = rank_resp["chosen"]
            rejected_summary = rank_resp.get("rejected_summary", "")

        # Phase 3: stage drafts on disk
        drafts_dir.mkdir(parents=True, exist_ok=True)
        drafts: list[dict] = []
        seen_hashes: set[str] = set()
        for c in chosen:
            edit = self._lookup_edit(hypotheses, c.get("framing_id"),
                                     c.get("edit_index"))
            if edit is None:
                warnings.append(f"ranker pointed at missing edit "
                                f"{c.get('framing_id')}#{c.get('edit_index')}")
                continue
            body = _truncate_bytes(edit.get("body", ""), MAX_DRAFT_BYTES)
            h = _content_hash(body)
            if h in seen_hashes:
                continue  # dedup by content
            seen_hashes.add(h)
            draft = self._stage_draft(
                drafts_dir=drafts_dir, run_id=run_id, edit=edit,
                framing_id=c.get("framing_id"), score=c.get("score"),
                reason=c.get("reason"), body=body,
            )
            drafts.append(draft)

        # Phase 4: enqueue approvals (best-effort)
        approval_ids = await self._enqueue_approvals(drafts, run_id, need)

        # Aggregate LLM timing across all hypotheses + ranker for display
        total_elapsed_ms = 0.0
        total_completion = 0
        total_prompt = 0
        for h in hypotheses:
            total_elapsed_ms += float(h.get("elapsed_ms") or 0)
            u = h.get("usage") or {}
            total_prompt += int(u.get("prompt_tokens") or 0)
            total_completion += int(u.get("completion_tokens") or 0)
        total_elapsed_ms += float(rank_resp.get("elapsed_ms") or 0)
        ru = rank_resp.get("usage") or {}
        total_prompt += int(ru.get("prompt_tokens") or 0)
        total_completion += int(ru.get("completion_tokens") or 0)
        overall_tps = (total_completion / (total_elapsed_ms / 1000.0)
                       if total_elapsed_ms > 0 and total_completion > 0 else None)

        # CC port: log the run outcome to per-agent MEMORY.md. Only success
        # path — early-return failures are already surfaced via `warnings`
        # / reply; adding them to MEMORY.md pollutes the useful signal.
        self._append_agent_memory_run(
            need=need, framings=framings,
            n_hyp_ok=sum(1 for h in hypotheses if h.get("ok")),
            n_drafts=len(drafts),
            run_id=run_id,
        )

        return {"ok": True, "run_id": run_id, "hypotheses": hypotheses,
                "chosen": chosen, "rejected_summary": rejected_summary,
                "drafts": drafts, "approval_ids": approval_ids,
                "warnings": warnings,
                "memory_root": str(memory_root_path),
                "n_sources": len(loaded),
                "need": need,
                **({"auto_target": auto_target} if auto_target else {}),
                "llm_stats": {
                    "elapsed_ms": round(total_elapsed_ms, 2),
                    "completion_tokens": total_completion,
                    "prompt_tokens": total_prompt,
                    "output_tps": round(overall_tps, 2) if overall_tps else None,
                    "n_calls": sum(1 for h in hypotheses if h.get("ok"))
                                + (1 if rank_resp.get("ok") else 0),
                }}

    # -- LLM steps -------------------------------------------------

    async def _explore(self, *, need: str, framing: dict,
                       sources_block: str, pool: str, model: str,
                       memory_index_block: str = "") -> dict:
        prompt = EXTRACTOR_SYSTEM.format(
            max_body=MAX_DRAFT_BYTES, lens=framing["lens"],
        )
        user_parts = [f"User need:\n{need}"]
        if memory_index_block:
            user_parts.append(
                "Existing memory (prefer `update` if a relevant entry "
                f"is already present):\n{memory_index_block}"
            )
        # CC port: per-agent MEMORY.md Observations — persistent notes that
        # survive across reflect runs (context is thrown away, MEMORY.md is
        # not). Inject before transcripts so the hypothesis can weight its
        # extraction by what the agent has learned before.
        if self._agent_memory_observations:
            user_parts.append(
                "Prior reflection observations (from this agent's own "
                f"persistent memory — weight your extraction accordingly, "
                f"but base claims on the transcripts, not these notes):\n"
                f"{self._agent_memory_observations}"
            )
        user_parts.append(f"Transcripts:\n{sources_block}")
        user_content = "\n\n".join(user_parts)
        try:
            resp = await self.ctx.bus.request("llm_driver", {
                "op": "run_turn",
                "system_prompt": prompt,
                "messages": [{"role": "user", "content": user_content}],
                "pool": pool, "model": model,
                "temperature": 0.5, "json_mode": True,
                "max_iterations": 1,
                "strip_thinking_blocks": True,
                "llm_timeout": 90.0,
            }, timeout=120.0)
        except Exception as e:
            return {"ok": False, "error": f"bus.request: {e}", "edits": []}
        if not resp.get("ok"):
            return {"ok": False, "error": resp.get("error"),
                    "raw": resp.get("content"), "edits": []}
        obj = _extract_json(resp.get("content") or "")
        if not isinstance(obj, dict) or "edits" not in obj:
            return {"ok": False, "error": "no edits[] in extractor JSON",
                    "raw": resp.get("content"), "edits": []}
        edits = obj.get("edits") or []
        if not isinstance(edits, list):
            return {"ok": False, "error": "edits is not a list",
                    "raw": resp.get("content"), "edits": []}
        # shape sanity
        cleaned: list[dict] = []
        for e in edits:
            if not isinstance(e, dict):
                continue
            if e.get("action") not in ("add", "update"):
                continue
            if not e.get("target") or not e.get("body"):
                continue
            cleaned.append(e)
        return {"ok": True, "edits": cleaned,
                "summary": obj.get("summary", ""),
                "usage": resp.get("usage"),
                "elapsed_ms": resp.get("elapsed_ms"),
                "output_tps": resp.get("output_tps")}

    async def _rank(self, *, need: str, hypotheses: list[dict],
                    pool: str, model: str, max_chosen: int) -> dict:
        prompt = RANKER_SYSTEM.format(max_chosen=max_chosen)
        # Compact view passed to the ranker — no huge bodies
        view = []
        for h in hypotheses:
            if not h.get("ok"):
                continue
            view.append({
                "framing_id": h["framing_id"],
                "summary": h.get("summary", ""),
                "edits": [{
                    "index": i, "action": e.get("action"),
                    "target": e.get("target"),
                    "title": e.get("title", ""),
                    "memory_type": e.get("memory_type", ""),
                    "rationale": e.get("rationale", ""),
                } for i, e in enumerate(h.get("edits") or [])],
            })
        user_msg = (f"Need:\n{need}\n\nHypotheses:\n"
                    f"{json.dumps(view, ensure_ascii=False, indent=2)}")
        try:
            resp = await self.ctx.bus.request("llm_driver", {
                "op": "run_turn",
                "system_prompt": prompt,
                "messages": [{"role": "user", "content": user_msg}],
                "pool": pool, "model": model,
                "temperature": 0.2, "json_mode": True,
                "max_iterations": 1, "strip_thinking_blocks": True,
                "llm_timeout": 60.0,
            }, timeout=90.0)
        except Exception as e:
            return {"ok": False, "error": f"bus.request: {e}"}
        if not resp.get("ok"):
            return {"ok": False, "error": resp.get("error")}
        obj = _extract_json(resp.get("content") or "")
        if not isinstance(obj, dict) or "chosen" not in obj:
            return {"ok": False, "error": "no chosen[] in ranker JSON"}
        chosen = obj.get("chosen") or []
        if not isinstance(chosen, list):
            return {"ok": False, "error": "chosen is not a list"}
        return {"ok": True, "chosen": chosen,
                "rejected_summary": obj.get("rejected_summary", ""),
                "elapsed_ms": resp.get("elapsed_ms"),
                "usage": resp.get("usage")}

    # -- staging & approval ----------------------------------------

    def _lookup_edit(self, hypotheses: list[dict],
                     framing_id: Any, edit_index: Any) -> Optional[dict]:
        for h in hypotheses:
            if h.get("framing_id") != framing_id:
                continue
            edits = h.get("edits") or []
            try:
                idx = int(edit_index)
            except (TypeError, ValueError):
                return None
            if 0 <= idx < len(edits):
                return edits[idx]
            return None
        return None

    def _stage_draft(self, *, drafts_dir: Path, run_id: str, edit: dict,
                     framing_id: str, score: Any, reason: str,
                     body: str) -> dict:
        ts = int(time.time())
        slug = _slugify(edit.get("target", "") or edit.get("title", ""))
        body_hash = _content_hash(body)[:8]
        # Include body hash so two drafts targeting the same file with
        # different content don't clobber each other at stage-time.
        fname = f"reflection_{ts}_{run_id}_{slug}_{body_hash}.md"
        dest = drafts_dir / fname
        meta = {
            "status": "draft",
            "proposed_action": edit.get("action"),
            "proposed_target": edit.get("target"),
            "proposed_title": edit.get("title", ""),
            "memory_type": edit.get("memory_type", ""),
            "reflection_run_id": run_id,
            "hypothesis_framing": framing_id,
            "score": score,
            "rationale": edit.get("rationale", ""),
            "ranker_reason": reason,
            "proposed_at": ts,
        }
        text = ("---\n"
                + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}"
                            for k, v in meta.items())
                + "\n---\n" + body + "\n")
        # Atomic temp + rename, Hermes-style
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, dest)
        return {"path": str(dest), "framing_id": framing_id,
                "action": edit.get("action"), "target": edit.get("target"),
                "title": edit.get("title", ""), "score": score}

    async def _enqueue_approvals(self, drafts: list[dict],
                                 run_id: str, need: str) -> list[str]:
        """Best-effort: try to register each draft with approval_queue.

        Failures (queue not running, op rejection) become warnings on the
        result object — drafts stay on disk regardless."""
        ids: list[str] = []
        for d in drafts:
            try:
                r = await self.ctx.bus.request("approval_queue", {
                    "op": "enqueue",
                    "action": "memory_edit",
                    "detail": {
                        "run_id": run_id, "need": need,
                        "draft_path": d["path"],
                        "proposed_target": d["target"],
                        "proposed_action": d["action"],
                        "title": d["title"],
                        "score": d["score"],
                    },
                    "requester": "reflection_agent",
                }, timeout=2.0)
            except Exception as e:
                log.info("reflection_agent: approval_queue not reachable (%s)", e)
                continue
            if isinstance(r, dict) and r.get("ok") and r.get("approval_id"):
                ids.append(r["approval_id"])
        return ids

    # -- bus surface ----------------------------------------------

    async def handle(self, msg) -> dict:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        op = payload.get("op", "reflect")
        if op != "reflect":
            return {"ok": False, "error": f"unknown op: {op!r}"}
        need = payload.get("need")
        auto = bool(payload.get("auto", False))
        if not auto and (not isinstance(need, str) or not need.strip()):
            return {"ok": False, "error": "missing or empty field: need"}
        return await self.reflect(
            need=need, auto=auto,
            sources=payload.get("sources"),
            memory_root=payload.get("memory_root"),
            n_hypotheses=int(payload.get("n_hypotheses", DEFAULT_HYPOTHESES)),
            pool=payload.get("pool"), model=payload.get("model"),
        )

    # -- reply formatting -----------------------------------------

    def _format_reply_parts(self, result: dict) -> tuple[str, list[tuple[str, str]]]:
        """Split the reply into (content_markdown, footer_meta)."""
        from yuxu.bundled.gateway.reply_helpers import format_llm_stats_footer

        # Usage hints are NOT failures; display the raw message.
        if result.get("stage") == "usage":
            return result.get("error") or "", []

        footer = format_llm_stats_footer(result.get("llm_stats"))

        if not result.get("ok"):
            stage = result.get("stage", "?")
            err = result.get("error", "(no error)")
            warns = result.get("warnings") or []
            warn_block = ("\n\n**Warnings:**\n" + "\n".join(f"- {w}" for w in warns)
                          if warns else "")
            content = f"❌ /reflect failed at `{stage}`: {err}{warn_block}"
            footer.insert(0, ("status", "failed"))
            return content, footer

        drafts = result.get("drafts") or []
        approvals = result.get("approval_ids") or []
        warns = result.get("warnings") or []
        lines = [f"✅ /reflect run `{result['run_id']}`"]
        if not drafts:
            lines.append("\nNo memory edits proposed.")
        else:
            lines.append(f"\n**{len(drafts)} draft(s) staged**:")
            for d in drafts:
                score = d.get("score")
                score_s = f" ({score:.2f})" if isinstance(score, (int, float)) else ""
                lines.append(f"- `{d['action']}` → `{d['target']}` "
                             f"— {d.get('title', '')}{score_s}")
        if warns:
            lines.append("\n**Warnings:**\n" + "\n".join(f"- {w}" for w in warns))

        footer.insert(0, ("sources", str(result.get("n_sources", 0))))
        footer.insert(1, ("drafts", str(len(drafts))))
        footer.insert(2, ("approvals",
                           f"{len(approvals)} queued" if approvals else "disk-only"))
        return "\n".join(lines), footer

    def _format_reply(self, result: dict) -> str:
        """String form — used by tests / fallback / console preview."""
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
            agent_name="reflection_agent",
        )
