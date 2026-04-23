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

## Memory 记账（Phase 4 minimum）

额外订阅 `memory.retrieved`，把每条被检索的 memory entry 的
`score.applied` 在 frontmatter 里 +1；一旦 `applied` 达到
`PERFORMANCE_RANKER_PROBATION_CLEAR_THRESHOLD`（默认 3）就把
`probation: true` 翻回 `false`，让该 entry 重新回到 execute mode 的可见
集合。

- **只做 applied / probation 清除**。`helped` / `hurt` 仍按 I6 留给
  iteration_agent 未来写入——没有 outcome 信号就不要凭空打分。
- **唯一的探测入口是 reflect mode**（execute mode 本来就不看
  probation）。reflection_agent 现阶段查 memory 用 `mode="reflect"`，
  所以刚被 curator update 进 probation 的 entry 会在接下来几次反思里
  自然毕业。
- **排名不受污染**。memory.retrieved 不进入 agent 错误/拒绝的滑动窗口，
  `rank` / `score` / `reset` 语义不变。

## Staleness auto-demote

后台定时任务（默认 24h，`PERFORMANCE_RANKER_SWEEP_INTERVAL_HOURS`）扫所有
已知 memory root，对 `updated` 字段超过
`PERFORMANCE_RANKER_STALENESS_WINDOW_DAYS`（默认 30 天）的条目做**一级降级**
（`validated → consensus → observed → speculative`；`speculative` 为底，不再下降）。
被降级条目的 `updated` 重置为今天，避免下次 sweep 立刻再降。

- **roots 的来源**：每次 `memory.retrieved` 事件把 payload 里的
  `memory_root` 记到集合里；注入到 agent 的 `memory_root=` 参数也会预
  注册。所以第一次 sweep 前必须要有至少一次 retrieval。测试可直接调
  `sweep_staleness` op 并传 `memory_root`。
- **豁免**：`tags` 含 `mandatory` 的条目不过期（硬规则）；没有
  `updated` 字段的条目不判断（无法定年龄）；evidence_level 为未知值的
  条目不动（不造惊喜）。
- **直写，不走 approval**：和 probation 清除同样路径——系统机械动作，
  归档留档通过 `_archive` 保底已有；降级是保守动作（不会提权）。
- **事件**：每次降级发 `memory.demoted` 事件，payload 含
  `{path, memory_root, from_level, to_level, age_days, reason}`。
- **启动不立即 sweep**：避免重启风暴；第一次 sweep 发生在一个完整
  interval 之后。需要立刻跑用 `sweep_staleness` op。

## 窗口

滑动窗口默认 24 小时（env `PERFORMANCE_RANKER_WINDOW_HOURS`）。过期事件
下一次查询时惰性清理。

## 操作

| op | payload | 返回 |
|---|---|---|
| `rank` | `{limit?: int, min_score?: float}` | `{ok, window_hours, ranked: [{agent, score, errors, rejections}]}` — 默认降序 |
| `score` | `{agent: str}` | `{ok, agent, window_hours, score, errors, rejections}` |
| `reset` | `{agent?: str}` | `{ok, cleared: int}` — 省略 agent 则清空全部 |
| `sweep_staleness` | `{memory_root?: str}` | `{ok, demoted: [...]}` — 同步跑一次 staleness 扫描；payload 可选追加一个 root（正常运行时 roots 从 `memory.retrieved` 事件自动收集） |

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
