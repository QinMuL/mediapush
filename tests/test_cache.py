import asyncio
import time

import aiosqlite
import pytest

from app.db.cache import Cache


@pytest.fixture
def cache(tmp_path):
    return Cache(str(tmp_path / "test.db"))


def test_tmdb_cache_set_get(cache):
    async def run():
        await cache.set_tmdb(123, "tv", {"title": "X"}, ttl_days=30)
        got = await cache.get_tmdb(123, "tv")
        assert got == {"title": "X"}

    asyncio.run(run())


def test_tmdb_cache_miss(cache):
    async def run():
        assert await cache.get_tmdb(999, "tv") is None

    asyncio.run(run())


def test_tmdb_cache_expired(cache):
    async def run():
        await cache.set_tmdb(1, "movie", {"a": 1}, ttl_days=3)
        async with aiosqlite.connect(cache.db_path) as db:
            await db.execute(
                "UPDATE tmdb_cache SET fetched_at=? WHERE tmdb_id=1 AND media_type='movie'",
                (time.time() - 4 * 86400,),
            )
            await db.commit()
        assert await cache.get_tmdb(1, "movie") is None

    asyncio.run(run())


def test_tmdb_cache_upsert_refreshes(cache):
    async def run():
        await cache.set_tmdb(1, "tv", {"v": 1}, ttl_days=3)
        await cache.set_tmdb(1, "tv", {"v": 2}, ttl_days=3)
        got = await cache.get_tmdb(1, "tv")
        assert got == {"v": 2}

    asyncio.run(run())


def test_delete_tmdb(cache):
    async def run():
        await cache.set_tmdb(7, "tv", {"x": 1}, ttl_days=3)
        n = await cache.delete_tmdb(7)
        assert n == 1
        assert await cache.get_tmdb(7, "tv") is None

    asyncio.run(run())


def test_pushed_dedup(cache):
    async def run():
        assert await cache.is_pushed("code1") is False
        await cache.mark_pushed("code1")
        assert await cache.is_pushed("code1") is True
        await cache.mark_pushed("code1")  # 幂等
        assert await cache.is_pushed("code1") is True

    asyncio.run(run())
