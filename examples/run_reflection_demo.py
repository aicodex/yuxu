"""End-to-end smoke test for reflection_agent against a real LLM.

Boots a minimal Bus + Loader, starts llm_service + llm_driver (skips
gateway — we call reflect() directly), feeds two real markdown sources,
prints the resulting drafts on disk.

Env (any one set wins; first-found per group):
    LLM_API_KEY  / TFE_API_KEY  / OPENAI_API_KEY
    LLM_BASE_URL / TFE_BASE_URL / OPENAI_BASE_URL
    REFLECTION_MODEL / TFE_MODEL  (defaults to MiniMax-M2.7-highspeed)
    REFLECTION_POOL  (defaults to "minimax")
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import yaml

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from yuxu.bundled.reflection_agent.handler import ReflectionAgent
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

WORK_DIR = HERE / "_reflection_run_local"
WORK_DIR.mkdir(parents=True, exist_ok=True)


def write_rate_limits_config() -> Path:
    if not API_KEY:
        raise SystemExit("missing API key (set LLM_API_KEY / TFE_API_KEY / OPENAI_API_KEY)")
    if not BASE_URL:
        raise SystemExit("missing base URL (set LLM_BASE_URL / TFE_BASE_URL / OPENAI_BASE_URL)")
    cfg = {
        POOL: {
            "max_concurrent": 3,
            "rpm": 30,
            "accounts": [{"id": "default", "api_key": API_KEY, "base_url": BASE_URL}],
        }
    }
    path = WORK_DIR / "rate_limits.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True),
                    encoding="utf-8")
    return path


def write_sample_sources() -> list[Path]:
    """Two short, real-ish 'session transcripts' for reflection to chew on."""
    src_dir = WORK_DIR / "sources"
    src_dir.mkdir(exist_ok=True)
    s1 = src_dir / "session_2026_04_22_naming.md"
    s1.write_text(textwrap.dedent("""\
        # Session 2026-04-22 — agent naming debate

        user: 我看到现有 bundled agent 名字风格不一致：有 project_manager / llm_driver 这种 noun_verb，
              也有 dashboard / scheduler 这种单 noun。我们要不要统一？

        assistant: 提议：所有系统级 agent 用 noun_verb（明确角色），业务 agent 可以单 noun。

        user: 同意。再加一条：handler 里的类名跟 folder 名一致（驼峰转换），别给 agent 起花名。

        assistant: 已记下。skill 命名也跟随 — 动词为主（create_project / classify_intent）。
        """), encoding="utf-8")

    s2 = src_dir / "session_2026_04_22_test_strategy.md"
    s2.write_text(textwrap.dedent("""\
        # Session 2026-04-22 — testing convention

        user: 别用真实网络做单元测试，太脆。

        assistant: 已经在做 — llm_driver / llm_service / harness / reflection 全部 mock，
                  fake bus + canned response。集成层用 httpx.MockTransport。

        user: 还有：每个新 agent / skill 都要有 happy + 失败 stage + 边界 三类测试，不能只 happy。

        assistant: 已经按这个套路写了 harness_pro_max（16 测试覆盖 happy/降级/冲突/失败 stage/
                  slash 端到端）和 reflection_agent（23 测试 同上+ranker 降级+dedup）。

        user: 测试数量不是目标 — 覆盖关键失败模式才是。少而精比多而散好。
        """), encoding="utf-8")

    return [s1, s2]


async def boot_and_run() -> int:
    rl_path = write_rate_limits_config()
    os.environ["RATE_LIMITS_CONFIG"] = str(rl_path)
    os.environ["CHECKPOINT_ROOT"] = str(WORK_DIR / "checkpoints")

    import yuxu.bundled
    bundled_path = Path(yuxu.bundled.__file__).parent
    bus = Bus()
    loader = Loader(bus, dirs=[str(bundled_path)])
    await loader.scan()

    # Bring up only what reflection_agent's reflect() actually needs.
    for name in ("rate_limit_service", "llm_service", "llm_driver"):
        await loader.ensure_running(name)
        if bus.query_status(name) not in ("ready", "running"):
            print(f"FAIL: {name} status={bus.query_status(name)}", file=sys.stderr)
            return 1

    sources = write_sample_sources()
    memory_root = WORK_DIR / "memory"

    # Direct construction — skip gateway register/install.
    # CC port: wire agent_memory_path so the demo also exercises the per-
    # agent MEMORY.md read/write path. Pre-seed the file with a sample
    # Observations section so we can see it flow into the first-turn prompt.
    agent_memory_path = WORK_DIR / "agent_memory_demo" / "MEMORY.md"
    agent_memory_path.parent.mkdir(parents=True, exist_ok=True)
    agent_memory_path.write_text(
        "---\n"
        "agent: reflection_agent\n"
        "scope: project\n"
        "---\n"
        "# reflection_agent — agent memory (demo)\n\n"
        "## Observations\n\n"
        "- demo seed: 过去观察到命名约定讨论最容易 drift 到实现细节,\n"
        "  hypothesis 里优先标记出 rule-level 和 impl-level 的分界。\n"
        "- demo seed: update 优于 add 当存在相关条目时。\n",
        encoding="utf-8",
    )

    ctx = SimpleNamespace(
        bus=bus,
        agent_dir=WORK_DIR / "_fake_agent_dir",
        name="reflection_agent",
        loader=loader,
        agent_memory_path=agent_memory_path,
    )
    ctx.agent_dir.mkdir(exist_ok=True)
    agent = ReflectionAgent(ctx)

    # CC port: install() would load observations — we bypass install() in
    # this demo but still want them in the prompt, so prime the cache now.
    agent._agent_memory_observations = agent._load_agent_memory_observations()
    print(f"\n=== loaded Observations (injected into each hypothesis prompt) ===")
    print(agent._agent_memory_observations or "(none)")

    log = logging.getLogger("reflection_demo")
    log.info("running reflect: pool=%s model=%s sources=%d",
             POOL, MODEL, len(sources))

    result = await agent.reflect(
        need=("Yuxu 框架的命名约定与测试纪律 —— 把这两个会话里浮出来的"
              "稳定结论凝结成可记忆的规则"),
        sources=[str(p) for p in sources],
        memory_root=memory_root,
        n_hypotheses=3,
        pool=POOL, model=MODEL,
    )

    print("\n" + "=" * 60)
    print(f"ok={result.get('ok')}  run_id={result.get('run_id')}")
    if not result.get("ok"):
        print(f"FAILED at stage={result.get('stage')}: {result.get('error')}")
        for w in result.get("warnings") or []:
            print(f"  warn: {w}")
        return 1

    hyps = result.get("hypotheses") or []
    print(f"\n{len(hyps)} hypotheses:")
    for h in hyps:
        edits_n = len(h.get("edits") or [])
        ok_flag = "✓" if h.get("ok") else "✗"
        print(f"  {ok_flag} {h.get('framing_id'):22} edits={edits_n}  "
              f"summary: {(h.get('summary') or '')[:80]}")
        if not h.get("ok"):
            print(f"      err: {h.get('error')}")

    print(f"\n{len(result.get('chosen') or [])} chosen by ranker; "
          f"rejected: {result.get('rejected_summary') or '(none)'}")

    drafts = result.get("drafts") or []
    print(f"\n{len(drafts)} drafts staged at {memory_root}/_drafts/:")
    for d in drafts:
        print(f"  - [{d.get('action')}] {d.get('target')} "
              f"({(d.get('score') or 0):.2f})  → {Path(d['path']).name}")
        print(f"    title: {d.get('title')}")

    print(f"\napproval_ids: {result.get('approval_ids')}")

    stats = result.get("llm_stats") or {}
    if stats:
        print(f"\n=== LLM stats (aggregated) ===")
        print(f"  calls:            {stats.get('n_calls')}")
        print(f"  total elapsed:    {stats.get('elapsed_ms', 0) / 1000:.2f}s")
        print(f"  prompt tokens:    {stats.get('prompt_tokens')}")
        print(f"  completion tokens:{stats.get('completion_tokens')}")
        print(f"  output tok/s:     {stats.get('output_tps')}")

        # Demonstrate the NEW draft-path reply by splitting into the
        # structured (content, footer_meta) gateway would receive.
        content, footer = agent._format_reply_parts(result)
        print(f"\n=== gateway.open_draft.content ===")
        print(content)
        print(f"\n=== gateway.open_draft.footer_meta ===")
        for k, v in footer:
            print(f"  {k}: {v}")
        print(f"\n=== what Telegram / Feishu renders (inline italic form) ===")
        print(agent._format_reply(result))
    if result.get("warnings"):
        print("\nwarnings:")
        for w in result["warnings"]:
            print(f"  - {w}")

    print("\n=== inspect drafts: ===")
    for d in drafts[:2]:
        p = Path(d["path"])
        print(f"\n--- {p.name} ---")
        print(p.read_text(encoding="utf-8")[:1500])

    # CC port dogfood: show MEMORY.md after the run. The "## Runs" section
    # should now have one new bullet from this invocation.
    print(f"\n=== agent MEMORY.md after run ({agent_memory_path}) ===")
    print(agent_memory_path.read_text(encoding="utf-8"))

    return 0


async def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return await boot_and_run()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
