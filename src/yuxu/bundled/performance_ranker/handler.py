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

import asyncio
import datetime as _dt
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

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

# I6 staleness — entries whose `updated` date is older than this window
# auto-demote one evidence level. Applied at a periodic sweep, not on
# every access. `mandatory`-tagged entries are exempt (hard rules don't
# age). Override via `PERFORMANCE_RANKER_STALENESS_WINDOW_DAYS` /
# `PERFORMANCE_RANKER_SWEEP_INTERVAL_HOURS`.
DEFAULT_STALENESS_WINDOW_DAYS = 30
DEFAULT_SWEEP_INTERVAL_HOURS = 24.0

# Evidence levels, ordered highest → lowest. Demotion moves one slot
# toward the tail; `speculative` is the floor (no further demotion).
EVIDENCE_LEVELS = ("validated", "consensus", "observed", "speculative")

INDEX_SKIP_DIRS = {"_archive", "_drafts"}

MEMORY_RETRIEVED_TOPIC = "memory.retrieved"
MEMORY_DEMOTED_TOPIC = "memory.demoted"


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

    TODO(yuxu/memory-#5): `score.applied` is tracked but no downstream
      consumer uses it for search ranking. See `memory/handler._match_score`
      TODO — add applied-count weighting when the search upgrade happens.
    TODO(yuxu/memory-#6): no observed→validated promotion. Frequent
      retrieval is a signal the entry is useful; once we define a
      threshold (e.g. `applied >= 5 AND helped >= 2`), promote the
      evidence_level here (peer to `_demote_level`, `_promote_level`).
      Blocked on: (a) what counts as "helped" — needs iteration_agent or
      explicit user signal; (b) tournament-style promotion vs threshold —
      which is more useful in practice? Wait for real usage data before
      picking.
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


# -- staleness sweep helpers -----------------------------------


def _parse_date(value) -> Optional[_dt.date]:
    """Coerce a frontmatter `updated` value to a date. Accepts date,
    datetime, or YYYY-MM-DD str; returns None for anything else.
    """
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    if isinstance(value, str):
        try:
            return _dt.date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def _demote_level(level: Optional[str]) -> Optional[str]:
    """Return the next-lower evidence tier, or None if already at floor
    (`speculative`) or at an unknown level that we refuse to mutate."""
    if level not in EVIDENCE_LEVELS:
        return None
    idx = EVIDENCE_LEVELS.index(level)
    if idx >= len(EVIDENCE_LEVELS) - 1:
        return None
    return EVIDENCE_LEVELS[idx + 1]


def _iter_memory_entries(root: Path) -> Iterable[Path]:
    if not root.exists():
        return
    for p in sorted(root.rglob("*.md")):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(root).parts
        if any(part in INDEX_SKIP_DIRS for part in rel_parts):
            continue
        if p.name.startswith(".") or p.name.startswith("_"):
            continue
        yield p


def _demote_for_staleness(path: Path, *,
                            window_days: int,
                            today: _dt.date) -> Optional[dict]:
    """Demote `path` one evidence level if its `updated` field is older
    than `window_days`. Returns a report dict on demotion, else None.

    Exemptions: no frontmatter / no `updated` / mandatory-tagged /
    already at floor / unparseable date / age within window.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    fm, body = parse_frontmatter(text)
    if not isinstance(fm, dict) or not fm:
        return None
    tags = fm.get("tags") or []
    if isinstance(tags, list) and "mandatory" in tags:
        return None

    updated = _parse_date(fm.get("updated"))
    if updated is None:
        return None
    age = (today - updated).days
    if age < window_days:
        return None

    current_level = fm.get("evidence_level")
    new_level = _demote_level(current_level)
    if new_level is None:
        return None

    fm["evidence_level"] = new_level
    fm["updated"] = today.isoformat()
    head = dump_frontmatter(fm)
    tail = body if body.startswith("\n") else ("\n" + body)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(head + tail, encoding="utf-8")
    os.replace(tmp, path)
    return {
        "path": str(path),
        "from_level": current_level,
        "to_level": new_level,
        "age_days": age,
        "reason": "staleness",
    }


@dataclass
class _Event:
    ts: float
    kind: str  # "error" | "rejected"


class PerformanceRanker:
    def __init__(self, bus, *,
                 window_hours: Optional[float] = None,
                 weight_error: float = DEFAULT_WEIGHT_ERROR,
                 weight_rejected: float = DEFAULT_WEIGHT_REJECTED,
                 probation_clear_threshold: Optional[int] = None,
                 staleness_window_days: Optional[int] = None,
                 sweep_interval_hours: Optional[float] = None,
                 memory_root: Optional[str] = None) -> None:
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
        self.staleness_window_days = int(
            staleness_window_days if staleness_window_days is not None
            else os.environ.get("PERFORMANCE_RANKER_STALENESS_WINDOW_DAYS",
                                DEFAULT_STALENESS_WINDOW_DAYS)
        )
        self.sweep_interval_hours = float(
            sweep_interval_hours if sweep_interval_hours is not None
            else os.environ.get("PERFORMANCE_RANKER_SWEEP_INTERVAL_HOURS",
                                DEFAULT_SWEEP_INTERVAL_HOURS)
        )
        self._memory_root_override = memory_root
        self._events: dict[str, deque[_Event]] = {}
        self._known_memory_roots: set[Path] = set()
        self._sweep_task: Optional[asyncio.Task] = None

    # -- lifecycle -------------------------------------------------

    def install(self) -> None:
        self.bus.subscribe("*.error", self._on_error)
        self.bus.subscribe("approval_queue.rejected", self._on_rejection)
        self.bus.subscribe(MEMORY_RETRIEVED_TOPIC, self._on_memory_retrieved)
        if self._memory_root_override:
            try:
                self._known_memory_roots.add(
                    Path(self._memory_root_override).resolve())
            except OSError:
                pass
        if self.sweep_interval_hours > 0:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                self._sweep_task = loop.create_task(
                    self._sweep_staleness_loop(),
                    name="performance_ranker.sweep_staleness")

    def uninstall(self) -> None:
        self.bus.unsubscribe("*.error", self._on_error)
        self.bus.unsubscribe("approval_queue.rejected", self._on_rejection)
        self.bus.unsubscribe(MEMORY_RETRIEVED_TOPIC, self._on_memory_retrieved)
        if self._sweep_task is not None and not self._sweep_task.done():
            self._sweep_task.cancel()
        self._sweep_task = None

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
        # Remember the root so staleness sweep knows what to scan even
        # without walk-up / env config.
        self._known_memory_roots.add(root)
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

    # -- staleness sweep ------------------------------------------

    async def _sweep_staleness_loop(self) -> None:
        """Periodic background task. Sleeps the full interval before the
        first sweep — no catch-up burst on agent startup."""
        interval = max(60.0, self.sweep_interval_hours * 3600.0)
        try:
            while True:
                try:
                    await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    raise
                try:
                    await self.sweep_staleness_once()
                except Exception:
                    log.exception("performance_ranker: staleness sweep failed")
        except asyncio.CancelledError:
            return

    async def sweep_staleness_once(self, *,
                                     today: Optional[_dt.date] = None
                                     ) -> list[dict]:
        """Run one staleness pass over every known memory root. Returns
        the list of demotion reports (one per demoted entry). Publishes
        a `memory.demoted` event for each demotion.
        """
        if today is None:
            today = _dt.date.today()
        roots = list(self._known_memory_roots)
        if not roots:
            return []
        reports: list[dict] = []
        for root in roots:
            if not root.exists():
                continue
            for path in _iter_memory_entries(root):
                try:
                    report = _demote_for_staleness(
                        path,
                        window_days=self.staleness_window_days,
                        today=today,
                    )
                except Exception:
                    log.exception(
                        "performance_ranker: demote failed for %s", path)
                    continue
                if report is None:
                    continue
                try:
                    report["memory_root"] = str(root)
                    report["path"] = str(path.relative_to(root))
                except ValueError:
                    report["path"] = str(path)
                reports.append(report)
                try:
                    await self.bus.publish(MEMORY_DEMOTED_TOPIC, dict(report))
                except Exception:
                    log.exception(
                        "performance_ranker: publish memory.demoted raised")
        return reports

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
        if op == "sweep_staleness":
            # Manual trigger (tests / operators). Runs one pass
            # synchronously, returns the demotion report list.
            extra_root = payload.get("memory_root")
            if isinstance(extra_root, str) and extra_root.strip():
                try:
                    self._known_memory_roots.add(
                        Path(extra_root).expanduser().resolve())
                except OSError:
                    pass
            reports = await self.sweep_staleness_once()
            return {"ok": True, "demoted": reports}
        return {"ok": False, "error": f"unknown op: {op!r}"}
