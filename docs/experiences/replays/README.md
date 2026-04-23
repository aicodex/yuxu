# Replay Corpus — regression benchmarks for the iteration agent

Hand-captured units from real sessions where the model (me, Claude) was
asked to do architectural work and got something **wrong or suboptimal**
first, then was pushed by the user to improve. Each file is a **test case**
the iteration agent will eventually run: given the same context + same
user challenge, can it do better than I did?

## Why capture these (not just the happy path)

The failures are the signal. Happy-path transcripts teach little — the
agent already got them right. **Challenge-correction loops** teach
everything, because they expose:

- What knowledge I was missing (reference docs I skipped)
- What shortcut I took (minimum-impl reflex, ignoring existing design)
- What memory I forgot (we discussed this two messages ago; I didn't
  carry it forward)
- What tool I didn't use (Grep over memory before proposing)

These are exactly the regressions a future agent might make. Capturing
them means we can test that a future iteration-agent variant doesn't
repeat them.

## File format

Each case is one markdown file. Filename: `YYYY-MM-DD-<slug>.md`.

Structure (all sections required):

```markdown
---
case_id: <stable-kebab>
date: YYYY-MM-DD
topic: <short>
difficulty: <low|medium|high>
tags: [architecture, reference-research, memory, ...]
my_score: <my performance on this case, 0-10>
---

## Context
What was the state going into the exchange — what ARCHITECTURE said,
what memory existed, what was just decided.

## User challenge
The user's verbatim (or close-paraphrase) message that exposed my gap.

## My (suboptimal) response
What I actually did. Be honest — include the wrong conclusion.

## What went wrong
Concrete: a step I skipped, a tool I didn't use, an inference I made
without checking.

## How the user corrected me
What pushed me to re-examine.

## Ideal response
What the response SHOULD have been given the same inputs.

## Applied learnings
- pointers to workflows / methodologies / feedback memories that, if
  followed, would have caught this.

## Regression signal
"If a future agent replicates my mistake, the symptom is: ..."
Keywords the evaluator can detect.
```

## How the evaluator will work (future)

When the iteration agent ships, an eval harness will:

1. Load each replay case.
2. Construct the minimal context (relevant ARCHITECTURE sections,
   relevant prior memory).
3. Feed the user challenge.
4. Capture the agent's response.
5. Compare: does the response exhibit any of the **Regression signal**
   symptoms? Does it apply the **Applied learnings**?
6. Score relative to `my_score` (the baseline set by me). Agent
   passes iff its score > mine.

Not built yet — these files sit inert until the eval harness lands.
Meanwhile they're useful human reading ("don't repeat these mistakes").

## Curation rules

- Only capture real cases. Do not invent hypotheticals.
- Be honest about your own mistakes. Softening the "what went wrong"
  section wastes the regression signal.
- One case = one failure mode. If a session had three distinct slip-
  ups, write three files.
- Keep context tight — the minimum needed to reproduce the decision.
  Not the whole session dump.
