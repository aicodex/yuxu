---
name: reference-driven-design
category: design
discovered_on: 2026-04-24
status: validated
applied_count: 6+
success_count: 5
notes: |
  Discovered while iterating on yuxu ARCHITECTURE + memory infra on
  2026-04-24. Every time we proposed a mechanism without first reading
  Claude Code / OpenClaw / Hermes source, we later found we had to
  backtrack. Every time we DID read the source first, we caught one or
  more blind spots and shipped a tighter design.
---

# Workflow: Reference-driven design

## When to apply

Any time yuxu is about to introduce a new mechanism, abstraction, or
architectural decision. Especially when the prompt is:

- "let's add feature X" (might exist in references already)
- "let's define Y" (references may have spent years refining Y)
- "we need a new Z" (probably not — check first)

**NOT for**: trivial refactors, bug fixes, or extending established patterns
within yuxu itself.

## The procedure

1. **Grep yuxu's own memory + docs first**
   - `ARCHITECTURE.md` / `AGENT_GUIDE.md` / `subscription_model.md`
   - `~/.claude/projects/-home-xzp-project-theme-flow-engine/memory/`
   - Existing `bundled/*` agents (someone may have solved this already
     in yuxu; reuse > new)

2. **Read reference implementations** — not their summaries, not their
   README, their source:
   - Claude Code (`/home/xzp/project/best_agent/claude-code-2.1.88-src/`)
   - OpenClaw (`/home/xzp/project/best_agent/openclaw/`)
   - Hermes (`/home/xzp/project/best_agent/hermes-agent/`)
   Dispatch one `Explore` subagent per reference with a focused
   question set. Do NOT just ask for a summary; ask for source paths,
   line numbers, and actual code fragments.

3. **Synthesize explicitly**:
   - "CC does X; OpenClaw does Y; our case is Z."
   - "We should adopt X because (evidence)."
   - "We should skip Y because (reason)."
   State citations, not intuition.

4. **Propose text + location + options** to the user before editing.
   Include the conflict check (does this collide with any existing
   yuxu principle? any reference's practice?).

5. **On sign-off, execute** — edit, run tests, commit with explicit
   citations in the message (cite the CC/OpenClaw files you borrowed
   from).

## What this prevents

- Reinventing mechanisms that CC / OpenClaw already ship (e.g.
  proposing ad-hoc "env var switches" when `subscription_model.md`
  already designs the exact mechanism)
- Proposing absolute definitions ("memory is X") when references
  already demonstrated the concept has fuzzy edges
- Removing capability that looks redundant today but is proven
  useful in production references (e.g. the "global scope" saga:
  seemed unneeded until we realized CC's ships empty-but-reserved)

## The two-sided rule

- Before proposing → read references.
- Before agreeing with a reference → verify it actually does what
  the summary says (e.g. I had to correct myself twice on CC: first
  missing `/compact`, then mischaracterizing "CC doesn't compress").

References are **seeds, not oracles**. Validate claims against the
actual code each time.

## See also

- `methodologies/adding-architecture-principle.md` — the meta procedure
  that embeds this workflow as step 2 ("research references")
- `../../feedback_reuse_designs_first.md` (memory) — the prior
  feedback rule this formalizes
- `../../feedback_don_t_work_in_isolation.md` (memory) + AGENT_GUIDE
  Principle — the same idea at principle level
