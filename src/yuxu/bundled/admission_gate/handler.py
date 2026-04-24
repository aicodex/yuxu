"""admission_gate — write-admission quality check for memory entries (I6).

Three independent stages, combined by AND:
  1. surface_check  — LLM judges actionable-rule vs verbose-obvious
  2. golden_replay  — originSessionId citation must resolve to a real
                      archived session JSONL (existence-only, v0)
  3. noop_baseline  — duplicate of an existing entry? Char-trigram
                      Jaccard over name+description

Any stage returning `pass=false` blocks the write. `surface_check` is
tolerant of infrastructure gaps (no llm_driver loaded → pass with
`skipped` note) so the gate doesn't compound outages.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from yuxu.core.frontmatter import parse_frontmatter

log = logging.getLogger(__name__)

NAME = "admission_gate"

DEFAULT_DEDUP_THRESHOLD = 0.6
DEFAULT_NGRAM = 3

SURFACE_CHECK_SYSTEM = """You gate memory entries for an AI agent framework.
Decide whether the proposed entry is a real actionable observation, rule,
user fact, or reference — or whether it is verbose-obvious, opinion with
no supporting observation, mis-typed (type field doesn't match content),
or empty filler.

Pass if the entry is specific, tied to a concrete context, and carries
signal a future agent would actually use.
Fail otherwise.

Output strict JSON — no prose, no markdown fence:
{"pass": true|false, "reason": "<one short sentence>"}"""


SESSIONS_SUBDIR = Path("docs") / "experiences" / "sessions_raw"
INDEX_SKIP_DIRS = {"_archive", "_drafts"}


def _walk_up_for(root: Path, *, marker: str) -> Optional[Path]:
    """Walk parents from `root` looking for a dir/file named `marker`.

    Returns the parent that contains `marker`, not `marker` itself.
    """
    for cand in (root, *root.parents):
        if (cand / marker).exists():
            return cand
    return None


def _resolve_session_root(override: Optional[str],
                           memory_root: Optional[Path]) -> Optional[Path]:
    if override:
        p = Path(override).expanduser().resolve()
        return p if p.exists() else None
    if memory_root is None:
        return None
    project = _walk_up_for(memory_root.resolve(), marker="yuxu.json")
    if project is None:
        return None
    cand = project / SESSIONS_SUBDIR
    return cand if cand.exists() else None


# -- stage 1: surface_check ------------------------------------------


async def _surface_check(ctx, fm: dict, body: str,
                          pool: Optional[str],
                          model: Optional[str]) -> dict:
    name = fm.get("name") or ""
    typ = fm.get("type") or ""
    desc = fm.get("description") or ""
    user_content = (
        f"name: {name}\n"
        f"type: {typ}\n"
        f"description: {desc}\n\n"
        f"body:\n{body.strip()[:4000]}"
    )
    try:
        resp = await ctx.bus.request("llm_driver", {
            "op": "run_turn",
            "system_prompt": SURFACE_CHECK_SYSTEM,
            "messages": [{"role": "user", "content": user_content}],
            "pool": pool, "model": model,
            "temperature": 0.0, "json_mode": True,
            "max_iterations": 1,
            "strip_thinking_blocks": True,
            "llm_timeout": 60.0,
        }, timeout=90.0)
    except LookupError:
        return {"pass": True,
                "reason": "llm_driver not loaded — infra gap, passing through",
                "skipped": "llm_driver_not_loaded"}
    except Exception as e:
        return {"pass": True,
                "reason": f"llm_driver raised: {e}",
                "skipped": "llm_driver_raised"}
    if not isinstance(resp, dict) or not resp.get("ok"):
        err = resp.get("error") if isinstance(resp, dict) else "non-dict"
        return {"pass": True,
                "reason": f"llm_driver not ok: {err}",
                "skipped": "llm_driver_not_ok"}
    content = resp.get("content") or ""
    verdict = _extract_json(content)
    if not isinstance(verdict, dict) or "pass" not in verdict:
        return {"pass": True,
                "reason": "llm verdict unparseable — passing through",
                "skipped": "verdict_unparseable"}
    ok = bool(verdict.get("pass"))
    reason = str(verdict.get("reason") or "")[:400]
    return {"pass": ok, "reason": reason or ("ok" if ok else "rejected")}


_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")


def _extract_json(text: str) -> Optional[Any]:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_BLOCK.search(text)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


# -- stage 2: golden_replay ------------------------------------------


def _golden_replay(fm: dict, memory_root: Optional[Path],
                    session_root_override: Optional[str]) -> dict:
    session_id = fm.get("originSessionId")
    if not session_id:
        return {"pass": True, "reason": "no session cited"}
    if not isinstance(session_id, str) or not session_id.strip():
        return {"pass": False, "reason": "originSessionId set but not a string"}

    session_root = _resolve_session_root(session_root_override, memory_root)
    if session_root is None:
        return {"pass": False,
                "reason": f"session cited ({session_id}) but no session_root "
                          "resolvable"}
    prefix = session_id.split("-")[0][:8]
    if not prefix:
        return {"pass": False, "reason": "originSessionId has no usable prefix"}
    for p in session_root.glob("*.jsonl"):
        if prefix in p.name:
            return {"pass": True,
                    "reason": f"session archive found: {p.name}"}
    return {"pass": False,
            "reason": f"originSessionId {session_id} not found under "
                      f"{session_root}"}


# -- stage 3: noop_baseline ------------------------------------------


_WS_RE = re.compile(r"\s+")


def _trigrams(text: str, n: int = DEFAULT_NGRAM) -> set[str]:
    s = _WS_RE.sub("", (text or "").lower())
    if len(s) < n:
        return {s} if s else set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _iter_memory_entries(root: Path) -> list[tuple[Path, dict]]:
    out: list[tuple[Path, dict]] = []
    if not root.exists():
        return out
    for p in sorted(root.rglob("*.md")):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(root).parts
        if any(part in INDEX_SKIP_DIRS for part in rel_parts):
            continue
        if p.name.startswith(".") or p.name.startswith("_"):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, _ = parse_frontmatter(text)
        if not isinstance(fm, dict) or not fm.get("name"):
            continue
        out.append((p, fm))
    return out


def _resolve_target_path(target_path: Optional[str],
                          memory_root: Optional[Path]) -> Optional[Path]:
    if not target_path:
        return None
    p = Path(target_path).expanduser()
    if not p.is_absolute() and memory_root is not None:
        p = memory_root / p
    try:
        return p.resolve()
    except OSError:
        return None


def _noop_baseline(fm: dict, memory_root: Optional[Path],
                    target_path: Optional[Path],
                    dedup_threshold: float) -> dict:
    if memory_root is None:
        return {"pass": True, "reason": "no memory_root — skipped"}
    if not memory_root.exists():
        return {"pass": True, "reason": "memory_root does not exist — skipped"}
    new_name = (fm.get("name") or "").strip().lower()
    new_desc = (fm.get("description") or "").strip()
    new_sig = _trigrams((fm.get("name") or "") + " " + new_desc)

    for path, existing_fm in _iter_memory_entries(memory_root):
        if target_path is not None and path.resolve() == target_path:
            continue
        e_name = (existing_fm.get("name") or "").strip().lower()
        if new_name and e_name and new_name == e_name:
            return {"pass": False,
                    "reason": f"name collision with {path.name}",
                    "match_path": str(path)}
        e_sig = _trigrams((existing_fm.get("name") or "") + " "
                          + (existing_fm.get("description") or ""))
        sim = _jaccard(new_sig, e_sig)
        if sim >= dedup_threshold:
            return {"pass": False,
                    "reason": f"description Jaccard {sim:.2f} >= "
                              f"{dedup_threshold:.2f} vs {path.name}",
                    "match_path": str(path)}
    return {"pass": True, "reason": "no near-duplicate found"}


# -- orchestration ---------------------------------------------------


def _summarize(stages: dict, overall: bool) -> str:
    tags: list[str] = []
    for name, r in stages.items():
        short = "ok" if r.get("pass") else "fail"
        if r.get("skipped"):
            short = "skip"
        tags.append(f"{name}={short}")
    head = "PASS" if overall else "FAIL"
    return f"{head} [{', '.join(tags)}]"


async def _op_check(input: dict, ctx) -> dict:
    entry_body = input.get("entry_body")
    if not isinstance(entry_body, str) or not entry_body.strip():
        return {"ok": False, "error": "missing or empty field: entry_body"}
    memory_root_s = input.get("memory_root")
    memory_root: Optional[Path] = None
    if isinstance(memory_root_s, str) and memory_root_s.strip():
        try:
            memory_root = Path(memory_root_s).expanduser().resolve()
        except OSError:
            memory_root = None

    target_path = _resolve_target_path(input.get("target_path"), memory_root)

    try:
        threshold = float(input.get("dedup_threshold", DEFAULT_DEDUP_THRESHOLD))
    except (TypeError, ValueError):
        threshold = DEFAULT_DEDUP_THRESHOLD

    pool = input.get("pool") or os.environ.get("ADMISSION_GATE_POOL")
    model = input.get("model") or os.environ.get("ADMISSION_GATE_MODEL")

    fm, body = parse_frontmatter(entry_body)
    if not isinstance(fm, dict) or not fm:
        return {
            "ok": True,
            "pass": False,
            "stages": {
                "surface_check": {"pass": False,
                                   "reason": "entry has no frontmatter"},
                "golden_replay": {"pass": True,
                                   "reason": "skipped — no frontmatter"},
                "noop_baseline": {"pass": True,
                                   "reason": "skipped — no frontmatter"},
            },
            "verdict": "FAIL [no-frontmatter]",
        }

    # Run cheap CPU stages first. If either fails, skip surface_check — it's
    # the only LLM call in the gate (1-2s, real tokens). AND-semantic means
    # one failure fails the gate; no need to spend the LLM budget confirming.
    # Stages dict keeps the canonical insertion order (surface / golden /
    # noop) for consumers that iterate; the `skipped` field flags the
    # short-circuit so callers can distinguish "failed" from "not reached".
    golden = _golden_replay(fm, memory_root, input.get("session_root"))
    noop = _noop_baseline(fm, memory_root, target_path, threshold)
    cheap_failed = not golden.get("pass") or not noop.get("pass")
    if cheap_failed:
        surface = {
            "pass": False,
            "reason": "skipped — cheap stage already failed; LLM call short-circuited",
            "skipped": True,
        }
    else:
        surface = await _surface_check(ctx, fm, body, pool, model)

    stages: dict[str, Any] = {
        "surface_check": surface,
        "golden_replay": golden,
        "noop_baseline": noop,
    }
    overall = all(bool(s.get("pass")) for s in stages.values())
    return {
        "ok": True,
        "pass": overall,
        "stages": stages,
        "verdict": _summarize(stages, overall),
    }


async def execute(input: dict, ctx) -> dict:
    op = (input or {}).get("op")
    if op == "check":
        return await _op_check(input, ctx)
    return {"ok": False, "error": f"unknown op: {op!r}"}
