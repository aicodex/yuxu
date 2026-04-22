# Core Interface Contract

> **`src/core/` is the framework's public standard, not an implementation detail.**
>
> Anything listed in this file is a contract. Downstream agents (bundled and user)
> rely on it. Breaking changes ripple through every agent ever written against this
> framework.
>
> **Rule of thumb**: if it's not in this file, it's not part of the contract. Touch
> core code freely for internal refactors; touch this file only with a deprecation plan.

Version: `0.1.0` (pre-1.0 — API may break; post-1.0 SemVer applies to this file)

## Contents

- [What Belongs in `core`](#what-belongs-in-core)
- [Bus API](#bus-api)
- [Loader API](#loader-api)
- [Agent Folder Convention](#agent-folder-convention)
- [Agent Contract (start / stop / get_handle)](#agent-contract-start--stop--get_handle)
- [AgentContext](#agentcontext)
- [Frontmatter Fields](#frontmatter-fields)
- [Message Format](#message-format)
- [Status State Machine](#status-state-machine)
- [Standard Event Topics](#standard-event-topics)
- [What Is **Not** in Core](#what-is-not-in-core)

---

## What Belongs in `core`

Only four categories are allowed in `src/core/`:

1. **Bootstrap prerequisites** — can't be loaded as an agent because agents need them to load (Bus, Loader, Frontmatter parser, main).
2. **Performance-critical paths** — putting them behind an agent call would be too slow (none currently).
3. **Guardian-level isolation** — more fundamental than Loader itself (none currently).
4. **Pure utilities** shared by 1–3 (e.g. frontmatter parsing).

Everything else — including things that "look like framework" (skills registry, memory, scheduler, approvals, gateway) — belongs in `src/agents_bundled/` or `data/projects/`.

Current core modules:

| File | Role |
|---|---|
| `bus.py` | Message routing & lifecycle coordination |
| `loader.py` | Agent discovery & startup orchestration |
| `context.py` | `AgentContext` — the single argument to every agent |
| `frontmatter.py` | YAML-frontmatter parser (used by Loader) |
| `main.py` / `__main__.py` | Process entrypoint |

---

## Bus API

`src.core.bus.Bus` — import via `from src.core import Bus`.

Instance is created once per process by `main.boot()` and passed to every agent's `start(bus, loader)`.

### Messaging

```python
register(name: str, handler: Callable[[Message], Any]) -> None
unregister(name: str) -> None
send(to: str, event: str, payload: Any = None, sender: str | None = None) -> Awaitable[None]
request(to: str, query: Any, timeout: float = 30.0) -> Awaitable[Any]
```

- `send` is fire-and-forget; the caller never blocks on the handler.
- `request` creates a Future; handler's return value (or raised exception) fulfills it.
- If no handler is registered for `to`, `send` logs a warning and drops; `request` raises `LookupError`.
- Handler exceptions **do not propagate into the Bus**; they are logged and (for requests) set on the future.

### Pub/Sub

```python
subscribe(topic: str, handler: Callable[[dict], Any]) -> None
unsubscribe(topic: str, handler: Callable[[dict], Any]) -> None
publish(topic: str, payload: Any = None) -> Awaitable[None]
```

- Topic matching uses `fnmatch` (shell glob). `*.error` matches `alpha.error`, etc.
- Subscriber exceptions are isolated (logged, do not affect other subscribers).
- Each subscriber runs in its own task; order is not guaranteed.

### Status

```python
publish_status(agent: str, state: str) -> Awaitable[None]
query_status(agent: str) -> str                 # unloaded by default
wait_for_service(agent: str, timeout: float | None = None) -> Awaitable[None]
ready(agent: str) -> Awaitable[None]            # shorthand for publish_status(agent, "ready")
```

### Lifecycle

```python
run_forever() -> Awaitable[None]
stop() -> Awaitable[None]
```

---

## Loader API

`src.core.loader.Loader` — import via `from src.core import Loader`.

### Discovery & graph

```python
scan() -> Awaitable[None]
build_dep_graph() -> list[str]                         # topo order; raises RuntimeError on cycle / unknown dep
filter(run_mode: str | None = None, scope: str | None = None) -> list[AgentSpec]
get_dep_graph() -> dict[str, list[str]]
get_state(name: str | None = None) -> dict
get_handle(name: str) -> Any | None                    # agent's get_handle(ctx) result, or None
specs: dict[str, AgentSpec]                            # read-only access
```

### Lifecycle

```python
ensure_running(name: str) -> Awaitable[str]            # single entrypoint for start / lazy-start
stop(name: str, cascade: bool = False) -> Awaitable[None]
restart(name: str) -> Awaitable[str]
```

- `ensure_running` is idempotent + recursively resolves `depends_on`.
- Concurrent callers are serialized per-agent via an internal lock.
- On failure, the agent's status becomes `failed` and the exception propagates.
- `stop(name)` calls the agent's optional `async def stop(ctx)` hook first (10s timeout), then cancels its task.

---

## Agent Folder Convention

```
{agent_root}/{name}/
├── AGENT.md        # required if driver != python-only, or whenever metadata is present
├── __init__.py     # required for python/hybrid: exposes async def start(bus, loader)
├── handler.py      # conventional location for main logic (imported by __init__.py)
├── skills/         # optional: agent-private skills (see project_skills_convention memory)
```

Names starting with `.` or `_` are skipped by Loader.

Two roots are scanned by default, in precedence order (user overrides bundled):
1. `src/agents_bundled/` (system-level, shipped with framework)
2. `config/agents/` (user-level)

---

## Agent Contract (start / stop / get_handle)

`{agent_dir}/__init__.py` may expose any of these three functions. All are optional except `start` (and even that is optional if `__init__.py` exists but is empty — Loader treats that as auto-ready, same as an agent with only AGENT.md).

```python
async def start(ctx: AgentContext) -> None:
    """Initialize: register bus handlers, subscribe topics, spawn background tasks.
    MUST call await ctx.ready() once initialized (or return without running forever).
    """

async def stop(ctx: AgentContext) -> None:
    """Optional. Graceful shutdown: flush buffers, close connections.
    Called BEFORE task.cancel() during loader.stop(name). Max 10s before cancel.
    """

def get_handle(ctx: AgentContext) -> Any:
    """Optional. Returned Python object becomes visible to other agents via
    `ctx.get_agent(name)`. Use for direct in-process coupling when bus.request
    would be cumbersome (e.g. context managers like rate_limit).
    """
```

### Lifecycle rules

- For `run_mode: persistent`, Loader wraps `start(ctx)` in a Task and waits for `ready_timeout`.
- For other run modes, Loader awaits `start(ctx)` to completion (bounded by `ready_timeout`).
- Loader publishes `loading` before invoking `start`, and `failed` if `start` raises.
- If `__init__.py` exists but has no `start()`, Loader still publishes `ready` (empty-init = valid).
- If no `__init__.py` at all (LLM-only agent), Loader publishes `ready`; `llm_driver` takes over.

---

## AgentContext

Defined in `src.core.context.AgentContext` — import via `from src.core import AgentContext`.

**Stability rule**: fields only grow, never rename or remove. Adding fields is safe for existing agents; every other change is breaking.

```python
@dataclass(frozen=True)
class AgentContext:
    name: str                              # = agent folder name
    agent_dir: Path                        # absolute path to the agent folder
    frontmatter: dict                      # parsed AGENT.md frontmatter (may be {})
    body: str                              # AGENT.md body (for LLM agents; may be "")
    bus: Bus                               # message bus
    loader: Loader                         # introspection / cross-agent handles
    logger: logging.Logger                 # pre-bound `agent.{name}`

    async def ready(self) -> None: ...
    def get_agent(self, name: str) -> Any: ...          # other agent's get_handle() result, or None
    async def wait_for(self, name: str, timeout: float | None = None) -> None: ...
```

## Frontmatter Fields

AGENT.md frontmatter (YAML). All optional unless noted; unknown keys are preserved on the `AgentSpec.frontmatter` dict for agent use.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `driver` | `llm` \| `python` \| `hybrid` | `python` if `__init__.py` exists else `llm` | Execution model |
| `run_mode` | `persistent` \| `scheduled` \| `triggered` \| `one_shot` \| `spawned` | `one_shot` | Lifecycle pattern |
| `depends_on` | list[str] | `[]` | Hard dependencies; started before this agent |
| `optional_deps` | list[str] | `[]` | Soft dependencies (not auto-started) |
| `scope` | `system` \| `user` \| `project` | `user` | Approval policy tier |
| `handler` | path | `handler.py` | (Reserved for non-conventional layouts) |
| `entry` | function name | `start` | Entrypoint in `__init__.py` |
| `ready_timeout` | float seconds | `30.0` | Time Loader waits for `bus.ready()` |
| `edit_warning` | bool | `false` | System-level strong confirmation on edits |

Unknown values for enumerated fields fall back to the default with a warning.

---

## Message Format

```python
@dataclass
class Message:
    to: str
    event: str
    payload: Any = None
    sender: str | None = None
    request_id: str | None = None
```

Handlers receive a `Message`. For bus requests, `request_id` is populated; handlers should not depend on it.

---

## Status State Machine

Valid states (enum in `Bus.STATES`):

```
unloaded → loading → ready → running
                  ↘       ↘
                   failed  stopped
```

- `unloaded`: never touched; default return of `query_status`
- `loading`: `_start` in progress
- `ready`: `bus.ready(name)` was called
- `running`: reserved (agents may publish this themselves; loader doesn't set it)
- `failed`: `start` raised or agent task crashed
- `stopped`: `loader.stop` invoked

`wait_for_service` returns when state ∈ {ready, running}; raises `RuntimeError` if state ∈ {failed, stopped}.

---

## Standard Event Topics

Agents SHOULD publish the relevant subset:

```
{agent}.status           # ready / running / idle / failed — emitted by Loader automatically
{agent}.progress         # payload: free-form progress info
{agent}.output           # payload: task output / file path
{agent}.need_approval    # payload: {id, action, detail}
{agent}.error            # payload: {msg, detail, ...}
```

Framework-emitted `_meta` topics:

```
_meta.state_change       # payload: {agent, state}; fires on every publish_status
_meta.ratelimit.throttled # payload: {agent, pool, wait_time}; emitted by rate_limit_service when waits exceed threshold (future)
_meta.cancel             # payload: {task_id}; from Bus.cancel()
```

Agents can define their own topics freely; `_meta.*` is reserved for framework use.

---

## What Is **Not** in Core

These look like framework, but are implemented as bundled agents — **do not import these from `src.core`**:

| Capability | Location |
|---|---|
| Checkpoint persistence | `src/yuxu/bundled/checkpoint_store/` |
| Rate limiting | `src/yuxu/bundled/rate_limit_service/` |
| LLM HTTP client | `src/yuxu/bundled/llm_service/` |
| LLM turn loop | `src/yuxu/bundled/llm_driver/` |
| Recovery scanning | `src/yuxu/bundled/recovery_agent/` |
| Resource monitoring | `src/yuxu/bundled/resource_guardian/` |
| Watchdog / restart policy | `src/yuxu/bundled/project_supervisor/` |
| Skill discovery / dispatch | Loader directly (unified agent model) |
| Approval / memory / scheduler / gateway | `src/yuxu/bundled/{approval_queue, approval_applier, memory_curator, scheduler, gateway}/` |
| Agent creator / reflection / curator | `src/yuxu/bundled/{harness_pro_max, reflection_agent, memory_curator}/` |
| MiniMax budget tracking | `src/yuxu/bundled/minimax_budget/` |
| Gateway inline expansion (`$ARGUMENTS`, `!cmd`) | `src/yuxu/bundled/gateway/inline_expander.py` |

These can be swapped by the user (dropping a same-named folder in `config/agents/`) without touching core.

---

## Evolution Policy

- **Adding a new Bus / Loader method**: allowed, but requires clear justification (no other pattern works).
- **Changing an existing method's signature or semantics**: major version bump + migration guide + deprecation window.
- **Adding an `AgentContext` field**: allowed; never renames/removes existing fields.
- **Changing the `start(ctx)` / `stop(ctx)` signature**: would invalidate every agent — only under extreme duress.
- **Adding a frontmatter field**: allowed; defaults preserve prior behavior.
- **Removing a frontmatter field**: major version bump; deprecation warning first.
- **Changing the state machine states**: major version bump.
- **Changing event topic conventions**: major version bump.

When in doubt: don't change core; build an agent.
