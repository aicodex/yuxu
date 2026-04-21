"""llm_service bundled agent."""
from __future__ import annotations

from .handler import LLMService

NAME = "llm_service"

_service: LLMService | None = None


async def start(ctx) -> None:
    rls = ctx.get_agent("rate_limit_service")
    if rls is None:
        raise RuntimeError(
            "llm_service: rate_limit_service handle unavailable. "
            "Ensure rate_limit_service is declared in depends_on and started."
        )
    global _service
    _service = LLMService(rate_limiter=rls.acquire)
    ctx.bus.register(NAME, _service.handle)
    await ctx.ready()


async def stop(ctx) -> None:
    if _service is not None:
        await _service.close()


def get_handle(ctx):
    return _service
