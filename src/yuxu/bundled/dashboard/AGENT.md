---
driver: python
run_mode: persistent
scope: system
edit_warning: true
depends_on: [gateway]
ready_timeout: 5
---
# dashboard plugin

Slash-command `/dashboard` —— 在当前聊天会话里开一张**自动刷新的卡片**，
展示项目实时状态（agent 列表 + 状态 + 时间戳），每 1s 更新一次。

用户一旦发任何非 `/dashboard` 的消息 → **dashboard 自动退出**（最后一帧
footer 标 `📴 Exited`，其余固定在最后时刻），避免历史消息里累积一堆刷新中的卡片。

## 触发

```
/dashboard
```

## 行为

- 开 `DraftHandle`（throttle 0.5s，配合 1s refresh 不抖）
- 后台 task 每 1s 调 `draft.set_content(snapshot) + flush()`
- 所有 adapter 共用：Telegram / Feishu 原地 edit 卡片；console 只在 finalize 出整张
- 用户发其他命令（`/help` 等）或普通消息 → 自动退出当前 session 的 dashboard

## 可定制

MVP 用 loader.get_state() 做默认快照。后续通过环境变量 `DASHBOARD_REFRESH_SEC`
调频率；更丰富的数据源（token 预算等）等 token_budget agent 就位后直接在
snapshot 里拼。

## 为什么是 agent 不是 core

插件化命令是用户体验策略（哪些 /xxx 存在、怎么刷新、怎么展示），不是框架机制。
Core 只负责：gateway 路由 `/xxx` 到 `gateway.command_invoked` 事件 + 命令注册表，
插件 agent 订阅事件自己决定怎么回。
