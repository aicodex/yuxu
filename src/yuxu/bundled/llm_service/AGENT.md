---
driver: python
run_mode: persistent
scope: system
edit_warning: true
depends_on: [rate_limit_service]
ready_timeout: 5
---
# llm_service

HTTP 客户端，出站 LLM 调用统一入口。账号选择与限流由 `rate_limit_service`
处理。**两套协议**：

- `api: openai`（默认）— OpenAI Chat Completions 兼容
- `api: anthropic-messages` — Anthropic Messages API（MiniMax 也有这条路径：
  `api.minimax.io/anthropic` / `api.minimaxi.com/anthropic`），
  **原生 thinking blocks 支持**

## 请求（通过 `bus.request("llm_service", {...})`）

```python
resp = await bus.request("llm_service", {
    "pool": "minimax",           # 必填：rate_limit pool 名
    "model": "MiniMax-M2.7",     # 必填
    "messages": [...],           # 必填：OpenAI 风格（system/user/assistant/tool）；
                                 # Anthropic 路径内部会转换为 Messages 格式
    "tools": [...],              # 可选（OpenAI 风格；自动转 Anthropic 的 input_schema）
    "temperature": 0.3,          # 可选
    "json_mode": False,          # 可选（OpenAI 路径的 response_format）
    "extra_body": {...},         # 可选：透传到请求体
    "timeout": 60.0,             # 可选：HTTP 超时
    "strip_thinking_blocks": True,  # 可选：OpenAI 路径剥 <think>...</think>（MiniMax 默认 endpoint）
    # v0.3 Anthropic 路径新增：
    "thinking": "medium",        # off|low|medium|high|xhigh 或 raw dict；默认 off
    "max_tokens": 4096,          # Anthropic 必填（默认 4096）
})
```

## 响应

成功：
```python
{"ok": True,
 "content": str | None,
 "tool_calls": [...],
 "reasoning": str | None,        # v0.3: Anthropic thinking / DeepSeek reasoning_content
 "stop_reason": str,
 "usage": {prompt_tokens, completion_tokens, total_tokens, ...},
 "elapsed_ms": float, "output_tps": float | None}
```
`stop_reason`：`end_turn` / `tool_use` / 其他原样透传。
`reasoning` 仅当 provider 返回 thinking/reasoning 通道时非空。

失败：`{"ok": False, "error": str}`；`provider_rate_limit` 错误带
`error_kind` / `error_code` / `retry_after_sec` 供 llm_driver 重试。

## 账号配置

在 `config/rate_limits.yaml` 的 pool 账号下配置：

```yaml
# OpenAI-compat 路径（默认）
minimax:
  max_concurrent: 5
  rpm: 60
  accounts:
    - id: key1
      api_key: xxx
      base_url: https://api.minimaxi.com/v1
      # api: openai  （默认，可省略）

# Anthropic-compat 路径（原生 thinking blocks 支持）
minimax_anthropic:
  max_concurrent: 5
  accounts:
    - id: mm_global
      api_key: yyy
      base_url: https://api.minimax.io/anthropic
      api: anthropic-messages     # 必须声明以启用 Anthropic 路径
    - id: mm_cn
      api_key: yyy
      base_url: https://api.minimaxi.com/anthropic
      api: anthropic-messages
```

URL 自动拼接：
- OpenAI：`{base_url}/chat/completions`
- Anthropic：`{base_url}/v1/messages`

Anthropic 路径额外 header：`anthropic-version: 2023-06-01`, `MM-API-Source: yuxu`。

## Thinking 级别

抄 OpenClaw `ui/src/ui/thinking.ts` 的 5 档预设：

| preset | 含义 | payload |
|---|---|---|
| `off`（默认） | 禁用 thinking | `{type: "disabled"}` |
| `low` | 浅思考 | `{type: "enabled", budget_tokens: 1024}` |
| `medium` | 中等 | `{type: "enabled", budget_tokens: 4096}` |
| `high` | 深思考 | `{type: "enabled", budget_tokens: 16384}` |
| `xhigh` | 极深 | `{type: "enabled", budget_tokens: 32768}` |

也可传 raw dict：`thinking={"type": "enabled", "budget_tokens": 9000}`。

**默认关**抄 OpenClaw 的防御注入——MiniMax Anthropic 流式下 thinking blocks
会泄漏为可见内容（OpenClaw `minimax-stream-wrappers.ts:45-51` 文档化）。需要
reasoning 时显式传 preset。

## 设计注意

- 同步单次调用；流式（stream=True）后续迭代
- 不做自动重试；业务层按需重试（llm_driver 会兜底 provider rate-limit 重试）
- 上下文管理：`rate_limit_service.acquire(pool)` 范围内完成 HTTP 调用，确保
  释放并发槽
- `strip_thinking_blocks`：默认关。MiniMax OpenAI 路径即使 prompt 禁了
  `<think>` 还会泄露；打开后服务端正则剥 `<think>…</think>` /
  `<thinking>…</thinking>`（含截断的孤儿 opener）。**Anthropic 路径下**
  thinking 已经是独立 block，直接拆到 `reasoning` 字段，不需要 strip

## 为什么是 agent 不是 core

HTTP 协议可替换，重试/fallback 策略会迭代，不在 boot 路径上。核心服务也是
agent 的典型例子：用户要换 provider 只需同名覆盖 `config/agents/llm_service/`，
无需动 core。
