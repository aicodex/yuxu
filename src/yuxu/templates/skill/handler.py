"""Skill 执行器。按约定暴露 async def execute(input: dict, ctx) -> Any。

ctx 是 `yuxu.core.context.AgentContext`（由 Loader 懒构造并传入）：
  - ctx.bus                   消息总线
  - ctx.logger                日志
  - ctx.agent_dir             本 skill 文件夹路径
  - ctx.frontmatter           SKILL.md frontmatter dict

`bus.request("{name}", payload)` 由调用者发起，Loader 自动将 payload 作为
`input` 传给本函数；返回值经 bus 回送给调用者。
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
