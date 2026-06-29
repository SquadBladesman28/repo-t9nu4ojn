#!/usr/bin/env python3
"""出图看护 + 自动回传：跑在 **Devin 自己的机器**上，不靠 LLM 轮询、不烧对话 token。

为什么要它：长任务里 LLM 反复 `get_output`/`exec` 盯进度，每一轮都耗对话 token；
最后一张卡死时干等更是浪费。这个看护进程接管"盯 + 回传"：

  gen_image.py 后台出图 → 本脚本后台盯 <outdir>/manifest.json →
  每落一张**新图**（含备份）就压成预览 JPEG（见 make_preview）→
  经隧道 /api/exec 推到用户机对应目录、sha256 自校验 → 记进 .delivered.json 不重发。

**绝不无限跑**，满足任一条件即收尾退出：
  ① manifest.ok==true（全出齐）；
  ② 指定了 --gen-pid 且该进程已结束（含重试用尽"放弃"的图，gen 会标 failed）；
  ③ --max-idle-min 内没有任何新图（兜底卡死）；
  ④ 到 --deadline-min 总时限。
退出前做最后一遍清扫，把"已交付/缺失"写进日志。

LLM 只需"点火 + 撒手"——启动它然后停手，之后零对话 token，用户那边图出一张到一张。

典型用法（在 Devin 机器后台起，**不在用户电脑上跑**）：
  nohup python scripts/gen_image.py --prompts-file jobs.json --outdir out --secrets-file s.env \
       > out/run.log 2>&1 & echo $! > out/gen.pid
  nohup python scripts/auto_deliver.py --outdir out \
       --tunnel http://xxxx.cpolar.cn/api/exec --token "Bearer ..." \
       --remote-dir "d:\\500_Code\\12345\\output\\fig" \
       --gen-pid "$(cat out/gen.pid)" --deadline-min 60 \
       > out/deliver.log 2>&1 &
"""
import argparse
import base64
import hashlib
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import make_preview as MP  # noqa: E402

CH = 7000  # cmd.exe 命令行上限 ~8191，base64 分片留足余量


def log(msg):
    sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stdout.flush()


# ---------- 纯逻辑（离线可测，不碰网络/磁盘） ----------

def collect_saved(manifest):
    """从 manifest 汇总所有已落盘图（完成的 + 未跑完内容的部分图），含备份。
    返回 [{name, path, role}]，path 为 Devin 机器上的本地绝对路径。"""
    out = []
    for bucket in (manifest.get("results") or []), (manifest.get("pending") or []):
        for item in bucket:
            for s in (item.get("saved") or []):
                p = s.get("path")
                if p:
                    out.append({"name": item.get("name"), "path": p,
                                "role": s.get("role")})
    return out


def new_items(manifest, delivered):
    """挑出还没回传过的图（按本地 path 去重）。"""
    return [it for it in collect_saved(manifest) if it["path"] not in delivered]


def is_finished(manifest, gen_alive, idle_exceeded, deadline_exceeded):
    """是否该收尾退出。任一成立即收尾，绝不无限跑。"""
    if manifest is not None and manifest.get("ok") is True:
        return True, "manifest.ok：全部出齐"
    if not gen_alive:
        return True, "出图进程已结束（含放弃的图）"
    if idle_exceeded:
        return True, "超过 max-idle 仍无新图，兜底收尾"
    if deadline_exceeded:
        return True, "到达 deadline 总时限"
    return False, ""


# ---------- 副作用：隧道上传 ----------

def exec_ps(tunnel, token, ps_cmd, timeout=60):
    body = json.dumps({"cmd": 'powershell -NoProfile -Command "%s"' %
                       ps_cmd.replace('"', '\\"')}).encode()
    req = urllib.request.Request(tunnel, data=body, method="POST",
                                 headers={"Authorization": token,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _remote_join(remote_dir, name):
    sep = "\\" if "\\" in remote_dir or ":" in remote_dir else "/"
    return remote_dir.rstrip("\\/") + sep + name


def upload(tunnel, token, local_path, remote_path, retries=3):
    """base64 分片 Add-Content 累加到 .b64tmp → 一次性 WriteAllBytes 还原 →
    Get-FileHash SHA256 与本地比对；无论成败都清掉 .b64tmp，不留垃圾。"""
    data = open(local_path, "rb").read()
    local_sha = hashlib.sha256(data).hexdigest()
    b64 = base64.b64encode(data).decode()
    tmp = remote_path + ".b64tmp"
    rp, tp = remote_path.replace("'", "''"), tmp.replace("'", "''")
    dirq = os.path.dirname(remote_path).replace("'", "''")

    def cleanup():
        try:
            exec_ps(tunnel, token, "if(Test-Path '%s'){Remove-Item '%s' -Force}" % (tp, tp))
        except Exception:
            pass

    for attempt in range(1, retries + 1):
        try:
            cleanup()
            exec_ps(tunnel, token, "New-Item -ItemType Directory -Force -Path '%s' | Out-Null" % dirq)
            for i in range(0, len(b64), CH):
                exec_ps(tunnel, token, "Add-Content -Path '%s' -Value '%s' -NoNewline" %
                        (tp, b64[i:i + CH]))
            r = exec_ps(tunnel, token,
                        "[IO.File]::WriteAllBytes('%s',[Convert]::FromBase64String((Get-Content '%s' -Raw)));"
                        "(Get-FileHash -Algorithm SHA256 '%s').Hash" % (rp, tp, rp))
            cleanup()
            remote_sha = (r.get("stdout") or "").strip().lower()
            if remote_sha == local_sha:
                return True
            log("校验不符（第 %d 次）：%s != %s" % (attempt, remote_sha[:12], local_sha[:12]))
        except Exception as e:
            log("上传异常（第 %d 次）：%s" % (attempt, e))
            cleanup()
        time.sleep(2 * attempt)
    return False


# ---------- 主循环 ----------

def _pid_alive(pid):
    if pid is None:
        return True  # 没给 pid 就靠 ok/idle/deadline 收尾
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _read_manifest(outdir):
    try:
        with open(os.path.join(outdir, "manifest.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def _save_state(path, delivered):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(sorted(delivered), f, ensure_ascii=False)
    except OSError:
        pass


def deliver_one(args, it):
    """压预览 + 上传一张；成功返回远端路径，失败返回 None。"""
    base = os.path.splitext(os.path.basename(it["path"]))[0] + ".jpg"
    preview = os.path.join(os.path.dirname(it["path"]), "preview", base)
    try:
        MP.make_preview(it["path"], preview, args.max_px, args.quality)
    except Exception as e:
        log("压缩失败 %s：%s（改传原图）" % (base, e))
        preview = it["path"]
        base = os.path.basename(it["path"])
    remote = _remote_join(args.remote_dir, base)
    ok = upload(args.tunnel, args.token, preview, remote)
    log(("已回传 " if ok else "回传失败 ") + base + (" (%s)" % it.get("role") if it.get("role") else ""))
    return remote if ok else None


def run(args):
    state_path = os.path.join(args.outdir, ".delivered.json")
    delivered = _load_state(state_path)
    start = time.time()
    last_new = start
    log("看护启动：outdir=%s remote=%s gen_pid=%s deadline=%dmin" %
        (args.outdir, args.remote_dir, args.gen_pid, args.deadline_min))

    while True:
        manifest = _read_manifest(args.outdir)
        pending_new = new_items(manifest, delivered) if manifest else []
        for it in pending_new:
            if deliver_one(args, it):
                delivered.add(it["path"])
                _save_state(state_path, delivered)
                last_new = time.time()

        now = time.time()
        idle_exceeded = args.max_idle_min > 0 and (now - last_new) > args.max_idle_min * 60
        deadline_exceeded = (now - start) > args.deadline_min * 60
        done, why = is_finished(manifest, _pid_alive(args.gen_pid),
                                idle_exceeded, deadline_exceeded)
        if done:
            # 最后清扫一遍，确保收尾时盘里有的都发了
            manifest = _read_manifest(args.outdir)
            for it in (new_items(manifest, delivered) if manifest else []):
                if deliver_one(args, it):
                    delivered.add(it["path"])
            _save_state(state_path, delivered)
            total = len(collect_saved(manifest)) if manifest else 0
            miss = [e.get("name") for e in (manifest.get("errors") or [])] if manifest else []
            log("收尾（%s）：已交付 %d/%d 张；缺失/失败：%s" %
                (why, len(delivered), total, miss or "无"))
            return 0 if (manifest and manifest.get("ok")) else 3
        time.sleep(args.poll_sec)


def build_parser():
    ap = argparse.ArgumentParser(description="出图看护 + 自动回传（跑在 Devin 机器，不烧 token）")
    ap.add_argument("--outdir", required=True, help="gen_image 的输出目录（含 manifest.json）")
    ap.add_argument("--tunnel", required=True, help="隧道 /api/exec 完整 URL")
    ap.add_argument("--token", required=True, help="隧道 Authorization 头（含 'Bearer '）")
    ap.add_argument("--remote-dir", dest="remote_dir", required=True, help="用户机目标目录")
    ap.add_argument("--gen-pid", dest="gen_pid", type=int, default=None,
                    help="出图进程 PID；给了就在它结束时收尾（推荐）")
    ap.add_argument("--max-px", dest="max_px", type=int, default=900, help="预览最长边像素")
    ap.add_argument("--quality", type=int, default=82, help="预览 JPEG 质量")
    ap.add_argument("--poll-sec", dest="poll_sec", type=int, default=15, help="轮询间隔秒")
    ap.add_argument("--max-idle-min", dest="max_idle_min", type=int, default=20,
                    help="多少分钟没新图就兜底收尾（0=不启用）")
    ap.add_argument("--deadline-min", dest="deadline_min", type=int, default=90,
                    help="总时限分钟，到点必退")
    return ap


if __name__ == "__main__":
    sys.exit(run(build_parser().parse_args()))
