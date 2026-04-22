---
driver: python
run_mode: persistent
scope: system
edit_warning: true
depends_on: [gateway, llm_driver]
ready_timeout: 5
---
# harness_pro_max

Agent-creator agent (v0). Subscribes to gateway slash-command `/new` and
walks the create-agent flow:

```
/new <description>
  ├─ classify_intent skill   → { agent_type, suggested_name, depends_on, ... }
  ├─ generate_agent_md skill → AGENT.md text + parsed frontmatter + warnings
  ├─ write <project>/agents/<suggested_name>/AGENT.md
  └─ loader.scan() + loader.ensure_running(suggested_name)
```

Replies via `gateway` op `send` with the new agent's path, status, and any
warnings.

## v0 限制

- **只能造 LLM-only agent**（`driver=llm`，无 `__init__.py`）。
  classify_intent 的 driver 建议被强制改写为 `llm`；warning 会附在回复里。
  python / hybrid 驱动需要手写 handler.py，后续版本扩展。
- **不写 handler.py / __init__.py**：交给 loader 默认 LLM-only 路径走。
- **不做权限审批**：建 agent 是用户主动 `/new`，没有 destructive side-effect。
  loader 装载新 agent 失败会回复错误并保留磁盘文件供人工排查。

## Operations

| op | payload | 返回 |
|---|---|---|
| `create_agent` | `{description, project_dir?, name?}` | `{ok, name, path, classification, warnings}` |

`project_dir` 缺省走 `_find_project_root(self.ctx.agent_dir)`（向上找 yuxu.json）。
`name` 缺省取 classify_intent 的 `suggested_name`。

## Slash command

注册 `/new`，args = description。等价于：
```
bus.request("harness_pro_max", {"op": "create_agent", "description": <args>})
```

## 为什么是 agent

订阅 `gateway.command_invoked` 是长跑订阅；持有 `ctx.loader` 才能 rescan +
ensure_running；这两件都不能下沉到 skill。

## 设计注意

- 失败语义：classify / generate 任一失败都把 raw / parsed 回写给用户便于
  下一次 /new 调整 prompt
- 名字冲突：目标路径已存在 → 不覆盖，回复冲突
- v0 不打 checkpoint；下一版加 `data/projects/.../agent_creation_log.jsonl`
