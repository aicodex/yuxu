---
driver: python
run_mode: persistent
scope: system
edit_warning: true
ready_timeout: 5
---
# project_manager

系统 agent：在 daemon 运行时**指挥 Loader** —— start / stop / restart 已扫到的
agent，查询当前 state。所有**静态脚手架**（create_project / create_agent /
list_projects / list_agents）已下沉为 skill，见 `yuxu/bundled/`。

## 操作

| op | payload | 返回 |
|---|---|---|
| `start_agent` | `{name}` | `{ok, status}` |
| `stop_agent` | `{name, cascade=false}` | `{ok}` |
| `restart_agent` | `{name}` | `{ok, status}` |
| `get_state` | `{name?}` | `{ok, state}` （代理 `Loader.get_state`） |

动态 op 在未运行 daemon 时（手工实例化、`loader=None`）返回
`{ok: false, error: "not running inside yuxu daemon"}`。

## CLI 映射

```bash
yuxu init <dir>                    → bundled/create_project
yuxu new agent <name>              → bundled/create_agent
yuxu list projects                 → bundled/list_projects
yuxu list agents                   → bundled/list_agents
# 动态 op：未来通过 IPC（pid 文件 / socket）暴露给 CLI；MVP 只在 chat 侧用
```

## 为什么是 agent 不是 skill

start/stop/restart **需要持有 daemon 内的 `Loader` 实例**；这是真长跑状态、
持续在线的 bus 服务，不能像 skill 那样一次性加载执行。剥离静态部分后剩下的
就是这块薄薄的运行时控制层。

## 设计注意

- `start(ctx)` 时缓存 `self.loader = ctx.loader`
- 不在 `~/.yuxu/projects.yaml` 之外存状态
- LLM 想"建项目/建 agent"直接 `bus.request("create_project", ...)` 等；
  Loader 自动路由（agent 和 skill 共用同一套地址）；不走本 agent
