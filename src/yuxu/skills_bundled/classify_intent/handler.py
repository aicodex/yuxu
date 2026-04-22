"""classify_intent skill — NL → structured agent-creation classification.

Calls llm_driver once with json_mode=True, asking the model to fill a fixed
schema. Validates the result has the required keys and recognized enum
values; returns the parsed dict on success or the raw text on failure so
callers can retry / inspect.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

log = logging.getLogger(__name__)

REQUIRED_KEYS = ("agent_type", "suggested_name", "run_mode",
                 "depends_on", "driver", "reasoning")
VALID_RUN_MODES = {"one_shot", "persistent", "scheduled", "triggered", "spawned"}
VALID_DRIVERS = {"python", "llm", "hybrid"}

SYSTEM_PROMPT = """You are an agent-creation classifier for the yuxu framework.

Given the user's free-form description of a new agent they want to create,
return a strict JSON object with these keys:

- agent_type: one of the available templates (caller passes the list)
- suggested_name: snake_case folder name (lowercase, [a-z0-9_], no leading digit)
- run_mode: one of one_shot / persistent / scheduled / triggered / spawned
- depends_on: list of yuxu agents this new agent will need (use [] if none).
  Common bundled options: llm_driver, llm_service, rate_limit_service,
  checkpoint_store, scheduler, gateway, approval_queue, skill_picker.
- driver: one of python / llm / hybrid (python = pure code; llm = LLM-only;
  hybrid = code + LLM)
- reasoning: 1-2 sentences justifying the choices

Rules:
- Pick the SMALLEST workable run_mode. Default to one_shot unless the agent
  must stay resident (e.g. subscribes to bus events → persistent;
  cron trigger → scheduled).
- Don't invent depends_on entries. Only include agents you'd actually use.
- Output STRICT JSON only — no prose, no markdown fences."""


def _build_user_message(description: str, available_templates: list[str]) -> str:
    return (
        f"Description:\n{description.strip()}\n\n"
        f"Available templates: {available_templates}"
    )


def _extract_json(text: str) -> dict | None:
    """Return the first JSON object parseable out of `text`, else None.

    Tolerates markdown fences and surrounding prose because some providers
    leak even with json_mode=True.
    """
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Find the first `{ ... }` span and try parsing it
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


def _validate(obj: Any, available_templates: list[str]) -> tuple[bool, str]:
    if not isinstance(obj, dict):
        return False, "result is not a JSON object"
    missing = [k for k in REQUIRED_KEYS if k not in obj]
    if missing:
        return False, f"missing keys: {missing}"
    if obj["agent_type"] not in available_templates:
        return False, (
            f"agent_type {obj['agent_type']!r} not in available_templates "
            f"{available_templates}"
        )
    if obj["run_mode"] not in VALID_RUN_MODES:
        return False, f"run_mode {obj['run_mode']!r} not in {sorted(VALID_RUN_MODES)}"
    if obj["driver"] not in VALID_DRIVERS:
        return False, f"driver {obj['driver']!r} not in {sorted(VALID_DRIVERS)}"
    if not isinstance(obj["depends_on"], list):
        return False, "depends_on must be a list"
    name = obj["suggested_name"]
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        return False, f"suggested_name {name!r} is not snake_case"
    return True, ""


async def execute(input: dict, ctx) -> dict:
    """Skill protocol entry. ctx must expose `.bus.request(...)` for llm_driver."""
    description = input.get("description")
    if not isinstance(description, str) or not description.strip():
        return {"ok": False, "error": "missing or empty field: description"}
    available = list(input.get("available_templates") or ["default"])

    pool = (input.get("pool")
            or os.environ.get("CLASSIFY_INTENT_POOL")
            or os.environ.get("NEWSFEED_POOL")
            or "openai")
    model = (input.get("model")
             or os.environ.get("CLASSIFY_INTENT_MODEL")
             or os.environ.get("TFE_MODEL")
             or "gpt-4o-mini")

    try:
        resp = await ctx.bus.request("llm_driver", {
            "op": "run_turn",
            "system_prompt": SYSTEM_PROMPT,
            "messages": [{"role": "user",
                          "content": _build_user_message(description, available)}],
            "pool": pool,
            "model": model,
            "temperature": 0.0,
            "json_mode": True,
            "max_iterations": 1,
            "strip_thinking_blocks": True,
            "llm_timeout": 60.0,
        }, timeout=90.0)
    except Exception as e:
        log.exception("classify_intent: bus.request raised")
        return {"ok": False, "error": f"bus.request raised: {e}"}

    if not resp.get("ok"):
        return {"ok": False, "error": resp.get("error") or "llm_driver not ok",
                "raw": resp.get("content")}

    raw = resp.get("content") or ""
    obj = _extract_json(raw)
    if obj is None:
        return {"ok": False, "error": "LLM did not return parseable JSON",
                "raw": raw}

    valid, why = _validate(obj, available)
    if not valid:
        return {"ok": False, "error": f"validation: {why}", "raw": raw,
                "parsed": obj}

    return {"ok": True, "classification": obj, "usage": resp.get("usage")}
