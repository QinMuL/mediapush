"""Cache TMDB 缓存 TTL 过期清除测试。"""

import asyncio

from app.db.cache import Cache


def test_tmdb_cache_hit_within_ttl(tmp_path):
    """TTL 内命中。"""
    cache = Cache(str(tmp_path / "t.db"))

    async def run():
        await cache.set_tmdb(1, "tv", {"title": "X"}, ttl_days=1)
        assert await cache.get_tmdb(1, "tv") == {"title": "X"}

    asyncio.run(run())


def test_tmdb_cache_expires_and_deletes(tmp_path):
    """过期行被物理删除（自动清除，非惰性残留）。"""
    cache = Cache(str(tmp_path / "t.db"))

    async def run():
        # ttl_days=0 → 写入即过期
        await cache.set_tmdb(1, "tv", {"title": "X"}, ttl_days=0)
        # 过期读取 → None
        assert await cache.get_tmdb(1, "tv") is None
        # 验证行已物理删除（直接查表）
        row = await cache._fetchone(
            "SELECT 1 FROM tmdb_cache WHERE tmdb_id=? AND media_type=?",
            (1, "tv"),
        )
        assert row is None

    asyncio.run(run())


def test_tmdb_cache_upsert_refreshes_payload(tmp_path):
    """upsert 覆盖时刷新 payload（前序 bug 回归）。"""
    cache = Cache(str(tmp_path / "t.db"))

    async def run():
        await cache.set_tmdb(1, "tv", {"v": 1}, ttl_days=1)
        await cache.set_tmdb(1, "tv", {"v": 2}, ttl_days=1)
        assert await cache.get_tmdb(1, "tv") == {"v": 2}

    asyncio.run(run())
