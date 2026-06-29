# Agent 运维排错（跨工具 / 跨任务）

> 详解+排错参考。**精简的核心规则在 `AGENTS.md`**；本文放根因/实测/示例，出问题再翻这里。

## 隧道（cpolar + 本地 REST API，仅 Devin）
- **`/api/files` 被沙盒限制在 workspace 内**。访问 D 盘别处（如 `D:\700_Resources\...`）会返回 `{"error":"Path escapes workspace"}`。→ 用 `/api/exec` 读写任意路径。
- **`/api/exec` 会缓冲整个进程的输出直到进程结束**。跑长任务会把请求挂到超时。→ 用 `Start-Process ... -RedirectStandardOutput <file> -RedirectStandardError <file>.err -NoNewWindow` 后台跑，再分次 `type`/读文件轮询输出。
- **PowerShell 引号地狱**：`{"cmd":"powershell -Command \"...\""}` 经 JSON→命令行→PowerShell 三层转义，复杂命令常返回空/报错。
  - 正解：用 `powershell -NoProfile -EncodedCommand <BASE64>`，其中 base64 是 **UTF-16LE** 编码的脚本：
    ```bash
    B64=$(iconv -f UTF-8 -t UTF-16LE script.ps1 | base64 -w0)
    curl ... -d "{\"cmd\":\"powershell -NoProfile -EncodedCommand $B64\"}" "$URL/api/exec"
    ```
  - 简单命令可用 `findstr`/`dir`/`copy` 等 cmd 内建避开引号。

## 高效 I/O（仅 Devin）
> Devin 的成本≈**往返次数 + 进上下文的输出字节**。下面是实测后最省的打法。

- **唯一通道是 `/api/exec`**：`/api/files` 只能列目录（不能读文件内容、不能写）；无 upload/write 端点。所有读写都走 exec。
- **读（拉取）很便宜、几乎不限大小、不限路径**：文件内容走在**响应**里，不受命令行长度限制；exec **不被 workspace 沙盒限制**，任意盘符/路径都能读（沙盒只限 `/api/files`）。`[Convert]::ToBase64String([IO.File]::ReadAllBytes('<任意path>'))` **一次调用**取回一个文件（实测单响应 ~60KB base64 没问题）。
  - **按需求匹配拉取范围，别无脑拉整棵树**：
    - 看**某个/某几个**任意路径的文件 → 每个文件一次 `ReadAllBytes|ToBase64String`（1 次往返/文件，路径短、无上限）；跨目录的几个文件也可 `tar -cf %TEMP%\b.tar <file1> <file2> ...` 显式列表一次拉。
    - **整树拉前先掂量大小，别无脑整包**（Devin 打开的文件夹可能很大，整包又慢又可能撑爆单次响应/超时）：
      - `D:\700_Resources\720_Agents`（小而固定）→ 直接整树拉。
      - **Devin 打开的文件夹（=隧道 workspace，见 `/api/ping`）→ 先探后定**。探一下（一次调用、只量不取内容）：
        - git 仓库（首选，自动尊重 `.gitignore`）：`git -C <ws> ls-files | Measure-Object` 数文件；大小可 `git -C <ws> ls-files | %{ (Get-Item $_).Length } | Measure-Object -Sum`。
        - 非 git：`Get-ChildItem <ws> -Recurse -File -Force -EA SilentlyContinue | ?{ $_.FullName -notmatch '\\(\.git|node_modules|\.?venv|dist|build|target|__pycache__)\\' } | Measure-Object -Sum Length`。
      - **判定**：小（默认阈值 **≲10MB 且 ≲500 文件**，可调）→ 整树拉；大 → **别整包**，改为拉目录结构当地图（`git ls-files` 或 `Get-ChildItem -Name`）+ 按需单拉/只拉相关子目录。
      - **整树拉怎么拉**：git 仓库用 `git -C <ws> archive --format=tar HEAD`（只含跟踪文件、自动跳过忽略项）→ base64；非 git 用 `cd <ws>; tar --exclude=.git -cf %TEMP%\b.tar .` → base64。一次往返拉回，本地 `tar -xf` 解开（实测 9 文件/47KB 一次搞定）。`tar.exe` 是 Win10+ 自带。
  - 一句话：**只拉你要看/要改的**，单文件就单拉，整目录批量改才整包拉。
- **写（推送）是贵的方向**：数据走在 **Windows 命令行**上，**单次上限约 5000 base64 字符**（实测 5000 可、8000 报错 exit 1），大文件得分块=多次调用。所以：
  - **本地改、只推一次**：拉一次→用文件工具本地编辑→推改动的文件。别"边改边推"或反复回拉确认。
  - **写 + 校验合并成一条命令**：写完同一调用里返回 `(Get-FileHash <dst> -SHA256)`，与本地 `sha256sum` 比对，**省掉单独的校验往返**。
  - 只推**你改过的**文件（配置/文本通常 1 块即 ~1–3 次调用）；改了很多个才考虑"推 tar 再远端解开"。
- **大二进制（如生成的图）**：尽量**在远端直接生成到目标目录**，别"远端生成→拉下来→再推上去"白跑命令行上限。
- **别把内容打到终端**：既乱码又烧 token，重定向到文件用文件工具读（见下节）。
- **能合并就合并**：独立命令用 `;`/`&` 串到一条 exec 里，减少往返。

## 读取文件内容：别看终端回显，落地成文件再读（仅 Devin）
**现象**：通过隧道读中文文件（`type` / `Get-Content` / `echo`，甚至直接看 `curl` 返回的 JSON）时，中文等多字节字符会“一个隔一个”地丢，整段像乱码。

**根因（已实测定位）**：**数据没坏，是显示问题。** 隧道 API 返回的字节是完整正确的；乱码只发生在**交互式终端（PTY）渲染**这一步——它对 CJK 多字节字符丢字。
> 实测证据：同一份 `type` 输出**重定向到文件**后用读文件工具/`xxd` 看，字节级完全正确（`# 全局 Agent 指令…`）；只有直接打印/回显到终端才花成 `# 全 Agent 指…`。

**正确做法（任选其一，都要“落地成文件”）**：
1. **重定向到文件再读**（最简单）：`curl ... > resp.json`，再用读文件工具/编辑器/`xxd` 打开 `resp.json` 里的 `stdout`，内容是对的。**永远不要靠终端回显判断文件内容。**
2. **base64 取回**（更稳，顺带保证二进制完整、绕开一切编码层）：远端
   ```
   powershell -NoProfile -Command "[Convert]::ToBase64String([IO.File]::ReadAllBytes('<path>'))"
   ```
   base64 是纯 ASCII，过终端不丢；**本地 `base64 -d > file` 解码到文件**再读。

一句话：**乱码=终端渲染问题，不是数据问题**；换成“写进文件再用文件工具读”就能看到正确内容，别盯着终端回显。

## 文件传回 Windows（二进制安全，仅 Devin）
分块 base64 写临时文件 → 远端解码落盘 → 校验 SHA：
```
# 分块（每块 ~5000 字符）：第一块 Set-Content -NoNewline，后续 Add-Content -NoNewline 到 <dst>.b64
# 落盘：[IO.File]::WriteAllBytes('<dst>',[Convert]::FromBase64String((Get-Content -Raw '<dst>.b64'))); Remove-Item '<dst>.b64'
# 校验：(Get-FileHash '<dst>' -Algorithm SHA256).Hash.ToLower()  ←  与本地 sha256sum 比对
```

## 本地工具联网走 v2rayN 代理（Codex/CC，**非 Devin**）
- **Devin 在云端，联网直连，不需要也不要用本机代理。** 本节只针对**本地工具**（Codex/CC 等）。
- 本机常挂 **v2rayN**：`127.0.0.1:10808`（SOCKS5）/ `10809`（HTTP），以本机实际端口为准。
- 本地工具拉 GitHub / 访问 OpenAI 等海外端点**超时/连不上**时，临时走代理：
  - git：`git -c http.proxy=http://127.0.0.1:10809 -c https.proxy=http://127.0.0.1:10809 clone ...`
  - 环境变量：`$env:HTTPS_PROXY='http://127.0.0.1:10809'; $env:HTTP_PROXY='http://127.0.0.1:10809'`（或 `socks5://127.0.0.1:10808`）
  - 实例：Codex 装 `skill-creator` 时 GitHub API/直连均超时，临时走 `127.0.0.1:10808` 后装成。
- 只在"必要时"加代理，别全局长期挂（会拖慢本可直连的请求）。