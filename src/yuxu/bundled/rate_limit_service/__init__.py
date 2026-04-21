"""rate_limit_service bundled agent.

Downstream agents acquire slots via `ctx.get_agent("rate_limit_service").acquire(pool)`
— this is a Python async context manager, not a bus call (for efficiency).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

from .handler import RateLimitService

NAME = "rate_limit_service"
DEFAULT_CONFIG = "config/rate_limits.yaml"

log = logging.getLogger(__name__)

_service: RateLimitService | None = None


def _load_config() -> dict:
    path = Path(os.environ.get("RATE_LIMITS_CONFIG") or DEFAULT_CONFIG)
    if not path.exists():
        log.info("rate_limit_service: no config at %s, starting with empty pools", path)
        return {}
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        log.exception("rate_limit_service: bad YAML at %s", path)
        return {}
    if not isinstance(cfg, dict):
        log.warning("rate_limit_service: config root must be a mapping")
        return {}
    return cfg


async def start(ctx) -> None:
    global _service
    _service = RateLimitService(_load_config())
    ctx.bus.register(NAME, _service.handle)
    await ctx.ready()


def get_handle(ctx):
    return _service
