---
driver: python
run_mode: persistent
scope: system
edit_warning: true
depends_on: [llm_driver]
optional_deps: [gateway, approval_queue, scheduler, performance_ranker]
ready_timeout: 5
---
# memory_curator

自动记忆凝结 agent。Hermes 思路（session 结束 / 上下文压缩前抢救） + OpenClaw
`self-improving-agent` 结构（append-only `improvement_log.md` + 周期回顾） +
yuxu 审批闭环（structured memory edit 必经 approval_queue）。

## 何时触发

| 触发 | 来源 | v0 状态 |
|---|---|---|
| `session.ended` 事件 | 其它 agent（gateway / 业务 agent）publish | 占位订阅，无人 publish 时 no-op |
| `/curate` slash command | 用户 | 实现 |
| `bus.request("memory_curator", {op: "curate", ...})` | 同侪 agent / 测试 | 实现 |
| `scheduler` 每日/每周跑 | 可选 scheduled job | 留接口，v0 不落盘实现 |

## 流程

```
一次 curate(sources, context_hint?)
  ├─ 加载 sources（同 reflection_agent 的 _load_sources 规则）
  ├─ 空 / 过短 → 提前跳过（"floor" 阈值，默认 200 字）
  ├─ 一次 LLM（temperature=0.2，比 reflection 的 0.5 更保守）
  ├─ LLM 严格 JSON：
  │   {
  │     "improvements": [<短句 insight>, ...],
  │     "memory_edits":  [<reflection_agent 同 schema>, ...],
  │     "summary": "<一句话>"
  │   }
  ├─ improvements → append 到 <memory_root>/_improvement_log.md
  │   （content-hash dedup；超过 MAX_LOG_BYTES 时 roll，保留尾部）
  └─ memory_edits → 落 draft 到 <memory_root>/_drafts/curator_*.md
      + 走 approval_queue → approval_applier 闭环（同 reflection_agent）
```

## 与 reflection_agent 的分工

| 维度 | reflection_agent | memory_curator |
|---|---|---|
| 触发 | 用户显式 `/reflect <need>` | 事件驱动 / 周期 / 手动补一次 |
| 视角 | 3 hypothesis 并行探索 | 1 pass Hermes 风 |
| 温度 | 0.5 | 0.2 |
| 产出 1 | 审批 drafts | **append-only log**（OpenClaw 有的，reflection 没有） + 审批 drafts |
| 对比 ranker | 有 | 没（curator 频率高，ranker 成本不划算） |
| 字节预算 | 宽 | 紧（改进日志 10KB 硬上限；单 draft 4KB） |

## Operations

| op | payload | 返回 |
|---|---|---|
| `curate` | `{sources?, transcript?, context_hint?, memory_root?, pool?, model?}` | `{ok, log_entries: int, drafts, approval_ids, summary, warnings}` |
| `curate` (auto) | `{auto: true, context_hint?, ...}` | 同上，`context_hint` 会自动追加 worst-agent 信息 |
| `status` | `{}` | `{ok, log_path, log_bytes, improvements_total}` |

`sources` 同 reflection_agent（文件路径 / 目录 / glob 都行）；
`transcript` 是直接传 text，绕过文件加载；
二者至少一个。`context_hint` 是可选提示（"this was a design debate about X"）。

**`auto: true`**: 先向 `performance_ranker` 查 `rank {limit:1}` 拿最差 agent，
把"focus curation on agent X, which has N errors and M rejections..."合并
进 context_hint 后走正常流程。用户 hint 不被覆盖，在前、ranker 追加在后。
ranker 未加载或无候选 → 跳过 auto hint、照常 curate（不报错），warnings 记一条。

## Slash command

- `/curate [hint]` — 直接传 hint 走正常 curate
- `/curate auto` — 走 auto 模式（ranker 选 focus agent）

## 发出的事件

| topic | payload |
|---|---|
| `memory_curator.curated` | `{run_id, log_entries, drafts, approval_ids, summary}` |
| `memory_curator.skipped` | `{run_id, reason}` — 比如 transcript 过短 |

## 为什么是 agent

- 长跑订阅 `session.ended`（未来 gateway / 其它 agent 会 publish）
- 需要持有 improvement_log.md 的 append lock（多源并发 curate 可能）
- v0 没订阅源不代表永远没有；先占位好过未来再加

## v0 约束

- 只产 `add` / `update` 两种 memory_edit（同 reflection_agent）
- improvement_log.md 总量硬上限 `MAX_LOG_BYTES=10_240`；超过 roll-trim 老的
- 单次 curate 最多产 5 条 improvements + 3 条 memory_edits（LLM prompt 级约束）
- 不自动 schedule；scheduler 对接留给用户配
