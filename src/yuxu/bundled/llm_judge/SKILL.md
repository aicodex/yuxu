---
name: llm_judge
version: "0.1.0"
author: yuxu
license: MIT
description: LLM-as-judge for comparing or scoring agent outputs. `compare` (pairwise, tournament primitive for iteration_agent) returns a winner plus per-dimension breakdown; `score` (pointwise, Hermes reflexion-style) returns 0.0-1.0 with labeled anchors. Both do JSON-mode / temp 0.0 / single-turn with optional multi-vote aggregation. Fallback to Jaccard-overlap heuristic when the LLM is unavailable or returns unparseable output — degraded with `confidence=0.3, fallback_used=true` rather than a useless tie.
triggers: [compare outputs, pairwise judge, score candidate, pointwise judge, tournament]
parameters:
  type: object
  required: [op]
  properties:
    op:
      type: string
      enum: [compare, score]
      description: "`compare` — pairwise A/B decision with order randomization and per-dim winners. `score` — pointwise 0.0-1.0 with anchor labels and per-dim scores."
    candidates:
      type: array
      description: For `compare` — exactly two `{id, body}` objects. Ids are free-form labels (e.g. `"variant_1"` / `"live"`).
      items: {type: object}
    candidate:
      type: string
      description: For `score` — the single output to rate.
    task:
      type: string
      description: What the candidates were asked to do. Included in the judge prompt; absent → "task not specified".
    rubric:
      type: string
      description: Evaluation guidance injected into the judge system prompt. Overrides the default generic rubric.
    dimensions:
      type: array
      items: {type: string}
      description: Axes to evaluate. Default `["correctness", "specificity", "actionability"]`. Caller can override per call (e.g. memory-entry judge might use `["specificity", "novelty", "scope"]`).
    n_votes:
      type: integer
      description: Number of parallel judge calls to aggregate. Default 1 (cheap). `n_votes >= 3` enables majority vote / variance-based confidence — spend this on high-stakes comparisons.
    randomize_order:
      type: boolean
      description: For `compare` — flip A/B per vote to mitigate positional bias. Default true.
    seed:
      type: integer
      description: Seed for order randomization; tests need determinism.
    pool:
      type: string
      description: llm_driver pool override. Defaults to `LLM_JUDGE_POOL` env.
    model:
      type: string
      description: llm_driver model override. Defaults to `LLM_JUDGE_MODEL` env.
    fallback_enabled:
      type: boolean
      description: Allow Jaccard-overlap fallback when the LLM path fails. Default true. Disable for pure LLM tests.
---
# llm_judge

Stateless judge skill. Two ops, one degraded-mode backstop.

## Op `compare` (pairwise, main tournament primitive)

```
input:  candidates=[{id:"a", body:"..."}, {id:"b", body:"..."}], task?, rubric?,
        dimensions?, n_votes?, randomize_order?, seed?
output: {
  ok: true,
  winner: "a" | "b" | "tie",
  confidence: float,        # majority fraction ∈ [0, 1]
  per_dimension: {
    "<dim>": {"winner": "a"|"b"|"tie", "margin": float}
  },
  votes: [{"order": "ab"|"ba", "winner", "per_dimension", "reason", "raw"}],
  summary: str,
  fallback_used: bool
}
```

Prompt-level bias mitigation:
- Explicit "length is NOT quality" instruction — Hermes/OC observation
- "Tie is a valid verdict, do not force a pick" — addresses enum-preference bias
  documented in `feedback_llm_agent_design_reality.md`
- Per-dimension evaluation is separate from overall — forces the judge to show
  work rather than pattern-match to one signal

Order randomization: default-on, per-vote flip, seeded for tests.

## Op `score` (pointwise, Hermes reflexion-style)

```
input:  candidate="...", task?, rubric?, dimensions?, n_votes?
output: {
  ok: true,
  score: float,             # 0.0..1.0, mean across votes
  anchor_label: str,        # from Hermes labeled scale
  confidence: float,        # 1 - (stddev / mean), clamped [0, 1]
  per_dimension: {"<dim>": float (0..1)},
  votes: [{"score", "per_dimension", "reason", "raw"}],
  summary: str,
  fallback_used: bool
}
```

Anchor scale (ported from Hermes `web_research_env.py:623-636`):

| Score | Label |
|------:|:------|
| 1.0   | fully correct |
| 0.7   | mostly correct |
| 0.4   | partially correct |
| 0.1   | mentions topic |
| 0.0   | incorrect / irrelevant |

## Fallback (both ops)

When `fallback_enabled=true` (default) and the LLM path fails (LookupError,
llm_driver raise, unparseable JSON): run a Jaccard-overlap heuristic between
the candidate text and the task text. Returns the degraded verdict with
`confidence=0.3` and `fallback_used=true`. Beats returning `winner=tie,
confidence=0.0` because downstream (iteration_agent tournament) can still
make progress while flagging low trust.

## Events

`llm_judge.verdict` fires on every call with `{op, winner|score, confidence,
fallback_used, vote_count, dimensions}`. I10: judge quality will itself be
scored once iteration_agent attributes tournament outcomes back to
judge decisions.

## What this skill does NOT do (v0)

- Listwise (3+ candidates). Callers do Swiss-bracket pairwise instead.
- Preset rubrics per task type (memory-entry / prompt-variant / agent-output).
  Caller injects `rubric` / `dimensions` per use case — keeps the skill
  policy-free per I2.
- Ensemble across different judge models. `n_votes` varies only seed + a
  small temp jitter within one model.
- Caching. MiniMax ignores `cache_control`; revisit once a Claude pool lands.
