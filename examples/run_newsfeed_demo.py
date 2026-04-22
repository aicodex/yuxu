"""M0 runner for newsfeed_demo.

Why this exists: `yuxu` CLI has no `run <agent>` subcommand yet (only `serve`).
This is a one-off script that boots a minimal Bus + Loader, writes a temp
rate_limits.yaml pointing to env-var-supplied credentials, lets the loader
start all dependencies normally, runs newsfeed_demo one-shot, and exits.

Env:
    LLM_API_KEY       (required) falls back to OPENAI_API_KEY
    LLM_BASE_URL      (required) falls back to OPENAI_BASE_URL / TFE_BASE_URL
    NEWSFEED_MODEL    falls back to TFE_MODEL, then "deepseek-chat"
    NEWSFEED_POOL     default "openai"
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

import yaml

# Ensure yuxu package is importable (repo-local dev)
HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from yuxu.core.bus import Bus
from yuxu.core.loader import Loader

POOL = os.environ.get("NEWSFEED_POOL", "openai")
MODEL = (
    os.environ.get("NEWSFEED_MODEL")
    or os.environ.get("TFE_MODEL")
    or "deepseek-chat"
)
API_KEY = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
BASE_URL = (
    os.environ.get("LLM_BASE_URL")
    or os.environ.get("OPENAI_BASE_URL")
    or os.environ.get("TFE_BASE_URL")
)

# M0 local work dir — checkpoints + rate_limits.yaml + logs go here
WORK_DIR = HERE / "_m0_run_local"
WORK_DIR.mkdir(parents=True, exist_ok=True)


def write_rate_limits_config() -> Path:
    if not API_KEY:
        raise SystemExit("missing LLM_API_KEY / OPENAI_API_KEY env var")
    if not BASE_URL:
        raise SystemExit("missing LLM_BASE_URL / OPENAI_BASE_URL / TFE_BASE_URL env var")
    cfg = {
        POOL: {
            "max_concurrent": 2,
            "rpm": 30,
            "accounts": [
                {
                    "id": "default",
                    "api_key": API_KEY,
                    "base_url": BASE_URL,
                }
            ],
        }
    }
    path = WORK_DIR / "rate_limits.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True),
                    encoding="utf-8")
    return path


async def boot_and_run() -> int:
    # 1. Write temp rate_limits.yaml with env credentials
    rl_path = write_rate_limits_config()
    os.environ["RATE_LIMITS_CONFIG"] = str(rl_path)
    os.environ["CHECKPOINT_ROOT"] = str(WORK_DIR / "checkpoints")

    # 2. Loader scans bundled + examples (holds newsfeed_demo)
    import yuxu.bundled
    bundled_path = Path(yuxu.bundled.__file__).parent
    bus = Bus()
    loader = Loader(bus, dirs=[str(bundled_path), str(HERE)])
    await loader.scan()

    if "newsfeed_demo" not in loader.specs:
        print(f"FAIL: newsfeed_demo not in specs. found: {sorted(loader.specs)}",
              file=sys.stderr)
        return 1
    if "rate_limit_service" not in loader.specs:
        print("FAIL: rate_limit_service not discovered (bundled path wrong?)",
              file=sys.stderr)
        return 1

    # 3. Let loader start everything via ensure_running; it will cascade deps.
    try:
        status = await loader.ensure_running("newsfeed_demo")
    except Exception as e:
        logging.getLogger("m0").exception("newsfeed_demo raise")
        print(f"FAIL: newsfeed_demo: {e}", file=sys.stderr)
        return 1

    logging.getLogger("m0").info("newsfeed_demo status=%s", status)
    print(f"\n=== M0 DONE ===\nstatus: {status}\n"
          f"check: {HERE / 'newsfeed_demo' / 'reports'}/")
    return 0


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("m0")
    log.info("M0 config: POOL=%s MODEL=%s BASE_URL=%s", POOL, MODEL, BASE_URL)
    return await boot_and_run()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
