"""Real-LLM smoke test for context_compressor — runs on actual session JSONLs.

Reads everything under `docs/experiences/sessions_raw/*.jsonl`, renders each
with `format_jsonl_transcript`, feeds to context_compressor at a few target
budgets, and prints size / ratio / elapsed / a head-and-tail sample of the
summary.

Run:
    python examples/run_context_compressor_demo.py

Env:
    REFLECTION_POOL / REFLECTION_MODEL / TFE_MODEL — LLM routing
    LLM_API_KEY + LLM_BASE_URL (or TFE_* or OPENAI_* fallbacks) — creds
    CC_DEMO_TARGETS — comma-separated target_tokens to sweep (default
                       "500,2000,5000")
    CC_DEMO_MAX_DOCS — cap how many JSONLs to feed (default all)
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import yaml

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from yuxu.core.bus import Bus
from yuxu.core.loader import Loader
from yuxu.core.session_log import format_jsonl_transcript

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

WORK_DIR = HERE / "_context_compressor_run_local"
WORK_DIR.mkdir(parents=True, exist_ok=True)

SESSIONS_ROOT = Path(
    os.environ.get("CC_DEMO_SESSIONS_DIR")
    or str(HERE.parent / "docs" / "experiences" / "sessions_raw")
).expanduser()


def write_rate_limits() -> Path:
    if not (API_KEY and BASE_URL):
        raise SystemExit("missing LLM_API_KEY / LLM_BASE_URL env")
    cfg = {POOL: {
        "max_concurrent": 3, "rpm": 30,
        "accounts": [{"id": "default", "api_key": API_KEY, "base_url": BASE_URL}],
    }}
    path = WORK_DIR / "rate_limits.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path


def find_jsonls() -> list[Path]:
    if not SESSIONS_ROOT.exists():
        raise SystemExit(f"no sessions dir: {SESSIONS_ROOT}")
    files = sorted(SESSIONS_ROOT.glob("*.jsonl"))
    if not files:
        raise SystemExit(f"no *.jsonl under {SESSIONS_ROOT}")
    max_docs = int(os.environ.get("CC_DEMO_MAX_DOCS") or 0)
    if max_docs > 0:
        files = files[:max_docs]
    return files


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / (1024 * 1024):.2f}MB"


def head_tail(text: str, head: int = 1200, tail: int = 600) -> str:
    if len(text) <= head + tail + 40:
        return text
    return (f"{text[:head]}\n\n"
            f"[... {len(text) - head - tail} chars elided ...]\n\n"
            f"{text[-tail:]}")


async def main() -> int:
    logging.basicConfig(level="WARNING",
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
                  "context_compressor"):
        await loader.ensure_running(name)

    files = find_jsonls()
    print(f"[load] {len(files)} session jsonl(s) under {SESSIONS_ROOT}")

    documents: list[dict] = []
    raw_total = 0
    for p in files:
        raw_bytes = p.stat().st_size
        rendered = format_jsonl_transcript(p)
        if not rendered.strip():
            print(f"  SKIP empty rendering: {p.name}")
            continue
        raw_total += raw_bytes
        documents.append({"id": p.name, "body": rendered})
        print(f"  {p.name}: raw={_fmt_bytes(raw_bytes)}  "
              f"rendered={_fmt_bytes(len(rendered))}  "
              f"est_tokens≈{len(rendered) // 4}")
    if not documents:
        print("no usable documents")
        return 1
    print(f"  TOTAL raw={_fmt_bytes(raw_total)}  "
          f"rendered_bytes={_fmt_bytes(sum(len(d['body']) for d in documents))}  "
          f"est_tokens≈{sum(len(d['body']) // 4 for d in documents)}")

    targets_env = os.environ.get("CC_DEMO_TARGETS") or "500,2000,5000"
    targets = [int(t.strip()) for t in targets_env.split(",") if t.strip()]

    for target in targets:
        print("\n" + "=" * 72)
        print(f"  target_tokens = {target}")
        print("=" * 72)
        t0 = time.monotonic()
        r = await bus.request("context_compressor", {
            "op": "summarize",
            "documents": documents,
            "task": ("Summarize yuxu engineering sessions so that a fresh "
                     "agent can pick up the iteration_agent design work."),
            "target_tokens": target,
            "pool": POOL,
            "model": MODEL,
            "custom_instructions": (
                "Prioritize architectural decisions, design critiques from "
                "the user, and open TODOs. Preserve file paths, agent "
                "names, and I-invariant references (I6, I11, etc.) "
                "verbatim."
            ),
        }, timeout=600.0)
        elapsed = time.monotonic() - t0

        ok = r.get("ok")
        if not ok:
            print(f"  FAIL: {r.get('error')}")
            continue
        print(f"  ok={ok}  skipped={r.get('skipped')}  "
              f"fallback_used={r.get('fallback_used')}  "
              f"elapsed={elapsed:.1f}s")
        print(f"  tokens: {r['total_tokens_before']} → "
              f"{r['total_tokens_after']}  "
              f"(savings {r.get('savings_ratio', 0):.1%})")

        for d in r.get("per_document", []):
            fb = " (fallback)" if d.get("fallback_used") else ""
            print(f"    · {d['id']}: "
                  f"{d['original_tokens']}→{d['summary_tokens']}{fb}")

        merged = r.get("merged_summary") or ""
        out_path = WORK_DIR / f"summary_target_{target}.md"
        out_path.write_text(merged, encoding="utf-8")
        print(f"  written: {out_path}  ({len(merged)} chars)")
        print("\n  --- head/tail sample ---")
        print(head_tail(merged))
        print("  --- end sample ---")

    print("\nDONE")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
