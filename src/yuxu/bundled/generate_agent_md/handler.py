"""generate_agent_md skill — LLM produces a yuxu AGENT.md given a spec.

Pure-prompt skill. The handler bakes the caller's chosen frontmatter fields
into the system prompt so the model only needs to write a coherent body and
echo the frontmatter back. After the LLM call we re-parse the frontmatter
and cross-check it against what the caller asked for; mismatches surface as
non-fatal warnings (the caller can decide to retry, edit, or accept).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

from yuxu.core.frontmatter import parse_frontmatter
from yuxu.core.principles import load_creation_context

log = logging.getLogger(__name__)

VALID_RUN_MODES = {"one_shot", "persistent", "scheduled", "triggered", "spawned"}
VALID_DRIVERS = {"python", "llm", "hybrid"}
VALID_SCOPES = {"user", "system"}

SYSTEM_PROMPT_TEMPLATE = """You are an AGENT.md author for the yuxu agent framework.

Produce ONE AGENT.md document for a new agent with the following spec:

- name: {name}
- run_mode: {run_mode}
- driver: {driver}
- scope: {scope}
- depends_on: {depends_on}

Description from the user:
\"\"\"
{description}
\"\"\"
{extra_hints_block}
The output MUST be a valid yuxu AGENT.md:

1. Begin with a YAML frontmatter block delimited by `---` lines, containing
   exactly these fields (and no others): driver, run_mode, scope, depends_on,
   ready_timeout. Use the spec values above; pick a sensible ready_timeout
   (5 for fast services, 30 for one_shots that wait, 180 for long jobs).
2. Below the frontmatter, write the body in markdown. Required sections:
   - `# {name}` (the H1 title)
   - One paragraph summary of what the agent does.
   - `## Operations` — list each op the agent exposes via bus.request, with
     payload and return shape. Skip if driver is "llm" or no bus ops planned.
   - `## Why this is an agent` — 1-2 sentences on why this is run-time
     resident or stateful (i.e. why not a skill).

Constraints:
- No markdown code-fence around the whole document. The frontmatter is the
  delimiter.
- Don't invent depends_on entries beyond what the spec provided.
- Keep the body concise (under 60 lines).
- Output the AGENT.md text directly, nothing before the opening `---`."""


def _build_system_prompt(*, name: str, description: str, run_mode: str,
                         driver: str, scope: str,
                         depends_on: list[str], extra_hints: str) -> str:
    extra_block = (
        f"\nAdditional hints from the caller:\n\"\"\"\n{extra_hints.strip()}\n\"\"\"\n"
        if extra_hints else ""
    )
    base = SYSTEM_PROMPT_TEMPLATE.format(
        name=name,
        description=description.strip(),
        run_mode=run_mode,
        driver=driver,
        scope=scope,
        depends_on=depends_on,
        extra_hints_block=extra_block,
    )
    # Append yuxu's architecture + operational principles so every new
    # AGENT.md is written in the context of the framework's invariants.
    # Falls back silently if the doc files are missing (partial install).
    creation_context = load_creation_context()
    if creation_context:
        return (
            base
            + "\n\n---\n\n"
            + "## Reference: yuxu framework context\n\n"
            + "Use the material below as invariants and operational "
            + "principles while writing the AGENT.md. Do not copy it "
            + "into the output; internalize it.\n\n"
            + creation_context
        )
    return base


def _strip_outer_fence(text: str) -> str:
    """Some models still wrap their answer in ``` despite instructions."""
    s = text.strip()
    m = re.match(r"^```[a-zA-Z0-9_-]*\n([\s\S]*?)\n```\s*$", s)
    return m.group(1) if m else s


def _check_consistency(*, frontmatter: dict, body: str, name: str,
                       run_mode: str, driver: str, scope: str,
                       depends_on: list[str]) -> list[str]:
    warnings: list[str] = []
    fm_run_mode = frontmatter.get("run_mode")
    if fm_run_mode != run_mode:
        warnings.append(f"frontmatter run_mode {fm_run_mode!r} != requested {run_mode!r}")
    fm_driver = frontmatter.get("driver")
    if fm_driver != driver:
        warnings.append(f"frontmatter driver {fm_driver!r} != requested {driver!r}")
    fm_scope = frontmatter.get("scope")
    if fm_scope != scope:
        warnings.append(f"frontmatter scope {fm_scope!r} != requested {scope!r}")
    fm_deps = list(frontmatter.get("depends_on") or [])
    if set(fm_deps) != set(depends_on):
        warnings.append(
            f"frontmatter depends_on {fm_deps!r} != requested {depends_on!r}"
        )
    if f"# {name}" not in body:
        warnings.append(f"body missing H1 title `# {name}`")
    return warnings


async def execute(input: dict, ctx) -> dict:
    """Skill protocol entry. ctx must expose `.bus.request(...)` for llm_driver."""
    name = input.get("name")
    description = input.get("description")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name or ""):
        return {"ok": False, "error": f"invalid name (snake_case required): {name!r}"}
    if not isinstance(description, str) or not description.strip():
        return {"ok": False, "error": "missing or empty field: description"}

    run_mode = input.get("run_mode", "one_shot")
    if run_mode not in VALID_RUN_MODES:
        return {"ok": False,
                "error": f"invalid run_mode {run_mode!r}; allowed {sorted(VALID_RUN_MODES)}"}
    driver = input.get("driver", "python")
    if driver not in VALID_DRIVERS:
        return {"ok": False,
                "error": f"invalid driver {driver!r}; allowed {sorted(VALID_DRIVERS)}"}
    scope = input.get("scope", "user")
    if scope not in VALID_SCOPES:
        return {"ok": False,
                "error": f"invalid scope {scope!r}; allowed {sorted(VALID_SCOPES)}"}
    depends_on = list(input.get("depends_on") or [])
    extra_hints = input.get("extra_hints") or ""

    pool = (input.get("pool")
            or os.environ.get("GENERATE_AGENT_MD_POOL")
            or os.environ.get("NEWSFEED_POOL")
            or "openai")
    model = (input.get("model")
             or os.environ.get("GENERATE_AGENT_MD_MODEL")
             or os.environ.get("TFE_MODEL")
             or "gpt-4o-mini")

    system_prompt = _build_system_prompt(
        name=name, description=description, run_mode=run_mode,
        driver=driver, scope=scope, depends_on=depends_on, extra_hints=extra_hints,
    )

    try:
        resp = await ctx.bus.request("llm_driver", {
            "op": "run_turn",
            "system_prompt": system_prompt,
            "messages": [{"role": "user",
                          "content": f"Produce the AGENT.md for `{name}` now."}],
            "pool": pool,
            "model": model,
            "temperature": 0.2,
            "max_iterations": 1,
            "strip_thinking_blocks": True,
            "llm_timeout": 120.0,
        }, timeout=150.0)
    except Exception as e:
        log.exception("generate_agent_md: bus.request raised")
        return {"ok": False, "error": f"bus.request raised: {e}"}

    if not resp.get("ok"):
        return {"ok": False, "error": resp.get("error") or "llm_driver not ok",
                "raw": resp.get("content")}

    raw = resp.get("content") or ""
    text = _strip_outer_fence(raw)
    if not text.lstrip().startswith("---"):
        return {"ok": False,
                "error": "output does not start with frontmatter `---`",
                "raw": raw}
    try:
        fm, body = parse_frontmatter(text)
    except Exception as e:
        return {"ok": False, "error": f"frontmatter parse: {e}", "raw": raw}
    if not isinstance(fm, dict) or not fm:
        return {"ok": False, "error": "empty or invalid frontmatter", "raw": raw}

    warnings = _check_consistency(
        frontmatter=fm, body=body, name=name, run_mode=run_mode,
        driver=driver, scope=scope, depends_on=depends_on,
    )

    return {
        "ok": True,
        "agent_md": text,
        "frontmatter": fm,
        "body": body,
        "warnings": warnings,
        "usage": resp.get("usage"),
        "elapsed_ms": resp.get("elapsed_ms"),
        "output_tps": resp.get("output_tps"),
    }
