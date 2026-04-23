---
case_id: memory-scoping-global-revive
date: 2026-04-24
topic: I6 scope taxonomy — whether to keep a global scope
difficulty: medium
tags: [architecture, memory, reference-research, dogfood-drives, empty-slot]
my_score: 4
---

## Context

Working on `docs/ARCHITECTURE.md` I6 (Three/Four-layer scope for
behavior-shaping data). In this session we had already:

1. Rewritten I6 once from "Three-layer memory" to "Four-layer scope
   for behavior-shaping data" with explicit Global / Project / Agent /
   Session layers, per user guidance.
2. Then I dispatched an Explore subagent on "is CC's global layer
   really needed?" The subagent reported: CC's `~/.claude/CLAUDE.md`
   defaults empty, system works without it. I concluded: "global
   scope is speculative, unused by any bundled agent, should be
   removed per dogfood-drives principle."
3. User approved option A ("drop global"), and I committed the
   removal. I6 became 3-layer.

Relevant principles already in place:
- AGENT_GUIDE Principle: "Dogfood drives" (don't pre-build what
  isn't needed)
- ARCHITECTURE I10: "Practice is the criterion for trustworthiness"
- Memory: `feedback_reuse_designs_first.md` (check references before
  proposing)

## User challenge

> 所以我们需要的是一个类似claude.md的吧。你要不看看现在，你的claude.md是空的吗？

Translation: "So we need something like CLAUDE.md, right? Why don't
you look at yours now — is it empty?"

## My (suboptimal) response

Before the challenge: I had removed the global scope based on
"CC default-empty = not needed" logic.

After the challenge: I Read all three CLAUDE.md files. All three were
populated (log-analysis rule, gstack skill list, gstack install
verification). I realized my prior conclusion was "one-sided": the
subagent summary "default empty" was about CC's ship state, not
about how active users use the slot.

I then proposed (correctly) reviving the global scope with explicit
"reserved empty slot" framing + write-path documentation modeled on
CC's `/memory` command and extractMemories.

## What went wrong

**Core error**: I treated "ships empty" as equivalent to "has no
purpose." These are different claims:

- "Ships empty" = true descriptive fact (CC's default)
- "Has no purpose" = false normative conclusion (users fill it heavily)

**Skipped step**: I did not check the user's *actual* CLAUDE.md files
before recommending removal. An extra 3 Read calls would have exposed
the gap immediately.

**Root cause**: The subagent research was leading ("is global layer
really needed?"). The framing biased toward "no" and I accepted the
first evidence that matched. I should have asked the opposite-direction
question too: "what DO active users typically put in CLAUDE.md?"

Also: I had just written AGENT_GUIDE's "Less is more, but
change-locality beats count" principle in the same session. One
phrasing there was "don't worship minimum count." I effectively did
worship it here, driven by dogfood-drives enthusiasm.

## How the user corrected me

With a single pointed rhetorical question: look at your own
CLAUDE.md, is it empty? The existence of populated CLAUDE.md files
on the user's own machine was instant, overwhelming counter-evidence.

## Ideal response

When the subagent returned "CC ships empty", I should have:

1. **Re-read my subagent prompt** — noticed it was leading.
2. **Check the real-world state** — ~/.claude/CLAUDE.md is right
   there, one Read away. If it's populated with non-trivial content,
   that already answers the question.
3. **Distinguished two hypotheses**:
   - H1: "CC users don't write global memory" (refuted by the real file)
   - H2: "CC's system prompt doesn't DEPEND on global memory"
     (supported by memdir.ts returning `''` on missing file)
4. **Concluded with the right framing**: "Global scope in CC is not
   *required* infrastructure, but it IS heavily used by active users.
   An empty slot is free to carry; removing the slot creates a
   future migration problem."
5. **Proposed** keeping global but marking it "reserved empty slot"
   with write paths modeled on CC's `/memory` command.

This is the proposal we ended up with after the correction. I
should have arrived at it in the first place.

## Applied learnings

- `workflows/reference-driven-design.md` step: "verify claims against
  actual code each time — references are seeds, not oracles." My
  subagent gave a partial truth; I needed to check the other half.
- `methodologies/adding-architecture-principle.md` step 2: conflict
  check. Removing global conflicts with the *user's actual practice*
  (their CLAUDE.md), not just with the codebase. Practice is also
  a form of evidence.
- `feedback_reuse_designs_first.md`: "reuse existing designs." CC's
  6 MemoryTypes are an existing design. I replaced it with 3 layers.
  Should have extended, not replaced.
- I10: practice criterion. "Used by power users" is practice.
  I took theoretical argument ("dogfood says skip") over empirical
  evidence ("users fill it").

## Regression signal

- Proposes removing a capability based on "default unused" without
  checking user-populated files.
- Takes one subagent's leading-question answer as conclusive.
- Prioritizes "less-is-more" theorem over observed usage.
- Fails to distinguish "not required" from "not valuable."

If a future agent gets this kind of challenge from the user, the
correct first tool call is **Read** on the actual state files. If
it reaches for **Edit** to remove capability before that Read, it
has regressed.
