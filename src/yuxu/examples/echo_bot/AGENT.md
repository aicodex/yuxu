---
driver: python
run_mode: persistent
scope: user
depends_on: [gateway]
ready_timeout: 5
---
# echo_bot（demo）

订阅 `gateway.user_message`，每收到一条用户消息 → 开一个 draft，先流式追加
💭thinking，然后流式追加 content，最后 finalize。全程**不调 LLM**（mock 模式），
用来验证 gateway / DraftHandle / 各 adapter 的渲染端到端能跑。

## 行为

- 看到任何用户消息 → 回一个卡片
- 卡片 quote 用户原话、thinking 是 2–3 段固定文本、content 是 echo + 打招呼
- footer 带 `Agent: echo_bot | Mode: mock`（将来接 token_budget 就换成真上下文）
- 忽略 `/stop` / `/cancel`（gateway 自动发 `gateway.user_cancel` 到别的 topic）

## 安装

```bash
yuxu examples install echo_bot --project /path/to/your_project
```

或手动：`cp -r .../yuxu/examples/echo_bot <project>/agents/`

## 测试

详见 `yuxu/src/yuxu/examples/README.md`。
