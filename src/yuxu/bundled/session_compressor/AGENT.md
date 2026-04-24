---
driver: python
run_mode: persistent
scope: system
edit_warning: true
depends_on: [context_compressor]
optional_deps: []
ready_timeout: 5
---
# session_compressor

永久记忆管道的写盘端。把原始 session JSONL 压成 memory entry 存到
`<project>/data/memory/sessions/<YYYY-MM-DD>-<uuid8>.md`，带完整 frontmatter，
让 memory skill 的 L0/L1/L2 渐进式披露直接能查到。

## Why agent not script
- 有生命周期：订阅 bus 事件（`session.archived`），不只是手工脚本
- 写盘副作用按 kernel invariants 不归 skill
- 将来 session 归档由 `archive_session.sh` / CC hook / gateway 多入口触发，
  bus 事件是统一聚合点

## Flow

```
session.archived {jsonl_path}  ──┐
                                  │
bus.request(compress_jsonl, ...) ─┼──> _compress_and_write()
                                  │       │
                                  │       ├── format_jsonl_transcript
                                  │       ├── context_compressor.summarize
                                  │       │     target = max(5000, est_tokens * 0.1)
                                  │       │     cap at 50_000
                                  │       ├── extract description (first sentence of
                                  │       │   Primary Request section)
                                  │       ├── write `<mem_root>/sessions/<date>-<id>.md`
                                  │       │   with frontmatter
                                  │       └── publish session.compressed
                                  │
                                  └─ returns memory entry path
```

## Frontmatter 输出契约

```yaml
---
name: Session <YYYY-MM-DD> <uuid8> — <description first 60 chars>
description: <first sentence from compressed body, truncated 200 chars>
type: session
scope: project
evidence_level: observed
status: current
tags: [session]
originSessionId: <full UUID>
source_path: <abs path to source JSONL>
source_bytes: <int>
compressed_bytes: <int>
compression_ratio: <float 0-1>
updated: YYYY-MM-DD
---
<compressed body, CC-style 9-section markdown>
```

搜索友好：description 里有核心关键词 → `memory.search` 能查到；
tags=[session] 让 `memory.list {tags: [session]}` 过滤出全部 session 条目。

## Operations

| op | payload | 返回 |
|---|---|---|
| `compress_jsonl` | `{jsonl_path, target_tokens?, memory_root?, pool?, model?}` | `{ok, memory_entry_path, source_bytes, compressed_bytes, compression_ratio, elapsed_ms}` |

## 事件

| topic | payload | 用途 |
|---|---|---|
| `session.compressed` | `{originSessionId, memory_entry_path, source_bytes, compressed_bytes, compression_ratio, elapsed_ms}` | 将来 iteration_agent 给压缩器打分；I10 实践检验 |

## 订阅

`session.archived` — payload 需含 `jsonl_path` 字段。`archive_session.sh`
归档完成后 publish 这个事件即可自动触发（脚本集成留后）。

## 不做

- **不清理原始 JSONL**：raw 留 `sessions_raw/` 作为 audit 源（I6 archive-
  don't-delete）
- **不去重**：重复压同一份 session 会覆盖同路径的 entry（idempotent by
  originSessionId）
- **不 meta-compress**：老 entry 二次压缩等 iteration_agent 的动态压缩策略
  再说

## 策略常量

- `TARGET_RATIO = 0.1` — 默认压到原体积 10%
- `MIN_TARGET_TOKENS = 5000` — 下限，太小的 session 不过度压
- `MAX_TARGET_TOKENS = 50000` — 上限，超大 session 也不搞成小说
- `MAX_DESCRIPTION_CHARS = 200`

env override: `SESSION_COMPRESSOR_TARGET_RATIO` /
`SESSION_COMPRESSOR_MIN_TOKENS` / `SESSION_COMPRESSOR_MAX_TOKENS`
