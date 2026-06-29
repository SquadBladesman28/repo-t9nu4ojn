# matsca-image-gen 设计原理与排错心法

这份讲"为什么这么设计"，帮你在出意外时判断该怎么办，而不是死记规则。配 `matsca-api.md`（事实）和 `../SKILL.md`（怎么用）。

## 目录
1. 多 Key 健康调度——最核心的防撞墙机制
2. 逐 Key 冷却 + 故障转移
3. 两层重试
4. 为何允许 ping
5. 保守并发：为什么是 2/Key、全局 6
6. 默认 n=N，赛马是开关
7. 增量持久化 + 断点续跑
8. 池级受阻判定（H）
9. 各开关的取舍
10. 隧道里让长任务"真脱离"
11. 下发前预检与输入健壮性

## 1. 多 Key 健康调度

提高吞吐的唯一手段是多把 Key，不是把单 Key 并发调高。给每把 Key 算一个健康分（在飞请求、队列长度、连续错误都会拉高分，首选 Key 略降分），分越低越优先。调度器按任务顺序，给每个 waiting 任务挑一把"没在冷却、running 没满、全局没满"的 Key——首选可用就用首选，否则挑最闲的。

## 2. 逐 Key 冷却 + 故障转移

这两件事针对不同性质的失败，别混：

- 冷却针对"这把 Key 暂时被限流/拥堵"（命中 `401/429/503` 或一组容量类业务码）：给它上 `6s × 2^(连击-1)` 的冷却、封顶 120s、带 jitter、尊重 `Retry-After`，成功一次清零连击。效果是把刚撞墙的 Key 晾一会儿，流量自动挪到健康 Key。
- 故障转移针对"这把 Key 自己坏了"（`account_token_invalid` / `image_permission_unavailable` / `401`）：只要还有别的 Key，立即改钉到另一把健康 Key 重试，不等大退避；只有一把 Key 时才退化成按退避重试。

## 3. 两层重试

- HTTP 层：对可重试状态码自动重试（生图 retries=2），指数退避、尊重 `Retry-After`（429 至少等 10s、封顶 30s）。
- 任务层：HTTP 层耗尽仍失败、且属于瞬时/可转移错误 → 任务退回 waiting，置 `retry_after`（`5s × 2^retry`、封顶 120s、与该 Key 冷却取大），最多重试 `MAX_TRANSIENT_TASK_RETRIES=4` 次。
- 客户侧错误（`content_policy_violation` 等）一层都不重试——重试只会累加风控错误率，且可能扣费不退。

## 4. 允许 ping，但不滥用

在 `--ping-only`（显式查状态）或 `--preflight-ping`（生成前对每把 Key 探一次封禁）时调，不做 30s 轮询（无头环境没有状态指示灯要刷）。。

## 5. 保守并发：为什么是 2/Key、全局 6

账户级评分风控对"高并发猛刷"比对"慢慢来"敏感得多。官方用低并发 + 多 Key 横向扩展来"既快又不挨罚"。本 CLI 照搬：每 Key 在飞 ≤2、全局 ≤6，把一部分吞吐让位给"少触发处罚"。想更快——加 Key，别加单 Key 并发。

## 6. 默认 n=N，赛马是开关

默认按官方：一个任务 = 一次 `POST /v1/images/generations` 带 `n=N`，一把 Key 一个请求拿回 N 张（全有或全无）。这样请求数最少、最不容易撞限流。`--race`（见 §9）才把它拆成 N 路 `n=1` 并发去抢"先出一张"，代价是更容易打满单 Key、更易撞墙——所以默认关。

## 7. 增量持久化 + 断点续跑

每个任务一落定就增量重写 `manifest.json`（中途被杀也保住已出图，快照里的 `pending` 是还没跑完、别当失败）；`--resume` 重跑同一 `outdir` 时按 `name` 跳过已成功且文件还在的内容。单请求超时默认 600s。

还有一处时序：任务一启动（建好 outdir 后）就先落一份全 `pending` 的初始 manifest，再去发第一个请求。因为首个请求可能要几十秒~10min 才回，轮询方若那之前读到「文件不存在」就无从判断进展——既然约定「只读 manifest 判进展」，那它就得从 0s 起一直存在。

## 8. 池级受阻判定

几把直连 Key 往往打的是同一个上游池，所以"满池"时故障转移救不了——换哪把 Key 都满。受阻侦测因此必须是池级的：持续零进展 ≥ `--block-after`（默认 90s）且（最近失败多为容量类，或所有 Key 都在冷却）→ 置 `manifest.blocked=true`、写 `block_reason`、并在 stderr 打 `>>> IMAGE_GEN_BLOCKED: <原因> <<<`。

受阻时会 ping 各 Key 来辨别是"被封"还是"上游容量满"（这是允许 ping 才有的能力）：返回 banned → `block_reason=key_banned`（另一套处理，等 ban 解除或换 Key）。`--give-up-after` 让受阻持续超时即提前收尾（退出码 3）。退避/冷却仍由前面几层负责，这里只做"侦测 + 发信号 + 可选熔断"。

轮询的 Agent 见到 `blocked:true` 应当非阻塞提醒用户：这是上游容量受阻，不是没钱也不是封号，工具在有界退避里自愈但可能很久。

## 9. 各开关的取舍

全部默认关。

- `--race`：把每内容拆成 N 个 `n=1` 并发，先到的当主图。要"先看到一张"时用；代价是更易打满单 Key、更易撞限流。
- `--coverage-first`：多内容批量时，排序优先"一张都还没有、也没在飞"的内容，先给每种各出一张铺版再补备份。默认按 index 顺序。
- `--edit`+`--mask` / `--variation`：改图走 `/v1/images/edits`（mask 标重绘区域），变体走 `/v1/images/variations`（可无 prompt）。
- 参数面（`--quality` 等 7 个）：默认都不发，显式给才透传（注意 `output_format`→`output_image_format`、`jpg`→`jpeg`）。
- `--block-after` / `--give-up-after` / `--no-ping-refine`：调受阻阈值、受阻超时收尾、受阻时不 ping 辨别封禁。

## 10. 隧道里让长任务"真脱离"

上游单张 30s~10min、批量几十分钟，别在前台同步等把会话/隧道占死。正确姿势是 fire-and-forget + 轮询 manifest：

1. 后台起，别套会到点杀进程的外层 `timeout`（脚本自己有 `--timeout` 和增量 manifest 兜底）：
   ```bash
   nohup python scripts/gen_image.py --prompts-file jobs.txt --outdir output/fig \
        > output/fig/run.log 2>&1 &
   ```
   在用户机上经隧道跑，用 `Start-Process`/计划任务起后台，别让隧道命令同步阻塞。
2. 去干别的，每隔一会儿只读 `output/fig/manifest.json` 判进展（`completed/failed/pending_count`、`saved_images`、`blocked`/`block_reason`）。
3. `blocked:true` → 非阻塞提醒用户，给①切内置 generate_image ②换渠道/晚点 ③继续等，并附已出/还缺清单。
4. 中断后续跑：同一 `outdir` 加 `--resume`，按 `name` 跳过已成功的、只补没出的。

## 11. 下发前预检与输入健壮性

宁可在发请求前就把问题挑明，也不要让它在跑了几十分钟后才以「静默丢图」或「整页 HTML 刷屏」的形式暴露。几条都遵循「失败要响、要早、要可读」：

- 批次内**重名去重**：同名内容会落到同一文件名互相覆盖，且 manifest 会把两者都记成功、`--resume` 也按 `name` 误跳——等于无感知丢图。`build_tasks` 阶段就把重名改成 `名-2/名-3` 并打提示。
- **mask 必须配 edit**：蒙版只在 `/v1/images/edits` 生效；只给 `--mask` 不给 `--edit` 旧版会静默走普通生图，用户以为在局部重绘其实没有。改成预检直接报错。
- **`--prompts-file` 读不了就干净退出**：路径不存在/不可读时给一行人话错误，而不是抛 `OSError` traceback。
- **`-n` 越界出提示**：钳到 1~4 的同时打日志说明，别让用户以为 `-n 8` 真生效了。
- **`--ping-only` 全失败返回非零（4）**：脚本化健康检查要能靠退出码判断，而不是永远 0。
- **非 JSON 响应收敛成一行**：网关 502 会回整页 nginx HTML，逐 Key 打进日志就是刷屏。去标签、压成单行、截到 200 字符。
- **受阻时并发 ping**：storm 下每把 Key 都可能等满 ping 超时，串行探测会被拖成 N×超时，改成多线程并发探。
