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
# 单张：Key 从本地 secrets.env 自动读（见「密钥来源」）
python scripts/gen_image.py "一只戴贝雷帽的橘猫，扁平插画风" --secrets-file D:\700_Resources\720_Agents\secrets.env

# 也可临时直接传裸 Key（逗号分隔，多把即自动开启健康调度+故障转移）
python scripts/gen_image.py "赛博朋克城市夜景" --keys "k1,k2,k3"

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

## 密钥来源：以本地 secrets.env 为准 + 过期自愈

以本机 `secrets.env`（如 `D:\700_Resources\720_Agents\secrets.env`）为唯一真源，里面同时放两样、用 `--secrets-file` 指向它：

```
MATSCA_API_KEYS=key1,key2,key3        # 静态直连 Key（会过期）
MATSCA_DEV_EMAIL=you@example.com      # dev 账号——静态 Key 失效时的兜底
MATSCA_DEV_PASSWORD=******
```

静态 `MATSCA_API_KEYS` 还有效就直接拿来用；一旦失效/缺失，脚本用文件里的 dev 凭据自动登录 reveal 一批新鲜 Key，**并把新 Key 回写这同一份 `secrets.env`**（只换 `MATSCA_API_KEYS=` 一行，dev 凭据与注释原样保留）。于是下次直接直连、不必再登录，过期了又自动补——本地文件始终是最新真源，不用手动更新 Key、也不用搞环境变量。

- 回写默认开；`--no-save-keys` 关掉，`--save-keys-file <路径>` 改回写目标。
- `secrets.env` 始终被 `.gitignore` 排除、永不进仓，私有不外泄。
- 经隧道在云端用时：Devin 跑通后同理把新鲜 Key 写回你本机的 `secrets.env`。

所有 Key 按直连处理，不分模式；多把自动开健康调度 + 故障转移。临时想绕开文件，也可 `--keys "k1,k2,k3"` 直接传裸 Key（最高优先）。

## 结果判定：以 manifest.json 为准

跑完（或中途想看进展）就读 `<outdir>/manifest.json`，别用别的方式猜：

- `results[]` = 已成功，每项的 `saved[]` 里有落盘路径、字节数、`role`（`primary`/`backup`）。
- `errors[]` = 已确定失败（带 `error` 文案和 `key_id`）。
- `pending[]` = 还没跑完——别把它当失败，工具还在重试。
- `ok` 仅当 `errors` 和 `pending` 都为空才为 `true`。
- 退出码同样表态：`0`=全成、`3`=部分成功、`4`=一张都没出（`--ping-only` 模式下 `4`=没有一把 Key 通过健康检查）。
- manifest 在任务一启动就先落一份全 `pending` 的（首个请求可能要等几十秒~10min），所以从 0s 起就能读到结构化状态，不会出现「文件还不存在」。

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

## 交付与回传：全部图（含备份）+ 压缩预览

把结果交给用户时，按 manifest 的 `results[].saved` 落地/回传，注意两点：

1. **每一张都要给，别只挑主图。** 默认 `-n 2` 时每内容有主图 + `（备1）`，备份和主图同等是产物；交付/回传时把 `saved[]` 里的**每张（含 backup）都按原命名带上**，落到对应目录（图→`output/fig/`），不要只发主图把备份漏在原地。
2. **回传/嵌网页用压缩预览，原图照旧保留。** 原图 1024² PNG 约 1.7MB/张，走隧道分块回传偏大、嵌页也重。用随附 `scripts/make_preview.py` 压成 ≤900px、q≈82 的 JPEG（约 50–160KB，肉眼几乎无差）再回传/嵌页：

   ```bash
   # 把整个出图目录压成预览（默认落各图同级 ./preview/），原 PNG 不动
   python scripts/make_preview.py output/fig --max-px 900 --quality 82
   ```

   后端优先 Pillow，无则回退 ImageMagick `convert`。生成网页/文档配图时优先引用预览 JPEG，体积小、加载快。

### 经隧道在云端跑：交给看护进程自动回传，别用 LLM 轮询烧 token

云端（如 Devin）经隧道给用户出图时，**不要让模型在轮询循环里干等**——每轮 `读 manifest` 都是对话开销，最后一张卡死时干等更浪费。改用随附 `scripts/auto_deliver.py`：它**跑在云端自己的机器上**（不在用户电脑上），盯 `manifest.json`，每落一张新图（含备份）就自动压预览、经隧道推到用户机对应目录、sha256 自校验，已传的记进 `.delivered.json` 不重发。

```bash
# 1) 后台出图，记下 PID
nohup python scripts/gen_image.py --prompts-file jobs.json --outdir out --secrets-file s.env \
     > out/run.log 2>&1 & echo $! > out/gen.pid
# 2) 后台起看护（在云端机器，不在用户电脑），出一张自动回传一张
nohup python scripts/auto_deliver.py --outdir out \
     --tunnel http://xxxx.cpolar.cn/api/exec --token "Bearer ..." \
     --remote-dir "d:\\path\\output\\fig" \
     --gen-pid "$(cat out/gen.pid)" --deadline-min 60 \
     > out/deliver.log 2>&1 &
```

模型只管"点火 + 撒手"，之后零对话 token。看护**绝不无限跑**，满足任一条件即收尾退出并把"已交付/缺失"写进日志：① manifest `ok`（全出齐）；② `--gen-pid` 进程结束（含重试用尽放弃的图）；③ `--max-idle-min` 内无新图（兜底卡死）；④ `--deadline-min` 总时限。卡死的最后一张会被 gen 标 `failed`、看护随之收尾，不会一直挂。

## 可选开关

**赛马 `--race` 与覆盖优先 `--coverage-first` 默认常开**（早交付、多内容先各凑一张）；其余开关默认关闭、显式开启才生效。详细取舍见 `references/matsca-image-gen-notes.md`。

| 开关 | 什么时候用 |
|---|---|
| `--no-race` | 关掉赛马（默认开）。赛马把每内容拆成多个 `n=1` 并发、先到的当主图，早交付；多内容/多 Key 时想退回每内容单请求 `n=N` 一次拿齐就加这个。 |
| `--no-coverage-first` | 关掉覆盖优先（默认开）。覆盖优先在多内容时先给每种各出一张铺版再补备份；想严格按内容顺序、一个补满再下一个就加这个。 |
| `--variation <img>` / `--edit <img>` `--mask <img>` | 图生图变体 / 改图 / 蒙版局部重绘。 |
| `--quality/--moderation/--background/--style/--output-format/--output-compression/--input-fidelity` | 需要把这些参数透传给服务端时（默认一个都不发）。 |
| `--aspect 16:9` | `size=auto` 时把画面比例写进提示词。 |
| `--block-after` / `--give-up-after` / `--no-ping-refine` | 调受阻判定阈值、受阻超时即收尾、受阻时不 ping 辨别封禁。 |
| `--resume` / `--no-date` / `--json-out` | 断点续跑 / 文件名不加日期 / 摘要 JSON 另写一份。 |

## 故障排查

- 一把 Key 反复 401/失效：先 `--ping-only` 看是不是 `banned`；是封禁就等 `ban_remaining_seconds`，是 `account_token_invalid` 就换 Key。
- 持续 429 / 池满：多给几把 Key 比调高并发管用，工具会自动冷却拥堵的 Key、把流量挪开。
- `content_policy_violation`：内容违规，改提示词，别重试——重试只会累加风控、可能扣费且不退。
- 静态 Key 全部 401 / 过期：只要 `secrets.env` 里还有 dev 凭据，脚本会自动登录 reveal 新鲜 Key 并回写该文件（见「本地 secrets.env 为准 + 过期自愈」），无需手动更新；想关掉回写用 `--no-save-keys`。
- `--mask` 必须配合 `--edit`：蒙版只在改图 `/v1/images/edits` 时生效；只给 `--mask` 不给 `--edit` 会直接报错（而非静默走普通生图）。
- 需要查钱包 / 换 Key / reveal 明文：走开发者登录（`/api/dev/*`）——凭据即「密钥来源」命 4 的 `MATSCA_DEV_*`，可手动 `--email`+`--password` 传入，脚本在没有静态 Key 时也会自动用它。

## 延伸参考

- 事实规格（端点、参数白名单、错误码与可重试性、manifest schema、退出码、并发/重试/冷却常量）→ `references/matsca-api.md`
- 设计原理与排错心法（多 Key 健康调度、逐 Key 冷却、两层重试、为何允许 ping、各开关的取舍、隧道脱离）→ `references/matsca-image-gen-notes.md`
- 干活的实现 → `scripts/gen_image.py`；回传/嵌页用的压缩预览 → `scripts/make_preview.py`；云端经隧道出图的看护+自动回传（不烧 token）→ `scripts/auto_deliver.py`；不联网的策略自测 → `scripts/offline_test.py`
