"""End-to-end close-the-loop smoke test: reflection_agent → approval_queue →
approval_applier → real memory file written.

Starts the real bundled agents (no fakes), runs one /reflect with MiniMax,
auto-approves every returned approval_id, then asserts the draft was moved
into `<memory_root>/<proposed_target>` and the draft file deleted.

Env: same as run_reflection_demo.py (LLM_API_KEY / TFE_API_KEY etc).
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

from yuxu.bundled.approval_applier.handler import ApprovalApplier
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

WORK_DIR = HERE / "_reflection_apply_run_local"
WORK_DIR.mkdir(parents=True, exist_ok=True)


def write_rate_limits_config() -> Path:
    if not API_KEY:
        raise SystemExit("missing API key")
    if not BASE_URL:
        raise SystemExit("missing base URL")
    cfg = {POOL: {
        "max_concurrent": 3, "rpm": 30,
        "accounts": [{"id": "default", "api_key": API_KEY, "base_url": BASE_URL}],
    }}
    path = WORK_DIR / "rate_limits.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True),
                    encoding="utf-8")
    return path


def write_sample_sources() -> list[Path]:
    src_dir = WORK_DIR / "sources"
    src_dir.mkdir(exist_ok=True)
    s1 = src_dir / "session_a.md"
    s1.write_text(textwrap.dedent("""\
        # Session — naming

        user: 系统级 agent 用 noun_verb（project_manager），业务 agent 用单 noun。
        assistant: 记下。类名跟 folder 一致，别起花名。
        user: skill 命名用动词（create_project / classify_intent）。
        """), encoding="utf-8")
    s2 = src_dir / "session_b.md"
    s2.write_text(textwrap.dedent("""\
        # Session — testing

        user: 单元测试不要打真实网络。
        assistant: 已经全部 mock，httpx.MockTransport 做 HTTP 层，fake bus 做内部依赖。
        user: happy + 失败 stage + 边界，三段都要有；少而精比多而散好。
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

    # Start the full stack this demo needs
    for name in ("rate_limit_service", "llm_service", "llm_driver",
                 "checkpoint_store", "approval_queue", "approval_applier"):
        await loader.ensure_running(name)
        status = bus.query_status(name)
        if status not in ("ready", "running"):
            print(f"FAIL: {name} status={status}", file=sys.stderr)
            return 1

    # reflection_agent: direct instantiation (skip gateway dep)
    ctx = SimpleNamespace(
        bus=bus,
        agent_dir=WORK_DIR / "_fake_agent_dir",
        name="reflection_agent",
        loader=loader,
    )
    ctx.agent_dir.mkdir(exist_ok=True)
    reflector = ReflectionAgent(ctx)

    sources = write_sample_sources()
    memory_root = WORK_DIR / "memory"

    log = logging.getLogger("loop")
    log.info("running reflect → enqueue → approve → apply")

    # ---- reflect ------------------------------------------------
    result = await reflector.reflect(
        need="Yuxu 命名和测试纪律",
        sources=[str(p) for p in sources],
        memory_root=memory_root,
        n_hypotheses=3, pool=POOL, model=MODEL,
    )
    if not result.get("ok"):
        print(f"reflect failed: {result}", file=sys.stderr)
        return 1

    drafts = result.get("drafts") or []
    approval_ids = result.get("approval_ids") or []
    print(f"\n[reflect] run_id={result['run_id']}")
    print(f"[reflect] drafts staged: {len(drafts)}")
    print(f"[reflect] approval_ids: {approval_ids}")
    for d in drafts:
        print(f"  - [{d.get('action')}] {d.get('target')} → "
              f"{Path(d['path']).name} (score={d.get('score'):.2f})")

    if not approval_ids:
        print("ERROR: no approval_ids; approval_queue not wired?",
              file=sys.stderr)
        return 1

    # ---- approve all (simulate user tapping 'approve' on every draft) ----
    print(f"\n[approve] auto-approving {len(approval_ids)} items")
    for aid in approval_ids:
        r = await bus.request("approval_queue", {
            "op": "approve", "approval_id": aid,
            "reason": "smoke test auto-approve",
        }, timeout=5.0)
        if not r.get("ok"):
            print(f"ERROR: approve {aid}: {r.get('error')}", file=sys.stderr)
            return 1
        print(f"  ✓ {aid}")

    # ---- let applier consume decided events ---------------------
    # bus publishes are async tasks; give them room to run
    for _ in range(40):
        await asyncio.sleep(0.05)

    # ---- verify side effects ------------------------------------
    print(f"\n[verify] inspecting {memory_root}")
    missing_drafts = sum(1 for d in drafts if not Path(d["path"]).exists())
    written_targets = [d for d in drafts
                        if (memory_root / d["target"]).exists()]
    print(f"[verify] drafts deleted: {missing_drafts}/{len(drafts)}")
    print(f"[verify] targets written: {len(written_targets)}/{len(drafts)}")
    for d in written_targets:
        tp = memory_root / d["target"]
        sz = tp.stat().st_size
        print(f"  ✓ {tp}  ({sz} bytes)")

    success = (missing_drafts == len(drafts)) and \
              (len(written_targets) == len(drafts))
    if not success:
        print("\nFAILED: some drafts not applied", file=sys.stderr)
        # Dump leftovers for debugging
        for d in drafts:
            dp = Path(d["path"])
            tp = memory_root / d["target"]
            print(f"  draft: exists={dp.exists()} path={dp}")
            print(f"  target: exists={tp.exists()} path={tp}")
        return 1

    # Peek at one applied memory file to prove content fidelity
    if written_targets:
        sample = memory_root / written_targets[0]["target"]
        print(f"\n=== sample applied memory entry ({sample.name}) ===")
        print(sample.read_text(encoding="utf-8")[:1500])

    print("\n=== LOOP CLOSED ===")
    return 0


async def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return await boot_and_run()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
