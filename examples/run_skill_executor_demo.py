"""Real-MiniMax smoke test for skill_executor.

Proves both execution modes end-to-end:

Mode A (bus-dispatch):
  An external caller invokes `skill_executor.execute(skill_name="classify_intent", ...)`.
  skill_executor routes through the bus-registered `skill.classify_intent`
  handler, which itself calls llm_driver → MiniMax, returns JSON classification.

Mode B (inline-expand):
  A bespoke inline skill with `!`cmd`` preamble and `$ARGUMENTS` gets expanded
  (shell runs, args substituted). The expanded prompt is passed as a user
  message to llm_driver → MiniMax, model produces a coherent answer using
  the injected data.

Env: standard TFE_API_KEY / TFE_BASE_URL / TFE_MODEL.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from yuxu.bundled.skill_executor.handler import SkillExecutor
from yuxu.bundled.skill_picker.handler import SkillPicker
from yuxu.core.bus import Bus
from yuxu.core.loader import Loader

POOL = os.environ.get("REFLECTION_POOL", "minimax")
MODEL = (os.environ.get("REFLECTION_MODEL")
         or os.environ.get("TFE_MODEL")
         or "MiniMax-M2.7-highspeed")
API_KEY = (os.environ.get("LLM_API_KEY")
           or os.environ.get("TFE_API_KEY")
           or os.environ.get("OPENAI_API_KEY"))
BASE_URL = (os.environ.get("LLM_BASE_URL")
            or os.environ.get("TFE_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL"))

WORK_DIR = HERE / "_skill_executor_run_local"
WORK_DIR.mkdir(parents=True, exist_ok=True)


def write_rate_limits() -> Path:
    if not (API_KEY and BASE_URL):
        raise SystemExit("missing API_KEY / BASE_URL env")
    cfg = {POOL: {
        "max_concurrent": 3, "rpm": 30,
        "accounts": [{"id": "default", "api_key": API_KEY, "base_url": BASE_URL}],
    }}
    p = WORK_DIR / "rate_limits.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def write_inline_skill_fixture() -> Path:
    """Create a project-scope inline skill that uses !cmd + $ARGUMENTS."""
    skill_dir = WORK_DIR / "project_skills" / "system_brief"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: system_brief\n"
        "version: \"1.0.0\"\n"
        "author: demo\n"
        "description: Render a brief about the host. Uses !cmd preambles.\n"
        "context: inline\n"
        "argument-names: [subject]\n"
        "---\n"
        "请用中文给我一份**关于 $subject** 的简报，基于以下 runtime 事实：\n"
        "\n"
        "- 当前时间：!`date -u +%Y-%m-%dT%H:%M:%SZ`\n"
        "- Python 版本：!`python3 --version 2>&1 | head -1`\n"
        "- 进程 PID：!`echo $$`\n"
        "\n"
        "限制：只写三段，每段两句，不超过 300 字。\n",
        encoding="utf-8",
    )
    enable_file = WORK_DIR / "project_skills_enabled.yaml"
    enable_file.write_text(yaml.safe_dump({"enabled": ["system_brief"]}),
                             encoding="utf-8")
    return skill_dir


async def demo_mode_a(bus: Bus, ctx: SimpleNamespace) -> None:
    """Mode A: caller → skill_executor.execute → skill.classify_intent
    → classify_intent handler → llm_driver → MiniMax."""
    print("\n" + "=" * 60)
    print("MODE A — bus-dispatched skill via skill_executor")
    print("=" * 60)

    r = await bus.request("skill_executor", {
        "op": "execute",
        "skill_name": "classify_intent",
        "input": {
            "description": "Build an agent that summarizes morning news twice a day",
            "pool": POOL, "model": MODEL,
        },
    }, timeout=150.0)

    print(f"ok={r.get('ok')}")
    if not r.get("ok"):
        print(f"error: {r.get('error')}")
        print(f"raw: {(r.get('raw') or '')[:300]}")
        return
    cls = r.get("classification") or {}
    print(f"  agent_type:     {cls.get('agent_type')}")
    print(f"  suggested_name: {cls.get('suggested_name')}")
    print(f"  run_mode:       {cls.get('run_mode')}")
    print(f"  driver:         {cls.get('driver')}")
    print(f"  depends_on:     {cls.get('depends_on')}")
    print(f"  reasoning:      {cls.get('reasoning')}")


async def demo_mode_b(bus: Bus, ctx: SimpleNamespace,
                       executor: SkillExecutor) -> None:
    """Mode B: skill_executor.expand_inline → fully-expanded prompt →
    llm_driver → MiniMax."""
    print("\n" + "=" * 60)
    print("MODE B — inline expansion with !cmd preamble")
    print("=" * 60)

    # Add the project-scope inline skill to skill_picker's scopes
    skill_dir = write_inline_skill_fixture()
    scope_root = skill_dir.parent
    enable_file = WORK_DIR / "project_skills_enabled.yaml"
    # Build a combined scope list: the existing global (with our enable file)
    # PLUS a project scope for the inline fixture. skill_picker stops
    # visibility against `for_project` → we must pass that when loading.
    picker = ctx.loader.get_handle("skill_picker")
    from yuxu.bundled.skill_picker.registry import (
        SkillScope,
        installed_skills_bundled_root,
    )
    global_enable = WORK_DIR / "skills_enabled.yaml"
    picker.registry.scan([
        SkillScope.global_scope(
            skills_root=installed_skills_bundled_root(),
            enable_file=global_enable,
        ),
        SkillScope(skills_root=scope_root, enable_file=enable_file,
                    scope="project", owner="demo_proj"),
    ])
    await executor.rescan()

    # Expand the inline skill with args="yuxu 框架".
    # Pass for_project so the project-scoped skill is visible.
    expand = await bus.request("skill_executor", {
        "op": "expand_inline", "skill_name": "system_brief",
        "args": "yuxu 框架", "for_project": "demo_proj",
    }, timeout=30.0)

    print("\n--- expanded prompt (what the LLM will see) ---")
    print(expand.get("expanded_prompt", "(empty)"))

    if not expand.get("ok"):
        print(f"expand failed: {expand.get('error')}")
        return

    # Feed the expanded prompt as a user message to llm_driver
    print("\n--- calling llm_driver with the expanded prompt ---")
    r = await bus.request("llm_driver", {
        "op": "run_turn",
        "system_prompt": "You are a terse Chinese analyst.",
        "messages": [{"role": "user",
                      "content": expand["expanded_prompt"]}],
        "pool": POOL, "model": MODEL,
        "temperature": 0.3, "max_iterations": 1,
        "strip_thinking_blocks": True, "llm_timeout": 90.0,
    }, timeout=120.0)

    if not r.get("ok"):
        print(f"llm call failed: {r.get('error')}")
        return
    print(f"\n--- MiniMax response ({r.get('usage', {}).get('completion_tokens', '?')} completion tokens) ---")
    print(r.get("content"))


async def main() -> int:
    logging.basicConfig(level="INFO",
                         format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    rl = write_rate_limits()
    os.environ["RATE_LIMITS_CONFIG"] = str(rl)
    os.environ["CHECKPOINT_ROOT"] = str(WORK_DIR / "checkpoints")

    import yuxu.bundled
    bundled_path = Path(yuxu.bundled.__file__).parent
    bus = Bus()
    loader = Loader(bus, dirs=[str(bundled_path)])
    await loader.scan()
    # Enable classify_intent in the global skills_bundled scope so
    # skill_executor registers it. The enable file travels with the package
    # install; we point at WORK_DIR instead.
    global_enable = WORK_DIR / "skills_enabled.yaml"
    global_enable.write_text(
        yaml.safe_dump({"enabled": ["classify_intent", "generate_agent_md"]}),
        encoding="utf-8",
    )
    os.environ["YUXU_GLOBAL_SKILLS_ENABLE"] = str(global_enable)

    for name in ("rate_limit_service", "llm_service", "llm_driver",
                  "skill_picker", "skill_executor"):
        await loader.ensure_running(name)
        if bus.query_status(name) not in ("ready", "running"):
            print(f"FAIL: {name} not ready", file=sys.stderr)
            return 1

    # Re-point skill_picker at our enable file so classify_intent counts
    # as enabled (its default enable_file is config/skills_enabled.yaml
    # which doesn't exist in this smoke test scratch dir).
    picker = loader.get_handle("skill_picker")
    from yuxu.bundled.skill_picker.registry import (
        SkillScope,
        installed_skills_bundled_root,
    )
    picker.registry.scan([
        SkillScope.global_scope(
            skills_root=installed_skills_bundled_root(),
            enable_file=global_enable,
        ),
    ])
    executor = loader.get_handle("skill_executor")
    rescan = await executor.rescan()
    print(f"skill_executor registered: {rescan.get('registered')}")

    demo_ctx = SimpleNamespace(bus=bus, loader=loader,
                                name="demo", agent_dir=WORK_DIR)

    await demo_mode_a(bus, demo_ctx)
    await demo_mode_b(bus, demo_ctx, executor)

    print("\n=== DONE ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
