"""scheduler bundled agent."""
from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

from .handler import NAME, Scheduler

DEFAULT_CONFIG = "config/schedules.yaml"

log = logging.getLogger(__name__)

_scheduler: Scheduler | None = None


def _load_schedules() -> list[dict]:
    path = Path(os.environ.get("SCHEDULES_CONFIG") or DEFAULT_CONFIG)
    if not path.exists():
        log.info("scheduler: no config at %s, starting with no schedules", path)
        return []
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        log.exception("scheduler: bad YAML at %s", path)
        return []
    if isinstance(cfg, dict):
        cfg = cfg.get("schedules") or []
    if not isinstance(cfg, list):
        log.warning("scheduler: config root must be a list "
                    "or {schedules: [...]}")
        return []
    return cfg


async def start(ctx) -> None:
    global _scheduler
    # `SCHEDULER_RESERVATION_CHECK=1` turns on reservation gating before fire.
    # Default off so existing setups keep current behavior; turn on when
    # MINIMAX_RESERVATIONS is also configured.
    reservation_check = os.environ.get(
        "SCHEDULER_RESERVATION_CHECK", "0"
    ).lower() in ("1", "true", "yes", "on")
    _scheduler = Scheduler(ctx.bus, _load_schedules(),
                           reservation_check=reservation_check)
    ctx.bus.register(NAME, _scheduler.handle)
    await _scheduler.start_all()
    await ctx.ready()


async def stop(ctx) -> None:
    global _scheduler
    if _scheduler is not None:
        await _scheduler.stop_all()


def get_handle(ctx):
    return _scheduler
