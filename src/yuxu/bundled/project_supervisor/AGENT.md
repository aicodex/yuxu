---
driver: python
run_mode: persistent
scope: system
edit_warning: true
ready_timeout: 5
---
# project_supervisor

持久 agent 的 watchdog：订阅 `_meta.state_change`，检测 `persistent` agent 进入
`failed` 状态时自动 `loader.restart(name)`。滑动窗口限制重启速率，防止死循环。

## 行为

- 只看 `run_mode: persistent` 的 agent
- 不重启自己（避免递归）
- **重启限流**：窗口内重启次数超上限 → 放弃 + 发 `project_supervisor.giveup`
- 启动时扫一遍当前状态：任何已 `failed` 的 persistent agent 都会尝试恢复一次

## 默认参数

| 参数 | 默认 |
|---|---|
| `max_restarts` | 5 次 |
| `window_sec` | 300 秒（5 分钟） |
| `restart_delay` | 2 秒（事件触发到尝试重启的间隔，吸收抖动） |

环境变量：`SUPERVISOR_MAX_RESTARTS` / `SUPERVISOR_WINDOW_SEC` / `SUPERVISOR_DELAY_SEC`。

## 操作（通过 `bus.request("project_supervisor", {...})`)

| op | 返回 |
|---|---|
| `report` | `{ok, restarts: {agent: [ts...]}, give_ups: [...]}` |
| `reset` | 清空重启历史 |

## 事件

| 事件 | 何时发 |
|---|---|
| `project_supervisor.restarted` | 成功 restart 后 |
| `project_supervisor.restart_failed` | `loader.restart` 抛错 |
| `project_supervisor.giveup` | 超出重启上限放弃 |

## 设计注意

- Project（`project.md` 层）生命周期留待后续版本；目前只做 agent 级 watchdog
- 内核级兜底（10 行 watchdog 重启 supervisor 本身）暂不实现；进程级由 systemd/supervisord 管

## 为什么是 agent 不是 core

重启策略（限流窗口 / giveup 条件 / 未来的 LLM 诊断）会迭代；core 只做生命周期原语
（`loader.restart`），**怎么用**是 supervisor 的策略。supervisor 自己挂了由外层
systemd 拉起进程，形成两层兜底。
