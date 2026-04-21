---
driver: python
run_mode: persistent
scope: system
edit_warning: true
ready_timeout: 10
---
# gateway

外部前端（Telegram / 控制台 / 未来 Slack/飞书等）接入层。双向桥：

**入站**：平台 adapter 收到用户消息 → gateway 规范化为 `InboundMessage`
→ `bus.publish("gateway.user_message", ...)` 给其他 agent（主要是未来的 `shell`）处理。

**出站**：任何 agent `bus.publish("gateway.reply", {session_key, text})` 或
`bus.request("gateway", {op: "send", session_key, text})` → gateway 分发到对应 adapter。

## 启用的 adapters（靠环境变量）

| 变量 | 作用 |
|---|---|
| `GATEWAY_CONSOLE_ENABLED` | `true/false`，默认 `true`。stdin/stdout 本地调试入口 |
| `TELEGRAM_BOT_TOKEN` | 设了才启 telegram adapter，long-poll 模式 |
| `TELEGRAM_ALLOWED_USER_IDS` | 可选，逗号分隔的 Telegram user_id 白名单 |

## 操作（通过 `bus.request("gateway", {...})`)

| op | payload | 返回 |
|---|---|---|
| `send` | `{session_key, text, reply_to?}` | `{ok, message_id?}` |
| `sessions` | `{}` | `{ok, sessions: [{session_key, source, created_at}]}` |

（未来）：`op=choice`（select UI）、`op=edit`（消息更新 / streaming）、
`op=typing_start/stop` —— 现在不做，等业务驱动需求再加。

## 发布的事件

```
gateway.user_message
  payload: {
    session_key: str,       # "{platform}:{chat_id}:{thread_id or default}"
    source: {platform, chat_id, user_id, thread_id, chat_type},
    text: str,
    reply_to_message_id: str | None,
    ts: ISO8601,
  }

gateway.user_cancel         # 用户发了 /stop
  payload: { session_key: str }
```

## 订阅的事件

```
gateway.reply
  payload: { session_key, text, reply_to? }
```

## Session

Key = `f"{platform}:{chat_id}:{thread_id or 'default'}"`。

MVP 在内存里维护 `{session_key: SessionEntry}`。后续可持久化到 checkpoint_store
（为重启续接）。

## 为什么是 agent 不是 core

前端协议会演进（Telegram API 变、飞书加 stream、新增 Discord 等），
错误恢复 / auth / allowlist 都是策略。Core 不感知"用户"概念，用户就是
gateway 抽象出来的一个 session。

## 设计注意

- 单一 session 的消息处理是**异步 + 不阻塞 gateway 主循环**：入站消息 publish 后立即返回，
  下游 agent 花多久都不影响下一条用户消息的接收
- Adapter crash 互不影响：每个 adapter 自己 try/except；一个挂了其他继续
- 长连接（Telegram long-poll）在 asyncio.create_task 里跑，gateway 停时取消
