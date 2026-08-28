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


# -------------------- 目录监控：list_dir / resolve_path / create_share -------------------- #
class _FakeLoginClient:
    """登录态 client 桩（fs_files / share_send / share_update）。"""

    def __init__(self, pages=None, share_resp=None):
        self.pages = list(pages or [])   # fs_files 逐页响应
        self.share_resp = share_resp
        self.calls: list = []

    async def fs_files(self, payload, **kw):
        self.calls.append(("fs_files", payload))
        return self.pages.pop(0)

    async def share_send(self, payload, **kw):
        self.calls.append(("share_send", payload))
        return self.share_resp

    async def share_update(self, payload, **kw):
        self.calls.append(("share_update", payload))
        return {"state": True}


def _login_p(monkeypatch, fake_client):
    """构造带登录 cookie 的 provider，_build_client 替换为桩。"""
    p = Pan115Provider("UID=12345;CID=67890;")
    monkeypatch.setattr(Pan115Provider, "_build_client", lambda self: fake_client)
    return p


def test_list_dir_only_dirs_with_pagination(monkeypatch):
    """list_dir：nf=1 仅目录；目录 id 取 cid 键（无 fid）；自动翻页。"""
    # 实测 webapi 老格式：data 为列表，count 在顶层
    client = _FakeLoginClient(pages=[
        {"state": True, "data": [
            {"cid": 11, "pid": 0, "n": "剧A"},
            {"cid": 12, "pid": 0, "n": "剧B"},
        ], "count": 3},
        {"state": True, "data": [
            {"cid": 13, "pid": 0, "n": "剧C"},
        ], "count": 3},
    ])
    p = _login_p(monkeypatch, client)

    async def run():
        items = await p.list_dir(100)
        assert [i["name"] for i in items] == ["剧A", "剧B", "剧C"]
        assert all(i["is_dir"] for i in items)
        assert items[0]["fid"] == 11
        # 请求带 nf=1（仅目录）
        payloads = [c[1] for c in client.calls]
        assert all(pl.get("nf") == 1 for pl in payloads)
        assert payloads[0]["cid"] == 100

    asyncio.run(run())


def test_resolve_path_drills_down(monkeypatch):
    """resolve_path：逐级下钻 /媒体/新剧 → cid；大小写不敏感。"""
    client = _FakeLoginClient(pages=[
        {"state": True, "data": [{"cid": 50, "pid": 0, "n": "Media"}], "count": 1},
        {"state": True, "data": [{"cid": 51, "pid": 50, "n": "新剧"}], "count": 1},
    ])
    p = _login_p(monkeypatch, client)

    async def run():
        cid = await p.resolve_path("/media/新剧")
        assert cid == 51

    asyncio.run(run())


def test_resolve_path_not_found(monkeypatch):
    client = _FakeLoginClient(pages=[
        {"state": True, "data": [], "count": 0},
    ])
    p = _login_p(monkeypatch, client)

    async def run():
        with pytest.raises(Pan115Error, match="不存在"):
            await p.resolve_path("/没有的目录")

    asyncio.run(run())


def test_resolve_path_empty_returns_root(monkeypatch):
    """/ → cid 0（根目录本身可作监控目录）。"""
    p = Pan115Provider("UID=1;CID=2;")
    assert asyncio.run(p.resolve_path("/")) == 0 or True  # 根路径无段不调接口
    # 空 parts → 不请求任何页，直接返回 0
    client = _FakeLoginClient()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(Pan115Provider, "_build_client", lambda self: client)
    try:
        async def run():
            assert await p.resolve_path("/") == 0
        asyncio.run(run())
    finally:
        monkeypatch.undo()


def test_create_share_permanent(monkeypatch):
    """create_share：share_send 建分享 + share_update duration=-1 永久。"""
    client = _FakeLoginClient(share_resp={
        "state": True, "data": {"share_code": "swNEW123", "receive_code": "ab12"},
    })
    p = _login_p(monkeypatch, client)

    async def run():
        code, pwd = await p.create_share(12345)
        assert code == "swNEW123"
        assert pwd == "ab12"
        # share_send 带 file_ids + ignore_warn
        send = next(c for c in client.calls if c[0] == "share_send")[1]
        assert send["file_ids"] == "12345"
        # share_update 设永久 -1
        upd = next(c for c in client.calls if c[0] == "share_update")[1]
        assert upd == {"share_code": "swNEW123", "share_duration": -1}

    asyncio.run(run())


def test_create_share_margin_retries(monkeypatch):
    """share_send 遇 margin 限速 → 等待后重试成功。"""
    sleeps: list = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr("app.providers.pan115.asyncio.sleep", fake_sleep)
    client = _FakeLoginClient(share_resp=None)
    # share_send 依次返回：margin → 成功
    seq = [
        {"margin": 7},
        {"state": True, "data": {"share_code": "swM1", "receive_code": "x1"}},
    ]

    async def share_send(payload, **kw):
        return seq.pop(0)

    client.share_send = share_send
    p = _login_p(monkeypatch, client)

    async def run():
        code, _pwd = await p.create_share(777)
        assert code == "swM1"
        assert sleeps == [7.0]

    asyncio.run(run())


def test_create_share_no_cookie_raises():
    """无 cookie → Pan115Error（创建分享需登录态）。"""
    p = Pan115Provider("")

    async def run():
        with pytest.raises(Pan115Error, match="cookie"):
            await p.create_share(1)

    asyncio.run(run())


def test_list_dir_no_cookie_raises():
    p = Pan115Provider("")

    async def run():
        with pytest.raises(Pan115Error, match="cookie"):
            await p.list_dir(0)

    asyncio.run(run())


# -------------------- 归档移动：fs_move 990009 忙重试 -------------------- #
_BUSY = {
    "state": False,
    "error": "移动[X]操作尚未执行完成，请稍后再试！",
    "errno": 990009,
    "errtype": "war",
}


def test_fs_move_busy_retries(monkeypatch):
    """errno 990009（上一次移动仍在服务端执行）→ 渐进等待后重试成功。"""
    sleeps: list = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr("app.providers.pan115.asyncio.sleep", fake_sleep)
    client = _FakeLoginClient()
    seq = [_BUSY, {"state": True}]
    calls: list = []

    async def fs_move(fid, pid=0, **kw):
        calls.append((fid, pid))
        return seq.pop(0)

    client.fs_move = fs_move
    p = _login_p(monkeypatch, client)

    async def run():
        await p.fs_move(777, 999)
        assert sleeps == [3.0]  # 首次被拒 → 等 3s → 重试成功
        assert calls == [(777, 999)] * 2

    asyncio.run(run())


def test_fs_move_busy_exhausted_raises(monkeypatch):
    """990009 三次仍忙 → Pan115Error（调用方下轮补移兜底）。"""
    sleeps: list = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr("app.providers.pan115.asyncio.sleep", fake_sleep)
    client = _FakeLoginClient()

    async def fs_move(fid, pid=0, **kw):
        return _BUSY

    client.fs_move = fs_move
    p = _login_p(monkeypatch, client)

    async def run():
        with pytest.raises(Pan115Error, match="尚未执行完成"):
            await p.fs_move(777, 999)

    asyncio.run(run())
    assert sleeps == [3.0, 6.0]  # 渐进 3s/6s 后耗尽


def test_fs_move_other_error_no_retry(monkeypatch):
    """非 990009 错误（如目标目录无效）→ 立即抛，不等待不重试。"""
    sleeps: list = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr("app.providers.pan115.asyncio.sleep", fake_sleep)
    client = _FakeLoginClient()

    async def fs_move(fid, pid=0, **kw):
        return {"state": False, "error": "参数错误", "errno": 990002}

    client.fs_move = fs_move
    p = _login_p(monkeypatch, client)

    async def run():
        with pytest.raises(Pan115Error, match="移动失败"):
            await p.fs_move(777, 999)

    asyncio.run(run())
    assert sleeps == []  # 不等待
