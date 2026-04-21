---
driver: python
run_mode: persistent
scope: system
edit_warning: true
ready_timeout: 5
---
# project_manager

系统 agent：管理 yuxu 项目 + agent 的**创建 / 启动 / 列出**。一份逻辑两个入口：
- **CLI 库模式**（pre-daemon）：`yuxu` 命令行直接调静态方法（`ProjectManager.create_project(...)`）
- **Bus 模式**（daemon 运行时）：`bus.request("project_manager", {op: ...})`，供
  `shell` agent、`harness_pro_max` 等在聊天窗口里"帮我建个项目/新 agent"时调用

## 操作

### 静态（不需要 daemon 在跑）

| op | payload | 返回 |
|---|---|---|
| `create_project` | `{dir, force=false}` | `{ok, path}` |
| `create_agent` | `{project_dir, name, template="default"}` | `{ok, path}` |
| `list_projects` | `{}` | `{ok, projects: [{name, path, yuxu_version, exists}]}` |
| `list_agents` | `{project_dir}` | `{ok, agents: [{name, source: "bundled"/"user", path}]}` |

### 动态（需要 daemon；仅 bus 模式）

| op | payload | 返回 |
|---|---|---|
| `start_agent` | `{name}` | `{ok, status}` |
| `stop_agent` | `{name, cascade=false}` | `{ok}` |
| `restart_agent` | `{name}` | `{ok, status}` |
| `get_state` | `{name?}` | `{ok, state}` （代理 Loader.get_state） |

动态 op 在未运行 daemon 时（静态调用）返回 `{ok: false, error: "not running inside yuxu daemon"}`。

## CLI 映射

```bash
yuxu init <dir>                    → create_project
yuxu new agent <name> [--project D]→ create_agent
yuxu list projects                 → list_projects
yuxu list agents [--project D]     → list_agents
# 动态 op 未来可走 IPC（pid 文件 / socket）；MVP 只在 chat 侧用
```

## 为什么是 agent 不是 core

CLI 脚本做"创建项目/创建 agent"是**策略**（模板如何排布、怎样注入默认值、
未来要不要给模板变量替换）；不在 bootstrap 路径上。做成 agent 的好处是
chat 入口（shell / harness_pro_max）可以直接复用同一份逻辑，而不是 CLI 再
实现一遍 + chat 再实现一遍。

## 设计注意

- 静态方法操作文件系统；纯函数风格，不依赖 `self.loader`
- 动态 op 在 `start(ctx)` 时缓存 `self.loader = ctx.loader`；CLI 直接实例化时
  传 `loader=None`
- 不在 `~/.yuxu/projects.yaml` 之外存状态；所有持久都落到项目目录或 `~/.yuxu/`
