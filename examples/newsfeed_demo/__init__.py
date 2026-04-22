"""newsfeed_demo — M0 validation example agent."""
from __future__ import annotations

import logging

from .handler import NewsfeedDemo

log = logging.getLogger(__name__)


async def start(ctx) -> None:
    agent = NewsfeedDemo(ctx)
    result = await agent.run_once()
    if not result.get("ok"):
        raise RuntimeError(
            f"newsfeed_demo failed: {result.get('error', 'unknown error')}"
        )
    print(f"[newsfeed_demo] ok: {result['report_path']}")
    print(f"[newsfeed_demo] usage: {result['usage']}")
    print(f"[newsfeed_demo] preview: {result['content_preview']}...")
    # start() returning normally + one_shot mode → loader publishes ready
