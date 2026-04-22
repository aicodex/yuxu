---
driver: python
run_mode: persistent
scope: system
edit_warning: true
ready_timeout: 5
---
# checkpoint_store

本地文件系统 checkpoint 持久化服务。Agent 可以在任意时刻保存/读取自己的状态
（工作进度、中间产出、对话快照），进程重启后由 recovery_agent 决策 resume。

## 操作（通过 `bus.request("checkpoint_store", {...})`）

| op | payload | 返回 |
|---|---|---|
| `save` | `{namespace, key, data}` | `{ok: true, path}` |
| `load` | `{namespace, key}` | `{ok: true, data, saved_at}` 或 `{ok: false, error: "not_found"}` |
| `list` | `{namespace}` | `{ok: true, keys: [...]}`（字典序） |
| `list_namespaces` | `{}` | `{ok: true, namespaces: [...]}` |
| `delete` | `{namespace, key}` | `{ok: true}` 或 `{ok: false, error: "not_found"}` |

## 存储位置

默认 `data/checkpoints/{namespace}/{key}.json`。可通过环境变量 `CHECKPOINT_ROOT` 覆盖根目录。

## 文件格式

```json
{
  "version": 1,
  "namespace": "...",
  "key": "...",
  "saved_at": "<ISO8601 UTC>",
  "data": <agent payload>
}
```

## 可靠性

- 原子写入：写 `.tmp` 后 `os.replace()`，避免半写入文件
- 并发：`handle()` 按 namespace 加 `asyncio.Lock`，同 namespace 内所有 op 串行，
  防 `.tmp` 文件互踩 / load 撞到 mid-write 不一致状态。不同 namespace 并行
- namespace / key 只允许不含路径分隔符 / `..` / 前导 `.`，防止越狱
- 同步 IO：小 checkpoint 无需包装；大文件（MB 级）后续再 `asyncio.to_thread`

## 为什么是 agent 不是 core

文件 IO + 存储格式是业务策略（后续可换 Redis / S3），**不在 bootstrap 路径**。
符合 `docs/CORE_INTERFACE.md` 的归属规则：能做成 agent 的就做成 agent。
