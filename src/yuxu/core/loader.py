"""Agent folder loader and lifecycle orchestrator.

Scans agent dirs, parses AGENT.md frontmatter, builds dep graph with cycle detection,
provides `ensure_running` as the single start entrypoint.

Agent folder convention:
  {dir}/AGENT.md   - frontmatter + prompt/description (both optional)
  {dir}/__init__.py - Python entry; may expose:
       async def start(ctx) -> None          # called on load (required if file exists)
       async def stop(ctx) -> None           # called on graceful shutdown (optional)
       def get_handle(ctx) -> Any            # what other agents can grab via ctx.get_agent (optional)

Start convention:
  1) register bus handlers / subscribe topics / launch background tasks
  2) call await ctx.ready()
  3) return (persistent agents: spawn long-lived tasks before returning)

If `__init__.py` exists but exposes no start(), Loader still imports it and
marks the agent ready (empty __init__.py is allowed). If no __init__.py at
all, the agent is LLM-only and also auto-marked ready.
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import session_log
from .bus import Bus
from .context import AgentContext
from .frontmatter import parse_frontmatter

log = logging.getLogger(__name__)

VALID_DRIVERS = {"llm", "python", "hybrid"}
VALID_RUN_MODES = {"persistent", "scheduled", "triggered", "one_shot", "spawned"}
VALID_KINDS = {"agent", "skill"}
# Port of Claude Code 2.1.88 `tools/AgentTool/agentMemory.ts:12-13` scope enum.
# yuxu adapts CC paths to its own layout (project → `<project>/data/agent-memory/`;
# local → `<project>/.yuxu/local/agent-memory/` gitignored by convention; user →
# `~/.yuxu/agent-memory/`). An agent whose AGENT.md omits `memory:` gets None.
VALID_MEMORY_SCOPES = {"user", "project", "local"}


def resolve_agent_memory_path(
    scope: Optional[str],
    agent_name: str,
    project_root: Optional[Path],
) -> Optional[Path]:
    """Per-scope MEMORY.md path for an agent (CC AgentTool `memory:` port).

    Returns None when the scope is invalid, or when a project-scoped / local-
    scoped agent is loaded outside a yuxu project (no `yuxu.json` ancestor).
    Does NOT create the file — caller (Loader._start) does that.
    """
    if scope not in VALID_MEMORY_SCOPES:
        return None
    if scope == "user":
        return Path.home() / ".yuxu" / "agent-memory" / agent_name / "MEMORY.md"
    if project_root is None:
        return None
    if scope == "project":
        return project_root / "data" / "agent-memory" / agent_name / "MEMORY.md"
    if scope == "local":
        return project_root / ".yuxu" / "local" / "agent-memory" / agent_name / "MEMORY.md"
    return None


_AGENT_MEMORY_SEED = """---
agent: {name}
scope: {scope}
---
# {name} — agent memory

Persistent notes this agent keeps across sessions. The handler owns
read/write semantics; see `reference_cc_agent_protocol.md` for the
port of Claude Code's convention.
"""


@dataclass
class AgentSpec:
    name: str
    path: Path
    frontmatter: dict
    body: str = ""
    kind: str = "agent"
    driver: str = "python"
    run_mode: str = "one_shot"
    depends_on: list[str] = field(default_factory=list)
    optional_deps: list[str] = field(default_factory=list)
    scope: str = "user"
    handler_path: Optional[str] = None
    entry: str = "start"
    ready_timeout: float = 30.0
    edit_warning: bool = False
    has_init: bool = False
    has_agent_md: bool = False
    has_skill_md: bool = False
    has_handler: bool = False
    # CC port: agent-scoped persistent MEMORY.md. Value = "user" | "project" |
    # "local" when AGENT.md declares it, else None.
    memory_scope: Optional[str] = None


class Loader:
    def __init__(self, bus: Bus, dirs: list[str]) -> None:
        self.bus = bus
        self.dirs = [Path(d) for d in dirs]
        self.specs: dict[str, AgentSpec] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.modules: dict[str, object] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # -- scan --------------------------------------------------------

    async def scan(self) -> None:
        self.specs.clear()
        for d in self.dirs:
            if not d.exists():
                continue
            for agent_dir in sorted(d.iterdir()):
                if not agent_dir.is_dir() or agent_dir.name.startswith((".", "_")):
                    continue
                spec = self._load_spec(agent_dir)
                if spec is None:
                    continue
                if spec.name in self.specs:
                    log.warning("loader: %s at %s overrides %s",
                                spec.name, spec.path, self.specs[spec.name].path)
                self.specs[spec.name] = spec

    def _load_spec(self, agent_dir: Path) -> Optional[AgentSpec]:
        init = agent_dir / "__init__.py"
        agent_md = agent_dir / "AGENT.md"
        skill_md = agent_dir / "SKILL.md"
        default_handler = agent_dir / "handler.py"

        # Kind classification by folder shape (Python semantics):
        #   __init__.py present  → agent (has a lifecycle to run)
        #   no __init__.py, has handler.py (or SKILL.md) → skill (passive)
        #   no __init__.py, only AGENT.md            → LLM-only agent
        if init.exists():
            kind = "agent"
        elif default_handler.exists() or skill_md.exists():
            kind = "skill"
        elif agent_md.exists():
            kind = "agent"  # LLM-only agent
        else:
            return None

        # Pick metadata file: skills prefer SKILL.md, agents use AGENT.md.
        if kind == "skill":
            md_file = skill_md if skill_md.exists() else (agent_md if agent_md.exists() else None)
        else:
            md_file = agent_md if agent_md.exists() else None

        fm: dict = {}
        body = ""
        if md_file is not None:
            fm, body = parse_frontmatter(md_file.read_text(encoding="utf-8"))

        # Resolve handler filename (OpenClaw/CC frontmatter `handler:` override)
        handler_filename = fm.get("handler") or "handler.py"
        handler_file = agent_dir / handler_filename
        has_handler = handler_file.exists()

        driver = fm.get("driver") or ("python" if kind == "agent" and init.exists() else "llm")
        if driver not in VALID_DRIVERS:
            log.warning("loader: %s has invalid driver=%s, defaulting to python",
                        agent_dir.name, driver)
            driver = "python"

        # run_mode default: skills are reactive by nature
        run_mode_default = "triggered" if kind == "skill" else "one_shot"
        run_mode = fm.get("run_mode", run_mode_default)
        if run_mode not in VALID_RUN_MODES:
            log.warning("loader: %s has invalid run_mode=%s, defaulting to %s",
                        agent_dir.name, run_mode, run_mode_default)
            run_mode = run_mode_default

        entry_default = "execute" if kind == "skill" else "start"

        # CC port: AGENT.md `memory:` field declares per-agent persistent
        # MEMORY.md scope. Unknown / missing values are silently dropped to
        # None (Loader logs a warning; downstream treats None = no agent
        # memory). Only `agent` kind honours this — skills are stateless.
        memory_scope: Optional[str] = None
        raw_memory = fm.get("memory")
        if raw_memory is not None:
            if kind == "skill":
                log.warning("loader: %s has memory=%r but is a skill; ignoring "
                             "(skills are stateless)", agent_dir.name, raw_memory)
            elif isinstance(raw_memory, str) and raw_memory in VALID_MEMORY_SCOPES:
                memory_scope = raw_memory
            else:
                log.warning("loader: %s has invalid memory=%r (expected one of "
                             "%s); treating as None", agent_dir.name, raw_memory,
                             sorted(VALID_MEMORY_SCOPES))

        return AgentSpec(
            name=agent_dir.name,
            path=agent_dir,
            frontmatter=fm,
            body=body,
            kind=kind,
            driver=driver,
            run_mode=run_mode,
            depends_on=list(fm.get("depends_on") or []),
            optional_deps=list(fm.get("optional_deps") or []),
            scope=fm.get("scope", "user"),
            handler_path=handler_filename,
            entry=fm.get("entry", entry_default),
            ready_timeout=float(fm.get("ready_timeout", 30.0)),
            edit_warning=bool(fm.get("edit_warning", False)),
            has_init=init.exists(),
            has_agent_md=agent_md.exists(),
            has_skill_md=skill_md.exists(),
            has_handler=has_handler,
            memory_scope=memory_scope,
        )

    # -- dep graph ---------------------------------------------------

    def build_dep_graph(self) -> list[str]:
        """Return topological order. Raises RuntimeError on cycle or unknown hard dep."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n: WHITE for n in self.specs}
        order: list[str] = []
        stack: list[str] = []

        def dfs(n: str) -> None:
            if color.get(n) == GRAY:
                cycle = " -> ".join(stack[stack.index(n):] + [n])
                raise RuntimeError(f"circular dependency: {cycle}")
            if color.get(n) == BLACK:
                return
            color[n] = GRAY
            stack.append(n)
            for d in self.specs[n].depends_on:
                if d not in self.specs:
                    raise RuntimeError(f"{n} depends on unknown agent: {d}")
                dfs(d)
            stack.pop()
            color[n] = BLACK
            order.append(n)

        for n in list(self.specs):
            dfs(n)
        return order

    def get_dep_graph(self) -> dict[str, list[str]]:
        return {n: list(s.depends_on) for n, s in self.specs.items()}

    def filter(self, run_mode: Optional[str] = None, scope: Optional[str] = None,
               kind: Optional[str] = None, surface: Optional[str] = None) -> list[AgentSpec]:
        out = list(self.specs.values())
        if run_mode is not None:
            out = [s for s in out if s.run_mode == run_mode]
        if scope is not None:
            out = [s for s in out if s.scope == scope]
        if kind is not None:
            out = [s for s in out if s.kind == kind]
        if surface is not None:
            out = [s for s in out if surface in (s.frontmatter.get("surface") or [])]
        return out

    # -- lifecycle ---------------------------------------------------

    async def ensure_running(self, name: str) -> str:
        if name not in self.specs:
            raise KeyError(f"unknown agent: {name}")
        lock = self._locks.setdefault(name, asyncio.Lock())
        async with lock:
            status = self.bus.query_status(name)
            if status in ("ready", "running"):
                return status
            spec = self.specs[name]
            if spec.depends_on:
                await asyncio.gather(*[self.ensure_running(d) for d in spec.depends_on])
            await self._start(spec)
            return self.bus.query_status(name)

    async def _start(self, spec: AgentSpec) -> None:
        await self.bus.publish_status(spec.name, "loading")
        try:
            if spec.kind == "skill":
                await self._start_skill(spec)
                return

            if not spec.has_init:
                # LLM-only agent: kernel just marks it ready; llm_driver handles it.
                await self.bus.publish_status(spec.name, "ready")
                await self._write_lifecycle(spec, "ready")
                return

            mod = self._import_agent_module(spec)
            self.modules[spec.name] = mod
            entry_fn = getattr(mod, spec.entry, None)
            if entry_fn is None:
                await self.bus.publish_status(spec.name, "ready")
                await self._write_lifecycle(spec, "ready")
                return

            ctx = self._build_context(spec)
            result = entry_fn(ctx)
            if asyncio.iscoroutine(result):
                if spec.run_mode == "persistent":
                    task = asyncio.create_task(result, name=f"agent:{spec.name}")
                    self.tasks[spec.name] = task
                    task.add_done_callback(lambda t, n=spec.name: self._on_task_done(n, t))
                    await self.bus.wait_for_service(spec.name, timeout=spec.ready_timeout)
                    await self._write_lifecycle(spec, "ready")
                else:
                    # one_shot / scheduled / triggered / spawned: awaiting start()
                    # until it returns IS the "session". Emit session.ended after.
                    await asyncio.wait_for(result, timeout=spec.ready_timeout)
                    if self.bus.query_status(spec.name) not in ("ready", "running", "stopped"):
                        await self.bus.publish_status(spec.name, "ready")
                    await self._emit_session_end(spec, state="completed")
            else:
                if self.bus.query_status(spec.name) not in ("ready", "running"):
                    await self.bus.publish_status(spec.name, "ready")
                await self._write_lifecycle(spec, "ready")
        except asyncio.TimeoutError:
            log.error("loader: %s ready_timeout after %.1fs", spec.name, spec.ready_timeout)
            await self.bus.publish_status(spec.name, "failed")
            await self._emit_session_end(spec, state="failed", reason="ready_timeout")
            raise
        except Exception as e:
            log.exception("loader: %s failed to start", spec.name)
            await self.bus.publish_status(spec.name, "failed")
            await self._emit_session_end(spec, state="failed", reason=f"start_error: {e}")
            raise

    def _build_context(self, spec: AgentSpec) -> AgentContext:
        agent_memory_path: Optional[Path] = None
        if spec.memory_scope is not None:
            project_root = session_log.find_project_root(spec.path)
            agent_memory_path = resolve_agent_memory_path(
                spec.memory_scope, spec.name, project_root,
            )
            if agent_memory_path is not None:
                # Create file with seed content on first lookup. Lifecycle start
                # is the natural moment — the handler can read it immediately.
                try:
                    agent_memory_path.parent.mkdir(parents=True, exist_ok=True)
                    if not agent_memory_path.exists():
                        agent_memory_path.write_text(
                            _AGENT_MEMORY_SEED.format(
                                name=spec.name, scope=spec.memory_scope,
                            ),
                            encoding="utf-8",
                        )
                except OSError as e:
                    log.warning("loader: could not init agent_memory_path %s: %s",
                                 agent_memory_path, e)
                    agent_memory_path = None
            elif spec.memory_scope in ("project", "local"):
                log.info("loader: %s declares memory=%s but no yuxu.json found "
                          "above %s — agent_memory_path will be None",
                          spec.name, spec.memory_scope, spec.path)
        return AgentContext(
            name=spec.name,
            agent_dir=spec.path,
            frontmatter=dict(spec.frontmatter),
            body=spec.body,
            bus=self.bus,
            loader=self,
            logger=logging.getLogger(f"agent.{spec.name}"),
            agent_memory_path=agent_memory_path,
        )

    def get_handle(self, name: str) -> Any:
        """Return the named agent's `get_handle(ctx)` result, or None."""
        mod = self.modules.get(name)
        if mod is None:
            return None
        fn = getattr(mod, "get_handle", None)
        if fn is None:
            return None
        spec = self.specs.get(name)
        if spec is None:
            return None
        try:
            return fn(self._build_context(spec))
        except Exception:
            log.exception("loader: get_handle(%s) raised", name)
            return None

    def _import_agent_module(self, spec: AgentSpec):
        mod_name = f"_agents.{spec.name}"
        init_path = spec.path / "__init__.py"
        s = importlib.util.spec_from_file_location(
            mod_name, init_path, submodule_search_locations=[str(spec.path)]
        )
        if s is None or s.loader is None:
            raise ImportError(f"cannot import {init_path}")
        mod = importlib.util.module_from_spec(s)
        sys.modules[mod_name] = mod
        s.loader.exec_module(mod)
        return mod

    def _import_skill_module(self, spec: AgentSpec, handler_file: Path):
        mod_name = f"_skills.{spec.name}"
        s = importlib.util.spec_from_file_location(
            mod_name, handler_file, submodule_search_locations=[str(spec.path)]
        )
        if s is None or s.loader is None:
            raise ImportError(f"cannot import {handler_file}")
        mod = importlib.util.module_from_spec(s)
        sys.modules[mod_name] = mod
        s.loader.exec_module(mod)
        return mod

    async def _start_skill(self, spec: AgentSpec) -> None:
        """Register a skill's handler on the bus; no task spawned.

        Skill is passive: it has no lifecycle, just a function that runs when
        called via bus.request(spec.name, payload). We lazy-import the handler
        module and register a bus handler that wraps execute(input, ctx).
        """
        handler_file = spec.path / (spec.handler_path or "handler.py")
        if not handler_file.exists():
            raise FileNotFoundError(
                f"skill {spec.name}: handler file {handler_file.name} not found"
            )
        mod = self._import_skill_module(spec, handler_file)
        self.modules[spec.name] = mod
        execute_fn = getattr(mod, spec.entry, None)
        if execute_fn is None:
            raise AttributeError(
                f"skill {spec.name}: handler {handler_file.name} has no {spec.entry}()"
            )

        ctx = self._build_context(spec)

        async def _bus_handler(msg):
            payload = msg.payload if msg.payload is not None else {}
            result = execute_fn(payload, ctx)
            if asyncio.iscoroutine(result):
                result = await result
            return result

        self.bus.register(spec.name, _bus_handler)
        await self.bus.publish_status(spec.name, "ready")

    def _on_task_done(self, name: str, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.exception("agent %s task crashed", name, exc_info=exc)
            spec = self.specs.get(name)
            asyncio.create_task(self._handle_task_crash(name, spec, exc))

    async def _handle_task_crash(self, name: str, spec: Optional[AgentSpec],
                                  exc: BaseException) -> None:
        await self.bus.publish_status(name, "failed")
        if spec is not None:
            await self._emit_session_end(spec, state="failed",
                                          reason=f"crashed: {exc}")

    STOP_HOOK_TIMEOUT = 10.0  # seconds an agent's stop(ctx) has before we cancel

    async def stop(self, name: str, cascade: bool = False,
                   *, reason: Optional[str] = None) -> None:
        if cascade:
            dependents = [n for n, s in self.specs.items() if name in s.depends_on]
            for d in dependents:
                await self.stop(d, cascade=True, reason=reason)

        # Call optional async def stop(ctx) BEFORE cancelling its task.
        mod = self.modules.get(name)
        spec = self.specs.get(name)
        if mod is not None and spec is not None:
            stop_fn = getattr(mod, "stop", None)
            if stop_fn is not None:
                ctx = self._build_context(spec)
                try:
                    await asyncio.wait_for(stop_fn(ctx), timeout=self.STOP_HOOK_TIMEOUT)
                except asyncio.TimeoutError:
                    log.warning("loader: %s.stop() timed out after %.1fs",
                                name, self.STOP_HOOK_TIMEOUT)
                except Exception:
                    log.exception("loader: %s.stop() raised", name)

        task = self.tasks.pop(name, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self.bus.unregister(name)
        await self.bus.publish_status(name, "stopped")
        if spec is not None:
            await self._emit_session_end(spec, state="stopped", reason=reason)

    async def restart(self, name: str, *, reason: Optional[str] = None) -> str:
        await self.stop(name, reason=reason or "restart")
        return await self.ensure_running(name)

    # -- session transcript helpers ---------------------------------

    async def _write_lifecycle(self, spec: AgentSpec, state: str,
                                reason: Optional[str] = None) -> None:
        """Append a lifecycle JSONL line; do not publish session.ended.

        Used for non-terminal transitions (ready) so readers can see when a
        run *started*, independent of when it ends.
        """
        entry: dict[str, Any] = {"event": "lifecycle", "state": state}
        if reason:
            entry["reason"] = reason
        try:
            await session_log.append(spec.path, spec.name, entry)
        except Exception:
            log.exception("loader: session_log.append(%s, %s) raised",
                          spec.name, state)

    async def _emit_session_end(self, spec: AgentSpec, *, state: str,
                                 reason: Optional[str] = None) -> None:
        """Append a terminal lifecycle line AND publish session.ended."""
        entry: dict[str, Any] = {"event": "lifecycle", "state": state}
        if reason:
            entry["reason"] = reason
        transcript_path: Optional[Path] = None
        try:
            transcript_path = await session_log.append(spec.path, spec.name, entry)
        except Exception:
            log.exception("loader: session_log.append(%s, %s) raised",
                          spec.name, state)
        try:
            await self.bus.publish("session.ended", {
                "agent": spec.name,
                "state": state,
                "reason": reason,
                "transcript_path": str(transcript_path) if transcript_path else None,
            })
        except Exception:
            log.exception("loader: publish session.ended for %s raised",
                          spec.name)

    def get_state(self, name: Optional[str] = None):
        if name is not None:
            return {"name": name, "status": self.bus.query_status(name)}
        return {n: self.bus.query_status(n) for n in self.specs}
