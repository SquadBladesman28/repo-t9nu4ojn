# matsca-api：事实规格

纯事实速查：端点、参数、字段、错误码、阈值、常量。配 `../SKILL.md`（怎么用）和 `matsca-image-gen-notes.md`（为什么）一起看。

## 目录
1. 连接与鉴权
2. 端点
3. 请求参数
4. 取图与落盘
5. manifest.json schema 与退出码
6. 错误码与可重试性
7. 并发 / 重试 / 冷却常量

## 1. 连接与鉴权

- Base URL：`https://img.matsca.com`。
- 鉴权：`Authorization: Bearer <API_KEY>`。
- CLI 直发 `Bearer <key>`；所有 Key 按直连处理、不分模式。
- `GET /v1/ping` **可以用**——它是官方正式支持的健康端点，响应里带 `auth.banned` / `auth.ban_remaining_seconds`。CLI 只在 `--ping-only` 或 `--preflight-ping` 时调，不做轮询。
- `/api/dev/*` 是**账号自检**端点（查钱包 / 列 Key / reveal 明文），正常生图用不到；只有需要时才走开发者登录。

## 2. 端点

| 方法 | 端点 | 用途 |
|---|---|---|
| GET | `/v1/ping` | 健康检查；返回 `auth.banned` / `auth.ban_remaining_seconds` |
| POST | `/v1/images/generations` | 文生图（JSON，`n=1~4` 一次拿 N 张） |
| POST | `/v1/images/edits` | 改图/蒙版重绘（multipart，带 `image`，可选 `mask`） |
| POST | `/v1/images/variations` | 图生图/变体（multipart，带 `image`，prompt 可选） |
| POST | `/api/dev/login` | dev 账号密码登录拿 token（仅账号自检，生图用不到） |
| GET | `/api/dev/me` | 列出名下 Key（仅账号自检） |
| POST | `/api/dev/keys/{id}/relay` | 开启某 Key 的 relay（仅账号自检） |
| GET | `/api/dev/keys/{id}/reveal` | 取出某 Key 明文（仅账号自检） |

## 3. 请求参数

**`/v1/images/generations`（JSON body）**

| 字段 | 默认 | 说明 |
|---|---|---|
| `model` | `gpt-image-2` | 模型 |
| `prompt` | — | 提示词。`size=auto` 且给了比例时，把"画面比例：X"拼进 prompt |
| `n` | 服务端 1 / CLI 默认 2 | 1~4，一次请求返回 N 张（全有或全无）；CLI 产品化默认 `-n 2` = 主图+备1 |
| `size` | `auto` | `auto` / `1024x1024` / `1536x1024` / `1024x1536` … |
| `response_format` | `b64_json` | CLI 固定取 b64 落盘 |

**`/v1/images/edits`（multipart/form-data）**：字段同上（`n` 传字符串），外加文件字段 `image`，可选 `mask`（蒙版标注要重绘的区域）。

**`/v1/images/variations`（multipart/form-data）**：文件字段 `image`，加 `n`/`response_format`/`model`，prompt 可选。

**`/api/dev/login`（JSON body，仅账号自检用）**：`email` + `password`（对应 CLI `--email`/`--password`，凭据存放见 `../SKILL.md`「密钥」），返回 token 供后续 `/api/dev/*` 鉴权。

**透传参数白名单**（默认一个都不发，只有显式给了才透传）：

| CLI 开关 | 请求字段 | 取值 |
|---|---|---|
| `--quality` | `quality` | auto/low/medium/high |
| `--moderation` | `moderation` | auto/low |
| `--background` | `background` | auto/transparent/opaque |
| `--style` | `style` | 自由文本 |
| `--output-format` | `output_image_format` | png/jpeg/webp（jpg 归一化成 jpeg） |
| `--output-compression` | `output_compression` | 0~100（仅 jpeg/webp） |
| `--input-fidelity` | `input_fidelity` | low/high（改图保真度） |

## 4. 取图与落盘

- **取图鲁棒性**：优先用响应里的 `b64_json`；没有就下载 `url`（兼容内联 `data:` URI、带 `User-Agent` 头避开 WAF 403、可选 `--proxy`）。
- **扩展名**按图片 **magic bytes** 判（PNG/JPG/WEBP），判不出回退到请求格式、再回退 png——上游拥堵降级时可能返回与请求不同的格式。
- **文件名**：清洗非法字符（保留中文，非法→`-`，空则哈希兜底，截断 80 字符），产品化命名 `<描述>_<日期>.png`，主图无后缀、备份 `（备1）/（备2）`，`--no-date` 去掉日期。

## 5. manifest.json schema 与退出码

`manifest.json` 是机读判成败的**唯一依据**，每个任务一落定就增量重写（被中途杀掉也保住已出的图与状态）。

```json
{
  "created_at": "ISO8601",
  "ok": true,
  "saved_images": 3,
  "completed": 1, "failed": 0, "pending_count": 0,
  "results": [
    {"name":"image","index":1,"ok":true,"key_id":"key1-abcd","size":"auto",
     "n_requested":2,"n_got":2,
     "saved":[{"path":"output/fig/image_20260628.png","bytes":12345,"format":"png","role":"primary"},
              {"path":"output/fig/image_20260628（备1）.png","bytes":12000,"format":"png","role":"backup"}]}
  ],
  "errors":  [ {"name":"图2","index":2,"error":"...","key_id":"key1-abcd"} ],
  "pending": [ {"name":"图3","index":3,"status":"waiting","retry_count":1} ],
  "blocked": false, "block_reason": "", "blocked_since": null,
  "no_progress_seconds": 0, "gave_up": false
}
```

- 中途快照：`results`=已成功、`errors`=已确定失败、`pending`=还没跑完（别误判成失败）。`ok` 仅当 `errors` 与 `pending` 都为空。
- `saved[].role`：`primary`=主图（无后缀；赛马时先到者）、`backup`=备份。
- **受阻字段**：`blocked` / `block_reason`（`502_storm` / `429_congestion` / `key_banned`）/ `blocked_since` / `no_progress_seconds` / `gave_up`，随增量快照刷新。轮询见 `blocked:true` 即非阻塞提醒用户。
- `--resume`：重跑同一 `outdir` 时，按 `name` 命中且文件仍在的已成功内容会被跳过。

**退出码**：`0`=全部成功；`3`=部分成功（有 `errors[]`，含 `--give-up-after` 熔断收尾）；`4`=一张都没出。

## 6. 错误码与可重试性

| 类别 | 码 / 状态 | 重试? | 处理 |
|---|---|---|---|
| 可重试 HTTP | `408/409/425/429/500/502/503/504/520/522/524` | 是 | HTTP 层退避重试 + 任务层重试，尊重 `Retry-After` |
| 瞬时业务码 | `upstream_timeout` / `upstream_unreachable` / `upstream_session_pool_exhausted` / `api_agent_queue_full` / `upstream_rate_limited` / `no_available_account` / `account_concurrency_exhausted` / `upstream_direct_unavailable` / `upstream_server_error` / `upstream_error` | 是 | 同上；命中冷却码的还给该 Key 上冷却 |
| Key 故障转移 | `account_token_invalid` / `image_permission_unavailable` / `401` | 是（换 Key） | 立即改钉到另一把健康 Key 重试 |
| 该上冷却 | `401/429/503` + `upstream_rate_limited` / `account_concurrency_exhausted` / `api_agent_queue_full` / `upstream_session_pool_exhausted` / `no_available_account` | — | 给该 Key 6s→120s 指数退避+连击冷却 |
| 客户侧（**不重试**） | `content_policy_violation` / `invalid_request` / `high_res_not_enabled` / 余额不足 / 鉴权失败 等 | 否 | 直接失败，按 `error` 文案处理或告诉用户 |

## 7. 并发 / 重试 / 冷却常量

| 常量 | 值 | 含义 |
|---|---|---|
| `QUEUE_CONCURRENCY_PER_KEY` | 2 | 每把 Key 同时 running 的任务数 |
| `IMAGE_REQUEST_CONCURRENCY_PER_KEY` | 2 | 每把 Key 在飞图片请求数 |
| `GLOBAL_IMAGE_REQUEST_CONCURRENCY` | 6 | 全局在飞图片请求总数 |
| `MAX_TRANSIENT_TASK_RETRIES` | 4 | 任务层最大重试 |
| `TASK_RETRY_BASE_MS / MAX` | 5000 / 120000 | 任务重试指数退避区间 |
| `KEY_COOLDOWN_BASE_MS / MAX` | 6000 / 120000 | 逐 Key 冷却指数退避区间 |
| `HTTP_RETRIES_IMAGE` | 2 | 生图请求 HTTP 层重试次数 |
| `IMAGE_TIMEOUT_S` | 600 | 单次生图墙钟上限（`--timeout` 覆盖） |
| `PING_TIMEOUT_S` | 8 | ping 超时 |
| `BLOCK_NO_PROGRESS_MS` | 90000 | 池级受阻：持续零进展阈值（`--block-after` 秒覆盖） |
| `CAP_WINDOW_MS` | 120000 | 受阻判定时统计最近容量类错误的窗口 |
