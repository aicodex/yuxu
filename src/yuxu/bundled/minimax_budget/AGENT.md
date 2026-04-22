---
driver: python
run_mode: persistent
scope: system
edit_warning: true
depends_on: [rate_limit_service, llm_service]
ready_timeout: 15
---
# minimax_budget

**MiniMax 专用**的配额跟踪 + 每 agent 消耗估计 agent。不是通用 budget 抽象——
不同厂的计费规则差别大（Claude 滚动窗、OpenAI 按 token + RPM/TPM、MiniMax 按
请求 + 固定 interval 窗），硬抽象会漏掉细节。别的厂需要独立 agent。

**纯跟踪 / 估计，不做 gate-keeping。** 撞 429 的决定权和退避策略在
rate_limit_service / llm_service 的运行层——本 agent 只负责**让上层看得清**。

## 两个数据源

1. **MiniMax 权威视图**（每 30s 轮询 `GET https://www.minimaxi.com/v1/token_plan/remains`）
   - 对 rate_limit_service 里所有 `base_url` 指向 `minimaxi.com` 的 account
     自动各自轮询（per-account snapshot，不共享）
   - 数据里 `current_*_total_count == 0` 约定为**无上限**哨兵
   - `remains_time` 是到本 interval 结束的毫秒数
2. **yuxu 本地 per-agent 归因**（订阅 `llm_service.request_completed` 事件）
   - `(agent, model) -> {requests, total_tokens}`
   - 简化假设：**每 agent 每次请求的 token 数按历史平均估**，给 `estimate` 用
   - MiniMax 按请求计——token 只是辅助观测

## Operations

| op | payload | 返回 |
|---|---|---|
| `snapshot` | `{account_id?}` | `{ok, accounts: [{id, interval: {used, total, remaining_sec, unlimited?}, weekly: {...}, models: [...]}]}` |
| `agent_usage` | `{agent?}` | `{ok, usage: [{agent, model, requests, total_tokens, avg_tokens_per_req}]}` |
| `estimate` | `{agent, n_requests}` 或 `{agent, n_tokens}` | `{ok, projected_requests, projected_tokens, based_on_avg}` |
| `refresh` | `{account_id?}` | 立即轮询一次（绕过缓存） |
| `reset_local` | `{agent?}` | 清本地归因（调试用） |

## 发出的事件

- `minimax_budget.interval_soft_cap` — 当 interval 已用 ≥ 80% 时（per-account,
  per-model_name）一次，下一个 interval 重置后可再发
- `minimax_budget.interval_hard_cap` — ≥ 95% 时再发一次
- `minimax_budget.weekly_soft_cap` / `weekly_hard_cap` — 仅在 `weekly_total > 0`
  的 key 上触发

**不自动截断请求**。事件让 resource_guardian / scheduler / performance_ranker
决定要不要降频（非刚需 agent 往后推 / 刚需照常）。

## 为什么 MiniMax 专用

- MiniMax 的 `/token_plan/remains` 响应 schema 是它独有
- `current_*_total_count: 0 = unlimited` 约定也是它独有
- 窗口边界（固定 interval vs Claude 的滚动）也不同
- 把这些特殊性塞进一个通用抽象会 over-engineering；独立 agent 更诚实

后续 Claude / OpenAI 接入时各建一个同名约定的 tracker agent
（`claude_budget`、`openai_budget`），发**相同 schema** 的事件给下游消费者
统一处理。

## v0 约束

- 只处理 `rate_limit_service` 里 `base_url` 含 `minimaxi.com` 的 account
- 默认每 30s 轮询一次；开 agent 时立刻拉一次
- 轮询失败只记 warning，不崩（下一轮再试）
- per-agent 归因单进程内存；重启就清零（v0 不做 checkpoint_store 持久化）
- 不做 ranker / 路由决策（`performance_ranker` 消费 `agent_usage` 输出）
