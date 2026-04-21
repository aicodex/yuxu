# Subscription Model — 设计稿 v0.2

> Status: **draft**, 不是 contract。这份文档描述 yuxu 下一阶段要加的一层
> 抽象——把 dashboard、通知、approval、定时推送、memory 更新确认等多种
> 用户交互统一成"信息源 + 订阅"模型。
>
> 写出来先评审。核心 API 稳定后再提升进 `CORE_INTERFACE.md` 或
> `bundled/feed/AGENT.md`。
>
> **v0.2 变更**：第一轮评审后收敛了 agent 类型分类、持久化、打断策略、
> 跨进程、敏感信息脱敏、订阅分级六个决定，都已写入正文。v0.1 里 §8 的
> 开放问题大多已关闭。

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

**设计原则**：多实例和单实例在 bus/context/memory 上行为**完全一致**，差
异**只**在 loader 和 name key 那一层。Agent 作者写代码时**不需要**知道
自己被起成几个实例。

#### 3.1.1 Agent 类型

AGENT.md frontmatter 新增/明确：

```yaml
instances: single | multi          # 默认 single；与 singleton 类型互斥
run_mode: persistent               # 已有：persistent | terminating | scheduled | triggered | one_shot
recovery: off | supervise          # persistent 型默认 supervise
```

**四种类型**（从行为差异角度分）：

- **singleton**：全系统最多启动 1 个。典型：loader-adjacent 基础服务
  （llm_driver / project_supervisor / feed / approval）。
- **persistent**：永远跑。挂了由 `project_supervisor` / `recovery_agent`
  自动拉起。典型：gateway、memory_store、feed、daily schedulers。
- **terminating**：跑到终止条件就退。退出是**正常结束**，不算 failure。
  典型：theme_cluster 处理完一批结果退、调研论文 agent 完成任务退、
  one-shot ETL。
- **pure-python**：极少数连 AGENT.md 都不读的纯函数工具。默认走 AGENT.md
  约定；这类 agent 不经常出现。

一个 agent 可以**组合**：例"调研论文 agent" = `terminating + multi`，
用户同时起 3 个（调研 AI / 调研医学 / 调研自选），各自完成任务后退出。

#### 3.1.2 多实例

- **single**：loader 最多起 1 个；bus register key = `{name}`。
- **multi**：loader 可起 N 个；bus register key = `{name}#{instance_id}`。
  `instance_id` 由实例化方式决定——gateway 用 `platform`；调研论文 agent
  可用用户起的 slug；任何 agent 作者定义的稳定字符串都行。

**多实例的 ready 语义：交给下游 agent 自己决定。**

框架只提供 `bus.ready(name#id)` 原语。"聚合 ready"（例如 3 个调研 agent
都 ready 才算 agent 整体 ready）**不是**框架职责——因为"可变个数"的
agent 存在（用户想起几个起几个），没有通用的聚合语义。

**Loader API 新增**（Phase 5 再做）：
- `loader.spawn(name, instance_id, overrides={...})`
- `loader.get_instances(name) -> list[str]`

*本 doc 保证订阅抽象对单/多实例都 work；loader 多实例实现细节不在本 doc 范围。*

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

**敏感信息脱敏（硬约束）**：

feed 在 `publish()` 时做字段名黑名单过滤：payload 里任何命中
`api_key` / `secret` / `token` / `password` / `bearer` 关键词的字段
**mask 为 `***`** 或 **reject 整条 publish**（由 feed 策略配置，默认 mask）。

理由：一旦订阅这个 source 的消费者里有一个 `gateway://` sink 推到用户
聊天，key 就泄露到外部平台了。source owner 可能忘了 sanitize，feed 必须
拉这道保险。

agent 注册 source 时可以声明**白名单字段**，不在白名单里的 key 命中
黑名单就过滤。典型：

```python
ctx.feed.register_source(
    id="gateway_config_snapshot",
    kind="state", scope="project",
    allowed_fields=["adapters", "platforms_ready", "pairing_required"],
    # api_key/app_secret 等自动被 mask
)
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

**Lifetime（订阅生命周期，三档）**：

订阅时声明：

| lifetime | 语义 | 可否退订 | 典型 |
|---|---|---|---|
| `forced` | 系统级强制订阅，目标用户/agent 无法 unsubscribe | 否 | 服务崩溃告警、配对请求通知、memory approval |
| `one_shot` | 存活到**明确结束信号**（关卡、源完成、sink 断开） | 自动 | `/dashboard` 订阅 |
| `durable` | 除非显式 unsubscribe 否则一直在，daemon 重启后恢复 | 是 | `/subscribe daily_summary` |

**订阅持久化按 lifetime 分档**：

- `durable` + `forced`：**必须持久化**。feed 把订阅元数据（source, sink,
  filter, priority, lifetime, owner_agent）写 `<project>/.yuxu/subs.yaml`；
  daemon 重启时加载、重新 attach。`forced` 持久化保证用户重启后立刻又
  被订阅。
- `one_shot`：**不持久化**。daemon 崩了就丢，用户得重新发 `/dashboard`。
- **stream kind 的消费位点必须 checkpoint**（durable stream 必做；
  one_shot stream 不做）：存在 `<project>/.yuxu/sub_cursors.yaml` 或
  每订阅一个小文件，重启后从上次位点续——避免丢消息。
- **state kind 不持久化值**：最新值重推一次就补齐了，不需要历史。

**强制订阅的覆盖范围**：system agent 启动时声明哪些 source 是 forced +
作用对象（all users / admin only / specific session）。MVP 只做
"forced for all gateway sessions"；细粒度白名单后续再加。

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

**决定**：高优 approval 到达时，**dashboard 等低优消费者直接 unsubscribe
+ 关卡**；**不自动恢复**。用户想继续看，自己再敲 `/dashboard`。

```
t=0   user: /dashboard
t=0.1 feed: dashboard subscribed loader/state, lifetime=one_shot, pri=0
t=0.2 gateway sink: draft open, 1s refresh loop on
...
t=5.0 theme_cluster agent: ctx.feed.open_approval(ttl=86400, scope=project)
t=5.0 feed: 本 session 存在低优 one_shot 订阅 → 直接 unsubscribe + 关卡
            (触发 gateway sink 的 close：draft 最后一帧 footer = 📴 Exited)
t=5.1 gateway sink: 发 approval 卡，"批准 memory 更新？ /y /n"
t=5.2 user: /y
t=5.2 gateway sink: 把 /y 路由回 feed → approval.resolve("approved")
t=5.2 feed: 通知 source owner 结束 approval
    (不恢复 dashboard；用户自己再敲 /dashboard)
```

**理由**：自动恢复 = 把 approval 挤下去再挤上去，视觉抖动大；用户重启成本
极低（一个命令），不如把控制权交回用户。

dashboard 本身对"被打断"无感——它只 subscribe 一个 source，unsubscribe
是 feed 按 priority 判定触发，对 dashboard agent 透明。

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

### 6.1 跨进程拓扑

v0.1 所有 agent 跑在同一个 daemon 进程里。Phase 4 引入 project_supervisor
隔离某些 agent 到子进程后：

```
daemon process (main bus)          supervised child process
 ┌─ feed ─────────┐                 ┌─ theme_cluster ─┐
 │ subs registry  │   bus bridge    │ (isolated)      │
 │ persistence    │◄──(sock/pipe)──►│                 │
 └───┬─────────┬──┘                 └─────────────────┘
     │         │
 fn:// sinks   bus:// sinks         注意：跨进程后 fn:// 失效，
                                     所有订阅者跨进程一律走 bus://
```

规则：
- **同进程**：`fn://` / `bus://` / `gateway://` 都能用
- **跨进程（同一 project）**：`fn://` 失效，feed 自动改走 `bus://`
  + project_supervisor 的 bus bridge，**可见性对订阅者透明**
- **全局 scope**：读取任何 project 都行；publish **需要授权**
  （`~/.yuxu/grants.yaml` 白名单；未在白名单的 agent 尝试 publish
  被 feed 拒绝并记 warning）

---

## 7. 落地路线图

**Phase 1 — feed 最小核**（state + stream + 持久化 + 脱敏）
- `bundled/feed/` 新 agent，bus ops `subscribe/unsubscribe/publish/list_*`
- 三档 lifetime：`one_shot` / `durable` / `forced`
- sink 只实现 `bus://` 和 `fn://`（`gateway://` 下阶段）
- 订阅元数据持久化到 `<project>/.yuxu/subs.yaml`（durable/forced）
- stream 消费位点 checkpoint 到 `<project>/.yuxu/sub_cursors.yaml`
- publish 时字段名黑名单脱敏（`api_key`/`secret`/`token`/`password`/`bearer`）
- 测试：注册源、订阅、推送、退订、广播、throttle、脱敏、重启恢复

**Phase 2 — gateway sink + dashboard 重写**
- feed 的 `gateway://` sink：state 类用 DraftHandle、stream 类 send 新消息、
  approval 类发带 y/n 引导卡
- dashboard 改成 feed 消费者（lifetime=one_shot），去掉自己的 refresh loop
- 打断策略实现：高优到达 → 低优 one_shot 直接 unsubscribe + 关卡（不恢复）

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

## 8. 已决决定与开放问题

### 8.1 已决（v0.2 收敛）

| # | 议题 | 决定 |
|---|---|---|
| 1 | **订阅持久化** | `durable` / `forced` 必持久化（订阅元数据 + stream 消费位点），`one_shot` 不持久化；state 不持久化值。见 §3.4 |
| 2 | **多实例 ready 语义** | 框架不聚合；`bus.ready(name#id)` 是原语，是否聚合由 agent 自己决定。见 §3.1 |
| 3 | **跨进程订阅可见性** | 同一 project 下**跨进程可见**；全局 scope **读随意、publish 需授权**。feed 感知进程边界，跨进程走 `bus://` + control_socket（见 §5） |
| 4 | **source 权限** | global scope：**读任意 agent**；**publish 需要预先授权**（MVP 手动 yaml 白名单，v0.2 再做 `yuxu grant` 子命令） |
| 5 | **订阅过期分级** | 三档 lifetime：`forced` / `one_shot` / `durable`。见 §3.4 |
| 6 | **优先级打断策略** | 高优 approval 到达 → 低优 one_shot 订阅**直接 unsubscribe + 关卡**，**不自动恢复**。见 §4.2 |
| 7 | **敏感信息脱敏** | feed 在 publish 做字段名黑名单过滤（key/secret/token/password/bearer）；默认 mask；agent 可声明 `allowed_fields` 白名单。硬约束。见 §3.3 |

### 8.2 仍开放

1. **强制订阅覆盖范围粒度**：system agent 声明哪些 source 是 forced +
   作用对象。MVP 只做"forced for all gateway sessions"；细粒度白名单
   （admin-only / per-role / per-session）格式未定。
2. **持久化 store 后端**：MVP yaml 文件；规模上去了切 sqlite？门槛在哪。
3. **全局 publish 授权协议**：手写 yaml 还是 CLI（`yuxu grant
   global-publish <agent>`）？等 control_socket IPC 做完再决定。
4. **订阅者能不能跨 project**：目前跨进程是同一 project 内；跨 project 的
   订阅属于"多 project 联邦"，本 doc 明确**不覆盖**，等 yuxu 真正跨
   project 跑时再议。
5. **filter 的 fn 模式**：MVP 字面匹配；fn 指向进程内 predicate，跨进程
   转发时怎么处理（序列化？重求值？）留待 Phase 2。

---

## 9. 后续步骤

- 本 doc 是 checkpoint，Phase 1 动代码前最后一次评审
- Phase 1 起一个 PR：feed agent + state/stream 两个 kind + bus/fn 两个 sink
  + 持久化（durable/forced）+ 敏感信息脱敏
- `project_subscription_abstraction.md` memory 与本文件同步更新
