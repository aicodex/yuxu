"""Real-MiniMax smoke test for harness_pro_max.

Boots the full LLM stack + harness_pro_max, drives `create_agent_from_description`
directly (skip gateway so we don't need Telegram here), prints:
- raw result + aggregated llm_stats
- `_format_reply_parts` output — the exact (content, footer_meta) the gateway
  would receive via `open_draft`

Validates that classify_intent + generate_agent_md (two LLM calls) both surface
elapsed_ms/output_tps and that harness aggregates them correctly.
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

from yuxu.bundled.harness_pro_max.handler import HarnessProMax
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

WORK_DIR = HERE / "_harness_run_local"
WORK_DIR.mkdir(parents=True, exist_ok=True)


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


def write_fake_project(root: Path) -> Path:
    """harness needs a yuxu.json upstream to find the project root + an
    agents/ dir to write into. Build a throwaway one here."""
    proj = root / "project"
    proj.mkdir(exist_ok=True)
    (proj / "yuxu.json").write_text("{}")
    (proj / "agents").mkdir(exist_ok=True)
    return proj


async def main() -> int:
    logging.basicConfig(level="INFO",
                         format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    rl = write_rate_limits()
    os.environ["RATE_LIMITS_CONFIG"] = str(rl)
    os.environ["CHECKPOINT_ROOT"] = str(WORK_DIR / "checkpoints")

    proj = write_fake_project(WORK_DIR)

    import yuxu.bundled
    bundled_path = Path(yuxu.bundled.__file__).parent
    # Loader must include project/agents/ so the AGENT.md harness writes
    # is discoverable on rescan. Real `yuxu serve` does this via
    # `scan_order: ["_system", "agents"]`; we mirror it here.
    bus = Bus()
    loader = Loader(bus, dirs=[str(bundled_path), str(proj / "agents")])
    await loader.scan()

    for name in ("rate_limit_service", "llm_service", "llm_driver"):
        await loader.ensure_running(name)
    # harness_pro_max ctx: fake agent_dir under the project so _find_project_root works
    agent_dir = proj / "_system" / "harness_pro_max"
    agent_dir.mkdir(parents=True, exist_ok=True)
    ctx = SimpleNamespace(
        bus=bus, loader=loader,
        name="harness_pro_max", agent_dir=agent_dir,
    )
    harness = HarnessProMax(ctx)

    description = ("Build an agent that polls the US NWS API every morning "
                    "at 8am for today's weather and posts a one-paragraph "
                    "summary via gateway.")
    print(f"[harness] creating agent from: {description}")
    r = await harness.create_agent_from_description(
        description, project_dir=proj,
    )

    print("\n=== raw result ===")
    print(f"  ok={r.get('ok')}")
    print(f"  name={r.get('name')}")
    print(f"  status={r.get('status')}")
    print(f"  path={r.get('path')}")
    print(f"  warnings={r.get('warnings')}")
    if r.get("classification"):
        print(f"  classification={r['classification']}")

    stats = r.get("llm_stats") or {}
    if stats:
        print(f"\n=== aggregated LLM stats (classify + generate) ===")
        print(f"  n_calls:         {stats.get('n_calls')}")
        print(f"  elapsed:         {stats.get('elapsed_ms', 0) / 1000:.2f}s")
        print(f"  output_tps:      {stats.get('output_tps')}")
        print(f"  prompt_tokens:   {stats.get('prompt_tokens')}")
        print(f"  completion_tokens:{stats.get('completion_tokens')}")

    content, footer = harness._format_reply_parts(r)
    print("\n=== gateway.open_draft.content ===")
    print(content)
    print("\n=== gateway.open_draft.footer_meta ===")
    for k, v in footer:
        print(f"  {k}: {v}")

    if r.get("ok") and r.get("path"):
        md_path = Path(r["path"]) / "AGENT.md"
        if md_path.exists():
            print(f"\n=== written AGENT.md (head) ===")
            print(md_path.read_text(encoding="utf-8")[:1200])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
