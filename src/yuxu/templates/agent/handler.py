"""Agent 主逻辑。__init__.py 的 start() 会实例化并调 handle。

handle(msg) 签名约定：
  - 入参：src.core.bus.Message（含 to / event / payload / sender / request_id）
  - 返回：任意可 JSON 序列化值；若被 bus.request 调用，返回值即 reply
  - 异常：handler 内 try-except 捕获预期错误，不可恢复的让它抛（bus 会记录）
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class MyAgent:
    def __init__(self, bus) -> None:
        self.bus = bus

    async def handle(self, msg) -> dict:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        op = payload.get("op", "default")
        try:
            if op == "default":
                return {"ok": True, "echo": payload}
            return {"ok": False, "error": f"unknown op: {op!r}"}
        except (KeyError, TypeError, ValueError) as e:
            return {"ok": False, "error": str(e)}

    # persistent 场景下的后台循环（按需启用）
    # async def run_forever(self) -> None:
    #     import asyncio
    #     while True:
    #         await asyncio.sleep(60)
    #         # 周期性工作
