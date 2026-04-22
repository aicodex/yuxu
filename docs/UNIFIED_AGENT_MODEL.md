# Unified Agent Model (proposal, pre-merge)

> **Status**: design draft, refined 2026-04-22 after user input.
> **Guiding principle** (user's framing):
>   - **逻辑分开**: skill and agent are two distinct concepts
>   - **代码合并**: one Loader, one registry, one dispatch
>   - **行为区分**: their runtime semantics differ even when called the same way
>
> **Motivation**: OpenClaw / Claude Code skill shape is close to our agent
> shape; sharing the code layer makes porting mechanical, while the
> logical/behavioral split preserves a crisp mental model.

## The core distinction: 主观能动性 (agency)

Think of it as an RPG:

- **Agent = character** — has personality (system prompt), memory (state),
  goals. The defining trait: **reasoning + decision-making**. It decides
  *when* to act, *what* to do next, *which* tool to pick.
- **Skill = ability on the skill tree** — "fireball", "identify". No
  thoughts of its own. Just logic that runs when invoked.

Skills are **called**. Agents **choose to call**. That's the whole split.

### Where the boundary blurs (and why it's still useful)

**A. Skills that wrap LLM reasoning internally** ("agentic workflow").
A skill like `summarize_ct_report` may itself call an LLM, parse
structured output, retry on failure. Under the hood it looks like a mini
agent. But from the outside, it's still called-by-caller, returns once,
holds no state between calls. It's a skill.

**B. Agents invoked as tools by another agent** ("multi-agent system").
`doctor_agent` decides it needs a diagram, calls `diagram_agent`. From
`doctor_agent`'s perspective, `diagram_agent` is just a skill — "give me
a diagram". But `diagram_agent` itself still has lifecycle, memory,
ongoing reasoning. From the system's perspective, it's an agent.

The blur is in the **calling protocol** (both look like `bus.request`).
The clarity is in **what's running on the other end**: stateless
function vs. stateful character.

## The three-layer model

### 1. Logic layer (逻辑分开)

| aspect                 | agent                        | skill                        |
|------------------------|------------------------------|------------------------------|
| agency / initiative    | **yes** (decides when/what)  | **no** (runs when called)    |
| dynamic memory         | yes (between calls)          | no (fresh each call)         |
| goals                  | yes                          | no                           |
| analog                 | RPG character                | ability on the skill tree    |

### 2. Code layer (代码合并)

Both share:
- One `Loader.scan()`
- One spec dataclass (`AgentSpec` with `kind: "agent" | "skill"` field)
- One bus dispatch (`bus.request("{name}", ...)`)
- One frontmatter parser
- One enable/opt-in mechanism

The only code branch: `ensure_running(spec)` —
- agent → call `start(ctx)`, spawn task
- skill → register bus handler that wraps `execute(input, ctx)`, no task

### 3. Behavior layer (行为区分)

Even though both reply to the same `bus.request("{name}", ...)` call,
their runtime semantics differ:

| behavior aspect               | agent                     | skill                       |
|-------------------------------|---------------------------|-----------------------------|
| lifespan of a call            | long (agent keeps living) | short (execute → return)    |
| state between calls           | preserved in memory       | none                        |
| can initiate outbound action  | yes (subscribe / publish) | no (only reactive)          |
| can be "running" with no call | yes (persistent mode)     | no (dormant until called)   |
| typical cost                  | holds resources           | pay-per-call                |

Callers (gateway, scheduler, other agents) **need not care** which kind
they're talking to — the bus hides it. But when designing a unit, the
author picks based on: does this thing need to remember anything between
calls? If yes → agent. If no → skill.

## The mechanical marker

**`__init__.py` presence** is how Loader physically distinguishes them
on disk:

- Has `__init__.py` exporting `start(ctx)` → **agent** (lifecycle exists)
- No `__init__.py`, only `handler.py` with `execute(input, ctx)` →
  **skill** (no lifecycle)

The marker matches Python's own semantics: no `__init__.py` = not a
package = no module-level lifecycle to run. It's not a yuxu invention;
it's inherited from the language.

## Call-graph rules

### Calling (cheap, unrestricted)

- Agents call skills, call other agents (as tools — blur case B). Free.
- Skills call skills, call agents (as tools). Free.
- Skills cannot self-execute; something has to invoke them.

### Starting agents (goes through the launcher by default)

**Default path**: everyone — skills, agents, gateway, user-initiated
`/new` — starts agents by calling the **launcher agent**:

```
bus.request("launcher", {op: "start", name: "...", args: {...}})
   → launcher validates, calls loader.ensure_running, records lifecycle
   → returns {ok, handle_ref}
```

**Why default through launcher** (the *监管* / supervision principle):
- One agent owns lifecycle bookkeeping. Dashboard reads from one place.
- Policy (quota / approval / dependency check) applied uniformly.
- If a unit dies unexpectedly, one agent knows about it and can restart.
- Matches yuxu Product Principle #3 ("可感知"): all starts visible in
  one trail.

**Escape hatch**: callers can still call `loader.ensure_running(name)`
directly. This is **jailbreak mode** — opt-out of monitoring. We don't
forbid it (a free system has to allow unsupervised paths), but default
docs, examples, AGENT_GUIDE all route through the launcher.

**Who can bypass?** Technically anyone inside the process (it's a Python
function call). Socially, the convention is: don't bypass unless you
have a reason you can articulate (e.g., the launcher itself, recovery
logic, a user explicitly writing their own `my_launcher` agent that
takes over the role).

### Naming: why `launcher` and not `sub_agent`

Claude Code / OpenClaw call this role "sub_agent" because their main
event loop is a chat with one primary agent; everything else is spawned
*under* that agent for the duration of a user ask.

yuxu is not chat-centric. Many agents live in parallel, persistently,
with no single "parent" chat. "sub_agent" implies a tree rooted in the
user turn — a bad fit. We use **`launcher`** — neutral, describes the
function (spin things up), no ownership implication.

### User-provided launchers

yuxu ships one default `launcher` agent. Users can write their own
(`agents/my_launcher/`) with `__init__.py` and wire
`bus.request("launcher", ...)` to it instead (or publish under a
different name and call that). That's the flex/jailbreak seam — users
always get a way out.

Code-level rule of thumb: **if you're writing `start(ctx)` or subscribing
to the bus for unsolicited events, you need agent (and `__init__.py`).
If all you do is run when called, you need skill (and no `__init__.py`).**

## Why unify at the code layer

Today's code-level duplication (even though concepts stay separate):

| code artifact | agent | skill |
|---|---|---|
| Folder root | `bundled/` or `agents/` | `skills_bundled/` or `<scope>/skills/` |
| Discovery | `Loader.scan` | `SkillRegistry.scan` |
| Registry object | `AgentSpec` | `SkillSpec` |
| Catalog | `loader.filter(...)` | `registry.catalog(...)` |
| Bus dispatch | Loader registers handler | `skill_executor` wraps + registers `skill.{name}` |

This duplication is accidental — both are "markdown + frontmatter + python
module with one async entry function". One `Loader` + one registry + one
dispatch path handles both; the activation rule is the only branch.

Three practical wins of unifying code:

1. **One registry** — `skill_picker` / `skill_executor` become thin helpers
   (or go away entirely, their function absorbed by Loader).
2. **OpenClaw / CC skill import friction drops** — same frontmatter reader,
   same on-disk shape as ours.
3. **Gateway routing is uniform** — to show a menu, gateway queries one
   registry and filters by kind + `surface`.

## Proposed concrete changes

### On-disk shape

Both kinds live side by side under `bundled/` (system) and `agents/` /
`skills/` (user scope). Loader scans both.

**Agent folder** — has lifecycle:

```
bundled/harness_pro_max/
    AGENT.md              # frontmatter + body
    __init__.py           # exports start(ctx), maybe stop(ctx)/get_handle(ctx)
    handler.py            # implementation (free form)
    ...
```

**Skill folder** — passive, no lifecycle:

```
bundled/classify_intent/
    SKILL.md              # frontmatter + body (the instructions for LLM or the tool schema)
    handler.py            # exports execute(input, ctx) -> dict
    # no __init__.py — this is the marker
```

The **absence of `__init__.py`** is the unambiguous mechanical rule:
Loader inspects the folder, sees no `__init__.py`, treats it as a skill.
No frontmatter field needed to classify — the folder shape says it.

### Frontmatter (shared schema, AGENT.md + SKILL.md read the same way)

Both files parse through the same `parse_frontmatter()`. Most fields are
shared; a few are only meaningful for one kind.

```yaml
---
# identity (starred = required, both kinds)
name*: snake_case
description: short sentence

# graph (both kinds)
depends_on: [other_agents]
optional_deps: [...]
scope: user | system | project

# --- agent-only ---
driver: python | llm | hybrid              # default python
run_mode: persistent | scheduled | triggered
ready_timeout: 30.0

# --- skill-only (tool-call shape) ---
parameters:                                # OpenAI function-calling schema
  type: object
  ...
triggers: ["natural phrase", ...]          # hints for intent classifier / menu
argument_names: ["arg1", "arg2"]           # for $ARGUMENTS substitution

# --- presentation (both kinds) ---
surface: [command, menu]                   # where gateway shows this unit
allowed_tools: [Bash, Read]                # CC compat, future-enforced
rate_limit_pool: minimax

# --- provenance (preserved verbatim from OpenClaw/CC imports) ---
version: 1.0.0
author: ...
license: MIT
tags: [...]
homepage: https://...
handler: self_improving.py                 # override default handler.py

# --- CC hints ---
model: sonnet
context: inline | fork
---
```

Loader rejects agent-only fields on skills (`run_mode` on a SKILL.md →
warning) and vice versa. One parser, but it knows which fields are
meaningful for which kind.

### Entry convention

**Agent** — `__init__.py` exports the lifecycle:

```python
# bundled/my_agent/__init__.py
async def start(ctx: AgentContext) -> None: ...
async def stop(ctx: AgentContext) -> None: ...   # optional
def get_handle(ctx) -> Any: ...                   # optional; returned from ensure_running

# Agents may ALSO expose a bus-callable `execute` for on-request handling,
# but they don't have to. Their presence on the bus is decided by what
# `start` wires up.
```

**Skill** — `handler.py` exports a single passive entry:

```python
# bundled/my_skill/handler.py
async def execute(input: dict, ctx: AgentContext) -> dict: ...
```

The `input` dict conforms to the skill's `parameters` schema. The return
shape is up to the skill (dict for bus consumers; markdown/text for
inline expansion).

For **pure-LLM agents** (`driver: llm`, no handler.py): `AGENT.md` body
becomes the system prompt; `__init__.py` stub calls into `llm_driver`.
Same pattern as today.

### Loader behavior

`Loader.scan()` — one discovery function. For each folder:

- has `__init__.py` → classify as **agent**, read `AGENT.md` (or fall back
  to folder name if absent)
- no `__init__.py` but has `handler.py` + `SKILL.md` → classify as **skill**
- neither → skip (with a warning if it has a `.md` file)

`Loader.ensure_running(name)`:

- **agent**: current behavior — call `start`, spawn task, wait for
  `ctx.ready()`. Task lives in loader's task map.
- **skill**: lazy-import module on first call; register bus handler at
  `{name}` that wraps `execute(input, ctx)`; no task, no state.

`loader.filter(kind=..., surface=...)`:

- replaces `SkillRegistry.catalog`
- filters by kind (`agent` / `skill`) and `surface` (`command` / `menu`)

### Bus addressing

Both kinds use `{name}` as the bus address — no `skill.` prefix. The
Loader guarantees name uniqueness across kinds at scan time (collision =
load error).

Callers migrate: `bus.request("classify_intent", ...)` not
`bus.request("skill.classify_intent", ...)`.

### Gateway in the new model

Gateway is an agent. Its routing is uniform across kinds:

```
user sends "/create_project ./foo"
  → gateway parses `/create_project`
  → bus.request("create_project", {args: "./foo"})      # skill
  → handler returns {ok, path}
  → gateway renders draft reply

user sends "跑个天气总结 agent"
  → gateway has no command match
  → bus.request("intent_router", {text: "..."})         # skill (future)
  → returns {target: "harness_pro_max", args: "..."}
  → gateway fires the real call

user sends "你好"
  → gateway no command, intent_router returns {target: null}
  → bus.request("chat_assistant", {...})                # agent, LLM wrapper
  → gateway renders
```

Menu = `loader.filter(surface="menu")` across both kinds.
User opt-in via `surface: [menu]` in their own `AGENT.md` or `SKILL.md`.

### User-created skills / agents as first-class

User's example: "user creates a Google-search skill, when they ask to
search it runs."

In this model:

- User drops `skills/google_search/` with `SKILL.md` (frontmatter:
  `parameters: {query: string}` + `triggers: ["search", "google"]` +
  `surface: [command, menu]`) + `handler.py` exporting `execute`. **No
  `__init__.py`** — it's a skill.
- Loader scans it, registers bus handler at `google_search`, not running.
- Gateway shows `/google_search` + intent_router routes "search X" to it.
- First call: bus dispatches → handler's `execute(input, ctx)` runs.

If instead they need **memory across calls** (e.g., Google-search agent
that caches results + rate-limits + tracks quota), they drop
`agents/google_search/` with `__init__.py` exporting `start(ctx)` that
sets up the cache + subscribes to the bus at `google_search`. Same bus
address, different lifecycle.

The user picks: stateless call → skill; live service → agent.

## Compatibility with OpenClaw / Claude Code

OpenClaw / CC skills are stateless tool-like units — they map cleanly to
yuxu **skills** (the no-`__init__.py` kind). Field mapping:

| OpenClaw / CC field | yuxu skill equivalent |
|---|---|
| `name / description / version / author / license / tags / homepage` | same field name, direct |
| `handler: self_improving.py` | same (override default handler.py) |
| `triggers` | same |
| `parameters` | same |
| `allowed-tools` (kebab) | `allowed_tools` (snake, accept both) |
| `model` | same (hint only today) |
| `context: inline / fork` | same (routed by inline_expander) |

Compat is not bit-for-bit (user stated explicitly); but conversion is
mechanical: read `SKILL.md`, write yuxu `SKILL.md` verbatim, maybe adjust
kebab→snake, ensure no `__init__.py`. A `skill_converter` skill can do
this one-pass.

## What happens to existing code

**`skills_bundled/` → `bundled/`** (physical merge into one tree):

- `classify_intent / generate_agent_md / create_project / create_agent /
  list_projects / list_agents` move under `bundled/`, keep their
  `SKILL.md`, keep `handler.py`, stay without `__init__.py` — they
  remain skills in the new model.
- `_shared.py` stays as a helper module; underscore prefix keeps Loader
  skipping it.

**`skill_picker` — delete**. Function absorbed into Loader:
- catalog → `loader.filter(kind="skill", surface=..., enabled=...)`
- load → `loader.specs[name].read_body()` + frontmatter dict
- rescan → `loader.scan()`

**`skill_executor` — mostly delete**. Function absorbed:
- bus dispatch → Loader registers `{name}` handler for skills directly
  (wraps `execute`)
- inline mode (`$ARGUMENTS` + `!cmd` preamble) → moves to
  `bundled/gateway/inline_expander.py` (or new module), called by
  gateway / intent_router when they want inline expansion
- fork mode → future

**Enable files** (`skills_enabled.yaml`): rename to just
`enabled.yaml` covering both kinds, or keep skill-only. Either way,
same semantics — an opt-in list that filters what Loader exposes.

**Migration stats**:

- Delete: `skill_picker/` (~450 LOC), `skill_executor/` dispatch portion
  (~250 LOC), `SkillRegistry` / `SkillScope` / `SkillSpec` classes
- Keep (move under new home): inline_expander ~100 LOC
- Move: 6 folders `skills_bundled/*` → `bundled/*`
- Update imports: `from yuxu.skills_bundled.X.handler import Y`
  → `from yuxu.bundled.X.handler import Y`
- Loader gets ~50 LOC: skill classification + lazy-import + bus wrapper
- Gateway gets ~30 LOC: surface-based menu filter
- Tests: rename + adjust asserts; ~200 LOC touched

Net: **~600 LOC deleted, ~80 LOC added**.

## What stays the same

- `Bus.request / send / publish / subscribe`
- `AgentContext` shape (fields only grow, never rename)
- Skill body markdown (still arbitrary text; AGENT.md body unchanged)
- `!cmd` preamble + `$ARGUMENTS` substitution (helper module)
- SKILL.md as a doc filename — kept, it aligns with OpenClaw/CC
- AGENT_GUIDE.md grows a "skill vs agent — when to pick which" section

## What this unblocks

1. **User-created skills/agents register with gateway via opt-in** —
   today skill surfaces require hand-writing command-register calls;
   after merge it's just a `surface: [command]` field.
2. **intent_router / chat_assistant** are just regular skills / agents.
3. **`skill_converter`** becomes a small skill (rename fields) instead
   of a registry translation layer.
4. **Quota management** (per-agent budget) applies uniformly — no
   "skills vs agents" branching in budget logic.
5. **One `Loader`, one registry** — simpler internals, simpler docs.

## What to be careful about

- **Import-time side effects on skills**. Skills import lazily on first
  bus dispatch (not at scan time) — keeps startup cheap and guards
  against crash-on-import. Trade-off: small delay on first call.
- **Bus address collision**. `skill.foo` goes away; `foo` (agent) and
  `foo` (skill) would collide. Loader rejects at scan time.
- **Enable-opt-in UX**. Agents auto-start today; skills need opt-in.
  Keep: all skills default-available (scope-dependent); `surface` field
  controls user-visible routing (command / menu).
- **Test churn**. Every test importing from `skills_bundled.*` needs
  updating. Mechanical but touches many files.
- **Blur case A (skill with LLM inside)**: skills are free to call
  `llm_driver`, parse JSON, loop for retries. They're still skills as
  long as they don't hold state between calls. Don't reach for agent
  just because the skill got smart.
- **Blur case B (agent called as tool)**: an agent calling another
  agent via `bus.request` treats the callee as a tool. That's fine —
  the callee is still an agent by construction; the caller just doesn't
  care. Don't "downgrade" the callee to skill; it still needs its
  lifecycle.

## Execution sketch

Step 1 — Loader upgrades (no user impact):
- `Loader.scan()` classifies each folder: has `__init__.py` → agent,
  no `__init__.py` + has `handler.py` → skill
- One `AgentSpec` dataclass with `kind: "agent" | "skill"` field
- `_register_skill_handler(spec)` that lazy-imports module, wraps
  `execute`, registers at `{name}`
- `ensure_running(spec)`:
  - agent → current behavior (call `start`, spawn task)
  - skill → register bus handler, publish ready, no task

Step 2 — move 6 skills_bundled folders → bundled:
- `git mv src/yuxu/skills_bundled/X src/yuxu/bundled/X`
- Keep `SKILL.md` filename, keep no `__init__.py` (marker)
- Update imports everywhere: `skills_bundled` → `bundled`

Step 3 — retire skill_executor + skill_picker:
- Delete `bundled/skill_picker/` and `bundled/skill_executor/`
- Move inline preamble `!cmd` / `$ARGUMENTS` helpers to
  `bundled/gateway/inline_expander.py`
- Update harness/reflection/curator/memory_curator callers to use
  `bus.request("{name}", ...)` directly instead of skill_executor hop

Step 4 — gateway + menu:
- Add `surface: list[str]` frontmatter field (parsed into AgentSpec)
- `loader.filter(kind=..., surface=...)` method
- Gateway op `list_menu` returns filtered specs (kind-agnostic)

Step 5 — docs + tests:
- AGENT_GUIDE.md: "agent vs skill — when to pick which" section,
  `__init__.py` as the marker, example pair
- SKILL_FORMAT.md: stays, documents SKILL.md fields, updated with
  "skills live under bundled/ now" note
- Tests: bulk rename imports, adjust registry assertions

Estimate: 1 full day, small chance of spillover.

## Open questions for discussion

(Re-framed under the refined model — code-unified / logic-split.)

1. **Classifier rule**: is `__init__.py` presence the right marker, or
   should we use an explicit `kind: agent | skill` frontmatter field?
   My vote: **`__init__.py` presence**. It's mechanical, self-documenting
   on disk, and matches what the OS/Python already enforces (if there's
   no `__init__.py`, the folder isn't a Python package — i.e., no
   lifecycle import to run).
2. **Filenames**: keep `AGENT.md` + `SKILL.md` distinction, or unify to
   `AGENT.md` for both?
   My vote: **keep both**. They signal the kind to a reader at a glance,
   and SKILL.md aligns with OpenClaw / CC naming (easier mental model
   for imports).
3. **Enable list**: one per-project `<project>/config/enabled.yaml`
   covering both kinds, or separate per kind?
   My vote: one combined file, kind-agnostic. Simpler UX.
4. **Do we backport existing OpenClaw skills** (the `self_improving`
   example) now, as a demo? Or wait until real use case?
   My vote: don't backport yet; wait for need.
5. **Old `skill.{name}` bus addresses**: deprecation window? or just
   delete? Today nobody depends on them.
   My vote: just delete. Zero production callers.

## Not in this document (future work)

- `intent_router` skill (the natural-language → command dispatcher)
- `chat_assistant` agent (fallback for plain messages)
- `skill_converter` skill (OpenClaw/CC import)
- per-agent quota budgets
- menu rendering in Telegram/Feishu adapters (surface field is input)

These all build naturally on the refined model.
