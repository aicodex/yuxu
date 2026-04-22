# How to Build a Yuxu Agent

> Audience: an LLM (or human) creating a new agent from a natural-language
> need. This is the **recipe book**. For the formal framework contract,
> see [CORE_INTERFACE.md](CORE_INTERFACE.md); for skills, [SKILL_FORMAT.md](SKILL_FORMAT.md).

## Agent vs. skill — pick before you scaffold

The unified Loader handles both, so you call them the same way
(`bus.request("{name}", ...)`). The difference is **agency**:

| | **agent** | **skill** |
|---|---|---|
| Has `__init__.py` exporting `start(ctx)`? | **yes** | **no** (just `handler.py`) |
| Can subscribe to events / run background tasks | yes | no |
| Holds state between calls (memory, caches) | yes | no — fresh each call |
| Can initiate action without being called | yes (persistent/scheduled) | no — reactive only |
| Typical example | `gateway`, `scheduler`, `reflection_agent` | `classify_intent`, `create_project` |

Rule of thumb: **if you need to remember anything between calls, or run when
nobody's calling you, write an agent. Otherwise write a skill.** Both live
under the same scope roots; the absence of `__init__.py` is what tells the
Loader you're a skill.

## TL;DR — the smallest working agent

A yuxu project has two roots Loader scans: `_system/` (bundled, don't touch)
and `agents/` (yours). Drop this into `agents/hello_bot/`:

```
agents/hello_bot/
├── AGENT.md
└── __init__.py
```

**AGENT.md**:
```yaml
---
driver: python
run_mode: one_shot
scope: user
depends_on: []
ready_timeout: 5
---
# hello_bot

Prints hello to the agent log on startup.
```

**__init__.py**:
```python
async def start(ctx):
    ctx.logger.info("hello from %s", ctx.name)
    await ctx.ready()
```

Run: `yuxu run hello_bot` (or start via `yuxu serve` when `run_mode: persistent`).

That's it. Every additional feature below is *optional*.

---

## Folder Anatomy

```
agents/{name}/
├── AGENT.md          # REQUIRED: frontmatter + body
├── __init__.py       # only if driver != llm; exposes start/stop/get_handle
├── handler.py        # convention for business logic (imported by __init__.py)
├── skills/           # optional: agent-private skills (see SKILL_FORMAT.md)
└── skills_enabled.yaml  # optional; enables the private skills
```

Rules:
- Folder name == agent's registered bus address
- Folder must not start with `.` or `_` (Loader skips those)
- Name must be `[a-z][a-z0-9_]*` (snake_case)

---

## AGENT.md Frontmatter Cookbook

All fields optional unless noted. See [CORE_INTERFACE.md §Frontmatter Fields](CORE_INTERFACE.md#frontmatter-fields)
for the authoritative table.

### Minimum (one_shot job)
```yaml
---
driver: python
run_mode: one_shot
scope: user
---
```

### Persistent event subscriber
```yaml
---
driver: python
run_mode: persistent
scope: user
depends_on: [gateway]
ready_timeout: 5
---
```

### LLM-only agent (no `__init__.py` needed)
```yaml
---
driver: llm
run_mode: one_shot
scope: user
depends_on: [llm_driver, llm_service, rate_limit_service]
ready_timeout: 180
---
# some_bot

YOU ARE a ... (AGENT.md body becomes the LLM system prompt)
```

### Scheduled / cron job
```yaml
---
driver: python
run_mode: scheduled
scope: user
depends_on: [scheduler]
schedule: "0 9 * * *"     # daily 09:00 (cron format)
ready_timeout: 10
---
```

### Field reference

| Field | Type | When to set | Example |
|---|---|---|---|
| `driver` | `python / llm / hybrid` | Always | `python` |
| `run_mode` | `persistent / one_shot / scheduled / triggered / spawned` | Always | `persistent` |
| `depends_on` | list[str] | List the bundled services you'll call | `[llm_driver, gateway]` |
| `optional_deps` | list[str] | Nice-to-have (not auto-started) | `[approval_queue]` |
| `scope` | `user / system / project` | Almost always `user` | `user` |
| `edit_warning` | bool | True for system agents that must confirm edits | `true` |
| `ready_timeout` | float seconds | How long to wait for `ctx.ready()` | `5` for services, `180` for long one_shots |

---

## Lifecycle Contract

`__init__.py` may expose any of these three; each is optional.

```python
async def start(ctx):
    """Runs once at load. Register handlers, subscribe topics, spawn tasks.
    MUST call await ctx.ready() once you're ready to receive traffic.
    For persistent agents: this task stays alive (loader wraps it)."""

async def stop(ctx):
    """Graceful shutdown. Called BEFORE the loader cancels your task.
    10-second budget. Close files, flush buffers, unsubscribe topics."""

def get_handle(ctx):
    """Return a Python object other agents can grab via ctx.get_agent(name).
    Use for in-process coupling (e.g. async context managers)."""
```

For a persistent agent that registers a bus endpoint:
```python
async def start(ctx):
    my = MyAgent(ctx)
    ctx.bus.register(ctx.name, my.handle)     # <== bus surface
    await my.install()                          # subscribe topics, etc.
    await ctx.ready()
```

For a one_shot that does work and returns:
```python
async def start(ctx):
    result = await do_the_work(ctx)
    ctx.logger.info("done: %s", result)
    await ctx.ready()
```

---

## AgentContext Quick Reference

Every lifecycle hook gets a frozen `AgentContext`:

```python
ctx.name           # str — agent folder name (== bus address)
ctx.agent_dir      # pathlib.Path — absolute path to YOUR folder
ctx.frontmatter    # dict — your AGENT.md frontmatter (full copy)
ctx.body           # str  — your AGENT.md body (useful for LLM-only)
ctx.bus            # Bus  — request / publish / subscribe / register
ctx.loader         # Loader — introspection; rarely used directly
ctx.logger         # pre-bound logging.Logger(agent.{name})

await ctx.ready()                        # declare yourself ready
ctx.get_agent(name)                      # other agent's get_handle() result
await ctx.wait_for(name, timeout=5.0)    # block until another agent is ready
```

---

## System Services Catalog

The bundled agents below ship with yuxu. **Declare what you use in your
`depends_on`** — Loader starts them before you. All are callable via
`bus.request(<name>, {<op>, ...}, timeout=...)`.

### llm_driver — run a multi-turn LLM loop

```python
r = await ctx.bus.request("llm_driver", {
    "op": "run_turn",
    "system_prompt": "...",
    "messages": [{"role": "user", "content": "..."}],
    "pool": "minimax",                        # rate_limit pool name
    "model": "MiniMax-M2.7-highspeed",
    "tools": [...],                           # optional: function-calling schemas
    "tool_dispatch": {"tool_name": "bus_addr"},
    "temperature": 0.3,
    "json_mode": False,
    "strip_thinking_blocks": True,            # scrub <think>...</think>
    "max_iterations": 32,
    "max_total_tokens": 200000,               # cumulative budget
    "llm_timeout": 120.0,
}, timeout=150.0)
# → {ok, content, messages, iterations, stop_reason, usage, error}
```

### llm_service — single LLM chat call (usually reached via llm_driver)

```python
r = await ctx.bus.request("llm_service", {
    "pool": "minimax",
    "model": "MiniMax-M2.7-highspeed",
    "messages": [...],
    "temperature": 0.3,
    "json_mode": False,
    "strip_thinking_blocks": True,
}, timeout=60.0)
# → {ok, content, tool_calls, stop_reason, usage}
```

### rate_limit_service — acquire a pool slot (use via handle, not bus)

Typically accessed by llm_service internally. You rarely call it directly,
but if you're integrating a non-LLM rate-limited API:
```python
rl = ctx.get_agent("rate_limit_service")   # handle
async with rl.acquire("my_pool") as slot:
    ...  # slot["extra"] carries account credentials
```

### checkpoint_store — atomic KV persistence

```python
await ctx.bus.request("checkpoint_store",
    {"op": "save", "namespace": "my_agent", "key": "progress",
     "data": {...}}, timeout=5.0)

r = await ctx.bus.request("checkpoint_store",
    {"op": "load", "namespace": "my_agent", "key": "progress"}, timeout=5.0)
# → {ok, data, saved_at}  or  {ok: false, error: "not_found"}

# Also: op: "list" (keys in a namespace), "list_namespaces", "delete"
```

### gateway — reply to users + register slash commands

```python
# Outbound (reply to a user who messaged you)
await ctx.bus.request("gateway",
    {"op": "send", "session_key": "...", "text": "hi"}, timeout=5.0)

# Register a slash command during start()
await ctx.bus.request("gateway", {
    "op": "register_command",
    "command": "/mybot",
    "agent": ctx.name,
    "help": "what /mybot does",
}, timeout=2.0)

# Subscribe to incoming commands
ctx.bus.subscribe("gateway.command_invoked", self._on_command)

async def _on_command(self, event):
    payload = event.get("payload") or {}
    if payload.get("command") != "/mybot":
        return
    session_key = payload.get("session_key")
    args = payload.get("args", "")
    # ... do work, reply via op:"send" ...
```

### approval_queue — destructive-action gate

```python
# Request approval (returns an id; status is "pending")
r = await ctx.bus.request("approval_queue", {
    "op": "enqueue",
    "action": "delete_memory",
    "detail": {"target": "/path/to/thing", "reason": "..."},
    "requester": ctx.name,
}, timeout=5.0)
aid = r["approval_id"]

# Subscribe to outcomes
ctx.bus.subscribe("approval_queue.decided", self._on_decided)
# payload: {approval_id, action, decision: "approved"|"rejected", ...}
```

### approval_applier — autoconsumer for `memory_edit` approvals

You don't call this directly. When you `approval_queue.enqueue` with
`action: "memory_edit"` and `detail.draft_path` + `.proposed_target`, and
the queue is then approved, approval_applier writes the draft content to
the target memory file and deletes the draft.

### scheduler — cron job runner

Add `run_mode: scheduled` + `schedule: "<cron expr>"` in your AGENT.md
frontmatter. scheduler fires your `start(ctx)` on that cadence. Bus ops:

```python
r = await ctx.bus.request("scheduler", {"op": "list"}, timeout=2.0)
# → {ok, schedules: [...], total_fires: int}
```

### Calling a skill — just `bus.request("{name}", ...)` (unified model)

Skills are discovered by Loader at scan time (folder with `handler.py` and
no `__init__.py`), registered directly on the bus. Call them like any
agent:

```python
r = await ctx.bus.request("classify_intent", {
    "description": "morning news summarizer",
    "agent_templates": ["llm_only", "python", "hybrid"],
}, timeout=10.0)
# → {ok, agent_type, suggested_name, run_mode, depends_on, driver, reasoning}
```

To list all skills (or any unit surfaced to a menu):

```python
# Via Loader directly
skills = ctx.loader.filter(kind="skill")
menu_items = ctx.loader.filter(surface="menu")  # kind-agnostic

# Via gateway (e.g. for a /menu command)
r = await ctx.bus.request("gateway",
    {"op": "list_menu", "surface": "menu"}, timeout=2.0)
# → {ok, items: [{name, kind, scope, description, triggers, surface}, ...]}
```

### project_manager — supervise sibling agents at runtime

```python
# start / stop / restart an already-scanned agent
await ctx.bus.request("project_manager",
    {"op": "start_agent", "name": "other_bot"}, timeout=5.0)

# query all agent states
r = await ctx.bus.request("project_manager",
    {"op": "get_state"}, timeout=2.0)
# → {ok, state: {"llm_driver": "ready", ...}}
```

### project_supervisor — restart policy (mostly passive)

Watches persistent agents; restarts on failure with rate limiting.
Reports counters on `op: "report"`. You rarely call it.

### resource_guardian — error rate monitor (passive)

Subscribes to `*.error` and `_meta.ratelimit.throttled`, fires alerts when
windows exceed thresholds. Query counters via `op: "report"`. Agents just
need to emit `{agent}.error` events (see Standard Event Topics below).

### recovery_agent — checkpoint inventory

```python
r = await ctx.bus.request("recovery_agent",
    {"op": "status"}, timeout=5.0)
# → {ok, inventory: {fresh: [...], stale: [...], abandoned: [...]}}

# Kick off GC of `abandoned` checkpoints
await ctx.bus.request("recovery_agent", {"op": "gc"}, timeout=10.0)
```

### memory_curator — Hermes-inspired session-end curation

```python
await ctx.bus.request("memory_curator", {
    "op": "curate",
    "transcript": "...",                 # or "sources": ["/path/...md"]
    "context_hint": "what this was about",
}, timeout=120.0)
# → {ok, run_id, log_entries, drafts, approval_ids, summary}
```
Produces (1) append-only entries in `<memory_root>/_improvement_log.md` +
(2) memory edit drafts in `<memory_root>/_drafts/`, routed through approval_queue.

### reflection_agent — multi-hypothesis user-driven exploration

```python
await ctx.bus.request("reflection_agent", {
    "op": "reflect",
    "need": "what am I getting wrong in X?",
    "sources": ["/path/session1.md", "/path/dir/"],
    "n_hypotheses": 3,
}, timeout=180.0)
# → {ok, run_id, hypotheses, chosen, drafts, approval_ids}
```

### harness_pro_max — the agent-creator

```python
await ctx.bus.request("harness_pro_max", {
    "op": "create_agent",
    "description": "build a bot that does X...",
    "project_dir": "/optional/override",
}, timeout=180.0)
# → {ok, name, path, status, warnings}
```
Runs classify_intent → generate_agent_md → writes AGENT.md → scans + starts.

### dashboard / help_plugin

UI / utility agents. Not meant to be called by your code.

---

## Bus Primitives Cookbook

### Register a bus endpoint
```python
ctx.bus.register(ctx.name, self.handle)      # self.handle: async def(msg) -> dict
```

`msg.payload` is whatever the caller passed. Return a dict (usually
`{"ok": bool, ...}`).

### Make a request
```python
r = await ctx.bus.request("other_agent", {"op": "...", ...}, timeout=5.0)
```
Raises `LookupError` if nobody registered that name. Returns whatever the
handler returned. Use a timeout that fits the worst case.

### Publish an event
```python
await ctx.bus.publish("my_agent.progress", {"percent": 42})
```
Fire-and-forget. Zero subscribers → no error.

### Subscribe to events (glob supported)
```python
ctx.bus.subscribe("gateway.*", self._on_gateway_anything)
ctx.bus.subscribe("approval_queue.decided", self._on_decided)
```

### Check / wait for another agent's state
```python
status = ctx.bus.query_status("llm_driver")      # "ready" / "unloaded" / ...
await ctx.wait_for("llm_driver", timeout=10.0)   # sugar
```

### Mark yourself ready (MUST call once during start)
```python
await ctx.ready()
```

---

## Patterns

### Pattern 1 — Slash-command agent

Responds to `/mycmd args` in gateway, replies via gateway.

```python
class MyBot:
    def __init__(self, ctx):
        self.ctx = ctx

    async def install(self):
        self.ctx.bus.subscribe("gateway.command_invoked", self._on_cmd)
        await self.ctx.bus.request("gateway", {
            "op": "register_command",
            "command": "/mycmd", "agent": self.ctx.name,
            "help": "what this does",
        }, timeout=2.0)

    async def _on_cmd(self, event):
        p = event.get("payload") or {}
        if p.get("command") != "/mycmd":
            return
        text = await self.do_work(p.get("args", ""))
        await self.ctx.bus.request("gateway", {
            "op": "send", "session_key": p.get("session_key"), "text": text,
        }, timeout=5.0)
```

depends_on: `[gateway]`, run_mode: `persistent`.

### Pattern 2 — LLM-driven agent (no Python)

```yaml
---
driver: llm
run_mode: one_shot
scope: user
depends_on: [llm_driver, llm_service, rate_limit_service]
ready_timeout: 180
---
# agent_body_is_the_system_prompt

YOU ARE a X assistant. Given input Y, produce Z.

Rules:
- ...
```

Zero Python. llm_driver reads your body as system prompt and responds.

### Pattern 3 — One-shot task with checkpointing

```python
async def start(ctx):
    # resume from checkpoint
    r = await ctx.bus.request("checkpoint_store", {
        "op": "load", "namespace": ctx.name, "key": "state",
    }, timeout=5.0)
    state = r["data"] if r.get("ok") else {"step": 0}

    while state["step"] < 10:
        await do_one_step(state)
        state["step"] += 1
        await ctx.bus.request("checkpoint_store", {
            "op": "save", "namespace": ctx.name, "key": "state",
            "data": state,
        }, timeout=5.0)

    await ctx.ready()
```

### Pattern 4 — Event-driven worker

```python
class Worker:
    async def install(self):
        self.ctx.bus.subscribe("some_topic.*", self._on_event)

    async def _on_event(self, event):
        try:
            await self.handle(event["payload"])
        except Exception as e:
            await self.ctx.bus.publish(f"{self.ctx.name}.error",
                                        {"msg": str(e)})
```

### Pattern 5 — Hybrid: Python orchestration + LLM step

```python
async def start(ctx):
    data = fetch_something_from_api()                    # Python
    resp = await ctx.bus.request("llm_driver", {         # LLM
        "op": "run_turn",
        "system_prompt": ctx.body,
        "messages": [{"role": "user", "content": str(data)}],
        "pool": "minimax", "model": "MiniMax-M2.7-highspeed",
        "temperature": 0.3, "strip_thinking_blocks": True,
    }, timeout=120.0)
    write_file(resp["content"])                          # Python
    await ctx.ready()
```

depends_on: `[llm_driver, llm_service, rate_limit_service]`.

### Pattern 6 — Agent that proposes memory edits

Do NOT write `<memory_root>/*.md` directly. Instead:
1. Stage as draft in `<memory_root>/_drafts/`
2. Enqueue via approval_queue with `action: "memory_edit"` + detail carrying
   `draft_path` / `proposed_target` / `proposed_action`
3. Let approval_applier do the actual write when the user approves

See `reflection_agent` or `memory_curator` for reference implementations.

---

## Standard Event Topics

Publish these where relevant; tools (resource_guardian, dashboard) subscribe:

| Topic | Payload | When |
|---|---|---|
| `{agent}.status` | auto, emitted by Loader | state changes |
| `{agent}.progress` | `{percent, note, ...}` | long jobs |
| `{agent}.output` | `{file, ...}` | produced artifacts |
| `{agent}.error` | `{msg, detail, ...}` | non-fatal errors; guardian counts |
| `{agent}.need_approval` | `{id, action, detail}` | blocking user approval |
| `session.ended` | `{session_key, transcript, ...}` | session closed (used by memory_curator) |

Reserved (framework): `_meta.*`. Don't publish under this prefix.

---

## Common Pitfalls

### 1. Forgetting `await ctx.ready()`
Loader waits for this signal; without it, persistent agents will time out
(`ready_timeout`) and get marked failed.

### 2. Blocking calls inside `start()`
Don't `requests.get(...)` or `time.sleep()`. Use `httpx` + `asyncio.sleep`.
Loader awaits your coroutine — blocking it blocks the whole daemon.

### 3. Raising in a bus handler
Bus isolates exceptions (logs + fails the request future). But this still
means the caller sees an error. Return `{"ok": False, "error": "..."}`
for recoverable failures instead of raising.

### 4. Hard-coding paths
Use `ctx.agent_dir` for your agent's files. For project paths, walk up
from `agent_dir` until `yuxu.json` is found.

### 5. Calling LLM directly with httpx
Always go through `llm_driver` (or at least `llm_service`) so rate-limiting,
pooling, and token budget discipline are honored.

### 6. Writing memory files directly
Use the memory_curator / reflection_agent → approval_queue → approval_applier
chain. Your agent should propose, not apply.

### 7. Missing `depends_on`
Loader won't pre-start services you didn't declare. If your agent calls
`bus.request("llm_driver", ...)` but `llm_driver` isn't in `depends_on`,
the request raises `LookupError`.

### 8. Forgetting to unsubscribe in `stop(ctx)`
Not fatal (the task dies anyway) but tidier. Ignore if in doubt.

---

## Shipping Checklist

Before handing the agent over to Loader:

- [ ] AGENT.md has valid frontmatter; `driver` / `run_mode` / `scope` set
- [ ] `depends_on` lists every bus address your code calls
- [ ] `start(ctx)` calls `await ctx.ready()` exactly once
- [ ] Bus handler returns `{"ok": bool, ...}` dicts, never raises for user errors
- [ ] Long-running tasks are `asyncio.create_task`'d, not awaited inline
- [ ] Any filesystem writes go to `ctx.agent_dir` or project `data/`
- [ ] If persistent, `stop(ctx)` (if defined) completes in <10s
- [ ] Body of AGENT.md explains *why* the agent exists (not just *what*)
- [ ] Tests: at least happy path + one failure branch + one boundary case

---

## See Also

- [CORE_INTERFACE.md](CORE_INTERFACE.md) — formal Bus / Loader / AgentContext contract
- [SKILL_FORMAT.md](SKILL_FORMAT.md) — skill folder + frontmatter conventions
- [subscription_model.md](subscription_model.md) — future info_source / feed / approval triple
- `src/yuxu/bundled/*/AGENT.md` — every bundled agent is a worked example
- `src/yuxu/templates/agent/` — scaffold template used by `create_agent` skill
