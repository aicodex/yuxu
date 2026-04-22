"""RuntimeMonitor — register this serve + scavenge stale entries.

Keeps `~/.yuxu/runtime/<slug>.json` in sync with the OS pid table so
`yuxu ps` (and any future observability) can trust it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

DEFAULT_PRUNE_INTERVAL_SEC = 30.0


def _home_dir() -> Path:
    override = os.environ.get("YUXU_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".yuxu"


def _runtime_dir() -> Path:
    return _home_dir() / "runtime"


def _slug_from_project(project_dir: Path) -> str:
    """Derive a stable filename slug from the project dir path.

    Uses the folder name plus a short hash suffix so two projects named
    "myproj" in different locations don't collide.
    """
    import hashlib
    name = project_dir.name or "project"
    name = re.sub(r"[^a-zA-Z0-9._-]+", "_", name)[:40] or "project"
    h = hashlib.sha256(str(project_dir.resolve()).encode()).hexdigest()[:8]
    return f"{name}-{h}"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _find_project_root(start: Path) -> Optional[Path]:
    """Walk up from `start` until we find a directory containing yuxu.json."""
    for cand in (start, *start.parents):
        if (cand / "yuxu.json").exists():
            return cand
    return None


def _infer_adapters(project_dir: Path) -> list[str]:
    """Best-effort: report which gateway adapters this serve might run.

    We peek at env + config/secrets/*.yaml to infer — no bus queries
    during startup (gateway might not be ready when we register).
    """
    adapters = ["console"]
    if (project_dir / "config" / "secrets" / "telegram.yaml").exists() or \
            os.environ.get("TELEGRAM_BOT_TOKEN"):
        adapters.append("telegram")
    if (project_dir / "config" / "secrets" / "feishu.yaml").exists() or \
            (os.environ.get("FEISHU_APP_ID") and os.environ.get("FEISHU_APP_SECRET")):
        adapters.append("feishu")
    return adapters


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    os.replace(tmp, path)


class RuntimeMonitor:
    def __init__(self, ctx, *,
                 prune_interval: float = DEFAULT_PRUNE_INTERVAL_SEC) -> None:
        self.ctx = ctx
        self.prune_interval = prune_interval
        self._my_file: Optional[Path] = None
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

    # -- lifecycle ------------------------------------------------

    async def install(self) -> None:
        self._register_self()
        self._task = asyncio.create_task(self._prune_loop(),
                                          name="runtime_monitor.prune")

    async def uninstall(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._my_file and self._my_file.exists():
            try:
                self._my_file.unlink()
            except OSError:
                pass

    # -- registration ---------------------------------------------

    def _my_entry(self) -> dict:
        project_dir = _find_project_root(Path(self.ctx.agent_dir).resolve())
        if project_dir is None:
            project_dir = Path.cwd().resolve()
        try:
            from .. import __version__ as _  # type: ignore[attr-defined]
            ver = _
        except Exception:
            try:
                from yuxu import __version__ as ver
            except Exception:
                ver = "unknown"
        return {
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "project_dir": str(project_dir),
            "yuxu_version": str(ver),
            "adapters": _infer_adapters(project_dir),
        }

    def _register_self(self) -> None:
        entry = self._my_entry()
        project_dir = Path(entry["project_dir"])
        slug = _slug_from_project(project_dir)
        self._my_file = _runtime_dir() / f"{slug}.json"
        try:
            _atomic_write_json(self._my_file, entry)
            log.info("runtime_monitor: registered %s (pid=%d)",
                     self._my_file, entry["pid"])
        except Exception:
            log.exception("runtime_monitor: could not write %s", self._my_file)

    async def _prune_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(self.prune_interval)
                if self._stopping:
                    return
                self.prune_stale()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("runtime_monitor: prune loop iteration failed")

    # -- queries --------------------------------------------------

    def list_entries(self, *, include_stale: bool = False) -> list[dict]:
        out: list[dict] = []
        rd = _runtime_dir()
        if not rd.exists():
            return out
        for p in sorted(rd.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            pid = data.get("pid")
            alive = isinstance(pid, int) and _pid_alive(pid)
            if not alive and not include_stale:
                continue
            out.append({**data, "alive": alive})
        return out

    def prune_stale(self) -> int:
        """Remove registry entries whose pid is not alive. Returns count removed."""
        rd = _runtime_dir()
        if not rd.exists():
            return 0
        removed = 0
        for p in sorted(rd.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            pid = data.get("pid")
            if isinstance(pid, int) and _pid_alive(pid):
                continue
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
        if removed:
            log.info("runtime_monitor: pruned %d stale entries", removed)
        return removed

    # -- bus surface ----------------------------------------------

    async def handle(self, msg) -> dict:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        op = payload.get("op", "list")
        if op == "list":
            return {"ok": True,
                    "entries": self.list_entries(
                        include_stale=bool(payload.get("include_stale", False))
                    )}
        if op == "self":
            if self._my_file is None or not self._my_file.exists():
                return {"ok": False, "error": "self not registered"}
            try:
                data = json.loads(self._my_file.read_text(encoding="utf-8"))
            except Exception as e:
                return {"ok": False, "error": f"read self: {e}"}
            return {"ok": True, "entry": data, "file": str(self._my_file)}
        if op == "prune":
            return {"ok": True, "removed": self.prune_stale()}
        return {"ok": False, "error": f"unknown op: {op!r}"}
