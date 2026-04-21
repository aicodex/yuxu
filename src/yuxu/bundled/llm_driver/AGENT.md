---
driver: python
run_mode: persistent
scope: system
edit_warning: true
depends_on: [llm_service]
ready_timeout: 5
---
# llm_driver

LLM 驱动循环。承担 Stage 1 `run_turn` 的职责：接收一轮对话请求，内部反复调用
`llm_service` + 执行 tool_call，直到 LLM 不再发 tool_call（或触发安全上限）。

## 请求（通过 `bus.request("llm_driver", {...}, timeout=...)`)

```python
result = await bus.request("llm_driver", {
    "op": "run_turn",
    "system_prompt": "you are ...",
    "messages": [{"role": "user", "content": "..."}],
    "pool": "minimax",                     # 必填：rate_limit pool
    "model": "abab6.5s-chat",              # 必填
    "tools": [                             # 可选：function tool schemas
        {"name": "get_price", "description": "...",
         "parameters": {"type": "object", ...}},
    ],
    "tool_dispatch": {"get_price": "price_agent"},  # 可选：tool 名 → bus 地址
    "max_iterations": 32,
    "max_output_bytes": 50000,
    "tool_timeout": 60,
    "llm_timeout": 180,
    "temperature": 0.3,
    "json_mode": False,
}, timeout=300)
```

## 响应

```python
{
    "ok": bool,                     # True 当且仅当 stop_reason == "complete"
    "content": str | None,          # 最后一轮 assistant 文本
    "messages": [...],              # 追加了所有 assistant/tool 轮次的完整 messages
    "iterations": int,
    "stop_reason": "complete" | "max_iter" | "error",
    "usage": {"prompt_tokens": int, "completion_tokens": int},
    "error": str | None,
}
```

## Tool 约定

驱动以 `bus.request(addr, {"op": "execute", "input": tool_input}, timeout=tool_timeout)`
执行每个 tool_call。`addr` 取 `tool_dispatch[name]`；缺省退回 tool 名本身。

Tool 响应有三种正常形态，都会被接受并转为 tool 消息内容：
- `{"output": <payload>}` → 取 `payload`
- `{"ok": False, "error": "..."}` → 转为 `{"error": "..."}`
- 其他 → 原样序列化

Tool 超时 / 异常 → 以 `{"error": "..."}` 形式喂回模型，**不中断循环**（让模型自己处理）。

## 设计说明

- 串行执行 tool_call（和 Stage 1 一致）
- 循环终止靠扫 tool_calls 是否为空（不看 stop_reason）
- 单次 tool 输出截断到 `max_output_bytes`（避免把 context 吃爆）
- 复用 Stage 1 `conversation.py` 的逻辑，替换 sync api_client → async bus.request

## 为什么是 agent 不是 core

多轮对话循环 / loop detection / context compact / tool timeout 策略都会演进。
把 run_turn 做成 agent 意味着业务可以换 driver（`config/agents/llm_driver/` 同名覆盖）
无需改 core。
