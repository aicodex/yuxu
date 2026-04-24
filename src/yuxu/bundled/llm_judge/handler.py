"""llm_judge — pairwise / pointwise LLM judge for iteration_agent and friends.

Design principles:
- I1/I2  skill (stateless), framework gives mechanism, caller supplies policy
  (rubric, dimensions, pool/model)
- I10    verdicts are published so future iteration_agent can score the judge
         itself from observed outcomes
- bias   prompt tells the LLM to ignore length bias and to use `tie` when
         genuinely undecidable (addresses enum-preference bias observed in
         `feedback_llm_agent_design_reality.md`); order is randomized by
         default in compare to mitigate A/B positional preference
- graceful degradation: Jaccard-overlap fallback instead of useless `tie`
  with confidence 0 when the LLM path fails

References:
- Hermes `web_research_env.py` judge/reward computation — pointwise + labels
- ROLL / admission_gate pattern — infra gap (no llm_driver) passes through
  with `fallback_used=true` rather than crashing the caller
"""
from __future__ import annotations

import json
import logging
import math
import os
import random
import re
from collections import Counter
from statistics import mean, pstdev
from typing import Any, Optional

log = logging.getLogger(__name__)

NAME = "llm_judge"

VERDICT_TOPIC = "llm_judge.verdict"

DEFAULT_DIMENSIONS = ("correctness", "specificity", "actionability")
DEFAULT_N_VOTES = 1

# Hermes anchor scale (web_research_env.py:623-636).
ANCHOR_THRESHOLDS = (
    (0.85, "fully correct"),
    (0.55, "mostly correct"),
    (0.25, "partially correct"),
    (0.05, "mentions topic"),
    (0.0, "incorrect"),
)

COMPARE_SYSTEM = """You are a judge for the yuxu agent framework. You compare
two candidate outputs (A and B) and decide which better solves the task, on
specific dimensions and overall.

Principles (ignore at your peril):
- Length is NOT quality. Do not prefer the longer candidate by default.
- "tie" is a VALID and often-correct verdict. Do not force a winner when the
  candidates are genuinely indistinguishable on a dimension.
- overall.winner may differ from individual dimension winners if dimensions
  conflict — name the tradeoff in overall.reason.
- Judge the content, not the style. Bullet lists and prose are equivalent.

Output STRICT JSON, no prose, no markdown fence:

{
  "per_dimension": {
    "<dim_name>": {
      "winner": "a" | "b" | "tie",
      "margin": <float 0.0-1.0, 0 = tie, 1 = one dominates>,
      "reason": "<one sentence>"
    }
  },
  "overall": {
    "winner": "a" | "b" | "tie",
    "reason": "<one sentence>"
  }
}"""


SCORE_SYSTEM = """You are a judge for the yuxu agent framework. Score a
single candidate on a 0.0-1.0 scale, per dimension and overall.

Anchor scale (apply to each dimension and to overall):
- 1.0 = fully correct / excellent
- 0.7 = mostly correct with minor gaps
- 0.4 = partially correct with significant gaps
- 0.1 = mentions topic but mostly wrong
- 0.0 = incorrect / irrelevant / empty

Principles:
- Length is NOT quality.
- Do not inflate scores to be polite. 0.4 is a normal verdict.
- Judge the content, not the style.

Output STRICT JSON, no prose, no markdown fence:

{
  "per_dimension": {
    "<dim_name>": {
      "score": <float 0.0-1.0>,
      "reason": "<one sentence>"
    }
  },
  "overall": {
    "score": <float 0.0-1.0>,
    "reason": "<one sentence>"
  }
}"""


# -- utilities --------------------------------------------------


_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")


def _extract_json(text: str) -> Optional[Any]:
    if not text or not isinstance(text, str):
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_BLOCK.search(text)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall((text or "").lower()))


def _ngrams(text: str, n: int = 3) -> set[str]:
    """Char-trigrams, whitespace-stripped — works for en/zh mixed text."""
    s = re.sub(r"\s+", "", (text or "").lower())
    if len(s) < n:
        return {s} if s else set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _anchor_label(score: float) -> str:
    s = max(0.0, min(1.0, float(score)))
    for thresh, label in ANCHOR_THRESHOLDS:
        if s >= thresh:
            return label
    return "incorrect"


def _normalize_dimensions(dims) -> list[str]:
    if isinstance(dims, (list, tuple)) and dims:
        out: list[str] = []
        for d in dims:
            if isinstance(d, str) and d.strip():
                out.append(d.strip())
        if out:
            return out
    return list(DEFAULT_DIMENSIONS)


# -- compare ---------------------------------------------------


def _compare_user_prompt(task: str, rubric: str, dimensions: list[str],
                          a_body: str, b_body: str) -> str:
    dims_str = ", ".join(dimensions)
    parts = [
        f"TASK: {task or 'task not specified'}",
        f"RUBRIC: {rubric or 'default (see system prompt principles)'}",
        f"DIMENSIONS TO EVALUATE: {dims_str}",
        "",
        "CANDIDATE A:",
        (a_body or "").strip()[:8000],
        "",
        "CANDIDATE B:",
        (b_body or "").strip()[:8000],
    ]
    return "\n".join(parts)


def _parse_compare_vote(content: str, dimensions: list[str]) -> Optional[dict]:
    obj = _extract_json(content or "")
    if not isinstance(obj, dict):
        return None
    per_dim_raw = obj.get("per_dimension") or {}
    overall = obj.get("overall") or {}
    if not isinstance(per_dim_raw, dict) or not isinstance(overall, dict):
        return None
    per_dim: dict[str, dict] = {}
    for dim in dimensions:
        entry = per_dim_raw.get(dim) or {}
        if not isinstance(entry, dict):
            continue
        w = entry.get("winner")
        if w not in ("a", "b", "tie"):
            continue
        try:
            margin = float(entry.get("margin", 0.0))
        except (TypeError, ValueError):
            margin = 0.0
        margin = max(0.0, min(1.0, margin))
        per_dim[dim] = {
            "winner": w,
            "margin": margin,
            "reason": str(entry.get("reason") or "")[:400],
        }
    ow = overall.get("winner")
    if ow not in ("a", "b", "tie"):
        return None
    return {
        "winner": ow,
        "per_dimension": per_dim,
        "reason": str(overall.get("reason") or "")[:400],
    }


async def _single_compare_call(ctx, *, system: str, user: str,
                                  pool: Optional[str], model: Optional[str],
                                  temperature: float) -> tuple[Optional[dict], str]:
    """One LLM call. Returns (parsed_or_None, raw_content)."""
    try:
        resp = await ctx.bus.request("llm_driver", {
            "op": "run_turn",
            "system_prompt": system,
            "messages": [{"role": "user", "content": user}],
            "pool": pool, "model": model,
            "temperature": temperature,
            "json_mode": True,
            "max_iterations": 1,
            "strip_thinking_blocks": True,
            "llm_timeout": 60.0,
        }, timeout=90.0)
    except LookupError:
        return None, "<llm_driver_not_loaded>"
    except Exception as e:
        log.warning("llm_judge: llm_driver raised: %s", e)
        return None, f"<llm_driver_raised: {e}>"
    if not isinstance(resp, dict) or not resp.get("ok"):
        err = resp.get("error") if isinstance(resp, dict) else "non-dict"
        return None, f"<llm_driver_not_ok: {err}>"
    return resp, resp.get("content") or ""


def _compare_fallback(a_body: str, b_body: str, task: str) -> dict:
    """Jaccard-overlap heuristic when LLM fails. Not great, but not nothing."""
    task_tokens = _tokens(task)
    if not task_tokens:
        # Without a task we can't score relative to anything — declare tie.
        return {"winner": "tie", "per_dimension": {}, "margin": 0.0}
    a_overlap = _jaccard(_tokens(a_body), task_tokens)
    b_overlap = _jaccard(_tokens(b_body), task_tokens)
    diff = a_overlap - b_overlap
    if abs(diff) < 0.05:
        return {"winner": "tie", "per_dimension": {}, "margin": abs(diff)}
    return {"winner": "a" if diff > 0 else "b",
            "per_dimension": {}, "margin": min(1.0, abs(diff) * 5)}


def _aggregate_compare(votes: list[dict], dimensions: list[str]) -> dict:
    """Majority vote for overall; per-dim majority + avg margin."""
    overall_counts: Counter = Counter(v["winner"] for v in votes)
    if not overall_counts:
        return {"winner": "tie", "confidence": 0.0, "per_dimension": {}}
    total = sum(overall_counts.values())
    top_winner, top_count = overall_counts.most_common(1)[0]
    # If there's a tie between two winners, declare tie.
    second = overall_counts.most_common(2)[1][1] if len(overall_counts) > 1 else 0
    if second == top_count and top_winner != "tie":
        winner = "tie"
    else:
        winner = top_winner
    confidence = top_count / total if total else 0.0

    per_dim_agg: dict[str, dict] = {}
    for dim in dimensions:
        winners: list[str] = []
        margins: list[float] = []
        for v in votes:
            d = v.get("per_dimension", {}).get(dim)
            if not isinstance(d, dict):
                continue
            winners.append(d.get("winner", "tie"))
            margins.append(float(d.get("margin", 0.0)))
        if not winners:
            continue
        c = Counter(winners)
        top_w, _ = c.most_common(1)[0]
        per_dim_agg[dim] = {
            "winner": top_w,
            "margin": round(mean(margins), 3) if margins else 0.0,
        }
    return {"winner": winner, "confidence": round(confidence, 3),
            "per_dimension": per_dim_agg}


async def _op_compare(input: dict, ctx) -> dict:
    cands = input.get("candidates")
    if not isinstance(cands, list) or len(cands) != 2:
        return {"ok": False, "error": "compare requires exactly 2 candidates"}
    a, b = cands[0], cands[1]
    if not (isinstance(a, dict) and isinstance(b, dict)):
        return {"ok": False, "error": "candidates must be dicts"}
    a_id = str(a.get("id") or "a")
    b_id = str(b.get("id") or "b")
    a_body = a.get("body")
    b_body = b.get("body")
    if not (isinstance(a_body, str) and a_body.strip()
            and isinstance(b_body, str) and b_body.strip()):
        return {"ok": False, "error": "candidate bodies must be non-empty"}

    task = str(input.get("task") or "")
    rubric = str(input.get("rubric") or "")
    dimensions = _normalize_dimensions(input.get("dimensions"))
    n_votes = max(1, int(input.get("n_votes", DEFAULT_N_VOTES) or DEFAULT_N_VOTES))
    randomize_order = bool(input.get("randomize_order", True))
    seed = input.get("seed")
    fallback_enabled = bool(input.get("fallback_enabled", True))
    pool = input.get("pool") or os.environ.get("LLM_JUDGE_POOL")
    model = input.get("model") or os.environ.get("LLM_JUDGE_MODEL")

    rng = random.Random(seed)
    votes: list[dict] = []
    fallback_used = False

    for i in range(n_votes):
        flip = randomize_order and rng.random() < 0.5
        order_str = "ba" if flip else "ab"
        if flip:
            ua, ub = b_body, a_body
        else:
            ua, ub = a_body, b_body
        temp = 0.0 if i == 0 else min(0.3, 0.05 * i)
        user = _compare_user_prompt(task, rubric, dimensions, ua, ub)
        parsed_resp, raw = await _single_compare_call(
            ctx, system=COMPARE_SYSTEM, user=user,
            pool=pool, model=model, temperature=temp)
        if parsed_resp is None:
            continue
        v = _parse_compare_vote(raw, dimensions)
        if v is None:
            continue
        # If order was flipped, swap winner labels back to original A/B.
        if flip:
            v["winner"] = {"a": "b", "b": "a", "tie": "tie"}[v["winner"]]
            v["per_dimension"] = {
                d: {**r,
                    "winner": {"a": "b", "b": "a",
                               "tie": "tie"}[r.get("winner", "tie")]}
                for d, r in v["per_dimension"].items()
            }
        votes.append({"order": order_str,
                       "winner": v["winner"],
                       "per_dimension": v["per_dimension"],
                       "reason": v["reason"],
                       "raw": raw[:1000]})

    if not votes:
        if not fallback_enabled:
            return {"ok": False, "error": "no usable LLM votes and fallback disabled"}
        fb = _compare_fallback(a_body, b_body, task)
        result = {
            "ok": True,
            "winner": fb["winner"],
            "confidence": 0.3,
            "per_dimension": {},
            "votes": [],
            "summary": f"fallback: jaccard overlap, diff={fb['margin']:.2f}",
            "fallback_used": True,
            "candidate_ids": {"a": a_id, "b": b_id},
        }
        await _publish_verdict(ctx, "compare", result)
        return result

    agg = _aggregate_compare(votes, dimensions)
    summary_parts = [f"{v_['order']}→{v_['winner']}" for v_ in votes]
    result = {
        "ok": True,
        "winner": agg["winner"],
        "confidence": agg["confidence"],
        "per_dimension": agg["per_dimension"],
        "votes": votes,
        "summary": (f"winner={agg['winner']}, "
                     f"votes=[{', '.join(summary_parts)}]"),
        "fallback_used": fallback_used,
        "candidate_ids": {"a": a_id, "b": b_id},
    }
    await _publish_verdict(ctx, "compare", result)
    return result


# -- score -----------------------------------------------------


def _score_user_prompt(task: str, rubric: str, dimensions: list[str],
                        candidate: str) -> str:
    dims_str = ", ".join(dimensions)
    parts = [
        f"TASK: {task or 'task not specified'}",
        f"RUBRIC: {rubric or 'default (see system prompt anchor scale)'}",
        f"DIMENSIONS TO EVALUATE: {dims_str}",
        "",
        "CANDIDATE:",
        (candidate or "").strip()[:10000],
    ]
    return "\n".join(parts)


def _parse_score_vote(content: str, dimensions: list[str]) -> Optional[dict]:
    obj = _extract_json(content or "")
    if not isinstance(obj, dict):
        return None
    per_dim_raw = obj.get("per_dimension") or {}
    overall = obj.get("overall") or {}
    if not isinstance(per_dim_raw, dict) or not isinstance(overall, dict):
        return None
    per_dim: dict[str, float] = {}
    for dim in dimensions:
        entry = per_dim_raw.get(dim) or {}
        if not isinstance(entry, dict):
            continue
        try:
            s = float(entry.get("score", 0.0))
        except (TypeError, ValueError):
            continue
        per_dim[dim] = max(0.0, min(1.0, s))
    try:
        overall_s = float(overall.get("score", 0.0))
    except (TypeError, ValueError):
        return None
    overall_s = max(0.0, min(1.0, overall_s))
    return {
        "score": overall_s,
        "per_dimension": per_dim,
        "reason": str(overall.get("reason") or "")[:400],
    }


def _score_fallback(candidate: str, task: str) -> dict:
    """Keyword overlap → very coarse 0-1 score."""
    task_tokens = _tokens(task)
    if not task_tokens:
        return {"score": 0.4, "per_dimension": {}, "reason": "no task — neutral"}
    overlap = _jaccard(_tokens(candidate), task_tokens)
    return {"score": round(overlap, 3),
             "per_dimension": {},
             "reason": f"jaccard overlap={overlap:.2f}"}


def _aggregate_score(votes: list[dict], dimensions: list[str]) -> dict:
    overall_scores = [v["score"] for v in votes]
    m = mean(overall_scores) if overall_scores else 0.0
    # Variance-based confidence: lower stddev = higher confidence.
    if len(overall_scores) > 1 and m > 0:
        sd = pstdev(overall_scores)
        conf = max(0.0, min(1.0, 1.0 - (sd / max(m, 1e-6))))
    else:
        conf = 1.0 if overall_scores else 0.0
    per_dim_agg: dict[str, float] = {}
    for dim in dimensions:
        vals = [v["per_dimension"].get(dim) for v in votes
                if dim in v.get("per_dimension", {})]
        vals = [v for v in vals if isinstance(v, (int, float))]
        if vals:
            per_dim_agg[dim] = round(mean(vals), 3)
    return {
        "score": round(m, 3),
        "confidence": round(conf, 3),
        "per_dimension": per_dim_agg,
    }


async def _op_score(input: dict, ctx) -> dict:
    candidate = input.get("candidate")
    if not isinstance(candidate, str) or not candidate.strip():
        return {"ok": False, "error": "score requires non-empty candidate"}

    task = str(input.get("task") or "")
    rubric = str(input.get("rubric") or "")
    dimensions = _normalize_dimensions(input.get("dimensions"))
    n_votes = max(1, int(input.get("n_votes", DEFAULT_N_VOTES) or DEFAULT_N_VOTES))
    fallback_enabled = bool(input.get("fallback_enabled", True))
    pool = input.get("pool") or os.environ.get("LLM_JUDGE_POOL")
    model = input.get("model") or os.environ.get("LLM_JUDGE_MODEL")

    votes: list[dict] = []
    for i in range(n_votes):
        temp = 0.0 if i == 0 else min(0.3, 0.05 * i)
        user = _score_user_prompt(task, rubric, dimensions, candidate)
        _parsed, raw = await _single_compare_call(
            ctx, system=SCORE_SYSTEM, user=user,
            pool=pool, model=model, temperature=temp)
        if _parsed is None:
            continue
        v = _parse_score_vote(raw, dimensions)
        if v is None:
            continue
        votes.append({"score": v["score"],
                       "per_dimension": v["per_dimension"],
                       "reason": v["reason"],
                       "raw": raw[:1000]})

    if not votes:
        if not fallback_enabled:
            return {"ok": False, "error": "no usable LLM votes and fallback disabled"}
        fb = _score_fallback(candidate, task)
        result = {
            "ok": True,
            "score": fb["score"],
            "anchor_label": _anchor_label(fb["score"]),
            "confidence": 0.3,
            "per_dimension": fb["per_dimension"],
            "votes": [],
            "summary": fb["reason"],
            "fallback_used": True,
        }
        await _publish_verdict(ctx, "score", result)
        return result

    agg = _aggregate_score(votes, dimensions)
    result = {
        "ok": True,
        "score": agg["score"],
        "anchor_label": _anchor_label(agg["score"]),
        "confidence": agg["confidence"],
        "per_dimension": agg["per_dimension"],
        "votes": votes,
        "summary": f"mean={agg['score']} over {len(votes)} votes",
        "fallback_used": False,
    }
    await _publish_verdict(ctx, "score", result)
    return result


# -- event publish ---------------------------------------------


async def _publish_verdict(ctx, op: str, result: dict) -> None:
    bus = getattr(ctx, "bus", None)
    if bus is None:
        return
    payload = {
        "op": op,
        "confidence": result.get("confidence"),
        "fallback_used": result.get("fallback_used"),
        "vote_count": len(result.get("votes", [])),
    }
    if op == "compare":
        payload["winner"] = result.get("winner")
        payload["dimensions"] = list(result.get("per_dimension", {}).keys())
    elif op == "score":
        payload["score"] = result.get("score")
        payload["anchor_label"] = result.get("anchor_label")
        payload["dimensions"] = list(result.get("per_dimension", {}).keys())
    try:
        await bus.publish(VERDICT_TOPIC, payload)
    except Exception:
        log.exception("llm_judge: publish %s raised", VERDICT_TOPIC)


# -- entry -----------------------------------------------------


async def execute(input: dict, ctx) -> dict:
    op = (input or {}).get("op")
    if op == "compare":
        return await _op_compare(input, ctx)
    if op == "score":
        return await _op_score(input, ctx)
    return {"ok": False, "error": f"unknown op: {op!r}"}
