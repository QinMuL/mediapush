"""ShareWatcher 目录监控测试：扫描→建分享→推卡片→标记 / 失败重试。"""

import asyncio

from app.core.share_watcher import ShareWatcher
from app.providers.exceptions import Pan115Error


class _FakeCache:
    """两阶段桩：record_share 登记 pending → mark_shared 置 ok。"""

    def __init__(self, dirs):
        self.dirs = dirs
        self.records: dict[tuple[int, int], dict] = {}
        self.recorded: list[tuple] = []

    async def list_share_dirs(self):
        return self.dirs

    async def get_shared_item(self, dir_id, file_id):
        return self.records.get((dir_id, file_id))

    async def record_share(self, dir_id, file_id, name, share_code, password=""):
        self.records[(dir_id, file_id)] = {
            "file_id": file_id, "dir_id": dir_id, "name": name,
            "share_code": share_code, "password": password, "status": "pending",
        }
        self.recorded.append((dir_id, file_id, share_code, password))

    async def mark_shared(self, dir_id, file_id):
        rec = self.records.get((dir_id, file_id))
        if rec:
            rec["status"] = "ok"


class _FakePan115:
    def __init__(self, dirs_map, cookie="UID=1;CID=2;"):
        self.dirs_map = dirs_map  # cid -> [items]
        self.cookie = cookie
        self.share_calls: list = []

    async def list_dir(self, cid):
        return self.dirs_map.get(cid, [])

    async def create_share(self, fid):
        self.share_calls.append(fid)
        if fid == 999:  # 特殊 fid：模拟建分享失败
            raise Pan115Error("创建分享失败：风控")
        return f"code{fid}", f"pwd{fid}"


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


class _FakeSettings:
    share_watch_interval_minutes = 10.0


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


def test_run_once_share_failure_not_marked(monkeypatch):
    _fast(monkeypatch)
    """建分享失败（fid=999 模拟风控）→ 不标记 → 下轮重扫重试。"""
    cache = _FakeCache([_dir(1, "/媒体", 100)])
    pan115 = _FakePan115({100: [
        {"fid": 999, "name": "剧C", "is_dir": True, "size": 0},
    ]})
    proc = _FakeProcessor()
    watcher = ShareWatcher(_FakeContainer(pan115, cache, proc), _FakeSettings())

    report = asyncio.run(watcher.run_once())

    assert report.failed == 1
    assert report.shared == 0
    assert cache.recorded == []  # 建分享失败：无登记
    assert proc.process_calls == []  # 未推送


def test_run_once_push_failure_not_marked(monkeypatch):
    _fast(monkeypatch)
    """推送失败（process 返回 ok=False）→ 不标记 → 下轮重试。"""
    cache = _FakeCache([_dir(1, "/媒体", 100)])
    pan115 = _FakePan115({100: [
        {"fid": 21, "name": "剧D", "is_dir": True, "size": 0},
    ]})
    proc = _FakeProcessor(fail_codes={"code21"})
    watcher = ShareWatcher(_FakeContainer(pan115, cache, proc), _FakeSettings())

    report = asyncio.run(watcher.run_once())

    assert report.failed == 1
    assert pan115.share_calls == [21]  # 分享已建
    # 关键：已登记 pending（码留存），下轮复用此码重推，绝不再建新分享
    assert cache.recorded == [(1, 21, "code21", "pwd21")]
    assert cache.records[(1, 21)]["status"] == "pending"
    assert report.shared == 0


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
