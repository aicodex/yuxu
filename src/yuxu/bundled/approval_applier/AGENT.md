---
driver: python
run_mode: persistent
scope: system
edit_warning: true
depends_on: [approval_queue]
optional_deps: [admission_gate]
ready_timeout: 5
---
# approval_applier

闭环 reflection → 真 memory write 的那一块。订阅 `approval_queue.decided`，
筛 `action == "memory_edit"` 的条目，已批就把 draft 内容落到目标路径，
已拒就只删 draft。不做 LLM 调用、不改 frontmatter，纯文件搬运工。

```
approval_queue.decided
  └─ filter action == "memory_edit"
      ├─ fetch full entry via bus.request(approval_queue, get, aid) → detail
      ├─ if decision == "approved":
      │    ├─ read <detail.draft_path>
      │    ├─ strip outer frontmatter (staging metadata)
      │    ├─ inner body is the real memory entry — atomic write to
      │    │   <memory_root>/<detail.proposed_target>
      │    └─ delete draft
      └─ if decision == "rejected":
           └─ delete draft
```

`memory_root` is derived as `draft_path.parent.parent`（draft 落在
`<memory_root>/_drafts/` 下，parent.parent 就是 root）。

## Operations

| op | payload | 返回 |
|---|---|---|
| `apply_draft` | `{draft_path, proposed_target, proposed_action}` | `{ok, target_path}` |

`apply_draft` 是手工出口，可用于测试或绕过 approval_queue 的直调。不
publish 事件；事件只有在经由 `.decided` 订阅路径时才发。

## 发出的事件

| topic | payload |
|---|---|
| `approval_applier.applied` | `{approval_id, target_path, action}` |
| `approval_applier.rejected` | `{approval_id, draft_path, archived_path}` |
| `approval_applier.skipped` | `{approval_id, reason}` |
| `approval_applier.gated` | `{approval_id, target_path, draft_path, archived_path, stages, verdict}` |

## v0 约束

- **只认 `memory_edit` action**；其他 approval_queue item 直接忽略
  （不 skip 事件，只是不插手）
- **只支持 `add` / `update` 两种 proposed_action**（reflection_agent v0
  产物边界）；`remove` 未来再加
- **`add` 时目标已存在 → skip + warn，不覆盖**；要覆盖发个 `update`
- **幂等**：draft 不存在（已被处理 / 手工删）→ skip + warn，不崩
- **admission_gate 写入闸**：approve 后、写盘前调用 `admission_gate.check`。
  任一 stage fail → draft 归档到 `_archive/gated/` + 发
  `approval_applier.gated`，不写入。Gate 未加载 / 报错 → 放行并记
  warning（gate 是优选，不是硬依赖）。手工出口 `apply_draft` 仍然跳过
  gate（用于测试和 debug）。

## 为什么是 agent

长跑订阅 + 需要感知 approval_queue 的生命周期事件 + 会做文件写入副作用
（按 kernel invariants 铁律，destructive 不归 skill）。

## 为什么不合并进 approval_queue

approval_queue 是通用审批基础设施（任何 action 类型都能走）。memory_edit
的具体 apply 逻辑（读 draft、切 frontmatter、原子写）是 memory 域的
业务，不该污染通用队列。
