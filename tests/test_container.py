"""Container 测试：空配置启动、reset_pan115、rebuild_tmdb、on_config_changed 分发。

用文件 sqlite + 注入 session_factory，避免命中全局 DB；mock TelegramService.start 避免联网。
"""
import tempfile
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.container import Container
from app.db import repository
from app.db.base import Base


@pytest.fixture
async def factory():
    db_path = tempfile.mktemp(suffix=".db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield f
    await engine.dispose()


# ---- 空配置启动：服务为 None，不阻塞 ----
async def test_create_with_empty_config(factory):
    c = await Container.create(session_factory=factory)
    assert c.pan115 is None
    assert c.tmdb is None
    assert c.telegram is None
    assert c.pusher is None
    assert c.pipeline is not None  # pipeline 始终构造
    assert c.pan115_ready() is False
    assert c.is_pipeline_running() is False


# ---- 配置 cookie 后 pan115 构建 ----
async def test_create_with_cookie(factory):
    async with factory() as s:
        await repository.set_config(s, "pan115_cookie", "UID=1; CID=2;")
    c = await Container.create(session_factory=factory)
    assert c.pan115 is not None
    assert c.pan115_ready() is True
    assert c._pan115_client.cookie == "UID=1; CID=2;"
    await c.close()


# ---- reset_pan115：换 cookie 重建 ----
async def test_reset_pan115_updates_cookie(factory):
    async with factory() as s:
        await repository.set_config(s, "pan115_cookie", "UID=old; CID=2;")
    c = await Container.create(session_factory=factory)
    assert c._pan115_client.cookie == "UID=old; CID=2;"
    async with factory() as s:
        await repository.set_config(s, "pan115_cookie", "UID=new; CID=2;")
    await c.reset_pan115()
    assert c._pan115_client.cookie == "UID=new; CID=2;"
    await c.close()


# ---- reset_pan115：清空 cookie ----
async def test_reset_pan115_clears(factory):
    async with factory() as s:
        await repository.set_config(s, "pan115_cookie", "UID=old; CID=2;")
    c = await Container.create(session_factory=factory)
    await c.reset_pan115(cookie="")
    assert c.pan115 is None
    assert c.pan115_ready() is False
    await c.close()


# ---- reset_pan115：从无到有 ----
async def test_reset_pan115_creates(factory):
    c = await Container.create(session_factory=factory)
    assert c.pan115 is None
    await c.reset_pan115(cookie="UID=fresh; CID=2;")
    assert c.pan115 is not None
    assert c._pan115_client.cookie == "UID=fresh; CID=2;"
    await c.close()


# ---- rebuild_tmdb ----
async def test_rebuild_tmdb(factory):
    c = await Container.create(session_factory=factory)
    assert c.tmdb is None
    async with factory() as s:
        await repository.set_config(s, "tmdb_api_key", "key123")
    await c.rebuild_tmdb()
    assert c.tmdb is not None
    await c.close()


# ---- rebuild_telegram：mock start 避免联网 ----
async def test_rebuild_telegram_constructs_service(factory, monkeypatch):
    async with factory() as s:
        await repository.set_config(s, "tg_bot_token", "1:fake")
        await repository.set_config(s, "tg_chat_id", "chat1")
    c = await Container.create(session_factory=factory)
    # mock start 不联网
    monkeypatch.setattr(c._telegram, "start", AsyncMock(return_value=None))
    # token 已在 create 时读到，telegram 应已构造
    assert c.telegram is not None
    assert c.pusher is not None
    # pipeline 已注入 pusher
    assert c.pipeline._pusher is c.pusher
    await c.close()


async def test_rebuild_telegram_clears_when_no_token(factory, monkeypatch):
    async with factory() as s:
        await repository.set_config(s, "tg_bot_token", "1:fake")
    c = await Container.create(session_factory=factory)
    assert c.telegram is not None
    async with factory() as s:
        await repository.set_config(s, "tg_bot_token", "")
    monkeypatch.setattr(
        "app.core.container.TelegramService.start", AsyncMock(return_value=True)
    )
    ok = await c.rebuild_telegram()
    assert ok is False
    assert c.telegram is None
    assert c.pusher is None
    assert c.pipeline._pusher is None
    await c.close()


# ---- on_config_changed 分发 ----
async def test_on_config_changed_pan115(factory, monkeypatch):
    c = await Container.create(session_factory=factory)
    called = {}

    async def fake_reset(*a, **k):
        called["pan115"] = True

    monkeypatch.setattr(c, "reset_pan115", AsyncMock(side_effect=fake_reset))
    await c.on_config_changed("pan115_cookie")
    assert called.get("pan115") is True


async def test_on_config_changed_proxy_rebuilds_both(factory, monkeypatch):
    c = await Container.create(session_factory=factory)
    monkeypatch.setattr(c, "rebuild_tmdb", AsyncMock(return_value=None))
    monkeypatch.setattr(c, "rebuild_telegram", AsyncMock(return_value=False))
    await c.on_config_changed("proxy_url")
    c.rebuild_tmdb.assert_awaited()
    c.rebuild_telegram.assert_awaited()


# ---- get_status ----
async def test_get_status(factory):
    c = await Container.create(session_factory=factory)
    status = await c.get_status()
    assert status["bot_running"] is False
    assert status["pipeline_running"] is False
    assert status["unpushed"] == 0
    assert status["config_health"]["tg_token"] is False
    assert status["config_health"]["schedule_interval"] == "5"
    await c.close()


# ---- run_pipeline_once: 无 pipeline 时跳过 ----
async def test_run_pipeline_once_no_pipeline(factory):
    c = await Container.create(session_factory=factory)
    c._pipeline = None
    res = await c.run_pipeline_once([("code", "")])
    assert res["skipped"] is True
    await c.close()
