---
driver: python
run_mode: persistent
scope: system
edit_warning: true
ready_timeout: 5
---
# rate_limit_service

通用限流池服务。不限于 LLM：爬虫、第三方 API 也通过同一机制。

## 使用（业务 agent 侧）

```python
# 通过 ctx.get_agent 拿到 RateLimitService 对象，直接 acquire
rls = ctx.get_agent("rate_limit_service")
async with rls.acquire("minimax", agent="reflection_agent",
                       cost_hint=1500) as lease:
    # lease = {"pool", "account", "extra", "tokens",
    #          "agent", "priority", "cost_hint", "actual_cost"}
    reply = await call_api(key=lease["extra"]["api_key"])
    # Success path: tell the pool the real token cost so DWRR deficit
    # accounting reflects truth. Failures (raises, no actual_cost set)
    # leave deficit untouched.
    lease["actual_cost"] = reply["usage"]["total_tokens"]
```

依赖声明（AGENT.md）：`depends_on: [rate_limit_service]`

## v0.2：Weighted fair queuing + retry priority lane

当 pool 有并发争抢时按**加权公平队列（DWRR）**分配 slot；retry-priority
waiter 走独立 FIFO 车道，**绝对先于** weighted waiter。

### 加权公平

每个 agent 有一个 `weight`（pool 配置里 `weights: {agent: N}`，未列的默认 1）
和一个内部 `credit`。scheduling 时：
- 所有 waiting agent 的 credit ≤ 0 → 每个 agent credit += weight（refill round）
- 挑 credit 最大的 agent 出列；同 agent 内 FIFO
- 成功完成后 `credits[agent] -= actual_cost`（caller 在 handle 里设 `actual_cost`）
- **失败 / 不设 actual_cost 不扣 credit** —— 防止 1002 / 网络错重复计费

效果：agent_1 weight=4 cost=1000 tokens/call，agent_2 weight=2 cost=2000
tokens/call → agent_1 拿到约 4 个 slot 时 agent_2 拿到 1 个（4/1 对 2/2）。

### Retry priority lane

`acquire(..., priority="retry")` 加入 retry 队列（FIFO）。调度顺序：
1. retry waiters（FIFO）—— 绝对优先
2. weighted waiters（DWRR）

典型用法：`llm_driver` 检测到 `ProviderRateLimitError`（HTTP 429 / MiniMax 1002）
时指数退避后以 `priority="retry"` 重新 acquire。

**计费语义**：
- 失败（`actual_cost` 没设，不论 priority）→ 不扣 credit。
- 成功（`actual_cost` 已设，不论 priority）→ 扣 credit。
- 一次 logical call 只扣一次：第一次失败没扣，retry 成功扣了那次真实 cost。
priority 字段**只影响入队顺序**，不影响计费。

## 配置

文件：`$RATE_LIMITS_CONFIG` 或 `config/rate_limits.yaml`。

```yaml
minimax:
  max_concurrent: 5      # 并发上限（每账号）
  rpm: 500               # 每分钟请求上限（每账号滑动窗口）
  strategy: least_load   # 或 round_robin
  weights:               # v0.2: 加权公平队列的 agent 权重（可选）
    reflection_agent: 4
    memory_curator: 2
    harness_pro_max: 1
  accounts:
    - id: key1
      api_key: xxx
    - id: key2
      api_key: yyy
ths_spider:
  max_concurrent: 20
  rpm: 600
  accounts:
    - id: default
      proxy: http://proxy1
tushare:
  max_concurrent: 2
  rpm: 500
  accounts:
    - id: default
```

字段：
- `max_concurrent`: per-account 并发上限；None 表示不限
- `rpm`: per-account 每分钟调用上限；None 表示不限
- `strategy`: `least_load`（默认，选 concurrent 最小的账号）或 `round_robin`
- `weights`: per-agent 权重字典（v0.2，可选），未列 agent 默认权重 1
- `accounts`: 账号列表，每项必须有 `id`；其余字段透传到 ctx.extra
- `acquire_timeout`: 等待槽位的超时（秒），默认 60；超时抛 `asyncio.TimeoutError`

## 操作（通过 `bus.request("rate_limit_service", {...})`）

| op | 返回 |
|---|---|
| `status` | `{ok, pools: {name: {max_concurrent, rpm, strategy, weights, credits, retry_waiters, weighted_waiters, accounts: [{id, concurrent, calls_1m}]}}}` |

## 行为

- 未知 pool → `acquire` 抛 `KeyError`（生产安全，避免无意绕过）
- Context 退出自动释放并发槽位；RPM 窗口靠时间戳 TTL 自动滚出

## 为什么是 agent 不是 core

限流策略（选账号 / 阈值 / 池类型）会演进（将来加 TPM / daily quota），
属于策略而非机制。走 `ctx.get_agent("rate_limit_service").acquire(pool)`
即可，Core 不感知限流概念。
