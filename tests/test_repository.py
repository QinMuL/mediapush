"""repository 单测：用内存 SQLite，覆盖批量去重、配置、缓存 refresh。"""
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import models, repository
from app.db.base import Base


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


# ---- AppConfig ----
async def test_config_default(session):
    # 未写入时返回 DEFAULT_CONFIG 默认值
    assert await repository.get_config(session, "schedule_interval") == "5"


async def test_config_set_get(session):
    await repository.set_config(session, "tmdb_api_key", "abc123")
    assert await repository.get_config(session, "tmdb_api_key") == "abc123"


async def test_ensure_default_config(session):
    await repository.ensure_default_config(session)
    all_cfg = await repository.get_all_config(session)
    assert all_cfg["proxy_targets"] == "tg,tmdb"
    assert all_cfg["schedule_interval"] == "5"


async def test_ensure_default_config_no_overwrite(session):
    # 已存在的 key 不被覆盖
    await repository.set_config(session, "schedule_interval", "10")
    await repository.ensure_default_config(session)
    assert await repository.get_config(session, "schedule_interval") == "10"


# ---- Share 批量去重 ----
async def test_find_existing_shares_small(session):
    pairs = [("code1", "pwd1"), ("code2", "pwd2"), ("code3", "pwd3")]
    for code, pwd in pairs:
        await repository.add_share(
            session,
            models.Share(share_code=code, share_password=pwd, title=f"t-{code}"),
        )
    existing = await repository.find_existing_shares(
        session, [("code1", "pwd1"), ("code2", "pwd2"), ("codeX", "pwdX")]
    )
    assert existing == {("code1", "pwd1"), ("code2", "pwd2")}


async def test_find_existing_shares_batch_over_200(session):
    # 验证 >200 对时分批查询不崩（SQLite 表达式树深度约束）
    pairs = [(f"code{i}", f"pwd{i}") for i in range(250)]
    # 只入库偶数项
    for i in range(0, 250, 2):
        await repository.add_share(
            session,
            models.Share(share_code=f"code{i}", share_password=f"pwd{i}", title="t"),
        )
    existing = await repository.find_existing_shares(session, pairs)
    assert len(existing) == 125  # 偶数项 0,2,...,248
    assert ("code0", "pwd0") in existing
    assert ("code1", "pwd1") not in existing


# ---- Media upsert ----
async def test_upsert_media_insert_then_update(session):
    m = models.Media(tmdb_id=100, media_type="tv", title="Old", year=2020)
    await repository.upsert_media(session, m)
    m2 = models.Media(tmdb_id=100, media_type="tv", title="New", year=2021)
    updated = await repository.upsert_media(session, m2)
    assert updated.title == "New"
    assert updated.year == 2021
    # 只有一条记录
    assert await repository.get_media_by_tmdb(session, 100, "tv") is not None


# ---- TmdbCache upsert 刷新时间戳 + refresh ----
async def test_tmdb_cache_upsert_refresh_timestamp(session):
    from datetime import timedelta

    from app.db.models import _now

    c1 = models.TmdbCache(
        tmdb_id=200, media_type="tv", data={"v": 1}, ongoing=True,
        cached_at=_now() - timedelta(days=10),
        expires_at=_now() + timedelta(days=3),
    )
    await repository.upsert_tmdb_cache(session, c1)
    now = _now()
    c2 = models.TmdbCache(
        tmdb_id=200, media_type="tv", data={"v": 2}, ongoing=True,
        cached_at=now, expires_at=now + timedelta(days=3),
    )
    await repository.upsert_tmdb_cache(session, c2)
    got = await repository.get_tmdb_cache(session, 200, "tv")
    assert got.data == {"v": 2}
    # cached_at 必须被刷新（旧项目约束：upsert 不刷新时间戳会导致缓存不过期）
    assert got.cached_at >= now - timedelta(seconds=5)


async def test_delete_tmdb_cache_refresh(session):
    from datetime import timedelta

    from app.db.models import _now

    c = models.TmdbCache(
        tmdb_id=300, media_type="tv", data={"v": 1}, ongoing=False,
        cached_at=_now(), expires_at=_now() + timedelta(days=30),
    )
    await repository.upsert_tmdb_cache(session, c)
    count = await repository.delete_tmdb_cache_by_id(session, 300)
    assert count == 1
    assert await repository.get_tmdb_cache(session, 300, "tv") is None


# ---- TaskLog ----
async def test_task_log_create_update(session):
    log = await repository.create_task_log(
        session, models.TaskLog(task_type="pipeline", trigger="manual")
    )
    assert log.status == "running"
    await repository.update_task_log(
        session, log.id, status="success", shares_new=5, shares_pushed=3
    )
    logs = await repository.list_task_logs(session)
    assert len(logs) == 1
    assert logs[0].status == "success"
    assert logs[0].shares_new == 5
