---
driver: python
run_mode: persistent
scope: system
edit_warning: true
depends_on: []
ready_timeout: 10
---
# scheduler

In-process 定时触发器。读 `config/schedules.yaml`，到点 `bus.send(target, event, payload)`。

## v0.1 支持的触发类型（严格二选一）

| 字段 | 语义 |
|---|---|
| `interval_sec: N` | 每 N 秒触发一次。第一次触发在启动后 N 秒。支持浮点 |
| `daily_at: "HH:MM"` | 每日本地时间 HH:MM 触发一次。当天若已过点，推到明天 |

**v0.2+**：cron 表达式、missed-fire 补发策略、on-demand `fire` op、reload 热加载。

## Config 示例（`config/schedules.yaml`）

```yaml
- name: preopen_research_daily
  target: preopen_research
  event: run
  payload: {}
  daily_at: "06:00"

- name: newsfeed_refresh
  target: newsfeed_demo
  event: refresh
  interval_sec: 900
```

也接受 `{schedules: [...]}` 包装形式。

**环境变量** `SCHEDULES_CONFIG` 可覆盖默认路径。

## 操作

| op | 返回 |
|---|---|
| `status` / `list` | `{ok, schedules: [{name, target, event, trigger, fires}], total_fires}` |

读字段级内省。v0.1 不支持运行时增删 schedule，v0.2+ 再加。

## 事件

| topic | payload | 时机 |
|---|---|---|
| `scheduler.tick` | `{schedule, target, event, fired_at, count}` | 每次成功 `bus.send` |
| `scheduler.error` | `{schedule, target?, error}` | 发送失败或任务 crash |

## 注意

- `target` 没有被 scheduler 自动拉起 —— 调度只负责定时，target agent 的 lifecycle
  由 project_supervisor / loader 管。如果 target 未加载，`bus.send` 会 warning 并丢弃，
  `.tick` 仍然发出（因为 scheduler 自己做完了工作）。
- wall-clock 为**本地时区**（服务器所在时区）。做跨时区系统时用 interval_sec + 自己算。
- 进程重启丢失 "已触发几次" 计数（只在内存），但不丢 schedule 定义（yaml 持久）。
  missed-fire 不补发 —— v0.2 再看策略。

## 为什么是 agent 不是 core

调度是策略（cron vs interval vs event-driven），会进化；核心只管 Bus 不崩。
用户可以同名覆盖 `config/agents/scheduler/` 替换成自己的策略。
