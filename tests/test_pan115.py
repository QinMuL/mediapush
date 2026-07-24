"""pan115 单测：mock client，验证缓存键含 password、405 永久错误、reset 重建。"""
from unittest.mock import AsyncMock

import pytest

from app.pan115.client import Pan115Client, Pan115PermanentError
from app.pan115.service import Pan115Service


# ---- Service 缓存键含 password ----
async def test_share_info_cache_hit_same_password():
    client = AsyncMock()
    client.share_info.return_value = {"data": {"share_title": "X"}}
    svc = Pan115Service(client)
    await svc.get_share_info("code1", "pwd1")
    await svc.get_share_info("code1", "pwd1")  # 命中缓存
    assert client.share_info.await_count == 1


async def test_share_info_cache_miss_diff_password():
    """同 share_code 不同访问码不应命中错误缓存（旧项目约束）。"""
    client = AsyncMock()
    client.share_info.return_value = {"data": {}}
    svc = Pan115Service(client)
    await svc.get_share_info("code1", "pwd1")
    await svc.get_share_info("code1", "pwd2")  # 不同 password 重新拉取
    assert client.share_info.await_count == 2
    # 两次调用都带了对应 receive_code
    client.share_info.assert_any_await("code1", "pwd1")
    client.share_info.assert_any_await("code1", "pwd2")


# ---- iter_share_files 透传 ----
async def test_iter_share_files_yields():
    async def fake_iter(code, pwd):
        for n in ("a.mkv", "b.mkv"):
            yield {"name": n}

    client = AsyncMock()
    client.iter_share_files = fake_iter
    svc = Pan115Service(client)
    files = [f async for f in svc.iter_share_files("c", "p")]
    assert [f["name"] for f in files] == ["a.mkv", "b.mkv"]


# ---- 405 永久错误 ----
class _FakeResp:
    def __init__(self, status):
        self.status_code = status


class _FakeHTTPError(Exception):
    def __init__(self, status):
        super().__init__("err")
        self.response = _FakeResp(status)


async def test_share_info_405_raises_permanent():
    c = Pan115Client()  # _client=None
    inner = AsyncMock()
    inner.share_info.side_effect = _FakeHTTPError(405)
    c._client = inner  # 注入 mock 底层 client
    with pytest.raises(Pan115PermanentError):
        await c.share_info("code", "pwd")


async def test_share_info_500_not_permanent():
    c = Pan115Client()
    inner = AsyncMock()
    inner.share_info.side_effect = _FakeHTTPError(500)
    c._client = inner
    with pytest.raises(_FakeHTTPError):
        await c.share_info("code", "pwd")


# ---- is_permanent_error 判定 ----
def test_is_permanent_error():
    assert Pan115Service.is_permanent_error(Pan115PermanentError("x")) is True
    assert Pan115Service.is_permanent_error(Exception("x")) is False


# ---- reset 重建 ----
async def test_reset_rebuilds_client(monkeypatch):
    """reset 后用新 cookie 重建 client（维持 webapi 端点）。"""
    built = {}

    def fake_build(self, cookie):
        built["cookie"] = cookie
        return AsyncMock()

    monkeypatch.setattr(Pan115Client, "_build", fake_build)
    c = Pan115Client("old_cookie")
    assert c.cookie == "old_cookie"
    await c.reset("new_cookie")
    assert c.cookie == "new_cookie"
    assert built["cookie"] == "new_cookie"
