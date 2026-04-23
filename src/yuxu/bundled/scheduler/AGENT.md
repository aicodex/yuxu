---
driver: python
run_mode: persistent
scope: system
edit_warning: true
depends_on: []
optional_deps: [minimax_budget]
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

## v0.2 priority tier（可选，默认 `normal`）

| priority | normal | soft cap | hard cap |
|---|---|---|---|
| `critical` | ✅ | ✅ | ✅ |
| `normal` (默认) | ✅ | ✅ | ⏸ skipped |
| `nice_to_have` | ✅ | ⏸ skipped | ⏸ skipped |

scheduler 订阅 `minimax_budget.{interval,weekly}_{soft,hard}_cap` 事件：
- soft_cap → throttle level 升到 `soft`
- hard_cap → throttle level 升到 `hard`
- level **只升不降**，直到 TTL 过期自动回落到 `normal`（默认 TTL = 1800s / 30 min）
- 任何 cap 事件到达都**续 TTL**（持续吃紧状态下会一直 throttled）

被 skip 的 fire 会发 `scheduler.skipped`，不占配额。

## v0.3 Reservation gate（可选，env 开关）

`SCHEDULER_RESERVATION_CHECK=1` 打开后，scheduler 每次 fire 前会问
`minimax_budget.can_serve(target)`。拒绝时发 `scheduler.skipped reason=reservation_locked`
（带 `diagnostic` 字段展示为什么拒绝：own_reserved / reserved_for_others /
remaining）。

配合 `MINIMAX_RESERVATIONS` env 使用——前者给关键 target 预留 5h 内的请求数，
后者让 scheduler 真的按预留 gate。budget agent 没起 / 出错 → 不 gate（graceful
degrade，不能因为监控挂了让定时任务全停）。

## Config 示例（`config/schedules.yaml`）

```yaml
- name: preopen_research_daily
  target: preopen_research
  event: run
  payload: {}
  daily_at: "06:00"
  priority: critical    # 业务刚需，cap 时也照跑

- name: newsfeed_refresh
  target: newsfeed_demo
  event: refresh
  interval_sec: 900
  priority: normal       # 默认可省

- name: reflection_nightly
  target: reflection_agent
  event: run
  daily_at: "03:00"
  priority: nice_to_have # 预算紧时先停
```

也接受 `{schedules: [...]}` 包装形式。

**环境变量** `SCHEDULES_CONFIG` 可覆盖默认路径。

## 操作

| op | 返回 |
|---|---|
| `status` / `list` | `{ok, schedules:[{name,target,event,trigger,priority,fires,skips}], total_fires, total_skips, throttle:{level, ttl_remaining_sec, expires_at, last_cap_topic}}` |
| `override_throttle` | payload `{level: normal\|soft\|hard, ttl_sec?: float}` → 手动改 level（例如用户催促 `/override normal`） |

读字段级内省。v0.1 不支持运行时增删 schedule，v0.2+ 再加。

## 事件

| topic | payload | 时机 |
|---|---|---|
| `scheduler.tick` | `{schedule, target, event, fired_at, count, priority}` | 每次成功 `bus.send` |
| `scheduler.skipped` | `{schedule, priority, throttle_level, reason, skipped_at, skip_count}` | 被 tier 挡下 |
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
