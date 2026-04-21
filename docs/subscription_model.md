# Subscription Model — 设计稿 v0.1

> Status: **draft**, 不是 contract。这份文档描述 yuxu 下一阶段要加的一层
> 抽象——把 dashboard、通知、approval、定时推送、memory 更新确认等多种
> 用户交互统一成"信息源 + 订阅"模型。
>
> 写出来先评审。核心 API 稳定后再提升进 `CORE_INTERFACE.md` 或
> `bundled/feed/AGENT.md`。

---

## 0. 术语对照

| 术语 | 含义 |
|---|---|
| **Bus** | 已有，core 里。无状态、topic-based 事件总线。基础设施。 |
| **Info Source** | 本文档引入。一个 agent 暴露出去的"值流"或"事件流"。有 id、kind、scope。 |
| **Subscription** | 本文档引入。{source, sink, filter?, priority?} 的绑定。feed 管生命周期。 |
| **Feed** | 本文档引入。bundled agent，维护 `{source → [subscription]}` 表，做路由和节流。 |
| **Sink** | 订阅的出口：`gateway://session_key`（推到用户聊天）、`bus://topic`、`fn://local`。 |
| **Agent Instance** | 一个 agent spec 的一次运行实例。单实例 agent 只有 1 个；多实例 agent 可 N 个。 |
| **Session Mem** | 某个 agent 实例（或其内部一个会话）私有的 memory 桶。 |

---

## 1. 动机

用户提到的场景：

1. **Dashboard** — 用户发 `/dashboard`，聊天里一张卡每 1s 刷新，展示
   agent 状态、token 用量、title 等。
2. **Memory 更新通知** — agent 想改自己的 memory，要等用户批准。
   project 级别 24h 不回就自动批准；全局级别必须等用户拍板。
3. **盘后总结订阅** — "每天 15:30 给我推一条盘后总结"。
4. **Agent title / context 消耗** — 用户想随时看某个 agent 现在在忙什么、
   上下文用了多少。
5. **配对请求通知** — 有陌生人给 bot 发了消息，需要提醒管理员。
6. **Dashboard 被高优打断** — dashboard 高速刷新过程中，memory approval
   到了，应该抢占显示位。

这些表面看各不相同，**本质都是**：
- 某个 agent 里有个"可观察的东西"
- 别的 agent / 外部用户想"订阅"它
- 订阅出口是 gateway 聊天、bus 内部、或 Python 回调

现在每个需求都要手写一套路由、节流、持久化、优先级——显然该抽出来。

---

## 2. 核心观察（来自 2026-04-21 对话）

- **用户 ≈ agent 的一个 session**：用户身份不是独立实体，而是"某个 agent
  实例里的一次会话"。一个真人在 feishu + telegram 各开一个聊天 =
  gateway 的两个平台实例里各一个 session。
- **Memory 三层**：session mem（实例私有）、project mem（项目共享）、
  global mem（跨项目、~/.yuxu 下）。订阅 scope 必须绑到这三层之一。
- **Agent 可单例可多实例**：大多数 bundled agent 单例；gateway 天然
  多实例（一平台一实例）。多实例共享 project/global mem，不共享 session mem。
- **Dashboard 不特别**：它只是"高频 state 订阅的一个消费者"，没有任何理由
  在 core 或 gateway 里给它开后门。

---

## 3. 三大抽象

### 3.1 Agent 实例模型

AGENT.md frontmatter 新增：

```yaml
instances: single | multi     # 默认 single
```

- **single**：loader 最多起 1 个，name 就是注册名。
- **multi**：loader 可起 N 个，每个实例有 `instance_id`（由实例化方式决定，
  例 gateway 用 `platform`）。bus register key 是 `{name}#{instance_id}`。

现有所有 bundled agent 不动（隐式 `single`）。只有 gateway 将来改
multi（feishu 实例 + telegram 实例）。

**Loader API 新增**（后加，不属于这次订阅落地）：
- `loader.spawn(name, instance_id, overrides={...})`
- `loader.get_instances(name) -> list[str]`

*本 doc 暂不展开多实例 loader 的全部细节，只保证订阅抽象对单/多实例都 work。*

### 3.2 Memory 三层

```
~/.yuxu/mem/                           ← global mem（跨 project）
  user_profile.yaml
  notifications.ndjson

<project>/.yuxu/mem/                   ← project mem（同项目共享）
  <agent_name>/
    facts.yaml
  _shared/
    themes.yaml

<project>/.yuxu/mem/_sessions/         ← session mem（实例/会话私有）
  <agent_name>[#<instance_id>]/
    <session_key>/
      history.ndjson
      scratch.yaml
```

约定：
- agent 读写 memory 一定走 `ctx.mem.session(...)` / `ctx.mem.project(...)` /
  `ctx.mem.global_()` 三个接口，不直接碰路径。
- 多实例 agent 的 session mem 自动带 `#instance_id`。
- 订阅系统对三层 mem 的变更事件都可以成为 info source（见 3.3）。

*实现留给 `bundled/memory_store` agent。本 doc 只定 scope 概念。*

### 3.3 Information Source

一个 agent 在 `start()` 里声明自己暴露哪些源：

```python
async def start(ctx):
    ctx.feed.register_source(
        id="dashboard_snapshot",
        kind="state",             # state | stream | approval
        scope="project",          # session | project | global
        throttle=1.0,             # state only, 默认 0.25
        schema={"agents": "list", "tokens": "dict"},
    )
    ...
    await ctx.ready()
```

**kind 三选一**：

| kind | 语义 | 例子 |
|---|---|---|
| `state` | "最新值"，snapshot 语义。新值盖旧值，订阅者只在乎当下。带 throttle。 | dashboard snapshot、agent title、context token 消耗 |
| `stream` | 事件流，每条不能丢。订阅者是消息队列消费者。 | 盘后总结、系统日志、配对请求事件 |
| `approval` | 一个待决事务。source owner 发起，订阅者（或 gateway 用户）回 y/n。带 TTL。 | memory 更新、non-trivial 操作确认 |

**scope**：决定订阅谁能看到。session 源只有同一个 session 里的订阅者能订；
project 源同 project 任何 agent 都能订；global 源跨 project。

**推送**（source owner 侧）：

```python
# state: 直接 set
await ctx.feed.publish("dashboard_snapshot", snapshot_dict)

# stream: append
await ctx.feed.publish("daily_summary", {"date": "...", "text": "..."})

# approval: 特殊 API（见 3.5）
```

### 3.4 Feed（订阅中枢）

Bundled agent。bus ops：

| op | payload | 返回 |
|---|---|---|
| `subscribe` | `{source_ref, sink, filter?, priority?, mode?}` | `{ok, sub_id}` |
| `unsubscribe` | `{sub_id}` | `{ok}` |
| `publish` | `{source_ref, payload, ts?}` | `{ok}` |
| `list_sources` | `{scope?}` | `{ok, sources: [...]}` |
| `list_subs` | `{sub_id? / sink?}` | `{ok, subs: [...]}` |

**`source_ref` 格式**：`{agent_name}[#{instance_id}]/{source_id}`
例：`gateway#feishu/command_invoked`、`dashboard/snapshot`。

**Sink 三种**：
- `gateway://{session_key}` → 推到用户聊天。state 源用 draft edit 复用一张卡；
  stream 源发新消息；approval 源发带按钮的卡（或纯文本引导 y/n 回复）。
- `bus://{topic}` → Bus publish；让普通 agent 不用学 feed API 也能消费。
- `fn://{callable_ref}` → Python 本地回调。仅进程内，最快，不跨 IPC。

**Priority**：
- 0 = 默认（fire-and-forget）
- 10 = 重要（approval 默认）
- 100 = 关键（系统 alert）

gateway sink 收到高优先级推送时可以**打断**低优先级的"正在播放"流
（例：dashboard 刷新到一半，approval 进来，feed 告诉 gateway 先暂停 dashboard
draft，推完 approval 再恢复）。具体打断策略由 gateway sink 决定，不是 feed
的职责；feed 只负责把 priority 透传。

**Filter**（可选）：`{key: value}` 字面匹配，或 `{fn: "dotted.path"}`
指向本进程的一个 predicate。MVP 只做字面匹配，fn 留给 Phase 2。

**Mode**：
- `push`（默认）：有新值/事件就主动推给 sink。
- `pull`：sink 主动问，feed 返回最新 state（仅 state kind 有意义）。

### 3.5 Approval（特化消费者）

Approval 类源不是 `feed.publish()` 推 value，而是开一个待决对象：

```python
req = await ctx.feed.open_approval(
    id="mem.update.theme_rank",
    title="Memory update: theme_rank.facts.yaml",
    diff="- ... \n+ ...",
    scope="project",
    ttl_auto_approve=86400,        # 24h 不回 → 自动批准
    # ttl_auto_reject / ttl_noop 二选一
)
decision = await req.wait()        # 返回 'approved' | 'rejected' | 'timeout'
```

feed 内部维护一个 approval 桶，每个请求被广播给所有 scope 命中的订阅者
（典型：gateway 把用户的 y/n 消息路由回 feed）。

approval 是第一个跨 session mem 的用例——同一个 approval 请求可能推到
用户的 feishu 和 telegram 两个 session，任何一边回 y 都作数。

---

## 4. 关键场景

### 4.1 Dashboard 重写为 feed 消费者

目前的 dashboard 轮询 `loader.get_state()` 自己渲染。改造后：

- `loader` 注册 `loader/state` (kind=state, throttle=1s)，自己在 agent
  状态变化时 publish。
- dashboard 的 `/dashboard` handler：
  ```python
  await ctx.feed.subscribe(
      source_ref="loader/state",
      sink=f"gateway://{session_key}",
      priority=0,
  )
  ```
- 用户发任何消息 → dashboard `unsubscribe` → gateway sink 关闭 draft。

所有刷新、节流、finalize 都在 feed + gateway sink 做，dashboard 本身
不再持 `asyncio.Task`。

### 4.2 Memory approval 打断 dashboard

```
t=0   user: /dashboard
t=0.1 feed: dashboard subscribed loader/state, pri=0
t=0.2 gateway sink: draft open, 1s refresh loop on
...
t=5.0 theme_cluster agent: ctx.feed.open_approval(ttl=86400, scope=project)
t=5.0 feed: 广播给所有 project-scope 订阅者中 priority ≥ 10 的 sink；
            本 session 的 dashboard draft 被 gateway sink 暂停
t=5.0 gateway sink: 新消息：approval 卡，"批准 memory 更新？ /y /n"
t=5.1 user: /y
t=5.1 gateway sink: 把 /y 路由回 feed → approval.resolve("approved")
t=5.1 feed: 通知 source owner，结束 approval；通知 dashboard sink 恢复
t=5.2 gateway sink: draft 恢复刷新
```

dashboard 本身对"被打断"无感——它只 subscribe 一个 source，其他都是
gateway sink 的策略。

### 4.3 盘后总结订阅

```python
# bundled agent "daily_market_summary" 每天 15:30 做：
await ctx.feed.publish("daily_summary", {
    "date": today(), "top_sectors": [...], "notes": "..."
})
```

用户侧：
```
/subscribe daily_market_summary
```
（这是个未来的 chat 命令，走 gateway.command_invoked → 发
`feed.subscribe` bus request → sink = `gateway://<session>`）

用户之后每天 15:30 收到一条 stream 推送。退订：`/unsubscribe daily_market_summary`。

### 4.4 配对请求通知

当前：`gateway.pairing_requested` 是个 Bus topic，没有 stateful 的订阅。
改造后：gateway 注册 `gateway/pairing_requested` 为 stream source，
notification agent 订阅 → sink=`gateway://<admin_session>`。

好处：管理员可以开/关通知、多个 admin 可以同时订阅、离线 admin 回来
后可以 pull（stream 的持久化由 feed 管，不是每个 source owner 各自重写）。

---

## 5. CLI ↔ Daemon IPC

### 5.1 为什么需要

现状：
- `yuxu pair approve` 写文件，gateway 轮询 mtime 热加载（刚实现的 MVP）。
- `yuxu serve` 之外所有子命令（list/status/setup）都是**无状态**的——只读
  文件、写文件，不跟运行中的 daemon 对话。

问题来了：
- 想从 CLI 看"现在 daemon 里谁在跑、谁 ready、谁崩了" → 需要 query daemon。
- 想 `yuxu subscribe <source>` 让 CLI 接收推送（用 stdout tail 调试
  订阅系统本身）→ 需要 daemon → CLI 推送。
- 想 CLI stop/restart 某个 agent → 需要 daemon 接命令。
- pair approve 想即时生效（不等 1s 轮询）→ 需要 CLI 主动敲 daemon。

这些都是同一个问题：**CLI 侧要能向 running daemon 发 bus.request / 订
bus.subscribe**。

### 5.2 协议草案

```
socket: <project>/.yuxu/run/daemon.sock   (unix domain socket)
        权限 600，owner = 启动 daemon 的用户
wire:   line-delimited JSON
message:
   request  →  {"id": int, "kind": "request", "topic": str, "payload": {...}}
   response ←  {"id": int, "kind": "response", "ok": bool, "payload"|"error": ...}
   event    ←  {"id": null, "kind": "event", "topic": str, "payload": {...}}
auth:   socket fs permissions only in v0.1; auth token 留给 v0.2
```

CLI 端实现：
- `yuxu status`：连 sock → `{topic: "loader", payload: {op: "list"}}` → 渲染。
- `yuxu pair approve`：写文件 **+** sock `{topic: "gateway", payload:
  {op: "pair_approve", ...}}` 让生效秒级；失败（daemon 没在跑）就只
  写文件（下次启动生效）。
- `yuxu subscribe <source>`（调试用）：sock 上订阅 bus `feed.push`
  topic，stdout 打印。

### 5.3 Daemon 侧

一个新的 bundled agent `control_socket`（或 `ipc`）：
- 起一个 `asyncio.start_unix_server`
- 每个连接 = 一个 "远端 bus session"：收到 request → 调本地
  `bus.request(topic, payload)`，结果回；收到 `subscribe` → 把 bus event
  发回 CLI。
- shutdown 时关闭所有连接。

和 gateway 的关系：**正交**。gateway 服务聊天平台，control_socket 服务
CLI/ops。它们都是把"外部请求"桥到 bus 的 adapter，只是协议不同。

### 5.4 为什么不用 HTTP/gRPC

- 单机 + 本地 CLI，unix socket 0 依赖
- 线路格式 line-delimited JSON + Python stdlib，无生成代码
- 将来要远程管理再加 HTTP adapter，不在 v0.1 范围

---

## 6. 与现有 Bus / Core 的关系

```
               ┌──────────── user ─────────────┐
               │                                │
               ▼                                ▼
         [gateway adapter]                [control_socket]
               │                                │
               └──────────────┬─────────────────┘
                              │
                              ▼
                            [Bus]
                              │
                ┌─────────────┼──────────────┐
                │             │              │
                ▼             ▼              ▼
             [feed]       [dashboard]    [theme_cluster]
                │
        ┌───────┼────────┐
        ▼       ▼        ▼
    (state) (stream) (approval)
```

- **Bus 不变**：无状态 topic 总线，feed / approval / control_socket 都架在上面。
- **Core 不变**：本次不动 core 一行。Loader 多实例支持留给后续单独的
  contract 升级。
- **所有新东西都是 bundled agent**：`feed`、`approval`、`control_socket`、
  `memory_store`（如果要上三层 mem）。

---

## 7. 落地路线图

**Phase 1 — feed 最小核**（state + stream，不含 IPC，不含 approval）
- `bundled/feed/` 新 agent，bus ops `subscribe/unsubscribe/publish/list_*`
- sink 只实现 `bus://` 和 `fn://`（`gateway://` 下阶段）
- 测试：注册源、订阅、推送、退订、多订阅者广播、throttle

**Phase 2 — gateway sink + dashboard 重写**
- feed 的 `gateway://` sink：state 类用 DraftHandle、stream 类 send 新消息
- dashboard 改成 feed 消费者，去掉自己的 refresh loop
- 打断策略：gateway sink 的 priority 抢占 MVP

**Phase 3 — approval**
- `open_approval` API、TTL 定时器、y/n 回流
- memory_store agent 的 update 流程接入 approval

**Phase 4 — control_socket IPC**
- unix socket adapter
- `yuxu status/subscribe/stop` CLI 子命令迁移到 IPC
- `yuxu pair approve` 走 IPC 立即生效（fallback 到文件模式）

**Phase 5 — 多实例 agent**
- Loader `spawn` / `instance_id` 支持
- Gateway 拆成 `gateway#feishu` / `gateway#telegram` 两个实例
- feed source_ref 的 `#instance_id` 格式激活

每个 Phase 都是一个独立 PR，可单独评审、单独回滚。

---

## 8. 开放问题

1. **持久化**：订阅本身要不要 checkpoint？重启后自动恢复？
   初步倾向：stream 的**消费位点**要持久化（避免丢消息），state 不用
   （最新值重推就行），订阅元数据（谁订了谁）写 yaml 里。

2. **多实例 agent 的 ready 语义**：每个实例各自 ready，还是 "agent 整体
   ready = 至少 1 个实例 ready"？倾向前者，给实例独立 bus 状态。

3. **跨进程订阅**：v0.1 所有 agent 在一个进程里，`fn://` sink 直通。将来
   分进程跑（project_supervisor 隔离某些 agent）时，`fn://` 失效，都得
   `bus://` 走——feed 需要感知跨进程 boundary 吗？

4. **source 权限**：global scope 的 source 任何 agent 能订吗？
   倾向 yes for 读，但 publish 要 scope owner 授权。

5. **订阅过期**：订阅永远不失效？还是带 TTL / heartbeat？
   倾向：`gateway://` sink 在 session 退出时自动取消；其他无 TTL。

6. **优先级与打断**：priority = 10 的 approval 进来时到底怎么打断
   priority = 0 的 dashboard？暂停 draft？开新卡？配置项？
   倾向：gateway sink 默认暂停 + 新卡，配置项调。

---

## 9. 后续步骤

- 收这份 doc 的评审意见
- Phase 1 起一个 PR：feed agent + state/stream 两个 kind + bus/fn 两个 sink
- 同步更新 `project_subscription_abstraction.md` memory（收敛设计决定）

写完这 9 节这份 doc 本身就是一个 checkpoint。真正动代码看评审意见再说。
