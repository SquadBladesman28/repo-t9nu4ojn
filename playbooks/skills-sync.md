# Playbook：技能同步（本地中心库 → 当前 Devin 账号云端）

> **本 playbook 含两件不同的事，别混**：
> - **① 同步上去（手动 + 需隧道）**：把本地中心库 skills 推到云端 `devin/skills` 分支。用户说"同步/拉取/部署技能"才做，agent 不要自己瞎触发。见 §2.1。
> - **② 每会话取用（自动 + 免隧道）**：**每个新 Devin 会话开局都要跑一次** clone+copy 把技能落进 `~/.devin/skills/`，否则技能没载入、agent 只会 fallback 到内置工具。见 §2.2。
> **正本在这里**：知识只一行触发指路；完整步骤/约束/红线都在本文件，随仓库走。

## 0. 铁律：本地=正本，云端=构建产物，单向同步
- **方向只有一个**：永远 `本地 D:\700_Resources\720_Agents → push 云端`。云端**绝不在 GitHub 手改**、绝不从云端 `pull` 回来覆盖本地。
- **云端是产物**：由本地 `skills/` 重组成 `.devin/skills/` 生成、每次从本地**重生/覆盖**；分叉时可 `--force-with-lease`（产物可重建，安全）。
- 全程**排除 `secrets.env`**；**private 优先、public 也可**——secrets.env 始终被 `.gitignore` 排除、永不进仓，公开仓也不泄密；别为改 private 折腾登录而卡死。

## 1. 一个动作，两种情形（"同步"自动判别）
触发后先判云端目标分支状态，自动走对应支——**不需要你区分是"首次"还是"更新"**：
- 云端**没有**（宿主仓里无 `devin/skills` 分支）→ **首次播种**：整套推上去。
- 云端**已有** → **增量更新**：本地为准，覆盖该分支。
- 判别：`git ls-remote <宿主仓URL> refs/heads/devin/skills`（有→更新；无→播种）。

## 2. 选定路线：分支 + 每会话 clone 进工作区，免 main / 免登录
**为什么不走 main**：「开机原生发现」需技能落默认分支 main，而落 main 需有人登录 GitHub 动手（平台禁 agent 直推 main/合并 PR；本地 git 对他人账号组织无写权；用户常登不了 GitHub）→ 走不通。该路线只需技能在**任一分支**：Devin 扫描的是**磁盘上家目录的 `~/.devin/skills/`**，与分支、与 main 无关（已实测）。
> ⚠️ **实测踩坑（关键）**：Devin **只扫家目录 `~/.devin/skills/`**。`git clone ... ~/skills-mirror` 把技能落在 `~/skills-mirror/.devin/skills/`，**不在扫描路径里、不会被索引**——必须再 `cp` 到 `~/.devin/skills/` 技能才进 `<available_skills>`。只 clone 不 copy = 技能用不了、agent 退回内置工具（曾因此把"画猫"用内置 `generate_image` 画了、没走本技能）。
**固定约定（免每次猜）**：宿主 = 该账号组织里**某个已连接仓**；分支名固定 **`devin/skills`**。

### 2.1 同步上去（需隧道；首次播种与增量更新同一套）
1. 经隧道 base64 从本地取 `skills/`+`playbooks/`+`AGENTS.md`（**排除 `secrets.env`**）。
2. 重组成 `.devin/skills/<name>/` 布局（SKILL.md 内指针均相对路径，**迁目录零改动**）。
3. 选一个**已连接仓**（空/废稿即可，**public 也行**）。⚠️ Agent **无权 API 建仓**；若该账号一个已连接仓都没有、用户又登不了 GitHub → 退回纯隧道用法（每次给隧道、直接读本地 SKILL.md 干活）。
4. `git push`（播种）或 `git push --force-with-lease`（更新）到分支 **`devin/skills`**。推分支 agent 有权、**不碰 main、不需合并、不需登录**。
5. 回报 commit + 远端树核对（`.devin/skills/...`、`playbooks/...`、`AGENTS.md` 都在、`secrets.env` 不在）。

### 2.2 日常每会话取用（免隧道 / 免 main / 免登录）——⚠️ 每个新会话开局都得跑一次
**新会话开局、动手干活前先执行**（否则技能没载入，agent 只会 fallback 到内置工具、用不上我们的技能）：
```
rm -rf ~/skills-mirror && git clone -b devin/skills <宿主仓URL> ~/skills-mirror && mkdir -p ~/.devin/skills && cp -r ~/skills-mirror/.devin/skills/* ~/.devin/skills/ && ls ~/.devin/skills/
```
→ 技能落进**家目录 `~/.devin/skills/`**（被扫描的唯一路径），立即进 `<available_skills>`，正常 invoke。
> **clone+copy 缺一不可**：只 clone 到 `~/skills-mirror` 不被索引（见 §2 踩坑）；`cp` 之后才生效。
> 不知宿主仓：列已连接仓、挑带 `devin/skills` 分支的那个。
> 最稳的做法是把这条命令放进该账号的**环境蓝图 initialize 步骤**，每个会话自动执行、不靠 agent 自觉。

## 3. 与"隧道只改本地、不 push"关系
本流程是"隧道只改本地、不 push"的**唯一同步类例外**：仅限**本地中心库 → 云端 `devin/skills` 分支**、仅在**逐次征得用户确认后**才 push；日常普通文件改动仍只改本地、不推。