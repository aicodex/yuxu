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

# CC port (tools/AgentTool/agentMemory.ts:12-13): per-agent 持久 MEMORY.md。
# 上下文用完可以丢，但 MEMORY.md 跨次保留，让 agent 有连续的"经验"。
# memory: project | user | local   （省略 = 无 per-agent memory）
#   project → <project>/data/agent-memory/{name}/MEMORY.md（默认，推荐）
#   user    → ~/.yuxu/agent-memory/{name}/MEMORY.md（跨项目共享）
#   local   → <project>/.yuxu/local/agent-memory/{name}/MEMORY.md（gitignore 约定）
# 启用后 ctx.agent_memory_path 非 None，Loader 启动时已把文件 seed 好；
# handler 在 start() 自己决定读（inject 到 system prompt / 初始 state）。
memory: project
---
# agent 描述

> 一句话说明：这个 agent 做什么（CC 的 `description` 语义——"when to use"，3-5 词最好）。

## 职责

- …
- …

## 输入 / 输出契约（CC AgentTool 风格）

**输入**（主 agent 通过 `bus.request("{name}", payload, timeout=...)` 送入）：
```json
{
  "op": "<你的主操作名>",
  "description": "<3-5 词任务标签>",     // 可选，给日志/监控用（CC 字段）
  "input": { ... },                      // 你的业务载荷
}
```

**输出**（你 handler 的返回，Loader 透传给调用方）：
```json
{
  "ok": true,
  "content": [{"type": "text", "text": "..."}],  // 主要结果（CC 结构）
  "usage": {                                      // 可选：调用量统计
    "input_tokens": N, "output_tokens": M
  },
  "duration_ms": 123
}
```
失败：`{"ok": false, "error": "<reason>"}`。

契约最小字段参考 CC 2.1.88 `tools/AgentTool/AgentTool.tsx:82-101`（input）
和 `agentToolUtils.ts:227-258`（output）。

## 依赖 / 配置

- 环境变量：…
- 配置文件：…

## Agent memory 用法（启用了 `memory:` 字段才有）

```python
# 在 start(ctx) 或 handle(msg) 里
if ctx.agent_memory_path and ctx.agent_memory_path.exists():
    notes = ctx.agent_memory_path.read_text(encoding="utf-8")
    # 解析 frontmatter + body，inject 到 system prompt / internal state

# 要写回：
ctx.agent_memory_path.write_text(updated_text, encoding="utf-8")
```

Loader 在 agent lifecycle 启动时已把文件 seed 好（有 `agent` / `scope`
frontmatter + 空 body）。重复启动不会覆盖已有内容。
