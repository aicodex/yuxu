"""Agent 入口。Loader 会 import 本模块并调用 start(ctx)。

三个可选函数（Loader 发现即调用）：
  async def start(ctx)   — 初始化（唯一必需；空文件则 Loader 自动 ready）
  async def stop(ctx)    — 优雅关停前的钩子（可选；超时 10s 会被 cancel）
  def get_handle(ctx)    — 暴露给其他 agent 的 Python 对象（可选）

ctx 字段（见 src/core/context.py）：
  ctx.name         当前 agent 名（= 文件夹名）
  ctx.agent_dir    当前 agent 目录（Path）
  ctx.frontmatter  AGENT.md 前言 dict
  ctx.body         AGENT.md 正文
  ctx.bus          消息总线
  ctx.loader       Loader（introspection 用）
  ctx.logger       已绑名 logger
  await ctx.ready()         宣告就绪
  ctx.get_agent(name)        拿别的 agent 的 get_handle() 返回值
  await ctx.wait_for(name)   等别的 agent ready
"""
from __future__ import annotations

from .handler import MyAgent

NAME = "my_agent"  # 与文件夹名保持一致

_agent: MyAgent | None = None


async def start(ctx) -> None:
    global _agent
    _agent = MyAgent(ctx.bus)
    ctx.bus.register(NAME, _agent.handle)
    await ctx.ready()


async def stop(ctx) -> None:
    # 可选：flush / close / cancel 等优雅关停
    pass


def get_handle(ctx):
    # 可选：返回 Python 对象；别的 agent 可 ctx.get_agent(NAME) 拿到
    return _agent
