"""PerformanceRanker — sliding-window per-agent "who's struggling" scorer.

Agent-ranking signals (v0.1):
  - `{agent}.error`            weight 1.0
  - `approval_queue.rejected`  weight 2.0 (attributed to payload.requester)

Memory-bookkeeping signal (Phase 4 minimum):
  - `memory.retrieved`         bumps per-entry `score.applied` in the memory
                                file's frontmatter; clears `probation: true`
                                once `applied` reaches the clear threshold.
                                `helped` / `hurt` are NOT tracked yet — those
                                land with iteration_agent, which produces
                                outcome signals to attribute.

Exposes `rank` / `score` / `reset` ops. Does not publish events — consumers
pull via bus.request when they need to decide e.g. which nice_to_have
reflection target to focus on.
"""
from __future__ import annotations

import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from yuxu.bundled._shared import dump_frontmatter
from yuxu.core.frontmatter import parse_frontmatter

log = logging.getLogger(__name__)

NAME = "performance_ranker"

DEFAULT_WINDOW_HOURS = 24.0
DEFAULT_WEIGHT_ERROR = 1.0
DEFAULT_WEIGHT_REJECTED = 2.0

# Number of `memory.retrieved` hits that clear a probation flag. Kept low —
# reflection_agent uses `mode=reflect` which is the only path that retrieves
# probation entries today, so each hit is signal that the updated content
# made it into a real exploration. Override via
# `PERFORMANCE_RANKER_PROBATION_CLEAR_THRESHOLD`.
DEFAULT_PROBATION_CLEAR_THRESHOLD = 3

MEMORY_RETRIEVED_TOPIC = "memory.retrieved"


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def _bump_applied(path: Path, probation_clear_threshold: int) -> None:
    """Increment `score.applied` in `path`'s frontmatter by 1.

    - Missing `score` → initialize `{applied: 1, helped: 0, hurt: 0,
      last_evaluated: today}`.
    - Non-int `applied` → treat as 0 before increment.
    - If the new `applied` meets `probation_clear_threshold` and
      `probation` is truthy → flip `probation` to False.
    - No frontmatter / parse error → skip silently (same policy as
      memory skill's indexer).
    - Write is atomic via tmp + os.replace.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    fm, body = parse_frontmatter(text)
    if not isinstance(fm, dict) or not fm:
        return

    score = fm.get("score")
    if not isinstance(score, dict):
        score = {"applied": 0, "helped": 0, "hurt": 0}
    applied_raw = score.get("applied", 0)
    try:
        applied = int(applied_raw)
    except (TypeError, ValueError):
        applied = 0
    score["applied"] = applied + 1
    # Preserve helped/hurt if present; otherwise default to 0 so readers
    # always see a well-formed score dict.
    score.setdefault("helped", 0)
    score.setdefault("hurt", 0)
    score["last_evaluated"] = _today()
    fm["score"] = score

    if fm.get("probation") and score["applied"] >= probation_clear_threshold:
        fm["probation"] = False

    head = dump_frontmatter(fm)
    tail = body if body.startswith("\n") else ("\n" + body)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(head + tail, encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class _Event:
    ts: float
    kind: str  # "error" | "rejected"


class PerformanceRanker:
    def __init__(self, bus, *,
                 window_hours: Optional[float] = None,
                 weight_error: float = DEFAULT_WEIGHT_ERROR,
                 weight_rejected: float = DEFAULT_WEIGHT_REJECTED,
                 probation_clear_threshold: Optional[int] = None) -> None:
        self.bus = bus
        self.window_hours = float(
            window_hours if window_hours is not None
            else os.environ.get("PERFORMANCE_RANKER_WINDOW_HOURS",
                                DEFAULT_WINDOW_HOURS)
        )
        self.weight_error = float(weight_error)
        self.weight_rejected = float(weight_rejected)
        self.probation_clear_threshold = int(
            probation_clear_threshold if probation_clear_threshold is not None
            else os.environ.get("PERFORMANCE_RANKER_PROBATION_CLEAR_THRESHOLD",
                                DEFAULT_PROBATION_CLEAR_THRESHOLD)
        )
        self._events: dict[str, deque[_Event]] = {}

    # -- lifecycle -------------------------------------------------

    def install(self) -> None:
        self.bus.subscribe("*.error", self._on_error)
        self.bus.subscribe("approval_queue.rejected", self._on_rejection)
        self.bus.subscribe(MEMORY_RETRIEVED_TOPIC, self._on_memory_retrieved)

    def uninstall(self) -> None:
        self.bus.unsubscribe("*.error", self._on_error)
        self.bus.unsubscribe("approval_queue.rejected", self._on_rejection)
        self.bus.unsubscribe(MEMORY_RETRIEVED_TOPIC, self._on_memory_retrieved)

    # -- helpers ---------------------------------------------------

    def _window_sec(self) -> float:
        return self.window_hours * 3600.0

    def _prune(self, dq: deque[_Event], now: float) -> None:
        cutoff = now - self._window_sec()
        while dq and dq[0].ts < cutoff:
            dq.popleft()

    def _record(self, agent: str, kind: str) -> None:
        if not agent or not isinstance(agent, str):
            return
        if agent.startswith("_"):
            # Underscore-prefixed (e.g. "_meta", test helpers) are bus infra,
            # not real agents — don't rank them.
            return
        now = time.monotonic()
        dq = self._events.setdefault(agent, deque())
        self._prune(dq, now)
        dq.append(_Event(ts=now, kind=kind))

    def _breakdown(self, agent: str) -> tuple[int, int]:
        dq = self._events.get(agent)
        if not dq:
            return (0, 0)
        now = time.monotonic()
        self._prune(dq, now)
        errors = sum(1 for e in dq if e.kind == "error")
        rejections = sum(1 for e in dq if e.kind == "rejected")
        return (errors, rejections)

    def _compute_score(self, errors: int, rejections: int) -> float:
        return errors * self.weight_error + rejections * self.weight_rejected

    # -- subscribers ----------------------------------------------

    async def _on_error(self, event: dict) -> None:
        topic = (event or {}).get("topic", "")
        # topic shape: "{agent}.error" (but skip the self-emitted topics we
        # publish when we grow events later, and skip resource_warning pings)
        if not topic.endswith(".error"):
            return
        if topic.endswith(".resource_warning"):
            return
        agent = topic[:-len(".error")]
        if not agent:
            return
        self._record(agent, "error")

    async def _on_rejection(self, event: dict) -> None:
        payload = (event or {}).get("payload") or {}
        if not isinstance(payload, dict):
            return
        requester = payload.get("requester")
        if not requester or not isinstance(requester, str):
            return
        self._record(requester, "rejected")

    async def _on_memory_retrieved(self, event: dict) -> None:
        """Bump `score.applied` on every retrieved memory entry; clear the
        probation flag once the counter reaches `probation_clear_threshold`.

        No helped/hurt here — those need outcome signals that only
        iteration_agent can attribute, and land with that agent.
        """
        payload = (event or {}).get("payload") or {}
        if not isinstance(payload, dict):
            return
        paths = payload.get("paths") or []
        memory_root = payload.get("memory_root")
        if not isinstance(paths, list) or not paths:
            return
        if not isinstance(memory_root, str) or not memory_root:
            return
        try:
            root = Path(memory_root).resolve()
        except OSError:
            return
        for rel in paths:
            if not isinstance(rel, str) or not rel:
                continue
            try:
                abs_path = (root / rel).resolve()
                abs_path.relative_to(root)  # reject `..` escapes
            except (OSError, ValueError):
                continue
            if not abs_path.is_file():
                continue
            try:
                _bump_applied(abs_path, self.probation_clear_threshold)
            except Exception:
                log.exception("performance_ranker: score bump failed for %s",
                              abs_path)

    # -- bus surface ----------------------------------------------

    async def handle(self, msg) -> dict:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        op = payload.get("op", "rank")
        if op == "rank":
            limit = payload.get("limit")
            min_score = float(payload.get("min_score", 0.0))
            rows: list[dict] = []
            for agent in self._events:
                errs, rejs = self._breakdown(agent)
                s = self._compute_score(errs, rejs)
                if s <= 0 or s < min_score:
                    continue
                rows.append({"agent": agent, "score": s,
                             "errors": errs, "rejections": rejs})
            rows.sort(key=lambda r: (-r["score"], r["agent"]))
            if isinstance(limit, int) and limit > 0:
                rows = rows[:limit]
            return {
                "ok": True,
                "window_hours": self.window_hours,
                "ranked": rows,
            }
        if op == "score":
            agent = payload.get("agent")
            if not agent or not isinstance(agent, str):
                return {"ok": False, "error": "missing field: agent"}
            errs, rejs = self._breakdown(agent)
            return {
                "ok": True,
                "agent": agent,
                "window_hours": self.window_hours,
                "score": self._compute_score(errs, rejs),
                "errors": errs,
                "rejections": rejs,
            }
        if op == "reset":
            target = payload.get("agent")
            if target:
                dq = self._events.pop(target, None)
                return {"ok": True, "cleared": len(dq) if dq else 0}
            total = sum(len(dq) for dq in self._events.values())
            self._events.clear()
            return {"ok": True, "cleared": total}
        return {"ok": False, "error": f"unknown op: {op!r}"}
