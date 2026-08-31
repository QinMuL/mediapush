"""ShareWatcher 目录监控测试：扫描→建分享→推卡片→标记 / 失败退避 / 违规 blocked。"""

import asyncio
import time

from app.core.share_watcher import ShareWatcher
from app.providers.exceptions import Pan115Error


class _FakeCache:
    """两阶段桩：record_share 登记 pending → mark_shared 置 ok；
    record_share_failed 登记 failed（保留已有 share_code 供下轮复用）。"""

    def __init__(self, dirs):
        self.dirs = dirs
        self.records: dict[tuple[int, int], dict] = {}
        self.recorded: list[tuple] = []
        self.failed_records: list[tuple] = []

    async def list_share_dirs(self):
        return self.dirs

    async def get_shared_item(self, dir_id, file_id):
        return self.records.get((dir_id, file_id))

    async def record_share(self, dir_id, file_id, name, share_code, password=""):
        self.records[(dir_id, file_id)] = {
            "file_id": file_id, "dir_id": dir_id, "name": name,
            "share_code": share_code, "password": password, "status": "pending",
            "fail_count": 0, "next_retry_at": 0.0, "fail_reason": "",
        }
        self.recorded.append((dir_id, file_id, share_code, password))

    async def record_share_failed(
        self, dir_id, file_id, name, *, fail_count, next_retry_at, reason
    ):
        existing = self.records.get((dir_id, file_id), {})
        self.records[(dir_id, file_id)] = {
            "file_id": file_id, "dir_id": dir_id, "name": name,
            "share_code": existing.get("share_code", ""),
            "password": existing.get("password", ""),
            "status": "failed",
            "fail_count": fail_count,
            "next_retry_at": next_retry_at,
            "fail_reason": reason,
        }
        self.failed_records.append((dir_id, file_id, fail_count, next_retry_at, reason))

    async def mark_shared(self, dir_id, file_id):
        rec = self.records.get((dir_id, file_id))
        if rec:
            rec["status"] = "ok"


class _FakePan115:
    def __init__(self, dirs_map, cookie="UID=1;CID=2;"):
        self.dirs_map = dirs_map  # cid -> [items]
        self.cookie = cookie
        self.share_calls: list = []
        self.makedirs_calls: list[str] = []
        self.moved: list[tuple[int, int]] = []

    async def list_dir(self, cid):
        return self.dirs_map.get(cid, [])

    async def create_share(self, fid):
        self.share_calls.append(fid)
        if fid == 999:  # 特殊 fid：模拟建分享失败
            raise Pan115Error("创建分享失败：风控")
        return f"code{fid}", f"pwd{fid}"

    async def fs_makedirs(self, path):
        self.makedirs_calls.append(path)
        return 999  # 归档目录 CID（桩固定值）

    async def fs_move(self, fid, to_cid):
        self.moved.append((fid, to_cid))


class _FakeProcessor:
    def __init__(self, fail_codes=frozenset()):
        self.process_calls: list = []
        self._fail = set(fail_codes)

    async def process(self, parsed):
        self.process_calls.append(parsed)
        from app.core.processor import ProcessResult

        if parsed.code in self._fail:
            return ProcessResult(False, f"推送失败：{parsed.code}")
        return ProcessResult(True, "已推送（测试）", title="X")


class _FakeContainer:
    def __init__(self, pan115, cache, processor):
        self.pan115 = pan115
        self.cache = cache
        self.processor = processor

    def refresh_cookie_file(self) -> bool:  # _loop 每轮调用（统一热更新入口）
        return False


class _FakeSettings:
    share_watch_interval_minutes = 10.0
    share_archive_dir = ""  # 默认不启用归档（现有用例语义不变）
    share_watch_notify = True
    tg_admin_ids: tuple = ()


class _ArchiveSettings(_FakeSettings):
    share_archive_dir = "/已分享"


def _dir(idx, path, cid):
    return {"id": idx, "path": path, "cid": cid, "shared": 0}


def _fast(monkeypatch):
    """吞掉限速/就绪等待，测试秒过。"""
    async def _no_sleep(sec):
        return None
    monkeypatch.setattr("app.core.share_watcher.asyncio.sleep", _no_sleep)


def test_run_once_shares_and_pushes_new_dirs(monkeypatch):
    """新子目录 → 建分享 → 推卡片 → 标记 ok；已分享（ok）的跳过。"""
    _fast(monkeypatch)
    cache = _FakeCache([_dir(1, "/媒体", 100)])
    pan115 = _FakePan115({100: [
        {"fid": 11, "name": "剧A", "is_dir": True, "size": 0},
        {"fid": 12, "name": "剧B", "is_dir": True, "size": 0},
    ]})
    # 剧B 已推送（ok 状态）
    cache.records[(1, 12)] = {
        "file_id": 12, "dir_id": 1, "name": "剧B",
        "share_code": "old12", "password": "", "status": "ok",
    }
    proc = _FakeProcessor()
    watcher = ShareWatcher(_FakeContainer(pan115, cache, proc), _FakeSettings())

    report = asyncio.run(watcher.run_once())

    assert report.dirs == 1
    assert report.new_items == 1  # 只有剧A
    assert report.shared == 1
    assert report.skipped == 1  # 剧B
    # 建分享 + 推送均发生，且访问码传对
    assert pan115.share_calls == [11]
    assert len(proc.process_calls) == 1
    p = proc.process_calls[0]
    assert p.provider == "115" and p.code == "code11" and p.password == "pwd11"
    # 两阶段：登记 pending（含码/密码）→ 推送成功置 ok
    assert cache.recorded == [(1, 11, "code11", "pwd11")]
    assert cache.records[(1, 11)]["status"] == "ok"


def test_run_once_share_failure_backoff_not_retry_next_round(monkeypatch):
    _fast(monkeypatch)
    """建分享失败（普通错误，非违规）→ 登记 failed 退避 → 下轮未到期跳过。"""
    cache = _FakeCache([_dir(1, "/媒体", 100)])
    pan115 = _FakePan115({100: [
        {"fid": 999, "name": "剧C", "is_dir": True, "size": 0},
    ]})
    proc = _FakeProcessor()
    watcher = ShareWatcher(_FakeContainer(pan115, cache, proc), _FakeSettings())

    report = asyncio.run(watcher.run_once())

    assert report.failed == 1
    assert report.blocked == 0
    assert report.shared == 0
    assert cache.recorded == []  # record_share 未调（建分享失败）
    assert cache.failed_records  # record_share_failed 被调
    assert proc.process_calls == []  # 未推送
    rec = cache.records[(1, 999)]
    assert rec["status"] == "failed"
    assert rec["fail_count"] == 1

    # 第二轮：退避未到期 → 跳过，不再 create_share
    pan115.share_calls.clear()
    report2 = asyncio.run(watcher.run_once())
    assert report2.backoff == 1
    assert report2.failed == 0
    assert pan115.share_calls == []


def test_run_once_auditing_counted_not_failed(monkeypatch):
    """审核中/快照生成中（新分享正常中间态）→ 计 auditing 不计 failed，INFO 不打堆栈。"""
    _fast(monkeypatch)
    from app.providers.exceptions import Pan115Error

    cache = _FakeCache([_dir(1, "/媒体", 100)])
    pan115 = _FakePan115({100: [
        {"fid": 61, "name": "剧H", "is_dir": True},
    ]})

    class _AuditProcessor(_FakeProcessor):
        async def process(self, parsed):
            self.process_calls.append(parsed)
            raise Pan115Error("分享审核中")

    proc = _AuditProcessor()
    watcher = ShareWatcher(_FakeContainer(pan115, cache, proc), _FakeSettings())

    report = asyncio.run(watcher.run_once())

    assert report.auditing == 1  # 归类为审核中
    assert report.failed == 0  # 不算失败
    assert report.shared == 0
    # 分享码已登记 pending → 下轮复用码重试（关键）
    assert cache.recorded == [(1, 61, "code61", "pwd61")]
    assert cache.records[(1, 61)]["status"] == "pending"
    assert "审核中" in report.summary()


def test_run_once_push_failure_backoff_keeps_share_code(monkeypatch):
    _fast(monkeypatch)
    """推送失败（process 返回 ok=False）→ 登记 failed 退避，但 share_code 留存，下轮复用码。"""
    cache = _FakeCache([_dir(1, "/媒体", 100)])
    pan115 = _FakePan115({100: [
        {"fid": 21, "name": "剧D", "is_dir": True, "size": 0},
    ]})
    proc = _FakeProcessor(fail_codes={"code21"})
    watcher = ShareWatcher(_FakeContainer(pan115, cache, proc), _FakeSettings())

    report = asyncio.run(watcher.run_once())

    assert report.failed == 1
    assert pan115.share_calls == [21]  # 分享已建
    # 关键：登记 pending（码留存）→ 推送失败转 failed 退避，但 share_code 保留
    assert cache.recorded == [(1, 21, "code21", "pwd21")]
    assert cache.records[(1, 21)]["status"] == "failed"
    assert cache.records[(1, 21)]["share_code"] == "code21"  # 码留存，下轮复用
    assert cache.records[(1, 21)]["fail_count"] == 1
    assert report.shared == 0


def test_run_once_violation_marks_blocked_no_retry(monkeypatch):
    """建分享遇'违规'→ 标记 blocked → 下一轮不再 create_share（静默跳过）。"""
    _fast(monkeypatch)
    cache = _FakeCache([_dir(1, "/待分享目录", 100)])

    class _ViolatePan115(_FakePan115):
        async def create_share(self, fid):
            self.share_calls.append(fid)
            raise Pan115Error("创建分享失败：分享含违规文件")

    pan115 = _ViolatePan115({100: [{"fid": 500, "name": "蜘蛛侠", "is_dir": True}]})
    proc = _FakeProcessor()
    watcher = ShareWatcher(_FakeContainer(pan115, cache, proc), _FakeSettings())

    report = asyncio.run(watcher.run_once())
    assert report.failed == 1
    assert report.blocked == 1
    assert pan115.share_calls == [500]  # 试过一次
    assert cache.failed_records  # 登记了失败
    rec = cache.records[(1, 500)]
    assert rec["status"] == "failed"
    assert "违规" in rec["fail_reason"]

    # 第二轮：blocked → 跳过，不再 create_share
    pan115.share_calls.clear()
    report2 = asyncio.run(watcher.run_once())
    assert report2.blocked == 1
    assert report2.failed == 0
    assert pan115.share_calls == []  # 关键：不再重试


def test_backoff_due_retries_create_share_again(monkeypatch):
    """退避到期 → 重新 create_share → 成功 → mark_shared。"""
    _fast(monkeypatch)
    cache = _FakeCache([_dir(1, "/媒体", 100)])

    class _FlakyPan115(_FakePan115):
        def __init__(self):
            super().__init__({100: [{"fid": 600, "name": "剧X", "is_dir": True}]})
            self.attempts = 0

        async def create_share(self, fid):
            self.attempts += 1
            if self.attempts == 1:
                raise Pan115Error("创建分享失败：网络超时")
            return f"code{fid}", f"pwd{fid}"

    pan115 = _FlakyPan115()
    proc = _FakeProcessor()
    watcher = ShareWatcher(_FakeContainer(pan115, cache, proc), _FakeSettings())

    report = asyncio.run(watcher.run_once())
    assert report.failed == 1
    assert cache.records[(1, 600)]["status"] == "failed"

    # 退避未到期 → 跳过
    pan115.share_calls.clear()
    asyncio.run(watcher.run_once())
    assert pan115.share_calls == []

    # 模拟退避到期 → 重新建分享成功
    cache.records[(1, 600)]["next_retry_at"] = time.time() - 1
    report3 = asyncio.run(watcher.run_once())
    assert report3.failed == 0
    assert report3.shared == 1  # 重建分享成功
    assert cache.records[(1, 600)]["status"] == "ok"


def test_pending_push_failure_backoff_due_reuses_code(monkeypatch):
    """pending 推送失败 → 退避到期 → 复用 share_code 重推成功（不重建分享）。"""
    _fast(monkeypatch)
    cache = _FakeCache([_dir(1, "/媒体", 100)])
    pan115 = _FakePan115({100: [{"fid": 700, "name": "剧Y", "is_dir": True}]})
    proc = _FakeProcessor(fail_codes={"code700"})  # 第一次推送失败
    watcher = ShareWatcher(_FakeContainer(pan115, cache, proc), _FakeSettings())

    # 第一轮：建分享成功（pending）→ 推送失败 → failed 退避（码保留）
    asyncio.run(watcher.run_once())
    assert cache.records[(1, 700)]["status"] == "failed"
    assert cache.records[(1, 700)]["share_code"] == "code700"

    # 退避到期 → 复用码重推
    cache.records[(1, 700)]["next_retry_at"] = time.time() - 1
    proc._fail = set()  # 第二次推送成功
    report2 = asyncio.run(watcher.run_once())
    assert report2.failed == 0
    assert report2.retried == 1  # 复用码重推成功
    assert pan115.share_calls == [700]  # 只建过一次分享（没重建）
    assert cache.records[(1, 700)]["status"] == "ok"


def test_run_once_no_dirs_nop():
    cache = _FakeCache([])
    pan115 = _FakePan115({})
    proc = _FakeProcessor()
    watcher = ShareWatcher(_FakeContainer(pan115, cache, proc), _FakeSettings())

    report = asyncio.run(watcher.run_once())
    assert report.dirs == 0
    assert pan115.share_calls == []


def test_run_once_no_cookie_skips():
    """无 cookie → 跳过（不扫目录不建分享）。"""
    cache = _FakeCache([_dir(1, "/媒体", 100)])
    pan115 = _FakePan115({100: [{"fid": 31, "name": "剧E", "is_dir": True}]}, cookie="")
    proc = _FakeProcessor()
    watcher = ShareWatcher(_FakeContainer(pan115, cache, proc), _FakeSettings())

    report = asyncio.run(watcher.run_once())

    assert report.new_items == 0
    assert pan115.share_calls == []


def test_run_once_list_dir_error_counts_failed(monkeypatch):
    """列目录异常（网络/登录态）→ 计失败不崩，下轮再看。"""
    cache = _FakeCache([_dir(1, "/媒体", 100)])

    class _Boom(_FakePan115):
        async def list_dir(self, cid):
            raise RuntimeError("connect timeout")

    pan115 = _Boom({})
    proc = _FakeProcessor()
    watcher = ShareWatcher(_FakeContainer(pan115, cache, proc), _FakeSettings())

    report = asyncio.run(watcher.run_once())
    assert report.failed == 1


def test_share_rate_limit_sleep_between_pushes(monkeypatch):
    """新分享就绪等待 5s + 连续推送限速 2s。"""
    sleeps: list = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr("app.core.share_watcher.asyncio.sleep", fake_sleep)
    cache = _FakeCache([_dir(1, "/媒体", 100)])
    pan115 = _FakePan115({100: [
        {"fid": 41, "name": "剧F", "is_dir": True},
        {"fid": 42, "name": "剧G", "is_dir": True},
    ]})
    proc = _FakeProcessor()
    watcher = ShareWatcher(_FakeContainer(pan115, cache, proc), _FakeSettings())

    asyncio.run(watcher.run_once())
    # 每个新分享：5s 就绪等待 + 推送成功后 2s 限速
    assert sleeps == [5.0, 2.0, 5.0, 2.0]


def test_start_stop_task_lifecycle():
    cache = _FakeCache([])
    pan115 = _FakePan115({})
    proc = _FakeProcessor()
    watcher = ShareWatcher(_FakeContainer(pan115, cache, proc), _FakeSettings())

    async def run():
        await watcher.start()
        assert watcher._task is not None and not watcher._task.done()
        await watcher.stop()
        assert watcher._task is None

    asyncio.run(run())


def test_watcher_report_summary():
    from app.core.share_watcher import WatchReport

    r = WatchReport(dirs=2, new_items=3, shared=2, failed=1, skipped=4)
    s = r.summary()
    assert "扫描 2 个目录" in s
    assert "推送 2" in s
    assert "失败 1" in s
    assert "已分享 4" in s

    # blocked + backoff 展示
    r2 = WatchReport(blocked=1, backoff=2)
    s2 = r2.summary()
    assert "违规" in s2
    assert "退避" in s2


# ==================== 归档：推送成功后移入 SHARE_ARCHIVE_DIR ====================
def test_archive_move_after_push_success(monkeypatch):
    """推送成功 → fs_makedirs 幂等建归档目录 + fs_move 移入。"""
    _fast(monkeypatch)
    cache = _FakeCache([_dir(1, "/媒体", 100)])
    pan115 = _FakePan115({100: [{"fid": 71, "name": "剧I", "is_dir": True}]})
    proc = _FakeProcessor()
    watcher = ShareWatcher(
        _FakeContainer(pan115, cache, proc), _ArchiveSettings()
    )

    report = asyncio.run(watcher.run_once())

    assert report.shared == 1
    assert pan115.makedirs_calls == ["/已分享"]
    assert pan115.moved == [(71, 999)]
    assert watcher._archive_cid == 999  # CID 已缓存


def test_archive_skipped_dir_retried(monkeypatch):
    """ok 状态仍在监控目录（上次移动失败/归档中途启用）→ 本轮仅补移。"""
    _fast(monkeypatch)
    cache = _FakeCache([_dir(1, "/媒体", 100)])
    cache.records[(1, 81)] = {
        "file_id": 81, "dir_id": 1, "name": "剧J",
        "share_code": "old81", "password": "", "status": "ok",
    }
    pan115 = _FakePan115({100: [{"fid": 81, "name": "剧J", "is_dir": True}]})
    proc = _FakeProcessor()
    watcher = ShareWatcher(
        _FakeContainer(pan115, cache, proc), _ArchiveSettings()
    )

    report = asyncio.run(watcher.run_once())

    assert report.skipped == 1
    assert report.shared == 0
    assert pan115.share_calls == []  # 不再建分享/推送
    assert proc.process_calls == []
    assert pan115.moved == [(81, 999)]  # 只补移动


def test_archive_move_failure_not_fatal(monkeypatch):
    """移动失败仅告警：推送仍计成功、状态 ok；缓存 CID 清空待下轮重解析。"""
    _fast(monkeypatch)

    class _BoomMove(_FakePan115):
        async def fs_move(self, fid, to_cid):
            raise Pan115Error("移动失败：风控")

    cache = _FakeCache([_dir(1, "/媒体", 100)])
    pan115 = _BoomMove({100: [{"fid": 91, "name": "剧K", "is_dir": True}]})
    proc = _FakeProcessor()
    watcher = ShareWatcher(
        _FakeContainer(pan115, cache, proc), _ArchiveSettings()
    )

    report = asyncio.run(watcher.run_once())

    assert report.shared == 1  # 推送成功不受影响
    assert report.failed == 0
    assert cache.records[(1, 91)]["status"] == "ok"
    assert watcher._archive_cid is None  # 缓存失效清空 → 下轮重解析


def test_archive_disabled_no_move(monkeypatch):
    """SHARE_ARCHIVE_DIR 空 → 不建目录不移动（仅标记）。"""
    _fast(monkeypatch)
    cache = _FakeCache([_dir(1, "/媒体", 100)])
    pan115 = _FakePan115({100: [{"fid": 95, "name": "剧L", "is_dir": True}]})
    proc = _FakeProcessor()
    watcher = ShareWatcher(_FakeContainer(pan115, cache, proc), _FakeSettings())

    report = asyncio.run(watcher.run_once())

    assert report.shared == 1
    assert pan115.makedirs_calls == []
    assert pan115.moved == []


def test_archive_interval_between_moves(monkeypatch):
    """移动提交成功后留 3s 间隔：连续补移不背靠背（防 990009 忙冲突）。"""
    sleeps: list = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr("app.core.share_watcher.asyncio.sleep", fake_sleep)
    cache = _FakeCache([_dir(1, "/媒体", 100)])
    for fid, nm in ((81, "剧J"), (82, "剧M")):
        cache.records[(1, fid)] = {
            "file_id": fid, "dir_id": 1, "name": nm,
            "share_code": f"old{fid}", "password": "", "status": "ok",
        }
    pan115 = _FakePan115({100: [
        {"fid": 81, "name": "剧J", "is_dir": True},
        {"fid": 82, "name": "剧M", "is_dir": True},
    ]})
    proc = _FakeProcessor()
    watcher = ShareWatcher(
        _FakeContainer(pan115, cache, proc), _ArchiveSettings()
    )

    report = asyncio.run(watcher.run_once())

    assert report.skipped == 2
    assert pan115.moved == [(81, 999), (82, 999)]
    assert sleeps == [3.0, 3.0]  # 每次移动提交后留间隔


def test_archive_move_failure_no_interval(monkeypatch):
    """移动失败 → 不留间隔（无提交成功，重试已由 fs_move 内部处理）。"""
    sleeps: list = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr("app.core.share_watcher.asyncio.sleep", fake_sleep)

    class _BoomMove(_FakePan115):
        async def fs_move(self, fid, to_cid):
            raise Pan115Error("移动失败：风控")

    cache = _FakeCache([_dir(1, "/媒体", 100)])
    pan115 = _BoomMove({100: [{"fid": 96, "name": "剧N", "is_dir": True}]})
    proc = _FakeProcessor()
    watcher = ShareWatcher(
        _FakeContainer(pan115, cache, proc), _ArchiveSettings()
    )

    asyncio.run(watcher.run_once())

    assert sleeps == [5.0, 2.0]  # 新分享就绪等待 + 推送限速；移动失败不留间隔


# ==================== 任务详情通知：成功/失败明细私信 admin ====================
class _FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


class _FakeTelegram:
    def __init__(self):
        self.bot = _FakeBot()


class _NotifyContainer(_FakeContainer):
    def __init__(self, pan115, cache, processor, telegram):
        super().__init__(pan115, cache, processor)
        self.telegram = telegram


class _NotifySettings(_FakeSettings):
    tg_admin_ids = (100, 200)


def test_notify_admin_success_and_failure_details():
    """成功/审核/失败明细都进入通知文本，每个 admin 各发一条。"""
    from app.core.share_watcher import WatchReport

    r = WatchReport(dirs=1, new_items=2, shared=1, failed=1, auditing=1)
    r.items = [{"dir": "/待分享", "name": "剧A"}]
    r.audit_items = [{"dir": "/待分享", "name": "剧B"}]
    r.failed_items = [{"dir": "/待分享", "name": "剧C", "reason": "推送失败：超时"}]

    telegram = _FakeTelegram()
    watcher = ShareWatcher(
        _NotifyContainer(None, None, None, telegram), _NotifySettings()
    )

    asyncio.run(watcher.notify_admin(r))

    assert len(telegram.bot.sent) == 2  # 两个 admin 各一条
    text = telegram.bot.sent[0][1]
    assert text.startswith("📂 目录监控：")
    assert "✅ 剧A（/待分享）" in text
    assert "⏳ 剧B（/待分享）审核中" in text
    assert "⚠️ 剧C（/待分享）：推送失败：超时" in text


def test_notify_admin_skipped_when_no_admin_or_telegram():
    """无 admin / telegram 未就绪 → 静默跳过。"""
    from app.core.share_watcher import WatchReport

    r = WatchReport(shared=1, items=[{"dir": "/d", "name": "x"}])

    # 无 telegram 属性（容器未挂载）
    watcher = ShareWatcher(_FakeContainer(None, None, None), _NotifySettings())
    asyncio.run(watcher.notify_admin(r))  # 不抛错

    # telegram 就绪但无 admin
    telegram = _FakeTelegram()
    watcher = ShareWatcher(
        _NotifyContainer(None, None, None, telegram), _FakeSettings()
    )
    asyncio.run(watcher.notify_admin(r))
    assert telegram.bot.sent == []


def test_notify_admin_reason_truncated():
    """超长失败原因截断到 120 字；整条文本超 3800 截断。"""
    from app.core.share_watcher import WatchReport

    long_reason = "错" * 300
    r = WatchReport(failed=1)
    r.failed_items = [{"dir": "/d", "name": "剧Z", "reason": long_reason}]

    telegram = _FakeTelegram()
    watcher = ShareWatcher(
        _NotifyContainer(None, None, None, telegram), _NotifySettings()
    )
    asyncio.run(watcher.notify_admin(r))

    text = telegram.bot.sent[0][1]
    assert "剧" + "Z" in text
    assert len(long_reason) > len(text)  # 原因未全量进入


def test_run_once_collects_failure_details(monkeypatch):
    """处理失败 → failed_items 记录目录/名称/原因（通知数据源）。"""
    _fast(monkeypatch)
    cache = _FakeCache([_dir(1, "/媒体", 100)])
    pan115 = _FakePan115({100: [{"fid": 999, "name": "剧C", "is_dir": True}]})
    proc = _FakeProcessor()
    watcher = ShareWatcher(_FakeContainer(pan115, cache, proc), _FakeSettings())

    report = asyncio.run(watcher.run_once())

    assert report.failed == 1
    assert report.failed_items == [
        {"dir": "/媒体", "name": "剧C", "reason": "创建分享失败：风控"}
    ]
    assert report.has_events


def test_run_once_quiet_round_has_no_events(monkeypatch):
    """静默轮（无成功/失败/审核）→ has_events=False，循环不发通知。"""
    _fast(monkeypatch)
    cache = _FakeCache([_dir(1, "/媒体", 100)])
    pan115 = _FakePan115({})  # 目录下无子目录
    proc = _FakeProcessor()
    watcher = ShareWatcher(_FakeContainer(pan115, cache, proc), _FakeSettings())

    report = asyncio.run(watcher.run_once())

    assert not report.has_events


def test_loop_notifies_when_enabled(monkeypatch):
    """循环轮：有事件 + 通知开启 → notify_admin 被调用；关闭 → 不调用。"""
    from app.core.share_watcher import WatchReport

    r = WatchReport(shared=1, items=[{"dir": "/d", "name": "x"}])
    calls: list = []

    real_sleep = asyncio.sleep  # 补丁前留存真实 sleep（补丁会替换全局模块属性）

    async def fast_sleep(sec):
        await real_sleep(0.001)  # 极短真实等待，避免忙转

    monkeypatch.setattr("app.core.share_watcher.asyncio.sleep", fast_sleep)

    class _LoopWatcher(ShareWatcher):
        async def run_once(self):
            return r

        async def notify_admin(self, report):
            calls.append(report)

    async def run(notify: bool):
        settings = _NotifySettings() if notify else _OffSettings()
        w = _LoopWatcher(_FakeContainer(None, None, None), settings)
        task = asyncio.create_task(w._loop())
        await real_sleep(0.05)  # 真实观察窗：等 _loop 跑过启动 sleep + run_once
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    class _OffSettings(_NotifySettings):
        share_watch_notify = False

    asyncio.run(run(True))
    assert calls  # 已通知

    calls.clear()
    asyncio.run(run(False))
    assert calls == []  # 未通知
