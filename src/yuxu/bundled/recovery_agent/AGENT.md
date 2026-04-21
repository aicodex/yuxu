---
driver: python
run_mode: persistent
scope: system
edit_warning: true
depends_on: [checkpoint_store]
ready_timeout: 10
---
# recovery_agent

Checkpoint 观察者 + 清道夫。

**定位**：业务 agent 自己决定怎么 resume（读自己 namespace 的 checkpoint）。
recovery_agent 只负责：
1. 启动时扫全仓 checkpoint，做一次 inventory
2. 按年龄分类（fresh/stale/abandoned）供前端/supervisor 查询
3. 按策略 GC 过期文件

## 阈值（默认）

| 状态 | 年龄 |
|---|---|
| `fresh` | < 1 小时 |
| `stale` | 1–24 小时 |
| `abandoned` | > 24 小时 |

可用环境变量 `RECOVERY_FRESH_SEC` / `RECOVERY_STALE_SEC` 覆盖。

## 操作（通过 `bus.request("recovery_agent", {...})`)

| op | 返回 |
|---|---|
| `status` | `{ok, scanned_at, inventory: {ns: {keys: [{key, saved_at, age_sec, category}]}}, counts}` |
| `rescan` | 重新扫描，返回同 `status` 结构 |
| `gc` `{max_age_days: N}` | 删除 `saved_at` 超过 N 天的 checkpoint，返回 `{ok, deleted: [...]}` |

## 事件

启动扫描完成时发布 `recovery_agent.scan_complete`，payload = inventory 摘要。

## 为什么是 agent 不是 core

恢复是"启动后"才做的事，不在 boot 路径；分类阈值、GC 规则是策略，会随运维
经验演进。业务 agent 自己 resume 自己的 checkpoint，recovery_agent 只做观察清理。
