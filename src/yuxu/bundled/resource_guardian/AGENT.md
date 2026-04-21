---
driver: python
run_mode: persistent
scope: system
edit_warning: true
ready_timeout: 5
---
# resource_guardian

资源健康监视。订阅关键事件，滑动窗口内计数，超阈值发 `{agent}.resource_warning`。

## 监视的事件（默认）

| 事件 | kind | 含义 |
|---|---|---|
| `*.error` | `error` | 任何 agent 发的 `.error` 事件 |
| `_meta.ratelimit.throttled` | `throttled` | rate_limit_service 因排队超时丢事件（需业务触发发布） |
| `*.resource_warning` | — | **不**订阅自己发的，避免循环 |

目前 rate_limit_service 不主动发 `_meta.ratelimit.throttled`，该通道留给未来扩展或
调用方手动上报。guardian 只做订阅侧，不做轮询。

## 阈值（默认）

| kind | 窗口内次数 | 窗口 |
|---|---|---|
| `error` | 5 | 60 秒 |
| `throttled` | 3 | 60 秒 |

超阈值时发 `{agent}.resource_warning`，同一 (kind, agent) 每个窗口最多一次。

环境变量可覆盖：
- `GUARDIAN_WINDOW_SEC`
- `GUARDIAN_ERROR_THRESHOLD`
- `GUARDIAN_THROTTLE_THRESHOLD`

## 操作（通过 `bus.request("resource_guardian", {...})`)

| op | 返回 |
|---|---|
| `report` | `{ok, window_sec, thresholds, per_agent: {agent: {kind: count}}}` |
| `reset` | 清空计数 |

## 设计

MVP 只做观察 + 告警，不自动降级/暂停。后续升级为 `driver: hybrid` 时接入 LLM
做策略决策（降级模型 / 暂停 agent / 通知用户）。

## 为什么是 agent 不是 core

资源策略（阈值 / 响应方式 / 降级规则）天然变化，与 core 生命周期无关。
任何观察-决策 agent 都按此形态建。
