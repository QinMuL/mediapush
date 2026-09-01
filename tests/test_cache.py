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


# -------------------- 目录监控：share_dirs / shared_items -------------------- #
def test_share_dirs_lifecycle(tmp_path):
    """add（重复刷新 cid）/ list（含已推送计数）/ del（连同记录）。"""
    import asyncio

    from app.db.cache import Cache

    cache = Cache(str(tmp_path / "t.db"))

    async def run():
        await cache.add_share_dir("/媒体", 100)
        await cache.add_share_dir("/媒体", 101)  # 重复 → 更新 cid（AUTOINCREMENT 序列仍自增）
        await cache.add_share_dir("/电影", 200)

        # id 从查询取（ON CONFLICT 下自增序列会跳号，不能硬编码）
        dirs = await cache.list_share_dirs()
        by_path = {d["path"]: d for d in dirs}
        media_id = by_path["/媒体"]["id"]
        movie_id = by_path["/电影"]["id"]

        # 两阶段：record_share(pending) → mark_shared(ok)
        await cache.record_share(media_id, 11, "剧A", "swA1", "pw1")
        await cache.record_share(media_id, 12, "剧B", "swB1")
        await cache.record_share(movie_id, 21, "影C", "swC1")

        # pending 阶段可查（含码/密码），计入 shared
        rec = await cache.get_shared_item(media_id, 11)
        assert rec["status"] == "pending"
        assert rec["share_code"] == "swA1" and rec["password"] == "pw1"
        assert await cache.get_shared_item(media_id, 99) is None

        await cache.mark_shared(media_id, 11)  # → ok
        rec = await cache.get_shared_item(media_id, 11)
        assert rec["status"] == "ok"

        dirs = await cache.list_share_dirs()
        by_path = {d["path"]: d for d in dirs}
        assert by_path["/媒体"]["cid"] == 101  # 刷新生效
        assert by_path["/媒体"]["shared"] == 2
        assert by_path["/电影"]["shared"] == 1

        # del：连同 shared_items
        removed = await cache.remove_share_dir("/媒体")
        assert removed == 1
        assert await cache.get_shared_item(media_id, 11) is None  # 记录已删
        assert await cache.remove_share_dir("/不存在") == 0
        await cache.close()

    asyncio.run(run())


# -------------------- 运行统计：stats（/status 展示） -------------------- #
def test_stats_counts(tmp_path):
    """stats()：按状态区分 pushed/dead，只统计 status='ok' 的 shared_items。"""
    import asyncio

    from app.db.cache import Cache

    cache = Cache(str(tmp_path / "t.db"))

    async def run():
        await cache.mark_pushed("abc12345", provider="115")
        await cache.mark_pushed("def67890", provider="115")
        await cache.mark_pushed("dead0001", provider="115")
        await cache.mark_dead("dead0001")
        await cache.set_tmdb(42, "tv", {"name": "剧"}, ttl_days=1)  # TMDB 缓存 1 条

        await cache.add_share_dir("/媒体", 100)
        dirs = await cache.list_share_dirs()
        dir_id = dirs[0]["id"]
        await cache.record_share(dir_id, 11, "剧A", "swA1")
        await cache.mark_shared(dir_id, 11)  # ok
        await cache.record_share(dir_id, 12, "剧B", "swB1")  # 仍 pending

        st = await cache.stats()
        assert st["pushed"] == 2  # dead0001 已失效不计入
        assert st["dead"] == 1
        assert st["tmdb_cache"] == 1
        assert st["share_dirs"] == 1
        assert st["shared_items"] == 1  # 只算 ok，pending 不计
        await cache.close()

    asyncio.run(run())


# -------------------- /reset 一键清空 -------------------- #
def test_clear_all_clears_data_keeps_share_dirs(tmp_path):
    """clear_all：业务数据表清空，share_dirs（/dir add 用户配置）保留。"""
    import asyncio

    from app.db.cache import Cache

    cache = Cache(str(tmp_path / "t.db"))

    async def run():
        await cache.set_tmdb(42, "tv", {"name": "剧"}, ttl_days=1)
        await cache.mark_pushed("abc12345", provider="115")
        await cache.add_share_dir("/媒体", 100)
        dir_id = (await cache.list_share_dirs())[0]["id"]
        await cache.record_share(dir_id, 11, "剧A", "swA1")

        counts = await cache.clear_all()
        assert counts["tmdb_cache"] == 1
        assert counts["pushed_shares"] == 1
        assert counts["shared_items"] == 1

        # 数据表全空，监控目录配置仍在
        st = await cache.stats()
        assert st["pushed"] == 0 and st["tmdb_cache"] == 0
        assert st["shared_items"] == 0
        assert st["share_dirs"] == 1
        assert await cache.get_shared_item(dir_id, 11) is None
        await cache.close()

    asyncio.run(run())
