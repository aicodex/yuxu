---
# === 必填 ===

# 与文件夹名保持一致
name: my_skill

# 一行说明 —— **catalog 中展示给 LLM 看**，决定它选不选你
# 写法建议：动词开头 + 核心能力 + 关键输入（<= 80 字）
description: Fetch current price for a stock symbol from MiniQMT.

# === 可选 ===

# 作用域。scan 时由 Loader 自动注入，这里写只作为自检/覆盖用
# scope: system | project | user

# 触发线索：关键词 / 意图片段，用于 catalog 筛选或 LLM 触发判断
# triggers:
#   - price
#   - 行情
#   - quote

# 输入输出 schema（OpenAI function-calling 格式；可被 tools 直接使用）
# parameters:
#   type: object
#   properties:
#     symbol: {type: string, description: A-股票代码（如 600519）}
#   required: [symbol]

# 执行需要的资源/依赖
# depends_on:
#   - rate_limit_service   # 若要限流
#   - checkpoint_store     # 若要读缓存

# 可选：限流池名
# rate_limit_pool: tushare

# 可选：Python 模型偏好（给能选模型的上游 agent 作参考）
# model: haiku

# 可选：系统级强确认（改 SKILL.md 要审批）
# edit_warning: true
---
# my_skill

> 一行 TL;DR，写给 LLM 选择时看的。

## 安装 / 启用

**安装** = 把这个文件夹放进 Loader 扫描路径之一：
- 系统（yuxu 自带）：`src/yuxu/bundled/my_skill/`
- 项目：`<project>/agents/my_skill/` 或 `<project>/skills/my_skill/`
- Agent 绑定：`<agent_dir>/skills/my_skill/`（由持有者 agent 扫）

skill 的**凭证是缺失 `__init__.py`**（有 `handler.py` + `SKILL.md`）。Loader
扫到这种结构就按 skill 处理：懒导入 + 注册 bus handler `{name}`。装了即
可用，**无需单独的 enable 文件**。

## 何时使用

- 用户问 "xxx 多少钱" / "股价" / 查行情时
- 需要实时报价，非历史数据

## 何时不要使用

- 历史日线 → 用 `get_daily_kline`
- 研报摘要 → 用 `fetch_research_report`

## 参数

| 字段 | 类型 | 必需 | 说明 |
|---|---|---|---|
| symbol | str | 是 | 6 位股票代码 |

## 返回

```json
{"symbol": "600519", "price": 1650.5, "ts": "2026-04-21T10:30:00+08:00"}
```

## 错误

- `invalid_symbol` — 代码格式错
- `upstream_timeout` — QMT 未响应

## 示例

```
user: 茅台现价多少
assistant: (calls my_skill with symbol="600519")
```
