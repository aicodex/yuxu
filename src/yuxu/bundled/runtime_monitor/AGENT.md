---
driver: python
run_mode: persistent
scope: system
edit_warning: true
ready_timeout: 5
---
# runtime_monitor

Per-serve registry of running yuxu instances on the current machine.

## What it does

1. **On startup** writes `~/.yuxu/runtime/<project_slug>.json` with:
   ```
   {pid, started_at, project_dir, yuxu_version, adapters}
   ```
2. **Periodically** (default 30s) scans `~/.yuxu/runtime/` and **prunes
   entries whose pid is dead** — so stale files from crashed serves don't
   linger.
3. **On graceful shutdown** removes its own entry.
4. Exposes `list()` via bus so other agents can know what's running.

## Why an agent, not just files

The file format alone is fine for observability, but the **liveness-check
+ stale cleanup loop** is a live responsibility. A bundled agent with
`run_mode: persistent` is the right vehicle — it fits yuxu's existing
lifecycle contract (start / stop / ctx.ready()).

## Operations

| op | payload | 返回 |
|---|---|---|
| `list` | `{include_stale?=false}` | `{ok, entries: [{pid, alive, started_at, project_dir, ...}]}` |
| `self` | `{}` | `{ok, entry: {...}}` — this serve's own registered data |
| `prune` | `{}` | `{ok, removed: int}` — force stale-sweep now |

## CLI companion

`yuxu ps` reads the same dir + does the same liveness check + prints a
table. It does NOT require yuxu serve to be running (works offline).

## File format

`~/.yuxu/runtime/<slug>.json` where `<slug>` is derived from project path
(`project_dir.name` or cwd name). File written with `os.replace()` for
atomicity. Stale entries removed by any live runtime_monitor or by
`yuxu ps` on read.

## v0 约束

- One monitor agent per serve (runs inside its daemon)
- `started_at` is ISO-8601 UTC
- Prune interval fixed 30s; no backoff
- Only tracks local-machine serves (no remote)
