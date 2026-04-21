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
| `FEISHU_APP_ID` + `FEISHU_APP_SECRET` | 两个都设才启 feishu adapter（**当前只支持出站**：发消息/发卡片/编辑卡片） |
| `FEISHU_API_BASE` | 可选，默认 `https://open.feishu.cn`；国际版 Lark 用 `https://open.larksuite.com` |
| `FEISHU_RECEIVE_ID_TYPE` | 可选，默认 `chat_id`；可选 `open_id` / `user_id` / `email` / `union_id` |

**Feishu 凭证获取**：不用手动去管理后台建应用。跑一次扫码：

```
yuxu feishu register               # 默认 feishu 域
yuxu feishu register --lark         # Lark 国际版
yuxu feishu register --no-save      # 不写文件，打印 export 语句让你手贴 shell
```

用户手机扫码 + Feishu App 内授权后，**Feishu 自动建好一个 bot 应用**，
CLI 会把 `{app_id, app_secret, domain, open_id, bot_name}` 写到
`<project>/config/secrets/feishu.yaml`（`.gitignore` 已包含），下次
`yuxu serve` 启动时 gateway 自动读取。

## 操作（通过 `bus.request("gateway", {...})`)

### 简单发送

| op | payload | 返回 |
|---|---|---|
| `send` | `{session_key, text, reply_to?}` | `{ok, message_id?}` |
| `sessions` | `{}` | `{ok, sessions: [{session_key, source, created_at}]}` |

### 结构化 draft（OpenClaw 风格，quote + 💭thinking + content + footer 卡片）

| op | payload | 返回 |
|---|---|---|
| `open_draft` | `{session_key, quote?: {user, text}, footer_meta?: [[k,v],...], thinking?, content?}` | `{ok, draft_id, message_id}` |
| `update_draft` | `{draft_id, thinking? / thinking_append?, content? / content_append?, footer_meta?, flush_now?}` | `{ok, message_id}` |
| `close_draft` | `{draft_id}` | `{ok, message_id}` |

Python agent 直接用更顺手：
```python
gw = ctx.get_agent("gateway")
async with gw.open_draft(session_key=..., quote_user="alice", quote_text="你好",
                         footer_meta=[("Agent", ctx.name)]) as draft:
    # 例：LLM 流到一半，把思考和正文分别追加
    draft.append_thinking("The user greets me...")
    await draft.flush()
    draft.append_content("你好！")
    await draft.flush()
# 离开 with 时自动 close（最后一次 finalize 编辑）
```

节流：`DraftHandle` 内置 250ms 节流 + 结尾收尾。多快的 chunk 下至多每 250ms 调一次
adapter.edit（配合 `maybe_flush()`）。

### 未来

- `op=ask_choice`（select UI）：v1.1（已锁定，见 project_agos_vision）
- `op=typing_start/stop`：需要时再加（drafts 已覆盖"正在思考"的可见反馈）

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
