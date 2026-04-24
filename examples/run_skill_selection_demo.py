"""Real-MiniMax smoke test for the skill_index + invoke_skill pair.

Drives an end-to-end tool-use round-trip:

1. `skill_index.list` → renders the `<available_skills>` XML catalog.
2. `build_directive(xml_block)` → wraps it into a system-reminder.
3. `llm_driver.run_turn` is invoked via bus with:
    - the directive as `attachments` (per-turn <system-reminder>),
    - `invoke_skill.TOOL_SCHEMA` as the only tool,
    - `tool_dispatch={"invoke_skill": "invoke_skill"}`.
4. The LLM picks a skill, tool-calls `invoke_skill({"name": ...})`.
5. `invoke_skill` fetches the body via `skill_index.read` and returns
   it as tool_result; llm_driver feeds it back in iteration 2.
6. Final response should reflect the chosen SKILL.md's guidance.

For each prompt in PROMPTS the demo prints: chosen skill (if any),
final content, iteration count, usage. Meant for human review — the
4 prompts span clear-match / clear-no-match cases.

Usage:
    LLM_API_KEY="$TFE_API_KEY" LLM_BASE_URL="$TFE_BASE_URL" \\
        python examples/run_skill_selection_demo.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from yuxu.bundled.invoke_skill.handler import TOOL_SCHEMA as INVOKE_SKILL_SCHEMA
from yuxu.bundled.skill_index.handler import build_directive
from yuxu.core.bus import Bus
from yuxu.core.loader import Loader

POOL = os.environ.get("SKILL_POOL", "minimax")
MODEL = (os.environ.get("SKILL_MODEL")
         or os.environ.get("TFE_MODEL")
         or "MiniMax-M2.7-highspeed")
API_KEY = (os.environ.get("LLM_API_KEY")
           or os.environ.get("TFE_API_KEY")
           or os.environ.get("OPENAI_API_KEY"))
BASE_URL = (os.environ.get("LLM_BASE_URL")
            or os.environ.get("TFE_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL"))

WORK_DIR = HERE / "_skill_selection_run_local"
WORK_DIR.mkdir(parents=True, exist_ok=True)

# Four probe prompts:
#   - clear match (explicit ask for a capability one skill owns)
#   - partial match (user wants the capability but the wording is soft)
#   - clear mismatch (generic chat / out-of-scope)
#   - ambiguous (multiple plausible skills — test "pick the most specific")
PROMPTS = [
    "帮我把当前项目最近的一条 session JSONL 压缩成可复用的 memory entry",
    "我要看看最近几条 memory entry 都记了什么",
    "随便跟我聊两句",
    "给我读一下某个 skill 的 SKILL.md 说明",
]

SYSTEM_PROMPT = (
    "You are yuxu's test assistant. You have access to a catalog of yuxu "
    "skills (exposed to you as <available_skills> via a system-reminder). "
    "Follow the directive in that reminder precisely. When you invoke a "
    "skill via the `invoke_skill` tool, after reading its SKILL.md body, "
    "summarise (in one short paragraph, in Chinese) what you would do "
    "next for the user's request — do NOT actually call any other tool."
)


def write_rate_limits() -> Path:
    if not (API_KEY and BASE_URL):
        raise SystemExit("missing TFE_API_KEY / TFE_BASE_URL env")
    cfg = {POOL: {
        "max_concurrent": 3, "rpm": 30,
        "accounts": [{"id": "default", "api_key": API_KEY, "base_url": BASE_URL}],
    }}
    p = WORK_DIR / "rate_limits.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def _extract_selected_skill(result: dict) -> str | None:
    """Scan result.messages for the invoke_skill tool_call args."""
    for m in result.get("messages", []):
        if m.get("role") != "assistant":
            continue
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function") or {}
            if fn.get("name") != "invoke_skill":
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                return None
            return args.get("name")
    return None


async def run_one(bus, prompt: str, directive: str) -> dict:
    """One round of the skill-selection loop.

    Generous bus timeout: each LLM call can be 30-60s on MiniMax M2.7,
    and the driver runs up to max_iterations of them back-to-back.
    """
    resp = await bus.request("llm_driver", {
        "op": "run_turn",
        "system_prompt": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
        "pool": POOL,
        "model": MODEL,
        "tools": [INVOKE_SKILL_SCHEMA],
        "tool_dispatch": {"invoke_skill": "invoke_skill"},
        "attachments": [directive],
        "max_iterations": 6,
        "max_tokens": 4096,
        "llm_timeout": 180.0,
    }, timeout=900.0)
    return resp


async def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "WARNING"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    rl = write_rate_limits()
    os.environ["RATE_LIMITS_CONFIG"] = str(rl)
    os.environ["CHECKPOINT_ROOT"] = str(WORK_DIR / "checkpoints")

    import yuxu.bundled
    bundled_path = Path(yuxu.bundled.__file__).parent
    bus = Bus()
    loader = Loader(bus, dirs=[str(bundled_path)])
    await loader.scan()

    for name in ("rate_limit_service", "llm_service", "llm_driver"):
        await loader.ensure_running(name)
    # skills/agents are lazy-loaded by Loader on first bus hit, but we
    # can also eagerly ensure the two we care about so the first call's
    # latency is import-only, not scan.
    for name in ("skill_index", "invoke_skill"):
        await loader.ensure_running(name)

    # Render the catalog once — same XML goes into every turn's attachments.
    xml_resp = await bus.request("skill_index", {"op": "list"})
    if not xml_resp.get("ok"):
        print(f"[FATAL] skill_index.list failed: {xml_resp}", file=sys.stderr)
        return 1
    xml_block = xml_resp["xml_block"]
    directive = build_directive(xml_block)
    print(f"=== catalog snapshot ===")
    print(f"  entries:   {len(xml_resp['entries'])}")
    print(f"  chars:     {xml_resp['rendered_chars']}")
    print(f"  compact:   {xml_resp.get('compact_used')}")
    print(f"  omitted:   {xml_resp.get('omitted', 0)}")
    print(f"  directive: {len(directive)} chars\n")

    summary: list[dict] = []
    for i, prompt in enumerate(PROMPTS, 1):
        print(f"\n=== prompt {i}/{len(PROMPTS)} ===")
        print(f"  user: {prompt}")
        resp = await run_one(bus, prompt, directive)
        chosen = _extract_selected_skill(resp)
        usage = resp.get("usage", {})
        print(f"  stop_reason: {resp.get('stop_reason')}")
        print(f"  iterations:  {resp.get('iterations')}")
        print(f"  selected:    {chosen or '(none)'}")
        print(f"  tokens:      prompt={usage.get('prompt_tokens')} "
              f"completion={usage.get('completion_tokens')}")
        print(f"  final:")
        content = resp.get("content") or ""
        for line in content.splitlines():
            print(f"      {line}")
        summary.append({
            "prompt": prompt,
            "selected": chosen,
            "stop_reason": resp.get("stop_reason"),
            "iterations": resp.get("iterations"),
        })

    print("\n=== summary ===")
    for row in summary:
        print(f"  [{row['selected'] or 'none':<22}] "
              f"iter={row['iterations']} "
              f"stop={row['stop_reason']}  "
              f"→ {row['prompt']}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
