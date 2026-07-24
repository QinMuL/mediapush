"""tmdb 单测：mock client，覆盖年份回退、缓存命中/过期、季集数补充。"""
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models import _now
from app.tmdb.service import TmdbService


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


# ---- 搜索年份回退 ----
async def test_search_year_fallback(session):
    client = AsyncMock()
    # 带年份返回空，不带年份返回结果
    client.search_tv.side_effect = lambda q, year=None: (
        {"results": []} if year else {"results": [{"id": 1, "name": "Show"}]}
    )
    svc = TmdbService(client)
    results = await svc.search("Show", "tv", year=2020)
    assert len(results) == 1
    assert results[0]["id"] == 1
    # 调用两次：带年份 + 回退
    assert client.search_tv.await_count == 2


async def test_search_year_hit_no_fallback(session):
    client = AsyncMock()
    client.search_tv.return_value = {"results": [{"id": 1}]}
    svc = TmdbService(client)
    await svc.search("Show", "tv", year=2020)
    # 带年份有结果则不回退
    assert client.search_tv.await_count == 1


# ---- 详情缓存 ----
async def test_get_details_cache_hit(session):
    client = AsyncMock()
    client.get_tv_details.return_value = {"id": 100, "status": "Ended"}
    svc = TmdbService(client)
    d1 = await svc.get_details(session, 100, "tv")
    d2 = await svc.get_details(session, 100, "tv")
    assert d1 == d2
    # 第二次命中缓存，client 只调用一次
    assert client.get_tv_details.await_count == 1


async def test_get_details_cache_expired_refetch(session):
    client = AsyncMock()
    client.get_tv_details.return_value = {"id": 100, "status": "Ended"}
    svc = TmdbService(client)
    await svc.get_details(session, 100, "tv")
    # 手动把缓存置为过期
    from app.db import repository

    cached = await repository.get_tmdb_cache(session, 100, "tv")
    cached.expires_at = _now() - timedelta(days=1)
    await session.commit()
    # 再次取应重拉
    await svc.get_details(session, 100, "tv")
    assert client.get_tv_details.await_count == 2


# ---- 整季集数补充（跳过 season 0）----
async def test_fill_episodes_season0_skips_specials(session):
    client = AsyncMock()
    # tv 详情含 season 0（特别篇）和 season 1（正剧）
    client.get_tv_details.return_value = {
        "id": 200,
        "status": "Ended",
        "seasons": [
            {"season_number": 0, "episode_count": 5},  # 特别篇，跳过
            {"season_number": 1, "episode_count": 12},  # 正剧，取这个
        ],
    }
    svc = TmdbService(client)
    count = await svc.fill_episodes_from_season(session, 200, 0)
    assert count == 12


async def test_fill_episodes_specific_season(session):
    client = AsyncMock()
    client.get_tv_season.return_value = {"id": 1, "episode_count": 10}
    svc = TmdbService(client)
    count = await svc.fill_episodes_from_season(session, 200, 2)
    assert count == 10
    client.get_tv_season.assert_awaited_once_with(200, 2)


# ---- total_episodes 优先 TMDB ----
async def test_total_episodes_tmdb_preferred(session):
    client = AsyncMock()
    client.get_tv_details.return_value = {
        "id": 300, "status": "Returning Series", "number_of_episodes": 28,
    }
    svc = TmdbService(client)
    total = await svc.get_total_episodes(session, 300, fallback=16)
    assert total == 28  # TMDB 优先


async def test_total_episodes_fallback_to_filename(session):
    client = AsyncMock()
    client.get_tv_details.return_value = {"id": 300, "status": "Ended"}  # 无 number_of_episodes
    svc = TmdbService(client)
    total = await svc.get_total_episodes(session, 300, fallback=16)
    assert total == 16  # 回退文件名推断


# ---- ongoing 判断（决定缓存 TTL）----
async def test_is_ongoing_returning_series(session):
    client = AsyncMock()
    client.get_tv_details.return_value = {"id": 1, "status": "Returning Series"}
    svc = TmdbService(client)
    await svc.get_details(session, 1, "tv")
    from app.db import repository

    cached = await repository.get_tmdb_cache(session, 1, "tv")
    assert cached.ongoing is True
    # 连载中 3 天
    assert (cached.expires_at - cached.cached_at).days >= 3


async def test_refresh_deletes_cache(session):
    client = AsyncMock()
    client.get_tv_details.return_value = {"id": 1, "status": "Ended"}
    svc = TmdbService(client)
    await svc.get_details(session, 1, "tv")
    count = await svc.refresh(session, 1)
    assert count == 1
    from app.db import repository

    assert await repository.get_tmdb_cache(session, 1, "tv") is None
