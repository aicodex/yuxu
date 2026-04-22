"""`yuxu run <agent>` — ephemeral boot, run the named agent once, exit.

Mirrors `serve.py` but skips autostart of persistent agents — only the
target's transitive deps come up. Intended for one-off / triggered /
spawned agents (e.g., business research jobs kicked off from cron).
For long-running daemons use `yuxu serve`.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from ..core.main import boot
from .serve import _load_project_config, _setup_logging

log = logging.getLogger(__name__)


async def _run(project_dir: Path, agent_name: str) -> int:
    cfg = _load_project_config(project_dir)
    scan_order = cfg.get("scan_order", ["_system", "agents"])
    dirs = [str(project_dir / d) for d in scan_order]

    os.environ.setdefault(
        "RATE_LIMITS_CONFIG",
        str(project_dir / "config" / "rate_limits.yaml"),
    )
    os.environ.setdefault(
        "CHECKPOINT_ROOT",
        str(project_dir / "data" / "checkpoints"),
    )

    bus, loader = await boot(dirs=dirs, autostart_persistent=False)

    if agent_name not in loader.specs:
        log.error("unknown agent: %s. known: %s",
                  agent_name, sorted(loader.specs))
        return 1

    spec = loader.specs[agent_name]
    if spec.run_mode == "persistent":
        log.error("%s is run_mode=persistent; use `yuxu serve` instead.",
                  agent_name)
        return 1

    rc = 0
    try:
        status = await loader.ensure_running(agent_name)
        log.info("run %s: status=%s", agent_name, status)
    except Exception:
        log.exception("run %s failed", agent_name)
        rc = 1
    finally:
        for name in list(loader.tasks):
            try:
                await loader.stop(name)
            except Exception:
                log.exception("shutdown: stop(%s) failed", name)
    return rc


def run_one_shot(project_dir: Path, agent: str, log_level: str = "INFO") -> int:
    project_dir = project_dir.expanduser().resolve()
    _setup_logging(project_dir, log_level)
    try:
        return asyncio.run(_run(project_dir, agent))
    except KeyboardInterrupt:
        log.info("yuxu run: KeyboardInterrupt, exiting")
        return 130
