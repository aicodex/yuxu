---
driver: python
run_mode: persistent
scope: system
edit_warning: true
depends_on: [checkpoint_store]
ready_timeout: 10
---
# approval_queue

Destructive 动作批准墙（v0.1，**无宽限期**，无自动过期）。

**定位**：当某个 agent 准备做"不能静默回滚"的事（删 memory / 删 agent / 外发消息 /
覆盖 AGENT.md 关键字段），它不能直接动手；它 enqueue 一个 approval request，拿到
`approval_id`，然后等 `approval_queue.decided` 事件（筛自己的 id），用户通过 gateway
/ CLI / telegram 做决定后才继续。

session 级 / project 级的小改动**不走本 agent**（按 `feedback_approval_policy.md`
的 v0.1 分级，走静默路径）。v0.2+ 再考虑 project 级超时宽限。

## 操作（通过 `bus.request("approval_queue", {...})`)

| op | 输入 | 返回 |
|---|---|---|
| `enqueue` | `{action: str, detail: any, requester: str}` | `{ok, approval_id, status: "pending"}` |
| `approve` | `{approval_id, reason?: str}` | `{ok, approval_id, status: "approved"}` |
| `reject` | `{approval_id, reason?: str}` | `{ok, approval_id, status: "rejected"}` |
| `list` | `{status?: "pending" \| "approved" \| "rejected"}` | `{ok, items: [...]}` |
| `get` | `{approval_id}` | `{ok, item: {...}}` |
| `status` | `{}` | `{ok, pending_count, total}` |

`action` 是人可读的动作描述（例如 `"delete_memory"` / `"send_external"` /
`"overwrite_agent_field"`）。`detail` 是这次动作的结构化描述（谁对什么做什么），用户
看到它决定是否批准。`requester` 默认取 `msg.sender`，显式传入覆盖。

## 事件

| topic | payload | 时机 |
|---|---|---|
| `approval_queue.pending` | `{approval_id, action, requester, detail}` | enqueue 成功时 |
| `approval_queue.approved` | `{approval_id, action, requester, reason}` | approve 成功时 |
| `approval_queue.rejected` | `{approval_id, action, requester, reason}` | reject 成功时 |
| `approval_queue.decided` | `{approval_id, action, requester, decision, reason}` | approve 或 reject 任一触发（聚合事件，方便 requester 只订阅一个 topic 判两种结果） |

**常见订阅模式**：requester agent 在 enqueue 后订阅 `approval_queue.decided`，筛自己
的 `approval_id`，收到后按 `decision` 分支。

## 持久化

状态（整个 queue）存在 `checkpoint_store` 的 `approval_queue/state` 下。启动时读回，
所以进程重启不丢 pending 项（destructive 动作不能因为重启就跳过批准）。

## 为什么是 agent 不是 core

批准策略会进化（v0.2 加宽限 / v0.3 加角色）。Core 只保证 Bus 本身不崩，不应该知道
"什么算 destructive"。更重要的是，按 `project_yuxu_vision.md` 的 bootstrap 式治理原
则，将来合并 agent 自举时，分类 / 批准规则本身都要能通过用户反馈迭代 —— 这条路径必
须是 agent，不能硬编码在内核。
