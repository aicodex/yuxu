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
async with rls.acquire("minimax") as lease:
    # lease = {"pool": "minimax", "account": "key1", "extra": {"api_key": "..."}}
    reply = await call_api(key=lease["extra"]["api_key"])
```

依赖声明（AGENT.md）：`depends_on: [rate_limit_service]`

## 配置

文件：`$RATE_LIMITS_CONFIG` 或 `config/rate_limits.yaml`。

```yaml
minimax:
  max_concurrent: 5      # 并发上限（每账号）
  rpm: 60                # 每分钟请求上限（每账号滑动窗口）
  strategy: least_load   # 或 round_robin
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
- `accounts`: 账号列表，每项必须有 `id`；其余字段透传到 ctx.extra
- `acquire_timeout`: 等待槽位的超时（秒），默认 60；超时抛 `asyncio.TimeoutError`

## 操作（通过 `bus.request("rate_limit_service", {...})`）

| op | 返回 |
|---|---|
| `status` | `{ok, pools: {name: {accounts: [{id, concurrent, calls_1m}]}}}` |

## 行为

- 未知 pool → `acquire` 抛 `KeyError`（生产安全，避免无意绕过）
- Context 退出自动释放并发槽位；RPM 窗口靠时间戳 TTL 自动滚出

## 为什么是 agent 不是 core

限流策略（选账号 / 阈值 / 池类型）会演进（将来加 TPM / daily quota），
属于策略而非机制。走 `ctx.get_agent("rate_limit_service").acquire(pool)`
即可，Core 不感知限流概念。
