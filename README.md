# yuxu（玉虚）

> 玉虚宫是元始天尊孕育、教化、护持弟子的宫阙。
> This framework takes the same role for your agents.

**yuxu** is a **long-running agent creation and supervision system** —
not a chatbot SDK, not a one-shot task runner, not an LLM wrapper. It is an
always-on runtime where agents are born from dialogue, live forever, burn
your token plan with productive background work, and stay alive through
crashes and deploys.

## 核心定位

- **7×24 自主运行**：进程级守护（systemd 等），内部有 watchdog agent 拉起崩溃 agent
- **Token plan 榨干机**：按 token 预算自动分配 agent 调度，余额多时拉起探索 agent，低时降级/暂停
- **用户 = 观察者 + 决策者**：交互入口是 Telegram / 飞书 / 网页，长得像 shell（`user@/project/agent: ...`），但底下是 chat 消息 + 事件订阅
- **一切皆 agent**：核心服务（checkpoint / rate limit / LLM / 调度 / 通知）都是内置 agent，不在 core 里硬编码；用户同名覆盖即可替换
- **接口冻结**：Core API（Bus / Loader / AgentContext）作为公开合约长期稳定，下游 agent 可以放心依赖

## 状态

**极早期骨架 —— 核心代码尚未迁入。** 当前主要在
[theme-flow-engine](https://github.com/aicodex/theme-flow-engine)
仓库里孵化（`src/core/` + `src/agents_bundled/` + `templates/`）。
骨架成熟后会整体搬到本仓库发布成 pip 包。

关注点（概念已定稿，详见 theme-flow-engine 的 `docs/CORE_INTERFACE.md`）：
- Agent 三函数合约：`async def start(ctx)` / `async def stop(ctx)` / `def get_handle(ctx)`
- `AgentContext` 字段只增不减（frozen dataclass）
- 五种 `run_mode`：persistent / scheduled / triggered / one_shot / spawned
- 三作用域 skill：global / project / agent；安装 = 有文件夹，启用 = 记录表
- 真 CLI `yuxu` 只给运维用（serve / init / status），**用户交互全走 chat + shell agent**

## 后续里程碑

- [ ] 代码迁入：core / bundled / templates / tests 搬过来
- [ ] CLI 骨架：`yuxu init` / `yuxu serve` / `yuxu status`
- [ ] 新增系统 agent：gateway（前端接入）/ shell（用户 shell UX）/ notification（订阅推送）/ token_budget（预算管理）
- [ ] pip 发布：`pip install yuxu`

## 许可

（待定）
