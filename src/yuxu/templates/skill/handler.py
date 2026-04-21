"""Skill 执行器。按约定暴露 async def execute(input: dict, ctx) -> Any。

ctx 约定（由 skill_picker / llm_driver 传入）：
  - ctx.bus                   消息总线
  - ctx.logger                日志
  - ctx.rate_limit(pool)      限流 context manager（走 rate_limit_service）
  - ctx.checkpoint             checkpoint_store 简写接口（可选）

ctx 的具体形状由 skill_picker 定；MVP 允许 ctx=bus 就够用。
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


async def execute(input: dict, ctx) -> Any:
    symbol = input.get("symbol")
    if not symbol:
        raise ValueError("symbol required")

    # 若需限流：
    # async with ctx.rate_limit("tushare"):
    #     data = await _fetch(symbol)

    # 占位实现
    return {"symbol": symbol, "price": 0.0}
