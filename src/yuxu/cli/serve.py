"""`yuxu serve` — run the framework from a project directory.

Reads `yuxu.json`, composes the Loader's scan dirs per `scan_order`,
boots all persistent agents, and runs forever.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from ..core.main import boot


def _load_project_config(project_dir: Path) -> dict:
    cfg_path = project_dir / "yuxu.json"
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"{cfg_path} not found. Run `yuxu init {project_dir}` first."
        )
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{cfg_path} is not valid JSON: {e}") from e


def _setup_logging(project_dir: Path, level: str) -> None:
    log_dir = project_dir / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_dir / "yuxu.log", encoding="utf-8"),
        ],
    )


async def _serve(project_dir: Path, extra_agents: list[str],
                 dev_mode: bool = False) -> None:
    cfg = _load_project_config(project_dir)
    scan_order = cfg.get("scan_order", ["_system", "agents"])
    if dev_mode:
        # Dev mode: substitute the installed bundled path for `_system` so
        # edits to src/yuxu/bundled/... take effect on restart without a
        # `yuxu sync`. Project-side dirs (`agents/`, `skills/`) unchanged.
        from pathlib import Path as _P
        import yuxu.bundled as _b
        bundled_root = _P(_b.__file__).parent
        dirs = [
            str(bundled_root) if d == "_system" else str(project_dir / d)
            for d in scan_order
        ]
        logging.getLogger(__name__).warning(
            "yuxu serve: DEV MODE — loading bundled agents from installed "
            "package %s (bypasses project _system/)", bundled_root,
        )
    else:
        dirs = [str(project_dir / d) for d in scan_order]

    # Defaults so bundled agents find their configs.
    # Users can override via their own env.
    os.environ.setdefault(
        "RATE_LIMITS_CONFIG",
        str(project_dir / "config" / "rate_limits.yaml"),
    )
    os.environ.setdefault(
        "CHECKPOINT_ROOT",
        str(project_dir / "data" / "checkpoints"),
    )

    bus, loader = await boot(
        dirs=dirs,
        extra_agents=extra_agents or None,
        autostart_persistent=True,
    )
    logging.getLogger(__name__).info(
        "yuxu serve: %d agents registered; %d persistent started",
        len(loader.specs),
        sum(1 for s in loader.specs.values() if s.run_mode == "persistent"),
    )
    await bus.run_forever()


def run_serve(project_dir: Path, extra_agents: list[str] | None = None,
              log_level: str = "INFO", dev_mode: bool = False) -> None:
    project_dir = project_dir.expanduser().resolve()
    _setup_logging(project_dir, log_level)
    try:
        asyncio.run(_serve(project_dir, extra_agents or [], dev_mode=dev_mode))
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("yuxu serve: KeyboardInterrupt, exiting")
