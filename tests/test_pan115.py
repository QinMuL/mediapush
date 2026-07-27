"""Pan115Provider 集成测试：mock p115client.tool.share_iterdir_walk。

验证 (cid, dirs, files) 元组解析与字段归一化（最高风险集成点）。
"""

import asyncio

import pytest
from p115client import tool

from app.providers.exceptions import Pan115Error
from app.providers.pan115 import Pan115Provider


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


def test_list_share_normalizes(monkeypatch):
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
    monkeypatch.setattr(
        tool, "share_iterdir_walk", lambda *a, **k: _FakeAIter([])
    )
    p = Pan115Provider("UID=12345;CID=67890;")

    async def run():
        with pytest.raises(Pan115Error):
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
