---
driver: python
run_mode: persistent
scope: system
edit_warning: true
depends_on: [rate_limit_service]
ready_timeout: 5
---
# llm_service

OpenAI Chat Completions 协议的 HTTP 客户端。所有出站 LLM 调用经此服务，
账号选择与限流由 `rate_limit_service` 处理。

## 请求（通过 `bus.request("llm_service", {...})`）

```python
resp = await bus.request("llm_service", {
    "pool": "minimax",           # 必填：rate_limit pool 名
    "model": "abab6.5s-chat",    # 必填
    "messages": [...],           # 必填：OpenAI 格式
    "tools": [...],              # 可选
    "temperature": 0.3,          # 可选
    "json_mode": False,          # 可选
    "extra_body": {...},         # 可选：透传到请求体
    "timeout": 60.0,             # 可选：HTTP 超时
})
```

## 响应

成功：
```python
{"ok": True, "content": str | None, "tool_calls": [...], "stop_reason": str, "usage": {...}}
```
`stop_reason`：`end_turn` / `tool_use` / 其他原样透传。

失败：`{"ok": False, "error": str}`。

## 账号配置

在 `config/rate_limits.yaml` 的 pool 账号下配置：

```yaml
minimax:
  max_concurrent: 5
  rpm: 60
  accounts:
    - id: key1
      api_key: xxx
      base_url: https://api.minimaxi.com/v1
    - id: key2
      api_key: yyy
      base_url: https://api.minimaxi.com/v1
```

`base_url` 必须是 OpenAI 兼容的根，`/chat/completions` 会被自动拼接。

## 设计注意

- 同步单次调用；流式（stream=True）后续迭代
- 不做自动重试；业务层按需重试（llm_driver 会兜底）
- 上下文管理：`rate_limit_service.acquire(pool)` 范围内完成 HTTP 调用，确保释放并发槽

## 为什么是 agent 不是 core

HTTP 协议可替换（OpenAI / MiniMax / 未来 Anthropic 原生），重试/fallback 策略会迭代，
不在 boot 路径上。核心服务也是 agent 的典型例子：用户要换 provider 只需同名覆盖
`config/agents/llm_service/`，无需动 core。
