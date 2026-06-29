# 720_Agents — AI Agent 知识与技能正本

给 AI 编程工具(Codex / Claude Code / Devin 等)读取的**单一事实来源**。改这一份,所有工具生效。
- `AGENTS.md`   : 全局指令/索引(Codex、Devin 读;经 ~/.codex/AGENTS.md 指针生效)
- `CLAUDE.md`   : Claude Code 全局指令(导入 AGENTS.md)
- `skills/`     : 可复用技能(CC Switch 同步到各 App;~/.cc-switch/skills 经 junction 指向此处)。技能可带随附 `skills/<name>/references/`(随技能加载的事实型规格:API 字段/错误码/凭证/schema)。
- `playbooks/`  : 中心沉淀经验库(认知/根因/排错心法/跨工具运维),平时不进上下文,出 bug 或要深挖才整份翻;靠 `AGENTS.md` 索引按需读
- `secrets.env` : 集中密钥文件(明文,见下)

## 密钥约定(重要)
- 本仓库为**私密仓库**,所有密钥**明文**集中存放在根目录 `secrets.env`,并**纳入 git**。
- 格式 `KEY=值`,一行一个。以后新增任何密钥都加到这里,**各 skill / 脚本统一从 `secrets.env` 读取**,不要把密钥散写进多个文件。
- 脚本读取顺序:进程环境变量 > `secrets.env`(支持绝对路径 `D:\700_Resources\720_Agents\secrets.env`,即使技能被 CC Switch 同步到别处也能找到)。
- ⚠️ 因含明文密钥,务必保持仓库私有,切勿公开或外发。

## 规则
- 内容只存这一份,各工具用各自机制指过来,不复制多份。
- 本目录用 git 做本地版本与备份(仅本地,提交时机由你决定)。
