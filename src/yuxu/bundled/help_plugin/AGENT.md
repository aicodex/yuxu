---
driver: python
run_mode: persistent
scope: system
edit_warning: true
depends_on: [gateway]
ready_timeout: 5
---
# help_plugin

Slash-command `/help` —— 把 gateway 命令注册表里所有 `/xxx` 及其说明
格式化成一条消息回复用户。

## 触发

```
/help                → 列出所有已注册命令
/help /dashboard     → 展示某条命令的详细说明（如果插件提供了）
```

## 行为

- 通过 `bus.request("gateway", {op: "list_commands"})` 拉取注册表
- 用 `bus.request("gateway", {op: "send", ...})` 回普通文本（不开 draft）
- 参数如果是 `/xxx` 则只展示该条

## 注册

自己也是一个命令插件，启动时向 gateway 注册 `/help`。

## 为什么是 agent 不是 core

所有命令插件（dashboard / help / 未来 pair / shell / compact 等）都是对等的，
都通过同一个 command_invoked 事件被 gateway 派发。`/help` 只是其中一个。
