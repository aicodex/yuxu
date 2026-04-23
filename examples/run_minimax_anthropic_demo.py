"""Real-MiniMax smoke test for the Anthropic Messages API path.

Sends one tiny request through llm_service's new `api: anthropic-messages`
route, varying `thinking` (off / medium) to confirm MiniMax's native
reasoning blocks come back in the `reasoning` field.

Run:
    MINIMAX_API_KEY=sk-... python examples/run_minimax_anthropic_demo.py

Or with a CN endpoint:
    MINIMAX_API_KEY=sk-... MINIMAX_BASE_URL=https://api.minimaxi.com/anthropic \\
        python examples/run_minimax_anthropic_demo.py

Keeps the prompt short and max_tokens low so the demo is cheap. Budget
impact: 2 requests total (~500 tokens each).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from yuxu.core.bus import Bus
from yuxu.core.loader import Loader

POOL = "minimax_anthropic"
MODEL = os.environ.get("MINIMAX_ANTHROPIC_MODEL", "MiniMax-M2.7")
API_KEY = os.environ.get("MINIMAX_API_KEY") or os.environ.get("TFE_API_KEY")
BASE_URL = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.io/anthropic")

WORK_DIR = HERE / "_minimax_anthropic_run_local"
WORK_DIR.mkdir(parents=True, exist_ok=True)


def write_rate_limits() -> Path:
    if not API_KEY:
        raise SystemExit("missing MINIMAX_API_KEY / TFE_API_KEY env")
    cfg = {
        POOL: {
            "max_concurrent": 2,
            "accounts": [{
                "id": "mm_anthropic",
                "api_key": API_KEY,
                "base_url": BASE_URL,
                "api": "anthropic-messages",
            }],
        },
    }
    p = WORK_DIR / "rate_limits.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


async def one_call(bus: Bus, *, sender: str, user_text: str,
                   thinking) -> dict:
    req: dict = {
        "pool": POOL, "model": MODEL,
        "messages": [{"role": "user", "content": user_text}],
        "max_tokens": 1024,
        "temperature": 0.2,
    }
    if thinking is not None:
        req["thinking"] = thinking
    return await bus.request("llm_service", req, timeout=120.0, sender=sender)


def _print_resp(label: str, r: dict) -> None:
    print(f"\n=== {label} ===")
    print(f"ok           : {r.get('ok')}")
    print(f"stop_reason  : {r.get('stop_reason')}")
    print(f"elapsed_ms   : {r.get('elapsed_ms')}")
    print(f"usage        : {r.get('usage')}")
    content = r.get("content") or ""
    print(f"content[:400]: {content[:400]!r}"
          + (" ...(truncated)" if len(content) > 400 else ""))
    reasoning = r.get("reasoning")
    if reasoning:
        print(f"reasoning[:400]: {reasoning[:400]!r}"
              + (" ...(truncated)" if len(reasoning) > 400 else ""))
    else:
        print("reasoning    : (none — thinking disabled or provider omitted)")


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
    for name in ("rate_limit_service", "llm_service"):
        await loader.ensure_running(name)
        status = bus.query_status(name)
        if status not in ("ready", "running"):
            print(f"FAIL: {name} status={status}", file=sys.stderr)
            return 1

    print(f"pool={POOL} model={MODEL} base_url={BASE_URL}")

    # Round 1: thinking off (default) — plain answer, no reasoning field.
    r1 = await one_call(
        bus, sender="anthropic_demo",
        user_text="In one short sentence, what is 23 × 17?",
        thinking="off",
    )
    _print_resp("thinking=off (default)", r1)

    # Round 2: thinking medium — expect a `reasoning` field populated.
    r2 = await one_call(
        bus, sender="anthropic_demo",
        user_text=(
            "You have 3 apples, then buy 2 more and eat 1. Then you give "
            "half (rounding down) to a friend. How many apples do you have?"
        ),
        thinking="medium",
    )
    _print_resp("thinking=medium", r2)

    # Quick diff summary so the human doesn't need to squint.
    print("\n=== summary ===")
    print(f"thinking=off    reasoning present? {bool(r1.get('reasoning'))}")
    print(f"thinking=medium reasoning present? {bool(r2.get('reasoning'))}")
    if r2.get("reasoning"):
        rt = len(r2.get("reasoning") or "")
        ct = len(r2.get("content") or "")
        print(f"thinking=medium reasoning chars={rt} content chars={ct}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
