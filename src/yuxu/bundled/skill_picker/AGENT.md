---
driver: python
run_mode: persistent
scope: system
edit_warning: true
ready_timeout: 5
---
# skill_picker

Skill 注册表 + catalog 分发。负责：
1. 扫描三作用域的 skill 文件夹（global / project / agent）
2. 读 `skills_enabled.yaml`，只给 catalog 返回已启用的
3. 按调用方身份（`for_agent` / `for_project`）做可见性过滤
4. 懒读完整 body（只有 `load` 时才会读 SKILL.md 的正文）

真正的"选哪个 skill 注入 prompt / 动态注册 handler 到 bus"等策略动作由
LLM agent（harness_pro_max 等）通过 bus 调用本服务实现。

## 操作（通过 `bus.request("skill_picker", {...})`）

| op | payload | 返回 |
|---|---|---|
| `catalog` | `{for_agent?, for_project?, only_enabled=True, triggers_any?}` | `{ok, skills: [...]}` |
| `load` | `{name, for_agent?, for_project?, only_enabled=True}` | `{ok, ...skill fields + body}` |
| `enable` | `{name, scope, owner?}` | `{ok}` |
| `disable` | `{name, scope, owner?}` | `{ok}` |
| `list_all` | `{}` | `{ok, skills: [...]}`（管理视角：无视启用+可见性） |
| `rescan` | `{}` | `{ok, count}` |

## 扫描范围

启动时自动：
- **全局**：`src/skills_bundled/` + `config/skills_enabled.yaml`
- **Agent 私有**：遍历 `loader.specs`，对每个 agent 的 `{agent_dir}/skills/` 自动加 scope

**Project 作用域**暂未自动扫描（需要 project.md 支持，待 P3+ 项目生命周期就位）。
用户可以在 `rescan` 时通过 payload 显式指定 `extra_projects: [[dir, project_id], ...]`。

## 可见性约束（必须）

agent X / project P 调用 catalog 时能看到：
- 所有已启用的 global skill
- 已启用的 project P 下的 skill
- 已启用的 agent X 自己的 skill
- **看不到** 其他 agent 的私有 skill，也看不到别的 project 的 skill

## 为什么是 agent 不是 core

skill 发现/启用/可见性 **不在 boot 路径上**（Bus/Loader 不依赖它）。是 agent 能力而非
框架机制。见 `docs/CORE_INTERFACE.md` 的归属规则。
