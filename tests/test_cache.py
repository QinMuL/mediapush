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


# -------------------- 巡检候选：ed2k 排除（旧数据 provider 误存） -------------------- #
def test_list_pushed_shares_excludes_ed2k(tmp_path):
    """旧数据 provider 迁移默认 '115' 但 code 是 ed2k:// → 巡检候选应排除。"""
    import asyncio

    from app.db.cache import Cache

    cache = Cache(str(tmp_path / "t.db"))

    async def run():
        await cache.mark_pushed("abc12345", provider="115")
        await cache.mark_pushed(
            "ed2k://|file|x.mkv|100|" + "A" * 32 + "|/", provider="ed2k"
        )
        # 旧数据：mark_pushed 时未传 provider → 默认 '115'，但 code 是 ed2k URL
        await cache._execute(
            "INSERT OR REPLACE INTO pushed_shares (share_code, pushed_at, provider) "
            "VALUES (?,?,?)",
            ("ed2k://|file|old.mkv|100|" + "B" * 32 + "|/", 1.0, "115"),
        )
        rows = await cache.list_pushed_shares(provider="115")
        codes = [r["share_code"] for r in rows]
        assert "abc12345" in codes
        assert all(not c.startswith("ed2k://") for c in codes)
        await cache.close()

    asyncio.run(run())
