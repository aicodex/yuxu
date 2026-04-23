# COMPARISON — yuxu vs Claude Code / OpenClaw / Hermes

Living reference for how yuxu lines up against the three systems we
study. Update when architectural choices land or when one of the
references evolves. Treat the table as the quick-check; the prose
below it captures the *why* — which choices we copied, which we
diverged on, and which gaps are deliberate vs pending work.

Convention: ✅ shipped, ⚠️ partial or contract-only, ❌ not attempted.
"Shipped" means runnable today with tests, not just present in a
design doc.

## Quick-check table

| Dimension | Claude Code | OpenClaw | Hermes | yuxu |
|---|---|---|---|---|
| **Kernel shape** | Single-process REPL + tool loop | Single-process CLI + plugins | `run_agent` main loop + reflection hooks | Bus + Loader, event-driven, multi-agent concurrent |
| **Agent model** | No explicit "agent" — one main loop + tools | Close to CC; adds subagent via Task tool | `run()` function + reflection hooks per agent | Unified `agent` (lifecycle) vs `skill` (stateless) split (I1/I4) |
| **Run modes** | Session one-shot | Session one-shot | Persistent + dream | 5 modes: persistent / scheduled / triggered / one_shot / spawned (I5) |
| **Memory — write** | `/memory` editor, manual | SQLite lazy; admin tool writes | Snapshot frozen at session start (Anthropic prefix cache) | curator + reflection auto-propose → approval_queue → approval_applier → admission_gate → disk |
| **Memory — read** | Auto-inject files into system prompt | Lazy `memory_search` / `memory_get` tools | Frozen snapshot fully in prompt | L0 stats / L1 list / L2 get + 5 retrieval modes (execute/reflect/explore/blank/debug) |
| **Memory — lifecycle** | None | No tiers, no demotion | No tiers | 4-tier evidence + probation + staleness auto-demote + archive-don't-delete |
| **Approval model** | Per-tool ask / always-allow / deny | Similar to CC | None | approval_queue + 2-day grace for non-system + hard confirm for system + write-admission gate |
| **Iteration / self-improvement** | ❌ deliberately none | ⚠️ subagent delegation only, no tournament | ⚠️ reflexion + dreaming, no variant tree | ⚠️ contract complete (I11), runtime absent |
| **Fitness signal** | ❌ | ❌ | ⚠️ reflexion score (pointwise) | ❌ planned — `llm_judge` skill next |
| **Fork / variant** | ❌ | ❌ | ❌ | ⚠️ I11 contract, runtime absent |
| **Session archive** | `~/.claude/projects/*/*.jsonl` | Own session store | Trajectory folder | Reuses CC's JSONL layout via `tools/archive_session.sh` |
| **Gateway / outbound** | None | Feishu / PDF / Slack plugins | None | `gateway` agent; outbound file send not yet ported |
| **Budget / quota** | None | None | None | `minimax_budget`: reservation floor + fair queue + 1002 retry |
| **Staleness / drift** | None | None | Dreaming bumps frequency, no demotion | Hard threshold demote (Slime-inspired) |
| **Admission gate** | None | None | None | 3-stage write-admission (ROLL-inspired: surface / golden_replay / noop_baseline) |

## yuxu-unique (none of the three do this)

- **Bus + Loader kernel** instead of monolithic loop. Cost: concurrency
  debugging is harder. Benefit: multi-agent evolves naturally,
  agent-to-agent is just event wiring.
- **Full 4-tier evidence lifecycle** (validated / consensus / observed
  / speculative) with probation, staleness, scoring. The three are all
  flat — a memory entry is either in or out.
- **Budget reservation floor**. CC / OpenClaw / Hermes rely on "stop
  when wallet empty"; yuxu reserves headroom for critical agents so
  one runaway can't starve the system.
- **Write-admission gate** with three-stage LLM / structural / dedup
  check. None of the three runs any automated quality filter between
  "the model proposed this" and "it lands in memory".

## Copied / heavily inspired

- **Session JSONL layout** — CC. Same `~/.claude/projects/<sanitized>/
  <uuid>.jsonl` convention; yuxu archives selectively via
  `tools/archive_session.sh`.
- **Lazy memory ops** — OpenClaw. `memory.list` / `get` / `search` ops
  match OC's tool shape, extended with mode filtering and stats.
- **Reflection → proposal → approval loop** — Hermes. yuxu adds the
  human approval step; Hermes writes directly to trajectory notes.
- **Slash commands as skills** — OpenClaw. yuxu treats both as the
  same bundled thing; routing layer stays aware it's two shapes
  (UNIFIED_AGENT_MODEL.md).
- **ROLL 3-layer verification → admission gate**. Direct port of ROLL's
  "false positive gate" into memory-write context; yuxu reframes as
  write-admission (feasible subset) while keeping the promotion-gate
  semantics as future extension.
- **Slime staleness concept → auto-demote runtime**. Concrete adaption:
  off-policy drift → evidence-tier demotion.

## Genuine greenfield (no reference to copy)

| Capability | CC | OC | Hermes | yuxu |
|---|---|---|---|---|
| Agent dynamically forking itself | ❌ | ❌ | ❌ | ❌ |
| Variant isolated execution | ❌ | ❌ | ⚠️ different configs | ❌ |
| Tournament / judge | ❌ | ❌ | ⚠️ pointwise reflexion only | ❌ |
| Outcome → memory attribution | ❌ | ❌ | ⚠️ writes to trajectory notes | ❌ |
| Replay / benchmark harness | ❌ | ❌ | ❌ | ❌ |

Hermes pointwise reflexion is the closest prior art for the next
piece (`llm_judge`); everything else on this row is original
engineering work.

## Deliberate divergences (not oversights)

- **CC refuses auto-extract memory; yuxu does it.** CC's stance: the
  user decides what to remember, memory edits are a UX choice. yuxu's
  iteration agent (future) needs a large-scale memory corpus that
  can't come from manual entry. We accept the automation-bias risk
  and gate it with admission_gate + approval_queue + probation.
- **OpenClaw uses SQLite; yuxu stays on markdown.** Per
  `feedback_mvp_discipline` — the SQLite switchover is at ~500
  entries. Current corpus is ~50. Premature migration would add
  schema ceremony for a non-problem.
- **Hermes freezes the memory snapshot for Anthropic prefix cache;
  yuxu doesn't.** Prompt caching is Anthropic-only; MiniMax (yuxu's
  main provider today) silently ignores `cache_control`. Implementing
  it now is dead code until a Claude backend lands. See
  `reference_impl_notes_3topics.md`.
- **CC has no run-mode taxonomy; yuxu has 5.** CC's single-session
  model works because everything is user-driven. yuxu's system agents
  (budget, monitor, recovery) need persistent lifecycles that an
  on-demand session model can't express.

## Gaps we know about but haven't fixed

- **Outbound file send in gateway** — OpenClaw has Feishu / PDF
  delivery. yuxu gateway can receive and route inbound but has no
  outbound send path for attachments. Tracked in
  `reference_openclaw_pdf_feishu.md`.
- **`memory.helped` / `hurt` writers** — Phase 4 shipped `applied`
  counter only. Actual reinforcement signals require
  iteration_agent's tournament outcome attribution.
- **Trajectory compressor** — Hermes does mid-run context trimming.
  yuxu doesn't; all agents rely on `llm_driver` defaults. Deferred
  per `reference_deferred_gaps.md`.
- **Request-id threading for cross-agent traces** — deferred; noted
  in `reference_deferred_gaps.md`.

## Summary for quick recall

**Two blocks yuxu leads on** (none of CC / OC / Hermes do this):
full memory lifecycle + event-driven multi-agent kernel.

**One block we're at the starting line with everyone else**:
iteration (fork + tournament + judge).

**No block we're lazily behind on**: every gap traces to a deliberate
sequencing decision, not an oversight.
