"""Real-MiniMax smoke test for minimax_budget.

Boots llm_service + minimax_budget wired to the real MiniMax `/token_plan/
remains` endpoint. Calls the LLM twice under two different fake-agent
senders, then prints:

- remote snapshot (interval + weekly counters decoded from MiniMax)
- per-agent usage (built locally from llm_service publish events)
- an estimate projection ("if agent X makes 10 more requests, ~N tokens")

No writes, no fancy UI — this verifies the whole pipeline end-to-end.
"""
from __future__ import annotations

import asyncio
import json
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

from yuxu.bundled.minimax_budget.handler import MiniMaxBudget
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

WORK_DIR = HERE / "_minimax_budget_run_local"
WORK_DIR.mkdir(parents=True, exist_ok=True)


def write_rate_limits() -> Path:
    if not (API_KEY and BASE_URL):
        raise SystemExit("missing TFE_API_KEY / TFE_BASE_URL env")
    if "minimaxi.com" not in BASE_URL:
        print(f"WARN: base_url {BASE_URL} does not look like MiniMax; "
               f"budget will be idle.", file=sys.stderr)
    cfg = {POOL: {
        "max_concurrent": 3, "rpm": 30,
        "accounts": [{"id": "default", "api_key": API_KEY, "base_url": BASE_URL}],
    }}
    p = WORK_DIR / "rate_limits.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


async def one_call(bus: Bus, sender: str, user_text: str) -> dict:
    class _Msg:
        def __init__(self, payload, sender):
            self.payload = payload
            self.sender = sender
    # Use bus.request so llm_service's own bus publishes its event
    return await bus.request("llm_service", {
        "pool": POOL, "model": MODEL,
        "messages": [{"role": "user", "content": user_text}],
        "temperature": 0.2, "strip_thinking_blocks": True,
    }, timeout=120.0, sender=sender)


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
    for name in ("rate_limit_service", "llm_service", "minimax_budget"):
        await loader.ensure_running(name)
        status = bus.query_status(name)
        if status not in ("ready", "running"):
            print(f"FAIL: {name} status={status}", file=sys.stderr)
            return 1

    budget = loader.get_handle("minimax_budget")
    if budget is None:
        print("minimax_budget handle unavailable", file=sys.stderr)
        return 1

    print(f"\n[accounts discovered] {len(budget._accounts)}")
    for a in budget._accounts:
        print(f"  - {a['id']}  → {a['base_url']}")

    print("\n[baseline snapshot BEFORE any calls]")
    snap = budget.snapshot()
    for acc in snap["accounts"]:
        print(f"  account={acc['id']} fetched_at={acc.get('fetched_at')}")
        for m in acc["models"]:
            iv, wk = m["interval"], m["weekly"]
            unl_w = "∞" if wk.get("unlimited") else f"{wk['total']:>6}"
            print(f"    {m['model_name']:<40} "
                  f"interval={iv['used']:>5}/{iv['total']:>5} "
                  f"weekly={wk['used']:>6}/{unl_w}")

    print("\n[driving 2 LLM calls with distinct sender agent names]")
    r1 = await one_call(bus, "agent_alpha",
                        "say 'hi from alpha' in <10 words")
    print(f"  alpha → ok={r1.get('ok')} tokens={r1.get('usage', {})}")
    r2 = await one_call(bus, "agent_beta",
                        "please write a haiku about yuxu in Chinese")
    print(f"  beta  → ok={r2.get('ok')} tokens={r2.get('usage', {})}")

    # give the publish events time to land on budget's subscriber
    for _ in range(30):
        await asyncio.sleep(0.05)

    print("\n[per-agent usage attributed by minimax_budget]")
    for u in budget.agent_usage()["usage"]:
        print(f"  agent={u['agent']:<14} model={u['model']:<30} "
              f"reqs={u['requests']} tokens={u['total_tokens']} "
              f"avg={u['avg_tokens_per_req']}")

    print("\n[estimate: if agent_alpha makes 10 more similar calls]")
    est = budget.estimate(agent="agent_alpha", n_requests=10)
    print(f"  history: {est['history_requests']} reqs, "
          f"{est['history_tokens']} tokens, avg={est['avg_tokens_per_req']}")
    print(f"  projected: {est['projected_requests']} reqs → "
          f"~{est['projected_tokens']} tokens")

    print("\n[force-refresh remote snapshot AFTER calls; "
          "expect interval_used to have gone up by ≥2]")
    await budget.refresh()
    snap = budget.snapshot()
    for acc in snap["accounts"]:
        for m in acc["models"]:
            if m["model_name"] != "MiniMax-M*":
                continue
            iv = m["interval"]
            print(f"  interval: used={iv['used']}/{iv['total']} "
                  f"used_fraction={iv['used_fraction']:.4f} "
                  f"remaining_sec={iv['remaining_sec']:.0f}")
            wk = m["weekly"]
            if wk.get("unlimited"):
                print(f"  weekly:   unlimited (total=0 sentinel)")
            else:
                print(f"  weekly:   used={wk['used']}/{wk['total']} "
                      f"used_fraction={wk['used_fraction']:.4f}")

    print("\n=== DONE ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
