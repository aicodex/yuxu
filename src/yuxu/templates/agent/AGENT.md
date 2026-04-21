---
# === 必填字段 ===
# driver: llm | python | hybrid
#   python = 主逻辑在 handler.py（Python service）
#   llm    = 只有 AGENT.md，由 llm_driver 托管，prompt 即 body
#   hybrid = 两者结合（Python 循环 + ctx.llm(...) 按需调用）
driver: python

# run_mode: persistent | scheduled | triggered | one_shot | spawned
#   persistent = 架子启动时自动拉起，永不退出
#   scheduled  = cron 触发，任务完即退（scheduler agent 管）
#   triggered  = 外部事件触发（如盘中 qmt 行情）
#   one_shot   = 用户/API 显式启动一次
#   spawned    = 父 agent spawn_subagent，任务完销毁
run_mode: one_shot

# === 可选字段 ===

# 硬依赖：启动前必等这些 agent ready
# depends_on: [llm_driver, checkpoint_store]

# 软依赖：运行时可用但不强求（不会阻塞启动）
# optional_deps: [resource_guardian]

# 作用域：system（系统级强确认）/ user（2 天宽限）
# scope: user

# 持久 agent 启动等待 ready 的超时（秒）
# ready_timeout: 30

# 系统级编辑警告：改 AGENT.md / handler.py 需要人工确认
# edit_warning: false
---
# agent 描述

> 一句话说明：这个 agent 做什么。

## 职责

- …
- …

## 输入输出

- 输入：通过 `bus.send("{name}", event, payload)` 或 `bus.request(...)`
- 输出：事件 `{name}.status/.progress/.output/.need_approval/.error`

## 依赖 / 配置

- 环境变量：…
- 配置文件：…
