# yuxu examples

Ready-to-copy agents for end-to-end smoke testing. No LLM key required
(each example mocks the response); optional MiniMax wiring shown at the end.

## echo_bot — gateway + DraftHandle E2E

Mocks a "thinking → content" LLM reply. Used to verify:
- Console adapter sees your typed input
- `gateway.user_message` bus topic fires
- `DraftHandle` opens, accumulates chunks, finalizes
- Console adapter renders a structured "card"
- Dedup, mention gating, cancel (`/stop`)

### Install & run

```bash
# 1. Bootstrap a throwaway project
yuxu init /tmp/yuxu_demo
cd /tmp/yuxu_demo

# 2. Install the example
yuxu examples install echo_bot

# 3. Launch the framework (stays in foreground, stdin is the "chat")
yuxu serve
```

You'll see:
```
[yuxu] kernel ready: ... agents loaded
[console] gateway ready. type a message and press Enter.
```

### Scenario 1 — plain message

**Type:** `hello`

**Expect (console output after ~1s):**
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
回复 local: hello

💭 Thinking
  Received user input. In mock mode I don't actually plan, just echoing it back with a friendly tone.

You said: "hello". Hi local! 👋

――――――――――――――――――――――――――――――――――――――――
Agent: echo_bot | Mode: mock
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

Why one block: console adapter is `supports_edit=False` so it
**only emits on finalize**. Streaming chunks are buffered client-side,
the full card prints once.

### Scenario 2 — cancel

**Type:** `/stop`

**Expect:** no card. Gateway detects the cancel token and publishes
`gateway.user_cancel` instead of `gateway.user_message`, so echo_bot
(which only subscribes to user_message) stays silent.

In `data/logs/yuxu.log` you'll see:
```
DEBUG yuxu.core.bus: publish _meta.state_change ...
DEBUG yuxu.bundled.gateway.handler: published gateway.user_cancel
```

### Scenario 3 — multiple rapid messages

**Type in quick succession:**
```
hi
hi
hi
```

**Expect:** **three separate cards** (one per message).

Dedup is *within a single draft*, so the same content repeated across
different user turns still produces fresh cards. If you want to verify
the in-draft dedup, the test suite covers it
(`tests/test_gateway_draft.py::test_dedup_skips_identical_snapshots`).

### Scenario 4 — Telegram path (optional)

Point a real Telegram bot at your daemon (long-poll):
```bash
export TELEGRAM_BOT_TOKEN='123456:ABC...'
yuxu serve
```
In a Telegram chat with your bot:
**Send:** `hello`

**Expect on Telegram:** a single HTML-formatted message with blockquote
for the quote + blockquote for thinking + content + italic footer.
Same quote/thinking/content/footer layout, edited in place as chunks
stream (because `TelegramAdapter.supports_edit=True`).

### Scenario 5 — swap in a real LLM (optional)

The echo_bot is mock only. For a real-LLM test, drop the example and
wire `llm_driver` + `llm_service` yourself:

```bash
# 1. Configure rate-limit pool with your MiniMax key
cat > /tmp/yuxu_demo/config/rate_limits.yaml <<'YAML'
minimax:
  max_concurrent: 2
  rpm: 60
  accounts:
    - id: key1
      api_key: sk-your-minimax-key
      base_url: https://api.minimaxi.com/v1
YAML

# 2. Replace echo_bot with a thin agent that calls llm_driver.
# (Template in templates/agent/; replace the handle() body with
#  await bus.request("llm_driver", {pool:"minimax", model:"abab6.5s-chat", ...}))
```

Real-LLM wiring is its own exercise — the echo_bot is deliberately
offline so the gateway stack can be verified first.
