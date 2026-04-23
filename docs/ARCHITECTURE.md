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

**Primary purpose** — yuxu memory stores **workflows** (reusable
operational patterns for agents) and **iteration outcomes** (what
worked, what didn't, on which agent). **Primary consumer: the
iteration agent.** User is a secondary consumer. This distinguishes
yuxu memory from Claude Code's (a coding-environment profile focused
on files + user prefs) and OpenClaw's (a user-facts database aimed
at remembering *the user*). yuxu memory is aimed at remembering
*how agents get work done well*.

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

**Memory access discipline — lazy, tool-mediated, never auto-injected.**
Validated against OpenClaw and Hermes: OpenClaw is fully lazy (tools
`memory_search` / `memory_get`, no snapshot); Hermes freezes a snapshot
at session start specifically to preserve Anthropic prefix cache —
that optimization doesn't apply to providers yuxu uses today, and
auto-injection doesn't scale past a few hundred entries regardless.
yuxu commits to the lazy model:

- **Never auto-inject memory into prompts.** All access goes through
  the `memory` skill ops (`list` / `get` / `search` / `stats`). Agents
  that `rglob` memory files directly violate the contract; the
  current eager `_load_sources` inside `memory_curator` is a
  dev-phase shortcut and must be refactored to use the skill before
  iteration_agent consumes memory.
- **Progressive disclosure levels** (implemented incrementally as
  scale demands):
  - L0 `memory.stats` — counts by type / scope / recency bucket;
    payload size independent of total entry count
  - L1 `memory.list {type?, scope?, tag?, since?, limit?}` — filtered
    index (frontmatter only)
  - L2 `memory.get {path}` — one entry's body
  - Cross-cut `memory.search {query, limit}` — ranked top-K, not
    full index
- **Frontmatter contract.** Every entry MUST have `name` +
  `description` + `type`. MAY have `tags: [...]` (L1 filter),
  `updated: YYYY-MM-DD` (recency / decay), `scope`, `evidence_level`,
  `status`, `score`, `probation` (below). Missing required fields →
  skipped from index. **`scope` marks *where the rule applies* (e.g.
  one agent's behavior vs. cross-project convention), not where the
  file is physically stored** — a global-scope entry can live in any
  memory directory; consumers filter by semantic applicability.
- **Scope-internal stratification is optional.** Within a scope,
  entries MAY be further split (e.g. durable `MEMORY.md` vs transient
  daily notes, per OpenClaw's pattern). Not mandatory for MVP; the
  `memory` skill sees all files uniformly until stratification lands.
- **Search / decay / SQLite index** are deferred to when real scale
  hits (~500+ entries). The op surface above is stable; the backend
  can change transparently.

**Retention — archive, don't delete.**
Forgotten failures repeat. Default write path is append + relocate,
never truncate + delete. Superseded entries move to `_archive/` with
a rationale file; rejected drafts preserve under `_archive/rejected/`;
failed-variant dead-ends archived per I11's variant layout. Hard
delete requires explicit user intent plus a warning.

**Evidence tiers + lifecycle status.**
Every entry MAY carry `evidence_level` ∈ `{validated, consensus,
observed, speculative}` (default `observed` for curator-generated)
and `status` ∈ `{current, archived}` (default `current`). Medical-
evidence analogy:

- `validated` — tournament victory or ≥ N successful signals
  (initial N = 10)
- `consensus` — architectural invariant / mechanism reasoning
  (ARCHITECTURE's I1-I11 sit here)
- `observed` — single real observation, not yet replicated
- `speculative` — untested hypothesis / literature pattern

Levels are orthogonal to `type` / `scope` / `tags`. **Schema stays
isomorphic** — a speculative entry has the same shape as a validated
one; only the `evidence_level` field differs. Consumers compose
filters freely.

**Retrieval modes (entropy management).**
Per Shannon, each retrieval reduces the consumer's hypothesis
entropy — useful for execution, hostile to exploration. Retrieval
takes a `mode` parameter with default filter policies:

- `blank`   → `[]` (clean slate)
- `explore` → only `mandatory`-tagged entries
- `execute` → `evidence_level ∈ {validated, consensus, observed}` +
              `status = current`  (**default** when mode unset)
- `reflect` → all levels, includes `status = archived`
- `debug`   → `observed` + `status = archived`

Agents declare a default in `AGENT.md` `memory_access.mode` and may
switch mid-run (iteration_agent switches `blank` → `reflect`
between variant generation and post-mortem phases). Mode is a
retrieval parameter, not a memory attribute.

**Mandatory tag — the only always-injected channel.**
Entries tagged `mandatory` inject under every mode including
`blank`. Reserved for a minimal set of hard rules (core kernel
invariants, destructive-action boundaries, secrets handling). The
temptation to over-add undermines the abstraction; candidates
require explicit approval like any other memory edit.

**Evidence is dynamic — scoring drives promotion / demotion.**
Each entry carries `score: {applied, helped, hurt, last_evaluated}`.
Weights are asymmetric (+5 helped, −1 hurt; `applied` is the
sampling base, not a scorable event) — same convention as the
framework-wide iteration signal. `performance_ranker` proposes
level transitions based on accumulated score and MUST cite the
run / tournament / session that produced the signal. No silent
promotion.

I11's tournament is the near-double-blind experiment for memory:
when variants draw different retrieval slices, the verdict
attributes credit to the delta memories. Approval-accept / reject
on non-tournament traffic contributes weak signal (≤ 1/5 tournament
weight) — correlation-only, no controlled comparison.

**Updates inherit level with probation.**
When a curated edit replaces an existing entry, the new version
inherits the prior `evidence_level` but its `score` resets and
`probation: true` is set. During probation: `execute` mode
excludes the entry (unvalidated change, risk of silent
propagation); `reflect` and `explore` include it. Probation clears
on a helped threshold; overdue entries auto-demote one level and
emit `memory.probation_failed` for user awareness.

**Admission gate before promote.**
Pure outcome-based scoring (tournament helped/hurt) is known to admit
false positives at alarming rates — ROLL reports ~40% of agent-RL
reward signals were false positives when no pre-filter was in place.
Memory promotion from `speculative` to `observed` therefore requires
passing a three-stage admission gate, adapted from ROLL's verification
layers:

1. **surface_check** — LLM-judge distinguishes semantic relevance
   from surface pattern match: does the entry actually apply to the
   task, or is retrieval only matching keywords?
2. **golden_replay** — in the source session that generated the
   entry, was the outcome driven by this memory's content, or is
   this a retrospective label pasted on an unrelated success?
3. **noop_baseline** — a variant without this entry cannot already
   win the same task. If the no-memory control passes, the entry's
   contribution is zero and it must not be promoted.

Any stage failing → entry stays `speculative`. Gating is not a
replacement for tournament scoring — it is a prerequisite. Quality
is gated *before* scoring, not only corrected after.

**Staleness as a hard threshold.**
Entries with `updated` older than a configurable window (initial
30 days) auto-demote one level unless recently touched by a
successful retrieval. Adapted from Slime's sample-level staleness
check in off-policy RL: when the underlying behavior distribution
has drifted, prior validation is no longer reliable evidence. A
stale `validated` entry that hasn't been exercised in a month
cannot be trusted at validated tier just because it once was.

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

### I11. Agent iteration is a bounded fork tree with memory-carry rollback

An agent's **tree** is `(AGENT.md, handler.py, memory/)`. Iteration
forks a tree into variants that share a parent pointer. Variants
compete; a winner merges back to `live`; losers stay archived (not
deleted) — future iterations can read why they failed.

**Rollback has two semantics:**
- *Hard*: restore live tree to an ancestor; discard intermediate memory.
- *With-memory*: restore code / prompt to an ancestor **but** cherry-
  pick selected memory entries (e.g. `dead-end-X.md`) from the failed
  exploration forward, so the next branch inherits "what didn't work
  and why." This is the primary differentiator from pure git rollback.

**Bounded exploration — no unbounded trees.** Every run carries an
`iteration_policy.yaml` with hard safety rails only:

```yaml
ceilings:
  max_depth: 3           # rollback-retry cycles
  max_per_level: 4       # fork breadth per layer
  total_budget: 12       # variant cap
  wall_clock: "2h"       # time ceiling (token budget tracked separately)
on_exhaustion:
  route: approval_queue
  surface_top_k: 3
```

The iteration agent picks criterion type (`test` / `llm_judge` /
hybrid) and decides when to terminate — `ceilings` are hard safety
rails, not policy. Its decisions + rationale are written to
`_tree.json` so `performance_ranker` can later score judge quality
itself (consistent with I10: the judge is also subject to practice).

When a ceiling is hit without the agent declaring a winner, the run
does not silently fail — it surfaces the top-k candidates + a
`_tree.json` summary via `approval_queue` for human judgment
(`pick_winner` / `reject_all` / `extend_budget`).

**Layout:**

```
<project>/data/variants/<agent>/
  _tree.json                # DAG: {variant_id: {parent, children,
                            #       status, fitness, reason}} plus the
                            #       iteration agent's judge_policy and
                            #       termination rationale for this run
  _archive/YYYYMMDD-<run>.json.gz
  <variant_id>/
    AGENT.md
    handler.py
    memory/                 # this variant's incremental memory
    _notes.md               # why forked / why failed / which memory
                            # to carry on rollback-with-memory
<project>/agents/<agent>/   # the live tree (= current winner)
```

**Fork scope — iterate self, other agents are environment.**
A fork only copies the iterated agent's own tree. Everything else is
read-only environment for the duration of the run:

| Object | Role | Fork treatment |
|---|---|---|
| Iterated agent's `AGENT.md` / `handler.py` / `memory/` | self | copy |
| Other agents' trees | environment | read-only, not copied |
| `data/memory/_shared/` / global scope | environment | read-only |
| Bus event stream | environment | live, not snapshotted |

During a run, the agent scope spawns ephemeral **variant sub-scopes**
at `data/variants/<agent>/<variant_id>/memory/`. These live for the
run's lifetime; at completion the winner's variant memory merges into
the agent scope, losers' variant memory is archived. This is an
extension of I6, not a new top-level scope.

**Storage guard — per-file and per-tree caps, with reference fallback.**
Bounded exploration is only bounded if one tree can't balloon storage.
Initial thresholds (subject to revision as real usage lands):

| File class | Limit | Over limit |
|---|---|---|
| `memory/*.md` | 100 KB each | store as reference (hardlink / symlink), not copy |
| `handler.py` | 50 KB | warn + refuse fork (tree needs decomposition first) |
| Attachments / embeddings / PDFs / historical session transcripts | — | never forked; read in place |

A forked tree's total byte sum has an initial ceiling of 500 KB. Over
the ceiling, `/fork` refuses and surfaces the offending file list for
human decision (prune, split, or exempt).

**Cross-agent blame — a signal, not inline intervention.**
If variant A' concludes the root cause is in agent B, A' does **not**
fork or modify B within its own run (that would break ceilings and
cross fork-scope boundaries). It emits a signal:

```
iteration.cross_blame
  payload: {from: "A#<variant>", to: "B", evidence, confidence: 0..1}
  → appended to data/iteration/cross_blame_queue.jsonl
  → performance_ranker weights B's "blamed-by-others" count (negative
    signal, scaled by confidence to prevent low-quality scapegoating)
  → B rises in the iteration priority queue via I9's normal ranking
```

This preserves fork scope (A's run touches only A), keeps ceilings
honest (no combinatorial blowup), and routes cross-agent feedback
through the same performance_ranker pipeline as any other negative
signal. Low-confidence blame earns small weight; high-confidence blame
can also be surfaced through `approval_queue` for human adjudication
when the stakes warrant it.

**Reuse of existing components:**
- `performance_ranker` feeds fitness signals and consumes
  `iteration.cross_blame` for cross-agent priority weighting.
- `approval_queue` + `approval_applier` handles both the merge-winner
  commit and the on-exhaustion surfacing — no new approval mechanism.
- `memory_curator` emits `dead-end-*.md` drafts as a variant ages out.
- `memory` skill (2-layer disclosure) indexes variant-local memory so
  a rolled-back branch can cheaply query its siblings' lessons.

**Deferred:**
- Tournament runner implementation (how variants get scheduled and
  scored) — needs its own agent.
- Tree backend: yuxu-native JSON (MVP) vs git worktree (later upgrade);
  the layout above is backend-agnostic.
- `handler.py` variants need human approval per I6; `AGENT.md` / prompt
  variants can be agent-proposed.
- Cross-blame queue consumer (iteration coordinator) — the queue file
  and event are specified here; the agent that reads them comes with
  iteration_agent v0.

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
