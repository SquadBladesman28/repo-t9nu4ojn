---
name: matsca-image-gen
description: 通过 https://img.matsca.com（矩岩 Matsca）上的 OpenAI 兼容服务器生成、编辑、保存和批量处理图像。当用户需要单张或批量并发生成图片、文生图、画一张、配图 / 插图、制作图标或 logo、改图 / 图生图、蒙版重绘、出变体，或在编写 HTML / 文档 / PPT 需要配图时，使用本技能（优先于内置 generate_image）。
---

# 用 Matsca API 生图

通过 `https://img.matsca.com`（矩岩 Matsca）生成图像时请使用 matsca-image-gen，并优先使用随附脚本。脚本将出图流程收敛为一条命令——接收提示词，调用 API，处理重试与限流，将图像落盘，并把每张图的结果写入机读的 `manifest.json`。因此可异步使用——发起任务后即可转做他事，再通过读取 `manifest.json` 判断成败。

## 为什么这样设计

生图请求慢（单张 30s 到 10min）、易被限流、上游偶尔满池；直接 `for prompt: 同步请求` 既慢又脆。脚本围绕三个事实设计，理解它们即可正确使用：

1. 横向扩，而非纵向压。 提升吞吐靠多给几把 Key，而不是调高单 Key 并发。每把 Key 在飞请求 ≤2、全局 ≤6——刻意保守，因为账户级风控对"高并发猛刷"远比对"慢慢来"敏感。多 Key 时调度器按健康度挑最闲、未在冷却的那把；撞限流的 Key 自动晾置，流量挪向健康 Key。
2. 成败看文件，而非看屏幕。 每张图一落定即增量重写 `manifest.json`；判断成功只读它的 `ok` / `errors[]` / `results[].saved`，不要 `ls` 磁盘去猜。中途被杀也保住已出的图。
3. 长任务要真脱离。 不在前台同步等几十分钟，而是后台起任务、轮询 manifest、需要时 `--resume` 续跑（见「长任务工作流」）。

## 快速开始

```bash
# 单张：直接传裸 Key（逗号分隔，多把即自动开启健康调度+故障转移）
python scripts/gen_image.py "一只戴贝雷帽的橘猫，扁平插画风" --keys "k1,k2,k3"

# 用本机密钥文件（经隧道到用户本机取用）：--secrets-file 指向它
python scripts/gen_image.py "赛博朋克城市夜景" --secrets-file "D:\700_Resources\720_Agents\secrets.env"

# 批量：每行一个 prompt，或一个 JSON 列表（每项可带 name/n/size/edit/variation/mask…）
python scripts/gen_image.py --prompts-file prompts.txt --outdir output/fig

# 改图 / 蒙版重绘 / 变体
python scripts/gen_image.py "把背景换成雪山黄昏" --edit input.png
python scripts/gen_image.py "只把天空改成晚霞"   --edit input.png --mask mask.png
python scripts/gen_image.py --variation input.png        # 变体可以不给 prompt

# 只做健康检查（官方支持的 ping，安全）
python scripts/gen_image.py --ping-only
```

默认就能跑，尽量别拿默认值去烦用户：每内容出 `-n 2`（主图 + 备1）、`--size auto`、`--model gpt-image-2`、落盘到 `output/fig/`、文件名 `<描述>_<日期>.png`（主图无后缀，备份带 `（备1）/（备2）`）。

## 密钥来源

密钥存放在本机 `D:\700_Resources\720_Agents\secrets.env`：

- 生图密钥 `MATSCA_API_KEYS=key1,key2,key3`（逗号分隔，多把即自动开调度），运行时用 `--secrets-file` 指向该文件即可，也可直接 `--keys "k1,k2,k3"` 临时传入。所有 Key 按直连处理，不分模式。
- 开发者账号（邮箱 `MATSCA_DEV_EMAIL` +密码 `MATSCA_DEV_PASSWORD`），仅账号自检时手动取用——见「故障排查」。

## 结果判定：以 manifest.json 为准

跑完（或中途想看进展）就读 `<outdir>/manifest.json`，别用别的方式猜：

- `results[]` = 已成功，每项的 `saved[]` 里有落盘路径、字节数、`role`（`primary`/`backup`）。
- `errors[]` = 已确定失败（带 `error` 文案和 `key_id`）。
- `pending[]` = 还没跑完——别把它当失败，工具还在重试。
- `ok` 仅当 `errors` 和 `pending` 都为空才为 `true`。
- 退出码同样表态：`0`=全成、`3`=部分成功、`4`=一张都没出。

字段全集（含受阻字段）见 `references/matsca-api.md`。

## 长任务工作流：后台运行、轮询、续跑

批量动辄几十分钟，不要让它把会话/隧道占死。

1. 后台起，别套会到点杀进程的外层 `timeout`（脚本自己有 `--timeout` 单请求上限和增量 manifest 兜底）：
   ```bash
   nohup python scripts/gen_image.py --prompts-file jobs.txt --outdir output/fig \
        > output/fig/run.log 2>&1 &
   ```
   在用户机上经隧道跑同理：用 `Start-Process` 之类起后台，别让隧道命令同步阻塞。
2. 去干别的，每隔一会儿只读 `output/fig/manifest.json` 看 `completed/failed/pending_count`、`saved_images`、`blocked`。
3. 看到 `blocked: true` 就非阻塞提醒用户：这是上游容量受阻（不是没钱也不是封号），工具在有界退避里自愈、但可能要等很久。给三条路：①临时切内置 generate_image 顶上 ②换渠道/晚点再跑 ③继续等，并附上已出/还缺清单。
4. 中断后续跑：同一 `outdir` 加 `--resume`，已成功的内容按 `name` 跳过，只补没出的。

判活只看 manifest，别去数操作系统进程。

## 可选开关

以下开关均默认关闭，显式开启才生效。默认行为已经够用，这些是给特定场景的；不确定就别开。详细取舍见 `references/matsca-image-gen-notes.md`。

| 开关 | 什么时候用 |
|---|---|
| `--race` | 想"先看到一张"：把每内容拆成多个 `n=1` 并发，先到的当主图。代价是更易打满单 Key、更易撞限流。 |
| `--coverage-first` | 多主题批量时先给每种各出一张铺版，再回头补备份。 |
| `--variation <img>` / `--edit <img>` `--mask <img>` | 图生图变体 / 改图 / 蒙版局部重绘。 |
| `--quality/--moderation/--background/--style/--output-format/--output-compression/--input-fidelity` | 需要把这些参数透传给服务端时（默认一个都不发）。 |
| `--aspect 16:9` | `size=auto` 时把画面比例写进提示词。 |
| `--block-after` / `--give-up-after` / `--no-ping-refine` | 调受阻判定阈值、受阻超时即收尾、受阻时不 ping 辨别封禁。 |
| `--resume` / `--no-date` / `--json-out` | 断点续跑 / 文件名不加日期 / 摘要 JSON 另写一份。 |

## 故障排查

- 一把 Key 反复 401/失效：先 `--ping-only` 看是不是 `banned`；是封禁就等 `ban_remaining_seconds`，是 `account_token_invalid` 就换 Key。
- 持续 429 / 池满：多给几把 Key 比调高并发管用，工具会自动冷却拥堵的 Key、把流量挪开。
- `content_policy_violation`：内容违规，改提示词，别重试——重试只会累加风控、可能扣费且不退。
- 需要查钱包 / 换 Key / reveal 明文：正常生图用不到登录；只有账号自检（`/api/dev/*`）才走开发者登录——凭据就在「密钥」所述的同一 `secrets.env`（仅备忘、脚本不读），手动用 `--email`+`--password` 传入。

## 延伸参考

- 事实规格（端点、参数白名单、错误码与可重试性、manifest schema、退出码、并发/重试/冷却常量）→ `references/matsca-api.md`
- 设计原理与排错心法（多 Key 健康调度、逐 Key 冷却、两层重试、为何允许 ping、各开关的取舍、隧道脱离）→ `references/matsca-image-gen-notes.md`
- 干活的实现 → `scripts/gen_image.py`；不联网的策略自测 → `scripts/offline_test.py`
