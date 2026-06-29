#!/usr/bin/env python3
"""离线自测：monkeypatch 网络层，验证官方策略移植是否正确（不联网）。

覆盖：
  1. 多 Key 并发上限（每Key≤2、全局≤6）
  2. 单请求 n=N 一次拿回多张
  3. 瞬时错误任务层重试 + 逐 Key 冷却，最终成功
  4. content_policy 客户侧错误不重试，直接失败
  5. 故障转移：首选 Key 401 时切到别的 Key
  6. preflight ping 解析 banned
"""
import base64
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gen_image as G  # noqa: E402

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nFAKE").decode()


def _ok_payload(n):
    return {"data": [{"b64_json": PNG, "output_format": "png"} for _ in range(n)]}


def run_sched(tasks, keys, **kw):
    khs = [G.KeyHealth(kid, raw) for kid, raw in keys]
    outdir = tempfile.mkdtemp()
    s = G.Scheduler(tasks, khs, outdir, timeout=5, **kw)
    s.run()
    return s, outdir


def test_concurrency_caps():
    peak = {"global": 0, "perkey": {}}
    lock = threading.Lock()

    def fake_gen(key, payload, timeout=600):
        with lock:
            # 估算当前在飞：用 sched 的计数不易拿到，这里用 sleep 制造重叠后由断言查峰值
            pass
        time.sleep(0.2)
        return _ok_payload(payload["n"])

    orig = G.generate_image
    G.generate_image = fake_gen
    try:
        tasks = [G.Task(i, "t%d" % i, "p", 1, "auto", "gpt-image-2") for i in range(1, 13)]
        keys = [("kA", "rawA"), ("kB", "rawB")]
        # 监控峰值
        s, outdir = _run_with_peak_monitor(tasks, keys)
        assert s.tasks and all(t.status == "completed" for t in s.tasks), "all should complete"
        assert peak_holder["global"] <= G.GLOBAL_IMAGE_REQUEST_CONCURRENCY, peak_holder
        for kid, v in peak_holder["perkey"].items():
            assert v <= G.QUEUE_CONCURRENCY_PER_KEY, (kid, v)
    finally:
        G.generate_image = orig
    print("PASS test_concurrency_caps  peak=%s" % peak_holder)


peak_holder = {"global": 0, "perkey": {}}


def _run_with_peak_monitor(tasks, keys):
    khs = [G.KeyHealth(kid, raw) for kid, raw in keys]
    outdir = tempfile.mkdtemp()
    s = G.Scheduler(tasks, khs, outdir, timeout=5)
    stop = {"v": False}

    def mon():
        while not stop["v"]:
            with s.lock:
                peak_holder["global"] = max(peak_holder["global"], s.global_inflight)
                for kid, c in s.per_key_inflight.items():
                    peak_holder["perkey"][kid] = max(peak_holder["perkey"].get(kid, 0), c)
            time.sleep(0.01)
    t = threading.Thread(target=mon, daemon=True)
    t.start()
    s.run()
    stop["v"] = True
    return s, outdir


def test_single_call_n():
    def fake_gen(key, payload, timeout=600):
        assert payload["n"] == 3, "should send n=3 in one call"
        return _ok_payload(3)
    orig = G.generate_image
    G.generate_image = fake_gen
    try:
        tasks = [G.Task(1, "multi", "p", 3, "auto", "gpt-image-2")]
        s, outdir = run_sched(tasks, [("kA", "rawA")])
        d = time.strftime("%Y%m%d")
        assert len(s.tasks[0].results) == 3, s.tasks[0].results
        assert s.tasks[0].results[0]["path"].endswith("multi_%s.png" % d), s.tasks[0].results[0]
        assert s.tasks[0].results[1]["path"].endswith("multi_%s（备1）.png" % d), s.tasks[0].results[1]
    finally:
        G.generate_image = orig
    print("PASS test_single_call_n")


def test_transient_retry_then_success():
    calls = {"n": 0}

    def fake_gen(key, payload, timeout=600):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise G.RequestError("pool full", status=429,
                                 code="account_concurrency_exhausted")
        return _ok_payload(1)

    orig = G.generate_image
    G.generate_image = fake_gen
    # 把退避调短，避免测试慢
    base, mx = G.TASK_RETRY_BASE_MS, G.TASK_RETRY_MAX_MS
    cbase = G.KEY_COOLDOWN_BASE_MS
    G.TASK_RETRY_BASE_MS = 50
    G.TASK_RETRY_MAX_MS = 200
    G.KEY_COOLDOWN_BASE_MS = 50
    try:
        tasks = [G.Task(1, "retry", "p", 1, "auto", "gpt-image-2")]
        s, outdir = run_sched(tasks, [("kA", "rawA")])
        assert s.tasks[0].status == "completed", s.tasks[0].status
        assert s.tasks[0].retry_count == 2, s.tasks[0].retry_count
        assert calls["n"] == 3, calls
    finally:
        G.generate_image = orig
        G.TASK_RETRY_BASE_MS, G.TASK_RETRY_MAX_MS = base, mx
        G.KEY_COOLDOWN_BASE_MS = cbase
    print("PASS test_transient_retry_then_success (retries=%d)" % s.tasks[0].retry_count)


def test_content_policy_no_retry():
    calls = {"n": 0}

    def fake_gen(key, payload, timeout=600):
        calls["n"] += 1
        raise G.RequestError("blocked", status=400, code="content_policy_violation")

    orig = G.generate_image
    G.generate_image = fake_gen
    try:
        tasks = [G.Task(1, "bad", "p", 1, "auto", "gpt-image-2")]
        s, outdir = run_sched(tasks, [("kA", "rawA")])
        assert s.tasks[0].status == "failed", s.tasks[0].status
        assert calls["n"] == 1, "content_policy must NOT retry, got %d" % calls["n"]
    finally:
        G.generate_image = orig
    print("PASS test_content_policy_no_retry")


def test_failover_on_401():
    seen = {"keys": []}

    def fake_gen(key, payload, timeout=600):
        seen["keys"].append(key)
        if key == "rawA":
            raise G.RequestError("token invalid", status=401, code="account_token_invalid")
        return _ok_payload(1)

    orig = G.generate_image
    G.generate_image = fake_gen
    cbase = G.KEY_COOLDOWN_BASE_MS
    G.KEY_COOLDOWN_BASE_MS = 30
    try:
        # 强制首选 A：把 A 放前面，任务首次会钉到 A，失败后 A 进冷却，转移到 B
        tasks = [G.Task(1, "fo", "p", 1, "auto", "gpt-image-2")]
        s, outdir = run_sched(tasks, [("kA", "rawA"), ("kB", "rawB")])
        assert s.tasks[0].status == "completed", s.tasks[0].status
        assert "rawB" in seen["keys"], seen
    finally:
        G.generate_image = orig
        G.KEY_COOLDOWN_BASE_MS = cbase
    print("PASS test_failover_on_401  attempts=%s" % seen["keys"])


def test_preflight_ping_banned():
    def fake_ping(key):
        if key == "rawA":
            return {"auth": {"banned": True, "ban_remaining_seconds": 5}}
        return {"auth": {"banned": False}}
    orig = G.ping_server
    G.ping_server = fake_ping
    try:
        khs = [G.KeyHealth("kA", "rawA"), G.KeyHealth("kB", "rawB")]
        s = G.Scheduler([], khs, tempfile.mkdtemp(), timeout=5, preflight_ping=True)
        s._preflight()
        assert s._cooldown_remaining("kA") > 0, "banned key should be cooled down"
        assert s._cooldown_remaining("kB") == 0
    finally:
        G.ping_server = orig
    print("PASS test_preflight_ping_banned")


# ── Phase 1 新特性 ────────────────────────────────────────────────────────────
JPEG = base64.b64encode(b"\xff\xd8\xff\xe0FAKEJPEG").decode()


def test_fetch_url_and_datauri_and_ext():
    """M 取图鲁棒：url(含 data:)也能收；按 magic bytes 判扩展名（jpeg→jpg）。"""
    def fake_gen(key, payload, timeout=600):
        return {"data": [
            {"url": "data:image/png;base64," + JPEG},  # 服务端 url 模式 + 内联 data URI
        ]}
    orig = G.generate_image
    G.generate_image = fake_gen
    try:
        tasks = [G.Task(1, "u", "p", 1, "auto", "gpt-image-2")]
        s, outdir = run_sched(tasks, [("kA", "rawA")])
        assert s.tasks[0].status == "completed", s.tasks[0].status
        # 内容是 JPEG magic，扩展名应判成 jpg（不被请求里的 png 误导）
        assert s.tasks[0].results[0]["path"].endswith(".jpg"), s.tasks[0].results
    finally:
        G.generate_image = orig
    print("PASS test_fetch_url_and_datauri_and_ext")


def test_filename_sanitize():
    """N 文件名清洗：非法字符 → -，保留中文。"""
    def fake_gen(key, payload, timeout=600):
        return _ok_payload(1)
    orig = G.generate_image
    G.generate_image = fake_gen
    try:
        tasks = [G.Task(1, '橘猫/海边:日落*?<>|"', "p", 1, "auto", "gpt-image-2")]
        s, outdir = run_sched(tasks, [("kA", "rawA")])
        fn = os.path.basename(s.tasks[0].results[0]["path"])
        for bad in '/:*?<>|"':
            assert bad not in fn, (bad, fn)
        assert "橘猫" in fn, fn
    finally:
        G.generate_image = orig
    print("PASS test_filename_sanitize  -> %s" % fn)


def test_params_passthrough():
    """L 参数面：显式参数透传进 payload；output_format→output_image_format。"""
    seen = {}

    def fake_gen(key, payload, timeout=600):
        seen.update(payload)
        return _ok_payload(1)
    orig = G.generate_image
    G.generate_image = fake_gen
    try:
        params = {"quality": "high", "moderation": "low", "output_image_format": "jpeg"}
        tasks = [G.Task(1, "p", "p", 1, "1024x1024", "gpt-image-2", params=params)]
        run_sched(tasks, [("kA", "rawA")])
        assert seen.get("quality") == "high", seen
        assert seen.get("moderation") == "low", seen
        assert seen.get("output_image_format") == "jpeg", seen
    finally:
        G.generate_image = orig
    print("PASS test_params_passthrough")


def test_variation_endpoint():
    """K 变体：有 var_path 时走 variation_image（/v1/images/variations）。"""
    img = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    img.write(b"\x89PNG\r\n\x1a\nSEED")
    img.close()
    seen = {"called": False, "fields": None}

    def fake_var(key, fields, files, timeout=600):
        seen["called"] = True
        seen["fields"] = fields
        assert "image" in files, files
        return _ok_payload(1)
    orig = G.variation_image
    G.variation_image = fake_var
    try:
        tasks = [G.Task(1, "v", "", 1, "auto", "gpt-image-2", var_path=img.name)]
        s, outdir = run_sched(tasks, [("kA", "rawA")])
        assert seen["called"], "variation_image must be called"
        assert s.tasks[0].status == "completed"
    finally:
        G.variation_image = orig
        os.unlink(img.name)
    print("PASS test_variation_endpoint")


def test_edit_with_mask():
    """K 蒙版：有 edit_path + mask_path 时 edit_image 收到 image + mask 两个文件。"""
    img = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    img.write(b"\x89PNG\r\n\x1a\nIMG")
    img.close()
    msk = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    msk.write(b"\x89PNG\r\n\x1a\nMASK")
    msk.close()
    seen = {"files": None}

    def fake_edit(key, fields, files, timeout=600):
        seen["files"] = files
        return _ok_payload(1)
    orig = G.edit_image
    G.edit_image = fake_edit
    try:
        tasks = [G.Task(1, "e", "把天空改成晚霞", 1, "auto", "gpt-image-2",
                        edit_path=img.name, mask_path=msk.name)]
        s, outdir = run_sched(tasks, [("kA", "rawA")])
        assert seen["files"] and "image" in seen["files"] and "mask" in seen["files"], seen
        assert s.tasks[0].status == "completed"
    finally:
        G.edit_image = orig
        os.unlink(img.name)
        os.unlink(msk.name)
    print("PASS test_edit_with_mask")


# ── Phase 2 赛马 + 覆盖优先 ────────────────────────────────────────────────────
def test_race_splits_into_n1():
    """D 赛马：need=3 时拆成 3 个 n=1 并发请求，先到为主图，共 3 张。"""
    calls = {"ns": []}

    def fake_gen(key, payload, timeout=600):
        calls["ns"].append(payload["n"])
        return _ok_payload(payload["n"])
    orig = G.generate_image
    G.generate_image = fake_gen
    try:
        tasks = [G.Task(1, "race", "p", 3, "auto", "gpt-image-2")]
        s, outdir = run_sched(tasks, [("kA", "rawA")], race=True)
        assert calls["ns"] == [1, 1, 1], calls["ns"]      # 每请求都是 n=1
        assert s.tasks[0].got == 3, s.tasks[0].got
        assert s.tasks[0].issued == 3, s.tasks[0].issued
        assert s.tasks[0].results[0]["role"] == "primary"
        assert s.tasks[0].results[1]["role"] == "backup"
    finally:
        G.generate_image = orig
    print("PASS test_race_splits_into_n1")


def test_coverage_first_spreads_primaries():
    """E 覆盖优先：2 内容各 need=2、单 Key（并发2），先各出一张再补备份。
    对比：关掉覆盖优先时按索引，前两并发都打到内容1。"""
    def make_recorder():
        order, lock = [], threading.Lock()

        def fake_gen(key, payload, timeout=600):
            with lock:
                order.append(payload["prompt"])
            time.sleep(0.15)
            return _ok_payload(payload["n"])
        return order, fake_gen

    orig = G.generate_image
    try:
        # 覆盖优先：前两个并发请求应分别属于两个内容
        order, fake = make_recorder()
        G.generate_image = fake
        tasks = [G.Task(1, "a", "promptA", 2, "auto", "gpt-image-2"),
                 G.Task(2, "b", "promptB", 2, "auto", "gpt-image-2")]
        run_sched(tasks, [("kA", "rawA")], race=True, coverage_first=True)
        assert set(order[:2]) == {"promptA", "promptB"}, ("coverage-first 前两并发应覆盖两内容", order)

        # 不开覆盖优先：按索引，内容1先吃满 2 个并发槽
        order2, fake2 = make_recorder()
        G.generate_image = fake2
        tasks2 = [G.Task(1, "a", "promptA", 2, "auto", "gpt-image-2"),
                  G.Task(2, "b", "promptB", 2, "auto", "gpt-image-2")]
        run_sched(tasks2, [("kA", "rawA")], race=True, coverage_first=False)
        assert order2[:2] == ["promptA", "promptA"], ("index 序应先吃满内容1", order2)
    finally:
        G.generate_image = orig
    print("PASS test_coverage_first_spreads_primaries")


def test_partial_success_keeps_primary():
    """赛马下主图已出但备份请求耗尽重试 → 仍判 completed(部分)，不丢主图。"""
    calls = {"n": 0}

    def fake_gen(key, payload, timeout=600):
        calls["n"] += 1
        if calls["n"] == 1:
            return _ok_payload(1)                      # 主图成功
        raise G.RequestError("storm", status=502, code="upstream_server_error")  # 备份一直失败
    orig = G.generate_image
    G.generate_image = fake_gen
    base, mx, cbase = G.TASK_RETRY_BASE_MS, G.TASK_RETRY_MAX_MS, G.KEY_COOLDOWN_BASE_MS
    G.TASK_RETRY_BASE_MS = G.TASK_RETRY_MAX_MS = G.KEY_COOLDOWN_BASE_MS = 20
    try:
        tasks = [G.Task(1, "p", "p", 2, "auto", "gpt-image-2")]
        s, outdir = run_sched(tasks, [("kA", "rawA")], race=True)
        assert s.tasks[0].status == "completed", s.tasks[0].status
        assert s.tasks[0].got == 1, s.tasks[0].got     # 只拿到主图，备份没出
    finally:
        G.generate_image = orig
        G.TASK_RETRY_BASE_MS, G.TASK_RETRY_MAX_MS, G.KEY_COOLDOWN_BASE_MS = base, mx, cbase
    print("PASS test_partial_success_keeps_primary")


# ── Phase 3 受阻 SOP（H）──────────────────────────────────────────────────────
def test_blocked_detection_and_giveup():
    """H：所有请求撞 502、零进展 → 触发 blocked(502_storm)；开 give-up 后提前收尾。
    ping_refine 关掉以免联网。"""
    def fake_gen(key, payload, timeout=600):
        raise G.RequestError("storm", status=502, code="upstream_server_error")
    orig = G.generate_image
    G.generate_image = fake_gen
    base, mx, cbase = G.TASK_RETRY_BASE_MS, G.TASK_RETRY_MAX_MS, G.KEY_COOLDOWN_BASE_MS
    G.TASK_RETRY_BASE_MS = G.TASK_RETRY_MAX_MS = G.KEY_COOLDOWN_BASE_MS = 20
    try:
        khs = [G.KeyHealth("kA", "rawA")]
        tasks = [G.Task(1, "blk", "p", 1, "auto", "gpt-image-2")]
        outdir = tempfile.mkdtemp()
        # block_after 设很短(0.1s)、给 give-up 0.3s，关 ping
        s = G.Scheduler(tasks, khs, outdir, timeout=5, ping_refine=False,
                        block_after_ms=100, give_up_after_ms=300)
        s.run()
        assert s.blocked or s.gave_up, ("应判受阻", s.blocked, s.gave_up)
        assert s.block_reason == "502_storm", s.block_reason
        assert s.gave_up, "give-up 应触发提前收尾"
        bs = s._block_status()
        assert bs["blocked"] is True and bs["gave_up"] is True, bs
    finally:
        G.generate_image = orig
        G.TASK_RETRY_BASE_MS, G.TASK_RETRY_MAX_MS, G.KEY_COOLDOWN_BASE_MS = base, mx, cbase
    print("PASS test_blocked_detection_and_giveup")


def test_blocked_ping_distinguishes_ban():
    """H：受阻时 ping 若返回 banned → 原因升级为 key_banned。"""
    def fake_gen(key, payload, timeout=600):
        raise G.RequestError("storm", status=502, code="upstream_server_error")

    def fake_ping(key):
        return {"auth": {"banned": True, "ban_remaining_seconds": 30}}
    og, op = G.generate_image, G.ping_server
    G.generate_image = fake_gen
    G.ping_server = fake_ping
    base, mx, cbase = G.TASK_RETRY_BASE_MS, G.TASK_RETRY_MAX_MS, G.KEY_COOLDOWN_BASE_MS
    G.TASK_RETRY_BASE_MS = G.TASK_RETRY_MAX_MS = G.KEY_COOLDOWN_BASE_MS = 20
    try:
        khs = [G.KeyHealth("kA", "rawA")]
        tasks = [G.Task(1, "blk", "p", 1, "auto", "gpt-image-2")]
        s = G.Scheduler(tasks, khs, tempfile.mkdtemp(), timeout=5, ping_refine=True,
                        block_after_ms=100, give_up_after_ms=300)
        s.run()
        assert s.block_reason == "key_banned", s.block_reason
    finally:
        G.generate_image, G.ping_server = og, op
        G.TASK_RETRY_BASE_MS, G.TASK_RETRY_MAX_MS, G.KEY_COOLDOWN_BASE_MS = base, mx, cbase
    print("PASS test_blocked_ping_distinguishes_ban")


if __name__ == "__main__":
    test_concurrency_caps()
    test_single_call_n()
    test_transient_retry_then_success()
    test_content_policy_no_retry()
    test_failover_on_401()
    test_preflight_ping_banned()
    # Phase 1
    test_fetch_url_and_datauri_and_ext()
    test_filename_sanitize()
    test_params_passthrough()
    test_variation_endpoint()
    test_edit_with_mask()
    # Phase 2
    test_race_splits_into_n1()
    test_coverage_first_spreads_primaries()
    test_partial_success_keeps_primary()
    # Phase 3
    test_blocked_detection_and_giveup()
    test_blocked_ping_distinguishes_ban()
    print("\nALL OFFLINE TESTS PASSED")
