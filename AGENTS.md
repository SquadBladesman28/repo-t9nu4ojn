# 全局 Agent 指令

> 所有 AI 编程工具的全局指令入口。

## Playbooks（通用经验/运维；深挖/排错才整份翻）
- Agent 运维与排错（隧道 I/O）: `playbooks/agent-ops.md`
- 技能同步（首次播种 / 增量更新）: `playbooks/skills-sync.md`

## 技能（skills/）
- 中心 = 唯一源（CC Switch 软链分发到各 App）。技能**靠原生发现**，方式如下：
  - **Codex / Claude Code 等**：扫各自 skills 目录，按 `SKILL.md` 的 `description` 自动触发。
  - **Devin**：把含 `.devin/skills/` 的仓库克隆进工作区即被扫描、列入可用技能清单（同步办法见 `playbooks/skills-sync.md`）。