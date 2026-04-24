"""session_compressor bundled agent — raw JSONL → compressed memory entry."""
from __future__ import annotations

from .handler import SessionCompressor

NAME = "session_compressor"

__all__ = ["SessionCompressor", "NAME", "start", "stop", "get_handle"]

_instance: SessionCompressor | None = None


async def start(ctx) -> None:
    global _instance
    _instance = SessionCompressor(ctx)
    await _instance.install()
    ctx.bus.register(NAME, _instance.handle)
    await ctx.ready()


async def stop(ctx) -> None:
    global _instance
    if _instance is not None:
        await _instance.uninstall()
        _instance = None


def get_handle(ctx):
    return _instance
