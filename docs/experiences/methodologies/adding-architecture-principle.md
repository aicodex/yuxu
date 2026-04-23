---
name: adding-architecture-principle
category: methodology
discovered_on: 2026-04-24
status: validated
applied_count: 10+
success_count: 9
failure_count: 1  # initially removed global scope; had to revive
notes: |
  Distilled from 2026-04-24 session where user fed ~8 new architecture-
  level principles in one session. The procedure below is what actually
  worked (once I remembered to follow it — several times I skipped
  steps and paid for it).
---

# Methodology: Adding an architecture-level principle

When the user makes an invariant-shape statement ("all X are Y",
"the only limit is Z", "agents must …"), this is the procedure to run
before changing any file.

## Inputs

- A user utterance that looks like an invariant.
- Current state of `docs/ARCHITECTURE.md`, `docs/AGENT_GUIDE.md`,
  and the memory folder at
  `~/.claude/projects/-home-xzp-project-theme-flow-engine/memory/`.

## Procedure

### 1. Detect whether it IS architecture-level

Trigger words: **所有 / 唯一 / 必须 / 本质 / 基本 / 原则 / 不能 / 要求 /
always / never / must / only / essentially**. Also anything that reads
like a taxonomy ("two kinds of …"), a budget rule ("bounded by …"), or
a criterion of evaluation ("X is measured by …").

If it's tactical ("let's format this reply as …"), execute directly.
Don't bill small things as architecture.

### 2. Conflict check (mandatory)

Tools: **Grep** over the three stores:

```
- docs/ARCHITECTURE.md              (I1–In + product principles)
- docs/AGENT_GUIDE.md                (Principles section)
- ~/.claude/projects/.../memory/     (feedback_*.md files)
```

Question to answer for each hit: "does my new principle subsume it,
contradict it, or complement it?"

- **Subsumes**: existing rule becomes redundant → propose deletion along
  with addition.
- **Contradicts**: surface explicitly. Do NOT quietly pick a winner.
  "You say A (new), existing I-n says B (old). Which do you want?"
- **Complements**: add as a new distinct rule.

### 3. Reference check (optional but strongly recommended)

Tools: **Agent(subagent_type=Explore)** dispatched in parallel to
`/home/xzp/project/best_agent/claude-code-2.1.88-src/` and
`/home/xzp/project/best_agent/openclaw/`. Question format:

> "Does CC/OpenClaw have anything resembling this rule? Give source
> paths + line numbers + actual code fragments. Don't give me a
> summary — give me the primary evidence."

Skip if the rule is purely yuxu-specific.

### 4. Decide level + placement

Two homes:

| Home | When |
|---|---|
| `ARCHITECTURE.md` invariant (`I-n`) | Statement is a load-bearing fact about the system (what it is, not how to work with it). |
| `AGENT_GUIDE.md` Principles section | Operational guidance for humans/LLMs when creating or modifying agents. |

Some rules get **both** (e.g. I10 practice-criterion → new invariant +
extended AGENT_GUIDE principle that now covers principles themselves).

### 5. Propose text + options

Draft the exact words. Show the user the target file + the exact
paragraph. Give **3-4 options**, including "change wording", "change
location", "don't add it", "add it but with a narrower scope". This
is the sign-off gate — do not edit until the user picks.

### 6. Execute

Tools: **Edit** (not Write — preserves surrounding context).
If adding to ARCHITECTURE: match the existing I-n heading style.
If adding to AGENT_GUIDE: match the existing bullet + **bold term**
pattern in the Principles list.

### 7. Verify injection chain

Tools: **Bash** (`python -m pytest tests/test_core_principles.py`).
The injection mechanism is in `src/yuxu/core/principles.py` which
loads both files and appends them to the system prompts of
`bundled/generate_agent_md` and `bundled/classify_intent`. Tests
confirm the appended text contains the new content.

### 8. Commit with citations

Git commit message must:
- Quote the principle verbatim.
- If ported from CC / OpenClaw, cite source path + line.
- If it conflicts with a previous decision, reference the reversed
  commit.
- Follow the `via Happy / Co-Authored-By Claude + Happy` footer per
  the session's CLAUDE.md convention.

### 9. Bump submodule

Tools: **Bash** from the outer repo:
```
cd /home/xzp/project/theme-flow-engine
git add yuxu && git commit -m "chore: bump yuxu submodule (<principle name>)"
```

### 10. Watch for your own violations

Later in the same session or the next, when you notice you're about
to violate the principle you just added, **stop**. Flag it to the
user with "I'm about to violate I-n; the escape hatch is X". This
is often how principles get refined in practice (we added, then a
real case surfaced a gap, then we updated).

## Tools inventory (this session's practice)

- **Read** — inspect existing files before editing.
- **Edit** — surgical edits to ARCHITECTURE / AGENT_GUIDE.
- **Write** — new files (mostly for `docs/experiences/`, new principles
  sometimes get their own memory file).
- **Grep** — conflict search over docs + memory.
- **Agent(subagent_type=Explore)** — parallel reference research into
  CC / OpenClaw / Hermes. Explicit prompts asking for source
  citations, NOT summaries.
- **Bash** — `pytest`, `git`, `find`.
- **TodoWrite** — for multi-step principle additions with >3 edits.

## Methods inventory

- **Flag-before-execute** (rule `feedback_flag_architecture_statements`)
- **Conflict-check-over-subsume-contradict-complement** (this doc step 2)
- **Reference-driven design** (`workflows/reference-driven-design.md`)
- **Options-based sign-off** (each architectural edit goes through A/B/C/D
  options; user picks). Why: prevents silent drift.
- **Empty-slot reservation** (example: global scope in I6 — "reserve, don't
  remove"). Inspired by CC's empty `~/.claude/CLAUDE.md`.
- **De-formalization** (example: I6 refusing to define "memory" — too
  many fuzzy edges; define "where to change X" instead).
- **Asymmetric scoring** (I10 + AGENT_GUIDE: +5 success, −1 failure).
  Applies to principles themselves: a rule that consistently fails
  to predict good outcomes gets retired.

## Anti-patterns (what I got wrong this session)

1. **Minimum-impl reflex** — proposed env var switches for
   reasoning/tool_use instead of reusing the subscription model that
   was already designed. The fix: step 1 conflict check MUST be
   executed mechanically, not skipped when tired.

2. **Premature removal** — removed global scope from I6 based on one
   subagent's "CC default empty" reading; user corrected with
   evidence from their own CLAUDE.md (non-empty, full of meta-rules).
   The fix: "default empty" ≠ "not useful"; reserve slot, don't delete
   taxonomy.

3. **Reductive reference claims** — claimed "CC doesn't compress"
   based on checking only `memdir.ts` (truncation). User pointed out
   `/compact`, I researched again and found three independent
   compression layers. The fix: ask subagents with specific
   exhaustive questions ("list ALL compression mechanisms"), not
   leading ones ("is there compression here?").

## See also

- `workflows/reference-driven-design.md` — the design-time workflow
  this method embeds.
- `../../../CLAUDE.md` files (global, project, yuxu) — the
  environment this method operates in.
- `src/yuxu/core/principles.py` — the injection mechanism (code-level
  detail of step 7).
