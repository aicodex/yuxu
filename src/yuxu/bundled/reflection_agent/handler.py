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

log = logging.getLogger(__name__)

COMMAND = "/reflect"
COMMAND_HELP = ("Iteratively explore past sessions for `<need>`; stage memory "
                "edit proposals you can approve. Usage: `/reflect <need>`.")

DEFAULT_HYPOTHESES = 3
MAX_SOURCE_BYTES = 8_192       # per source file, post-truncation
MAX_DRAFT_BYTES = 4_096        # per drafted memory entry body
MAX_TOTAL_SOURCE_BYTES = 64_000  # combined sources passed to one LLM call

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
- For `update`, `target` MUST be an existing memory file. For `add`, pick a
  new snake_case filename you have NOT seen referenced in the transcripts.
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

    async def install(self) -> None:
        self.ctx.bus.subscribe("gateway.command_invoked", self._on_command)
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

    # -- event handlers --------------------------------------------

    async def _on_command(self, event: dict) -> None:
        payload = event.get("payload") if isinstance(event, dict) else None
        if not isinstance(payload, dict) or payload.get("command") != COMMAND:
            return
        session_key = payload.get("session_key", "")
        need = (payload.get("args") or "").strip()
        if not need:
            await self._reply(session_key,
                              f"Usage: `{COMMAND} <need>`\n\n{COMMAND_HELP}")
            return
        result = await self.reflect(need=need)
        await self._reply(session_key, self._format_reply(result))

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

    async def reflect(self, *, need: str,
                      sources: Optional[list[str]] = None,
                      memory_root: Optional[Path | str] = None,
                      n_hypotheses: int = DEFAULT_HYPOTHESES,
                      pool: Optional[str] = None,
                      model: Optional[str] = None) -> dict:
        run_id = uuid.uuid4().hex[:8]
        warnings: list[str] = []
        memory_root_path, drafts_dir = self._resolve_paths(memory_root)
        loaded, load_warnings = _load_sources(sources, self._default_session_root())
        warnings.extend(load_warnings)
        if not loaded:
            return {"ok": False, "run_id": run_id, "stage": "load_sources",
                    "error": "no readable session sources",
                    "warnings": warnings}

        n = max(1, min(n_hypotheses, len(FRAMINGS)))
        framings = list(FRAMINGS[:n])

        pool = pool or os.environ.get("REFLECTION_POOL") \
            or os.environ.get("NEWSFEED_POOL") or "openai"
        model = model or os.environ.get("REFLECTION_MODEL") \
            or os.environ.get("TFE_MODEL") or "gpt-4o-mini"

        # Phase 1: parallel hypotheses
        sources_block = _format_sources(loaded)
        hyp_results = await asyncio.gather(*[
            self._explore(need=need, framing=fr, sources_block=sources_block,
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
                    "hypotheses": hypotheses, "warnings": warnings}

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

        return {"ok": True, "run_id": run_id, "hypotheses": hypotheses,
                "chosen": chosen, "rejected_summary": rejected_summary,
                "drafts": drafts, "approval_ids": approval_ids,
                "warnings": warnings,
                "memory_root": str(memory_root_path),
                "n_sources": len(loaded)}

    # -- LLM steps -------------------------------------------------

    async def _explore(self, *, need: str, framing: dict,
                       sources_block: str, pool: str, model: str) -> dict:
        prompt = EXTRACTOR_SYSTEM.format(
            max_body=MAX_DRAFT_BYTES, lens=framing["lens"],
        )
        try:
            resp = await self.ctx.bus.request("llm_driver", {
                "op": "run_turn",
                "system_prompt": prompt,
                "messages": [{"role": "user", "content":
                              f"User need:\n{need}\n\nTranscripts:\n{sources_block}"}],
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
                "usage": resp.get("usage")}

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
                "rejected_summary": obj.get("rejected_summary", "")}

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
        if not isinstance(need, str) or not need.strip():
            return {"ok": False, "error": "missing or empty field: need"}
        return await self.reflect(
            need=need,
            sources=payload.get("sources"),
            memory_root=payload.get("memory_root"),
            n_hypotheses=int(payload.get("n_hypotheses", DEFAULT_HYPOTHESES)),
            pool=payload.get("pool"), model=payload.get("model"),
        )

    # -- reply formatting -----------------------------------------

    def _format_reply(self, result: dict) -> str:
        if not result.get("ok"):
            stage = result.get("stage", "?")
            err = result.get("error", "(no error)")
            warns = result.get("warnings") or []
            warn_block = ("\n\nWarnings:\n" + "\n".join(f"- {w}" for w in warns)
                          if warns else "")
            return f"❌ /reflect failed at `{stage}`: {err}{warn_block}"
        drafts = result.get("drafts") or []
        approvals = result.get("approval_ids") or []
        warns = result.get("warnings") or []
        lines = [f"✅ /reflect run `{result['run_id']}` "
                 f"({result.get('n_sources', 0)} sources)"]
        if not drafts:
            lines.append("\nNo memory edits proposed.")
        else:
            lines.append(f"\n**{len(drafts)} draft(s) staged**:")
            for d in drafts:
                score = d.get("score")
                score_s = f" ({score:.2f})" if isinstance(score, (int, float)) else ""
                lines.append(f"- `{d['action']}` → `{d['target']}` "
                             f"— {d.get('title', '')}{score_s}")
                lines.append(f"  draft: `{d['path']}`")
        if approvals:
            lines.append(f"\n{len(approvals)} approval item(s) "
                         f"queued: {approvals}")
        else:
            lines.append("\n(approval_queue unreachable — drafts on disk only)")
        if warns:
            lines.append("\nWarnings:\n" + "\n".join(f"- {w}" for w in warns))
        return "\n".join(lines)

    async def _reply(self, session_key: str, text: str) -> None:
        if not session_key:
            return
        try:
            await self.ctx.bus.request("gateway", {
                "op": "send", "session_key": session_key, "text": text,
            }, timeout=5.0)
        except Exception:
            log.exception("reflection_agent: reply failed")
