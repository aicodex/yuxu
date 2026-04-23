# yuxu Architecture

Core mental model. Implementation details (specific adapters, quota
tracking, transcript formats) live in each agent's AGENT.md or in
complementary docs. This doc changes rarely.

## Identity

- Name: **yuxu** (pronounced /ˈjuːʃuː/)
- Nature: a **long-lived agent OS**, not an SDK. 7×24 autonomous;
  systemd pulls it up; users observe and approve, don't chat at it.
- Package: pip `yuxu`; projects depend on it.
- **What an agent is, conceptually**: a **reusable workflow** — a named,
  versioned, reusable unit of work that can be invoked, subscribed to,
  iterated on, and recombined. "Workflow" is broader than "prompt" or
  "function"; an agent holds the policy, dependencies, and state needed
  to repeat a job well.

## Vision (north star)

1. User describes a rough need; agents run 24/7 and self-iterate.
2. Token plan is squeezed — extra budget goes to background exploration.
3. User's primary UI is **observation + approval**, not dialog.
4. Subscription-based push: events find the user, not the other way around.
5. Real CLI (ops: `yuxu serve / init / status`) ≠ chat shell (end-user
   interacts through gateway with LLM agents). These are **strictly separate**.

## Product principles (4)

- **Agents self-iterate dialectically** — user gives a sketch, agents
  challenge it and refine. No forms with 20 fields.
- **Approvals are important but infrequent** — 2-day async grace for
  non-system changes; strong confirm for system changes.
- **Visibility is a first-class concern** — dozens of agents running is
  hard; UX has to carry that complexity (dashboards, subscriptions,
  hierarchical detail).
- **Always-on** — user can sleep; agents keep going.

## Core invariants

### I1. Everything is an agent

Core (Bus + Loader) is mechanism only. Policy = agents: LLM clients,
rate limits, persistence, scheduling, notifications, curation, memory,
approvals, gateways. Extending via a new agent is routine; extending
core is rare.

### I2. Mechanism vs policy

Kernel: high-frequency, no semantics, 5-year stable, failure = system
down. Agent: event-driven, has semantics, evolves, replaceable. When in
doubt, it's probably an agent.

### I3. Three-tier boundary

| Tier | Path | Who owns | Change rule |
|---|---|---|---|
| Core | `src/yuxu/core/` | Us | Frozen contract; additive-only changes |
| Bundled agents | `src/yuxu/bundled/` | Us (baseline) | User can override same-name |
| User agents | `<project>/agents/` | User | Free; 2-day grace |

### I4. Two kinds of agents

- **Prompt-launched** (`driver: llm`) — AGENT.md body is the system
  prompt; runs via llm_driver; no code.
- **Python-launched** (`driver: python`) — has `__init__.py` with
  `start(ctx)`; full control; can call LLM via llm_driver.
- (Hybrid mixes; rare.)

### I5. Five run modes

`persistent` (always on) / `scheduled` (cron) / `triggered` (event) /
`one_shot` (explicit) / `spawned` (by parent). Every agent declares one.

### I6. Four-layer scope for behavior-shaping data

"Memory" is a convenient word but **deliberately not formalized** —
edge cases are fuzzy (is `handler.py` memory? AGENT.md body? session
transcript? `rate_limits.yaml`?). yuxu formalizes **where to go to
change agent behavior** instead. Four scopes:

- **Global** — cross-project, user-wide. Typical: `~/.yuxu/`.
  **Reserved empty slot** (like Claude Code's `~/.claude/CLAUDE.md`,
  which ships empty); active users fill it over time with cross-
  project preferences, credentials, or meta-rules.
- **Project** — shared across agents in one project. Typical:
  `<project>/data/memory/_shared/` + project config.
- **Agent** — one specific agent's persistent state across runs:
  `AGENT.md`, `handler.py`, per-agent memory files, curated notes.
  Typical: `<project>/agents/<name>/` + `<project>/data/memory/<name>/`.
- **Session** — one conversation / run's ephemeral state: transcript,
  in-flight vars, scratch. Typical:
  `<project>/data/sessions/<agent>[#id]/<key>/`.

Forms are open: today markdown dominates; future may add yaml, sqlite,
embeddings, images. **Scope = where to look; form is orthogonal.**

Reads climb scopes (session → agent → project → global). Writes
crossing a scope boundary go through approval (`approval_queue` →
`approval_applier`).

**How do I change behavior X?**
- One agent's prompt → its `AGENT.md` (agent scope).
  iteration_agent proposes variants as drafts.
- One agent's code → its `handler.py` (agent scope). Human-only for
  now; iteration_agent v0.5+ may propose variants.
- A conversation's context → session scope files.
- Cross-project preferences → global scope (empty today; future
  `/memory` slash command opens an editor).
- Shared project facts → project scope (`_shared/`).

**How memory is written** (modeled on Claude Code's three paths):
- **User-initiated** (future): `/memory` slash opens the relevant
  scope file in `$EDITOR`. Analog: CC `commands/memory/memory.tsx`.
- **Auto-extraction**: `memory_curator` / `reflection_agent` generate
  drafts → `approval_queue` → `approval_applier`. Main write path
  today. Corresponds to CC's background extract system but with
  explicit draft + approval rather than a standard permission
  framework.
- **Direct LLM write**: discouraged at scope boundaries; routed
  through approval_queue instead.

**Why keep global empty?** Empty slot is free to carry; filling it
later avoids a migration. CC ships the slot empty; power users fill
it with meta-rules (tool preferences, coding styles). yuxu does the
same — when a cross-project use case lands, the path is already
reserved.

### I7. User-facing messages are subscription Info Sources

Any stream to the user (reply, reasoning, tool trace, dashboard,
notifications, approvals) registers as an Info Source; Feed routes
subscriptions to sinks (gateway, bus, fn). The main agent reply is a
`forced` subscription (un-cancellable); others are user-controllable.
Dashboards subscribe to the same sources, uniformly. See
`docs/subscription_model.md`.

### I8. Core stability contract

`src/yuxu/core/` API is frozen. Adding fields to `AgentContext` is
additive-only; never rename/remove. Changes need explicit eng review.

### I9. Iteration is continuous and budget-bound

Agents evolve indefinitely — reflection, curation, prompt variants,
workflow edits. The **only hard ceiling is the request budget** (MiniMax
5h interval, token plan, etc.). When the budget has headroom, yuxu
actively picks which agent to iterate next; when tight, it falls back to
the reservation floor (see `minimax_budget`).

Priority is a **weighted score**, highest first:

1. **User attention** — complaints, explicit `/improve` calls, recently
   rejected outputs, stuck approvals.
2. **Error and rejection rate** — per `performance_ranker`'s negative-
   signal accumulation.
3. **Token inefficiency** — value per token consumed (an agent that
   burns budget for little business output ranks up).

These combine into one score; the top-ranked agent gets the next
iteration slot. Unused headroom is spent on background exploration
(research, corpus build-up, memory consolidation) — never left idle.

### I10. Practice is the criterion for trustworthiness

Principles, methods, reference implementations, and design choices
earn trust only through verified practical effect. "Reference" means
"interesting to inspect", not "automatically correct". Initial
adoption is tentative; trust accumulates through observed outcomes.

Scoring (consistent with Asymmetric Iteration Signals in AGENT_GUIDE):
a principle / method that leads to better outcomes gains +5; one that
leads to worse outcomes gains −1. "Better" and "worse" are measured
by observable signals: execution errors (did it run?), user feedback
(acceptance, rejection, revision), and downstream agent performance
(error / rejection rate, quality). The weighting may be revised —
these are initial numbers, not fixed constants.

The scoring method itself is subject to the same test. If scores
fail to predict useful references, the scoring changes. This is
turtles all the way down — and the pattern is deliberate.

## Lifecycle states

```
unloaded → loading → ready → running
                          ↘
                           failed ─┐
                           stopped ─┴─→ supervisor decision
```

Terminal states (`failed` / `stopped` / `completed` on one_shot) emit
`session.ended` for downstream curation.

## User's five basic actions

- 跑 (run) — `loader.ensure_running`
- 停 (stop) — `loader.stop` / `bus.cancel`
- 问 (ask) — send to an agent / natural language via harness
- 看 (observe) — subscribe via Feed (`/dashboard`, event subs)
- 批 (approve) — approval_queue decisions

Users don't need to know about gateway / llm_driver / transport.

## Interaction paradigm (select-first)

Default UX is **choice cards**, not command lines. Telegram
`InlineKeyboardMarkup`, Feishu interactive cards, Slack blocks all
support this natively; plain text falls back to numbered lists. Shell-
style commands (e.g. `cd / ls`) are fallback shortcuts, not the norm.

## What's deliberately NOT here

Specific gateway adapter internals, rate-limit pool configs, MiniMax
quota tracking, session transcript JSONL format, specific agent
responsibilities, Bus/Loader Python API — these are details. See:

- `docs/CORE_INTERFACE.md` — Bus/Loader Python API contract
- `docs/AGENT_GUIDE.md` — creating an agent (how-to) + operational principles
- `docs/subscription_model.md` — Info Source / Feed / Sink design
- Each agent's `AGENT.md` — its own behavior

---

**Why this doc exists**: scattered memory + tribal knowledge used to
be how new agents (and new LLM sessions) oriented themselves. That
doesn't scale. This doc is the one-page canonical answer to "what is
yuxu, really?"
