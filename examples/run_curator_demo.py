"""Real-LLM smoke test for memory_curator.

Boots llm_service + llm_driver + checkpoint_store + approval_queue +
approval_applier + memory_curator, feeds a sample transcript, and prints
the resulting improvement_log.md + drafts.
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

from yuxu.bundled.memory_curator.handler import MemoryCurator
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

WORK_DIR = HERE / "_curator_run_local"
WORK_DIR.mkdir(parents=True, exist_ok=True)


def write_rate_limits() -> Path:
    if not (API_KEY and BASE_URL):
        raise SystemExit("missing API_KEY / BASE_URL env")
    cfg = {POOL: {
        "max_concurrent": 3, "rpm": 30,
        "accounts": [{"id": "default", "api_key": API_KEY, "base_url": BASE_URL}],
    }}
    path = WORK_DIR / "rate_limits.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path


TRANSCRIPT = textwrap.dedent("""\
    # session — yuxu agent 建立纪律

    user: 我发现你反复做决定时问我小问题。别问，直接做，只在真分叉点停下来。
    assistant: 记下。从现在起默认大包大揽 + 只在作用域/约束冲突/用户独知/destructive 动作停。

    user: 还有，你写测试总往上堆。少而精比多而散好，三段覆盖（happy / 失败 stage / 边界）够了。
    assistant: 已按这个标准重构最近三个 agent 的测试。

    user: 别追求极致薄。参考 CC 的完备度照搬，只在 yuxu 特性上延展。LOC ceiling 别当设计目标。
    assistant: 明白，scope ceiling 保留（时长控制），LOC ceiling 删除。
""")


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
    for name in ("rate_limit_service", "llm_service", "llm_driver",
                 "checkpoint_store", "approval_queue", "approval_applier"):
        await loader.ensure_running(name)

    # Direct instantiation (skip gateway slash registration)
    ctx = SimpleNamespace(
        bus=bus,
        agent_dir=WORK_DIR / "_fake_curator_dir",
        name="memory_curator",
        loader=loader,
    )
    ctx.agent_dir.mkdir(exist_ok=True)
    curator = MemoryCurator(ctx)

    mem_root = WORK_DIR / "memory"
    print("[curate] start")
    r = await curator.curate(
        transcript=TRANSCRIPT,
        memory_root=mem_root,
        context_hint="first real-LLM curator run",
        pool=POOL, model=MODEL,
    )

    print(f"\n[curate] ok={r.get('ok')}  run_id={r.get('run_id')}")
    if not r.get("ok"):
        print(f"  reason/error: {r.get('reason') or r.get('error')}")
        return 1
    print(f"  log_entries appended: {r['log_entries']}")
    print(f"  dupes dropped: {r.get('log_dupes_dropped', 0)}")
    print(f"  drafts staged: {len(r['drafts'])}")
    print(f"  approval_ids: {r['approval_ids']}")
    print(f"  summary: {r['summary']}")

    stats = r.get("llm_stats") or {}
    if stats:
        print(f"  elapsed: {stats.get('elapsed_ms', 0) / 1000:.2f}s  "
              f"tok/s: {stats.get('output_tps')}  "
              f"tokens: {stats.get('prompt_tokens')} in / "
              f"{stats.get('completion_tokens')} out")

    content, footer = curator._format_reply_parts(r)
    print("\n=== gateway.open_draft.content ===")
    print(content)
    print("\n=== gateway.open_draft.footer_meta ===")
    for k, v in footer:
        print(f"  {k}: {v}")

    log_path = mem_root / "_improvement_log.md"
    if log_path.exists():
        print(f"\n=== {log_path.name} ===")
        print(log_path.read_text(encoding="utf-8"))

    for d in r["drafts"]:
        p = Path(d["path"])
        print(f"\n=== draft: {p.name} (target={d['target']}) ===")
        print(p.read_text(encoding="utf-8")[:1500])

    # Auto-approve all to see the closed loop too
    print("\n[approve] auto-approving drafts")
    for aid in r["approval_ids"]:
        await bus.request("approval_queue", {
            "op": "approve", "approval_id": aid,
            "reason": "smoke test auto-approve",
        }, timeout=5.0)
    for _ in range(40):
        await asyncio.sleep(0.05)
    for d in r["drafts"]:
        target = mem_root / d["target"]
        state = "✓ applied" if target.exists() else "✗ not applied"
        print(f"  {state}: {target}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
