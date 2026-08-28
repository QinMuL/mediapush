"""Pan115Provider 集成测试：mock p115client.tool.share_iterdir_walk。

验证 (cid, dirs, files) 元组解析与字段归一化（最高风险集成点）。
预检（check_share_status）与 margin/快照渐进重试单独 mock 验证。
"""

import asyncio

import pytest
from p115client import tool

from app.providers.exceptions import Pan115Error
from app.providers.pan115 import Pan115Provider, ShareStatus


class _FakeAIter:
    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        self._it = iter(self._items)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


def _patch_precheck_ok(monkeypatch):
    """预检 mock：list_share 测试不联网（预检通过）。"""

    async def ok(self, code, password):
        return ShareStatus(state=1, title="T")

    monkeypatch.setattr(Pan115Provider, "check_share_status", ok)


def test_list_share_normalizes(monkeypatch):
    _patch_precheck_ok(monkeypatch)

    def fake_walk(client, code, receive_code, **kw):
        # 匿名读取：client 应为 None
        assert client is None, "list_share 应匿名读取（client=None）"
        assert code == "CODE"
        assert receive_code == "PWD"
        assert kw.get("app") == "web"
        assert kw.get("async_") is True
        return _FakeAIter([
            (1, [{"name": "Season 01", "is_dir": True, "size": 0}], []),
            (2, [], [
                {"name": "Show.S01E01.mkv", "is_dir": False, "size": 1000, "sha1": "abc"},
                {"name": "Show.S01E02.mkv", "is_dir": False, "size": 2000, "sha1": None},
            ]),
        ])

    monkeypatch.setattr(tool, "share_iterdir_walk", fake_walk)
    p = Pan115Provider("UID=12345;CID=67890;")

    async def run():
        files = await p.list_share("CODE", "PWD")
        assert len(files) == 3
        assert files[0].name == "Season 01"
        assert files[0].is_dir is True
        assert files[1].name == "Show.S01E01.mkv"
        assert files[1].is_dir is False
        assert files[1].sha1 == "abc"
        assert files[1].size == 1000
        assert files[2].size == 2000
        assert files[2].sha1 is None

    asyncio.run(run())


def test_list_share_without_cookie(monkeypatch):
    """无 cookie 也能匿名读取（核心：分享解析不依赖 cookie）。"""
    _patch_precheck_ok(monkeypatch)
    called = {}

    def fake_walk(client, code, receive_code, **kw):
        called["client"] = client
        called["code"] = code
        return _FakeAIter([
            (1, [], [{"name": "Movie.2020.mkv", "is_dir": False, "size": 5, "sha1": "x"}]),
        ])

    monkeypatch.setattr(tool, "share_iterdir_walk", fake_walk)
    p = Pan115Provider("")  # 无 cookie

    async def run():
        files = await p.list_share("CODE", None)
        assert called["client"] is None
        assert called["code"] == "CODE"
        assert len(files) == 1
        assert files[0].name == "Movie.2020.mkv"

    asyncio.run(run())


def test_check_health_no_cookie():
    """无 cookie 时健康检查返回 None（匿名可用）。"""
    p = Pan115Provider("")

    async def run():
        assert await p.check_health() is None

    asyncio.run(run())


def test_list_share_empty_raises(monkeypatch):
    _patch_precheck_ok(monkeypatch)
    monkeypatch.setattr(
        tool, "share_iterdir_walk", lambda *a, **k: _FakeAIter([])
    )
    p = Pan115Provider("UID=12345;CID=67890;")

    async def run():
        with pytest.raises(Pan115Error):
            await p.list_share("CODE", None)

    asyncio.run(run())


def test_list_share_precheck_fail_skips_walk(monkeypatch):
    """预检不可读（失效）时不走 iterdir，直接抛明确错误。"""
    walked = []

    def fake_walk(*a, **k):
        walked.append(1)
        return _FakeAIter([])

    monkeypatch.setattr(tool, "share_iterdir_walk", fake_walk)

    async def dead(self, code, password):
        return ShareStatus(state=7, message="分享已失效")

    monkeypatch.setattr(Pan115Provider, "check_share_status", dead)
    p = Pan115Provider("")

    async def run():
        with pytest.raises(Pan115Error, match="分享已失效"):
            await p.list_share("CODE", None)

    asyncio.run(run())
    assert walked == []


def test_list_share_margin_keyerror_translated(monkeypatch):
    """iterdir 途中被 margin 限速（KeyError 'data'）→ 转为明确的 Pan115Error。"""
    _patch_precheck_ok(monkeypatch)

    class _Boom:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise KeyError("data")

    monkeypatch.setattr(tool, "share_iterdir_walk", lambda *a, **k: _Boom())
    p = Pan115Provider("")

    async def run():
        with pytest.raises(Pan115Error, match="限速"):
            await p.list_share("CODE", None)

    asyncio.run(run())


def test_uid_from_cookie():
    from app.providers.pan115 import _uid_from_cookie

    assert _uid_from_cookie("UID=12345;CID=67890;") == 12345
    assert _uid_from_cookie("uid=999") == 999
    assert _uid_from_cookie("no uid here") is None


def test_no_cookie_constructs_without_raising():
    """无 cookie 不再报错（cookie 可选）。"""
    p = Pan115Provider("")
    assert p.cookie == ""
    assert p._uid is None
    assert p._build_client() is None


# -------------------- A2/A3/B2：margin / 快照 / 状态 -------------------- #
class _FakeSnapClient:
    """share_snap 桩：按序返回预设响应。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def share_snap(self, payload, **kw):
        self.calls += 1
        return self.responses.pop(0)


def _no_sleep(monkeypatch, into):
    async def fake_sleep(sec):
        into.append(sec)

    monkeypatch.setattr("app.providers.pan115.asyncio.sleep", fake_sleep)


def test_is_margin_response():
    from app.providers.pan115 import _is_margin_response

    assert _is_margin_response({"margin": 5})
    assert not _is_margin_response({"state": True, "data": {}, "margin": 5})
    assert not _is_margin_response({"margin": 5, "count": 3})
    assert not _is_margin_response({"state": True})


def test_margin_retry_then_success(monkeypatch):
    """margin → 等 min(margin, cap) 秒重试 → 成功。"""
    sleeps: list = []
    _no_sleep(monkeypatch, sleeps)
    p = Pan115Provider("")
    p._anon = _FakeSnapClient([
        {"margin": 5},
        {"margin": 90},  # 超 cap=30 → 等待封顶
        {"state": True, "data": {"share_state": 1, "shareinfo": {"share_title": "T"}}},
    ])

    async def run():
        status = await p.check_share_status("CODE", "PWD")
        assert status.readable
        assert status.state == 1
        assert status.title == "T"

    asyncio.run(run())
    assert sleeps == [5.0, 30.0]


def test_margin_exhausted_raises(monkeypatch):
    """持续 margin → 渐进重试耗尽 → Pan115Error（限速）。"""
    sleeps: list = []
    _no_sleep(monkeypatch, sleeps)
    p = Pan115Provider("")
    p._anon = _FakeSnapClient([{"margin": 2}] * 5)

    async def run():
        with pytest.raises(Pan115Error, match="限速"):
            await p.check_share_status("CODE", None)

    asyncio.run(run())
    assert sleeps == [2.0, 2.0, 2.0]  # 3 次重试


def test_snapshotting_progressive_backoff(monkeypatch):
    """快照生成中 → 3s/6s 渐进退避 → 第 3 次成功。"""
    sleeps: list = []
    _no_sleep(monkeypatch, sleeps)
    p = Pan115Provider("")
    snapshotting = {"state": True, "data": {"share_state": 0, "msg": "正在生成文件快照"}}
    p._anon = _FakeSnapClient([
        snapshotting,
        snapshotting,
        {"state": True, "data": {"share_state": 1, "shareinfo": {}}},
    ])

    async def run():
        status = await p.check_share_status("CODE", None)
        assert status.readable
        assert status.state == 1

    asyncio.run(run())
    assert sleeps == [3.0, 6.0]


def test_snapshotting_exhausted_raises(monkeypatch):
    """快照一直生成中 → 渐进重试耗尽 → Pan115Error。"""
    sleeps: list = []
    _no_sleep(monkeypatch, sleeps)
    p = Pan115Provider("")
    snapshotting = {"state": True, "data": {"share_state": 0, "msg": "正在生成文件快照"}}
    p._anon = _FakeSnapClient([snapshotting] * 5)

    async def run():
        with pytest.raises(Pan115Error, match="快照"):
            await p.check_share_status("CODE", None)

    asyncio.run(run())
    assert sleeps == [3.0, 6.0, 9.0]


def test_status_expired_by_share_state(monkeypatch):
    """share_state=7 → 已失效。"""
    p = Pan115Provider("")
    p._anon = _FakeSnapClient([
        {"state": True, "data": {"share_state": 7, "shareinfo": {}}},
    ])

    async def run():
        status = await p.check_share_status("CODE", None)
        assert not status.readable
        assert status.state == 7
        assert "失效" in status.message

    asyncio.run(run())


def test_status_auditing_and_violating(monkeypatch):
    """share_state=0 审核中；have_vio_file=1 违规。"""
    p = Pan115Provider("")
    p._anon = _FakeSnapClient([
        {"state": True, "data": {"share_state": 0, "shareinfo": {}}},
    ])

    async def run():
        st = await p.check_share_status("CODE", None)
        assert not st.readable and "审核" in st.message

    asyncio.run(run())

    p2 = Pan115Provider("")
    p2._anon = _FakeSnapClient([
        {"state": True, "data": {"share_state": 1, "shareinfo": {"have_vio_file": "1"}}},
    ])

    async def run2():
        st = await p2.check_share_status("CODE", None)
        assert st.violating and "违规" in st.message

    asyncio.run(run2())


def test_status_errno_cancelled(monkeypatch):
    """errno 4100009（已取消）→ 失效（check_response 抛 P115OSError）。"""
    p = Pan115Provider("")
    p._anon = _FakeSnapClient([
        {"state": False, "errno": 4100009, "error": "链接已失效"},
    ])

    async def run():
        status = await p.check_share_status("CODE", None)
        assert not status.readable
        assert status.state == 7
        assert "失效" in status.message

    asyncio.run(run())


# -------------------- 访问码语义（errno 实测：4100012/4100008 分享存在） -------------------- #
def test_status_need_code_not_dead(monkeypatch):
    """errno 4100012（请输入访问码）→ 分享活着，need_code=True 不算待定/失效。"""
    p = Pan115Provider("")
    p._anon = _FakeSnapClient([
        {"state": False, "errno": 4100012, "error": "请输入访问码",
         "data": {"is_access": 0}},
    ])

    async def run():
        st = await p.check_share_status("CODE", None)
        assert st.need_code is True
        assert st.code_changed is False
        assert st.readable  # 活着

    asyncio.run(run())


def test_status_code_changed_not_dead(monkeypatch):
    """errno 4100008（访问码错误）→ 分享活着但码被改，code_changed=True。"""
    p = Pan115Provider("")
    p._anon = _FakeSnapClient([
        {"state": False, "errNo": 4100008, "error": "访问码错误",
         "data": {"userinfo": {"user_id": "1"}}},
    ])

    async def run():
        st = await p.check_share_status("CODE", "oldpwd")
        assert st.need_code is True
        assert st.code_changed is True
        assert st.readable  # 资源还在，不撤卡

    asyncio.run(run())
