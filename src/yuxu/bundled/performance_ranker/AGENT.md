---
driver: python
run_mode: persistent
scope: system
depends_on: []
optional_deps: [approval_queue]
ready_timeout: 5
edit_warning: true
---
# performance_ranker

系统 agent：聚合 per-agent 的"负信号"（错误 / 提案被拒），产出**谁最需要
迭代**的排序。给 scheduler / reflection / harness 消费。

v0.1 信号（两种）：

| 信号 | 来源 | 归因 | 权重 |
|---|---|---|---|
| error | `*.error` 事件（topic shape = `{agent}.error`） | topic 前缀 | 1.0 |
| rejected | `approval_queue.rejected` 事件 payload | `requester` 字段 | 2.0 |

**不记的**：`.resource_warning` / `scheduler.skipped` / 正向事件。score
越高 = 越差。只读快照、不主动发事件（v0.1）。

## 窗口

滑动窗口默认 24 小时（env `PERFORMANCE_RANKER_WINDOW_HOURS`）。过期事件
下一次查询时惰性清理。

## 操作

| op | payload | 返回 |
|---|---|---|
| `rank` | `{limit?: int, min_score?: float}` | `{ok, window_hours, ranked: [{agent, score, errors, rejections}]}` — 默认降序 |
| `score` | `{agent: str}` | `{ok, agent, window_hours, score, errors, rejections}` |
| `reset` | `{agent?: str}` | `{ok, cleared: int}` — 省略 agent 则清空全部 |

## 用途（将来）

- scheduler 跑 nice_to_have 类 schedule 时，由 payload 决定 target：
  先 `bus.request("performance_ranker", {op: "rank", limit: 1})`，拿
  worst agent 的 name 填入 event payload，让 reflection_agent 针对 TA 反思
- harness `/perf` 命令：亮红某些 agent，请用户关注
- memory_curator：决定是否给某 agent 补 "feedback" memory

## 为什么是 agent

- 持有滑动窗口状态（skill 不能）
- 订阅 bus 被动收信号（skill 不能）
- 生命周期 = daemon 生命周期

## 为什么 score 系数硬编码

v0.1 不过度工程化：两个信号用 1/2 的权重简单分明。真有调参需求再外挂配置。
