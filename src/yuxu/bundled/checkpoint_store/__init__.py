"""checkpoint_store bundled agent."""
from __future__ import annotations

import os
from pathlib import Path

from .handler import CheckpointStore

NAME = "checkpoint_store"
DEFAULT_ROOT = "data/checkpoints"

_store: CheckpointStore | None = None


async def start(ctx) -> None:
    global _store
    root = os.environ.get("CHECKPOINT_ROOT") or DEFAULT_ROOT
    _store = CheckpointStore(Path(root))
    ctx.bus.register(NAME, _store.handle)
    await ctx.ready()


def get_handle(ctx):
    return _store
