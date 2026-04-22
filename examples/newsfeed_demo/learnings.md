# newsfeed_demo — M0 Learnings

Date: 2026-04-22
Driver: P7 dogfood-driven validation of yuxu's end-to-end chain
Status: **PASSED**（with 9 findings，都不阻塞 M0 结论）

---

## 跑了什么

**Run 1**（2026-04-22 01:02:48）
- AGENT.md 初版：`3 条 A 股热点新闻，markdown 格式`，无聚焦方向
- 结果：HTTP 200，status=ready，report 有效写出
- Tokens：prompt 262 / completion 551，用时 ~10s

**Run 2**（2026-04-22 01:03:38）
- AGENT.md 手改：加"聚焦北向资金 / 政策 / 新能源"+ 明令"不要输出 `<think>` 标签"
- 结果：HTTP 200，status=ready
- Tokens：prompt 323 / completion 285（**completion 降 ~48%**）用时 ~6.5s
- 内容聚焦度**显著提升** —— 3 条都踩中目标主题

---

## 什么跑通了（好消息）

1. **yuxu 端到端真能跑** —— Bus → Loader → rate_limit_service → llm_service → llm_driver → MiniMax HTTP → 回调 → 写文件。设计文档第 P5 条假设成立。
2. **one_shot agent lifecycle 正确**：`start(ctx)` 里 `await run_once()` 再 `return`，loader 自动 publish ready，进程干净退出。不需要特殊退出信号。
3. **AGENT.md body 当 system prompt 这条路工作**：handler 读 `self.ctx.body.strip()` 交给 llm_driver，用户通过改 markdown 文件迭代 prompt，不改 Python 代码。
4. **loader 的 bundled + 额外目录扫描**正常：`Loader(bus, dirs=[bundled_path, examples_path])` 把 12 个 bundled + 我放的 newsfeed_demo 一起发现。
5. **llm_driver 的 tool loop 在 max_iterations=1 无 tools 场景下**：直接单轮完成，stop_reason=complete。

---

## 撞到的洞（按严重度）

### 🔴 BLOCKER（开工前没想到，M0 撞到要补）

**F1. yuxu CLI 没有 `run <agent>` 子命令**
设计文档里写的 `yuxu run examples/newsfeed_demo` **不存在**。只有 `yuxu serve`（永远运行 daemon），`yuxu init/new/list/status/examples/pair/feishu`，没有"跑一次这个 agent 然后退出"。
→ 绕过方案：手写 `run_newsfeed_demo.py` 做 ephemeral boot。
→ 修复路径：未来给 CLI 加 `run <agent_dir>` 子命令，本质是 boot() + ensure_running(agent_name)。这是合理的 CLI 能力，可能 30-50 行代码。
→ **登记为 P1 Tech Debt**（阻塞 M0 风格的手工调试）。

**F2. llm_service 依赖"rate_limit_service 必须走 loader 正式 start"**
我一开始想绕 yaml，直接 `bus.register("rate_limit_service", rl.handle)` 手动注册。结果 llm_service 的 `ctx.get_agent("rate_limit_service")` 返回 None —— 因为那走的是 loader.modules（正式 start 的轨迹），手动 register 不填这个表。
→ 绕过：写临时 rate_limits.yaml + `os.environ["RATE_LIMITS_CONFIG"] = ...`，让 loader 正式 start。
→ **这是架构的一致性洞**：bus.register + loader.modules 两套路径没统一。文档应该明确"手动注册的 agent 无 get_handle 能力"，或者让 bus.register 同时更新 loader.modules。
→ **登记为 P2 Tech Debt**。

### 🟡 PRODUCT ISSUE（影响用户感知，但不阻塞）

**F3. MiniMax M2.7-highspeed 的 `<think>` 块无法通过 prompt 消除**
Run 2 prompt 里明令"不要输出任何 `<think>` 标签"，**依然输出 `<think>...</think>` 块**。这是模型的 reasoning 结构，不受 system prompt 控制。
→ 结果：report 里有大段思考过程，用户得 scroll 才能看到新闻。
→ 修复路径（两条）：
   - **P1 便宜**：llm_service 加一个 `strip_thinking_blocks: bool` 选项，regex 剥 `<think>...</think>`。
   - **P3 贵**：支持 MiniMax 的 reasoning API 路径（分离 reasoning 和 content 字段）。
→ **登记为 P1 Tech Debt**（真实用户体验会抱怨）。

**F4. 内容是纯 LLM 幻觉（no web_fetch）**
MiniMax 编出来的股票代码看起来像真的（招商银行 600036 / 宁德时代 300750 等），但**没有当天的真实新闻**。用户会认为这 demo 不可信。
→ 修复路径：加 `web_fetch` skill（接入公开新闻 API，如东方财富 / 雪球），喂给 LLM 做摘要。这是 **preopen_research 真正业务的关键一步**，v0.2 scope。
→ 登记为 P0 for preopen_research，**不阻塞 M0**（M0 本来就声明是 mock）。

### 🟢 OPERATIONAL（工程摩擦，容易 fix）

**F5. 环境变量拆成两套**（TFE_* 指向 MiniMax，OPENAI_* 指向 DeepSeek）
我 runner 默认 fallback 顺序 `LLM_* → OPENAI_* → TFE_*`，结果取了 OPENAI_BASE_URL=DeepSeek + TFE_MODEL=MiniMax-M2.7，模型和 endpoint 不匹配，400 Error "Model Not Exist"。
→ 调用侧必须**成对**指定 LLM_API_KEY + LLM_BASE_URL + NEWSFEED_MODEL。
→ 修复路径：rate_limits.yaml 的 account 应该用**预设好的 key 名**（例如 `api_key_env: TFE_API_KEY` 字段），rate_limit_service 自己读 env。避免调用侧拼字符串。
→ 登记为 P2 Tech Debt。

**F6. `NEWSFEED_MODEL` 默认 fallback 链在无 TFE_MODEL 时指向 `deepseek-chat`**
这个默认是我 runner 拍的，对 DeepSeek 可能对，对 MiniMax 错。**pool 和 model 的关系应该由 rate_limits 配置声明**，不由 runner 拍。
→ 同 F5，属于配置策略问题。

**F7. MiniMax 响应时间 6.5-10s**
对 one_shot demo 可接受，**对交互式 chat 会太慢**。preopen_research 隔夜跑没问题。
→ 未来 harness_pro_max 若要做流式 gateway 回复，必须**接 streaming**（llm_service 现在没做）。

**F8. 第一次运行我犯了配置错误但错误信息不够友好**
400 "Model Not Exist" 是从 API 原样透传的，没说"你的 pool=openai + model=MiniMax-M2.7 组合找的 base_url 不对"。要靠我看 httpx 日志才明白是 DeepSeek URL 在收 MiniMax 模型名。
→ llm_service 应该在 400 response 里注入上下文（`model={m} pool={p} base_url={url}`）帮 debug。
→ 登记为 P2 Tech Debt（DX 问题）。

**F9. 第一次 boot 时 llm_service 的错误被吃在 task crashed 里**
`ERROR yuxu.core.loader: agent llm_service task crashed` 然后 trackback —— supervisor 会重启。但 supervisor 没配，我是手工 bus + loader 跑的，整个进程报错退出。
→ 对 `yuxu serve` 正常流程这不是问题（supervisor 会 catch），对手工 boot 则看起来"错误被吞了又重新抛"。这是预期行为，文档化一下就好。

---

## 对 v0.1 scope 的影响

| 断言（design doc P5） | M0 验证后的状态 |
|---|---|
| core 端到端能串起来 | ✅ 验证。Bus + Loader + 核心 service 链正常 |
| AGENT.md 作为 prompt 的路径通 | ✅ 验证。手改 body + 重跑 = 迭代见效 |
| one_shot agent 能跑 | ✅ 验证。lifecycle 正确 |
| 凝结机制（v0.1 scope） | ⚪ M0 没触发（单次跑无 session memory 积累）。留给 preopen_research 驱动 |
| 业务驱动扩散的 scope ceiling | ⚪ 这次我**在 M0 之前就建了** approval_queue + scheduler。这违反了 P7。M0 验证了这两块**此刻未用**的事实 |

## 对 preopen_research 开工顺序的影响

按 audit 的"preopen_research 开工前，P0 Top 3 至少补 2 个"原则，加上 M0 新发现：

**必做（preopen_research 可行性门）**：
- [ ] F1：加 `yuxu run <agent>` CLI（30-50 LOC，提高开发可 iterate 性）
- [ ] F3：llm_service 加 `strip_thinking_blocks` 选项（~20 LOC，防用户感知问题）
- [ ] 原 audit P0：token budget per-turn（preopen_research 隔夜跑会烧预算）
- [ ] 原 audit P1：checkpoint_store namespace 锁（多 agent 并发撞）

**强烈建议**：
- [ ] F8：llm_service 400 response 注入上下文
- [ ] F4：web_fetch skill（不然 preopen_research 就是 MiniMax 幻觉机）

---

## 总结

**M0 PASS**。yuxu 端到端可跑，hook 成立，但路上撞到 9 个洞，**其中 3 个必须在 preopen_research 启动前补**（F1 / F3 + audit Top 3 的一部分）。

**设计文档 P5 / P7 假设验证**：core 是真的冻结得住（没出 core-level bug），系统级 agent 确实是"业务撞出来补"的格局 —— 我这次就是撞出来的。

**M0 总时长**：约 45 分钟（写 handler/runner 20 分钟 + 两次 run + observe + 写 learnings 25 分钟）。符合设计文档"3-7 天"的上限，实际比预期快。

**下一步**：把这 9 条 findings 登记到债务账，决定 preopen_research 开工前要补哪几条。
