"""Real-LLM smoke for session_compressor — raw JSONL → compressed memory entry.

Pipeline end-to-end:
    1. boot rate_limit_service + llm_service + llm_driver + context_compressor
       + session_compressor
    2. invoke session_compressor.compress_jsonl on a real session JSONL
    3. dump the resulting memory entry + frontmatter
    4. run memory.list + memory.get to prove the L1/L2 progressive disclosure
       finds it

Run:
    python examples/run_session_compressor_demo.py [path-to-jsonl]

If no path passed, picks the first session under docs/experiences/sessions_raw/.
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
from yuxu.core.frontmatter import parse_frontmatter
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

WORK_DIR = HERE / "_session_compressor_run_local"
WORK_DIR.mkdir(parents=True, exist_ok=True)

# Pretend this workdir is a yuxu project.
(WORK_DIR / "yuxu.json").write_text('{"name":"demo"}', encoding="utf-8")

SESSIONS_ROOT = HERE.parent / "docs" / "experiences" / "sessions_raw"


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


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / (1024 * 1024):.2f}MB"


async def main() -> int:
    logging.basicConfig(level="WARNING",
                         format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if len(sys.argv) > 1:
        jsonl_path = Path(sys.argv[1]).expanduser().resolve()
    else:
        candidates = sorted(SESSIONS_ROOT.glob("*.jsonl"))
        if not candidates:
            raise SystemExit(f"no JSONLs in {SESSIONS_ROOT}")
        jsonl_path = candidates[0]
    if not jsonl_path.exists():
        raise SystemExit(f"not found: {jsonl_path}")

    print(f"[target] {jsonl_path}")
    print(f"[target] raw size: {_fmt_bytes(jsonl_path.stat().st_size)}")

    rl = write_rate_limits()
    os.environ["RATE_LIMITS_CONFIG"] = str(rl)
    os.environ["CHECKPOINT_ROOT"] = str(WORK_DIR / "checkpoints")

    import yuxu.bundled
    bundled_path = Path(yuxu.bundled.__file__).parent
    bus = Bus()
    loader = Loader(bus, dirs=[str(bundled_path)])
    await loader.scan()
    for name in ("rate_limit_service", "llm_service", "llm_driver",
                  "context_compressor", "session_compressor", "memory"):
        await loader.ensure_running(name)
    print(f"[bus] {len(loader.specs)} agents/skills scanned; "
          f"pipeline ready")

    # Trick: make session_compressor resolve memory_root into our WORK_DIR.
    # Its _resolve_memory_root walks up from ctx.agent_dir looking for
    # yuxu.json. Inject one into WORK_DIR and pass a fake agent_dir under
    # WORK_DIR via the payload.
    print("\n[compress] invoking session_compressor.compress_jsonl ...")
    import time as _time
    t0 = _time.monotonic()
    r = await bus.request("session_compressor", {
        "op": "compress_jsonl",
        "jsonl_path": str(jsonl_path),
        "memory_root": str(WORK_DIR / "data" / "memory"),
        "pool": POOL,
        "model": MODEL,
    }, timeout=900.0)
    wall = _time.monotonic() - t0

    if not r.get("ok"):
        print(f"  FAIL: {r.get('error')}")
        return 1

    print(f"  ok in {wall:.1f}s (agent elapsed {r['elapsed_ms']/1000:.1f}s)")
    print(f"  source_bytes: {_fmt_bytes(r['source_bytes'])}")
    print(f"  compressed_bytes: {_fmt_bytes(r['compressed_bytes'])}")
    print(f"  compression_ratio: {r['compression_ratio']:.2%}")
    print(f"  fallback_used: {r['fallback_used']}")
    print(f"  memory_entry: {r['memory_entry_path']}")

    entry_path = Path(r["memory_entry_path"])
    text = entry_path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)

    print("\n=== memory entry frontmatter ===")
    for k, v in fm.items():
        print(f"  {k}: {v}")

    print("\n=== memory entry body (first 2000 chars) ===")
    print(body[:2000])
    print("\n=== memory entry body (last 800 chars) ===")
    print(body[-800:])

    # Progressive disclosure: use memory skill to find + read it.
    print("\n[memory.list type=session mode=reflect]")
    lst = await bus.request("memory", {
        "op": "list",
        "mode": "reflect",
        "memory_root": str(WORK_DIR / "data" / "memory"),
        "type": "session",
    }, timeout=10.0)
    for e in lst.get("entries", []):
        print(f"  - {e['path']} | {e.get('description', '')[:90]}")

    # Also demonstrate memory.search
    print("\n[memory.search 'admission gate' mode=reflect]")
    srch = await bus.request("memory", {
        "op": "search",
        "memory_root": str(WORK_DIR / "data" / "memory"),
        "query": "admission gate",
        "mode": "reflect",
        "limit": 3,
    }, timeout=10.0)
    for e in srch.get("entries", []):
        print(f"  - {e['path']} (score not exposed)")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
