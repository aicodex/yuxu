---
driver: python
run_mode: persistent
scope: system
edit_warning: true
depends_on: [skill_picker]
ready_timeout: 10
---
# skill_executor

Skill 运行时。`skill_picker` 管**发现 + 目录**，本 agent 管**真实执行**。两种
模式并存，按 skill 的 `context` frontmatter 路由：

## Mode A — bus-dispatch（默认）

启动时扫所有 visible + enabled skill，对有 `handler.py`（或 `handler:` 覆盖）
且 `context != "inline"` 的 skill：
- 动态 `importlib.util.spec_from_file_location` 导入模块
- 取模块的 `execute` 函数
- 在 bus 上注册地址 `skill.{name}`，调用时 `await execute(input, ctx)`

LLM 通过 llm_driver 的 `tool_dispatch` 即可调（tool 名 → `skill.{name}`）。

## Mode B — inline-expand

`context: inline` 的 skill 不走 bus。用户 / 另一个 agent 调：
```python
r = await bus.request("skill_executor",
    {"op": "expand_inline", "skill_name": "x", "args": "..."},
    timeout=30.0)
# r["expanded_prompt"] → 可直接塞进 LLM 的 user message
```

Inline 展开做三件事：
1. `$ARGUMENTS` → 原始 args 字符串；`$1` `$2` ... → shell-quote 后的位置参数；
   `$foo` `$bar` → 按 frontmatter `argument-names` 映射位置参数
2. 找并执行 `!`cmd`` (inline) 和 ` ```! ... ``` ` (fenced) 的 shell preamble，
   结果（stdout + stderr）替换原位
3. 返回完整文本

Preamble 执行安全：
- `/bin/sh` 跑（和 CC 一致）
- 每命令 30s timeout，超时杀进程 + 插入 `(timeout)` 标记
- stdout 上限 `MAX_PREAMBLE_BYTES=8192`，截断
- 非 0 退出不中断 skill —— 把 `[exit N]` 标记 + stderr 塞进 prompt 让 LLM 自判

**安全注意**：`!cmd` 以 yuxu 进程用户身份跑，**没有 sandbox**。skill 来源可信
是前提（和 CC 同样靠源信任）。

## Operations

| op | payload | 返回 |
|---|---|---|
| `execute` | `{skill_name, input?, args?}` | 按 skill 的 context 自动路由到 Mode A 或 B |
| `dispatch_bus` | `{skill_name, input}` | 强制 Mode A，返回 handler 结果 |
| `expand_inline` | `{skill_name, args}` | 强制 Mode B，返回展开 prompt 文本 |
| `rescan` | `{}` | 重扫 skill_picker，更新 bus 注册 |
| `status` | `{}` | 当前注册的 skill 列表 + 模式 |

## 为什么是独立 agent

- `skill_picker` 职责是 metadata 目录（无状态 I/O）
- `skill_executor` 持有 **动态导入的 Python 模块**（生命周期有状态）
- `rescan` 时需要清理旧注册 + 重导入，独立 agent 边界清晰
- 未来加 Mode C (fork) 时有地方扩

## v0 约束

- 单进程导入（多实例 skill 的时候会冲突；等多实例落地再解决）
- 不做 skill-level 权限（CC 的 `allowed_tools` 只读，不强制；我们的安全靠
  approval_queue 事后审批）
- Mode C (fork / sub-agent) 延后
- 每个 skill 导入失败只记 log，不崩 executor（其他 skill 照常工作）
