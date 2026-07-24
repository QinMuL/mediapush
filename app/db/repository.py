"""数据访问层。

批量去重按 200 对/批查询，避免 SQLite 表达式树深度超限（最大深度 1000，旧项目约束）。
"""
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.defaults import DEFAULT_CONFIG
from app.db import models

# 每批查询的对数上限（旧项目踩坑：超过会触发 SQLite 表达式树深度限制）
BATCH_SIZE = 200


# ---- AppConfig ----
async def get_config(
    session: AsyncSession, key: str, default: str | None = None
) -> str | None:
    """读取配置：DB > default 参数 > DEFAULT_CONFIG。"""
    row = await session.get(models.AppConfig, key)
    if row is not None:
        return row.value
    if default is not None:
        return default
    return DEFAULT_CONFIG.get(key)


async def set_config(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(models.AppConfig, key)
    if row is None:
        session.add(models.AppConfig(key=key, value=value))
    else:
        row.value = value
    await session.commit()


async def get_all_config(session: AsyncSession) -> dict[str, str]:
    """合并默认值与 DB 覆盖值。"""
    result = await session.execute(select(models.AppConfig))
    stored = {r.key: r.value for r in result.scalars()}
    merged = dict(DEFAULT_CONFIG)
    merged.update(stored)
    return merged


async def ensure_default_config(session: AsyncSession) -> None:
    """首次启动写入默认配置（已存在的 key 不覆盖）。"""
    for key, value in DEFAULT_CONFIG.items():
        existing = await session.get(models.AppConfig, key)
        if existing is None:
            session.add(models.AppConfig(key=key, value=value))
    await session.commit()


# ---- Share ----
async def find_existing_shares(
    session: AsyncSession, pairs: list[tuple[str, str]]
) -> set[tuple[str, str]]:
    """批量查询已存在的 (share_code, share_password)，200 对/批。

    用于去重，避免逐对查询的性能与表达式树深度问题。
    """
    existing: set[tuple[str, str]] = set()
    for i in range(0, len(pairs), BATCH_SIZE):
        chunk = pairs[i : i + BATCH_SIZE]
        conds = [
            (models.Share.share_code == code) & (models.Share.share_password == pwd)
            for code, pwd in chunk
        ]
        stmt = select(models.Share.share_code, models.Share.share_password).where(
            or_(*conds) if len(conds) > 1 else conds[0]
        )
        result = await session.execute(stmt)
        for code, pwd in result:
            existing.add((code, pwd))
    return existing


async def get_share_by_code(
    session: AsyncSession, share_code: str, share_password: str
) -> models.Share | None:
    stmt = select(models.Share).where(
        (models.Share.share_code == share_code)
        & (models.Share.share_password == share_password)
    )
    return await session.scalar(stmt)


async def add_share(session: AsyncSession, share: models.Share) -> models.Share:
    session.add(share)
    await session.commit()
    await session.refresh(share)
    return share


async def list_shares(
    session: AsyncSession, limit: int = 50, offset: int = 0
) -> list[models.Share]:
    stmt = (
        select(models.Share)
        .options(selectinload(models.Share.media))
        .order_by(models.Share.id.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    return list(result.scalars())


async def list_shares_filtered(
    session: AsyncSession,
    q: str | None = None,
    pushed: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[models.Share]:
    """Web 分享页：按标题/分享码搜索 + 推送状态筛选，含 media。"""
    stmt = select(models.Share).options(selectinload(models.Share.media))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            (models.Share.title.like(like)) | (models.Share.share_code.like(like))
        )
    if pushed is not None:
        stmt = stmt.where(models.Share.pushed.is_(pushed))
    stmt = stmt.order_by(models.Share.id.desc()).limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars())


async def count_shares_filtered(
    session: AsyncSession,
    q: str | None = None,
    pushed: bool | None = None,
) -> int:
    from sqlalchemy import func

    stmt = select(func.count()).select_from(models.Share)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            (models.Share.title.like(like)) | (models.Share.share_code.like(like))
        )
    if pushed is not None:
        stmt = stmt.where(models.Share.pushed.is_(pushed))
    return (await session.execute(stmt)).scalar() or 0


async def count_shares_since(
    session: AsyncSession, column: str, since
) -> int:
    """统计某时间字段 >= since 的分享数（仪表盘今日新增/今日推送）。"""
    from sqlalchemy import func

    col = getattr(models.Share, column)
    stmt = select(func.count()).select_from(models.Share).where(col >= since)
    return (await session.execute(stmt)).scalar() or 0


async def get_share_with_media(
    session: AsyncSession, share_id: int
) -> models.Share | None:
    stmt = (
        select(models.Share)
        .options(selectinload(models.Share.media))
        .where(models.Share.id == share_id)
    )
    return await session.scalar(stmt)


async def list_unpushed_shares(
    session: AsyncSession, limit: int = 50
) -> list[models.Share]:
    """未推送的分享（含 media），供 push_pending 扫尾。"""
    stmt = (
        select(models.Share)
        .options(selectinload(models.Share.media))
        .where(models.Share.pushed.is_(False))
        .order_by(models.Share.id.asc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars())


async def mark_share_pushed(session: AsyncSession, share_id: int) -> None:
    from app.db.models import _now

    share = await session.get(models.Share, share_id)
    if share is not None:
        share.pushed = True
        share.pushed_at = _now()
        await session.commit()


# ---- Media ----
async def get_media_by_tmdb(
    session: AsyncSession, tmdb_id: int, media_type: str
) -> models.Media | None:
    stmt = select(models.Media).where(
        (models.Media.tmdb_id == tmdb_id) & (models.Media.media_type == media_type)
    )
    return await session.scalar(stmt)


async def upsert_media(session: AsyncSession, media: models.Media) -> models.Media:
    existing = await get_media_by_tmdb(session, media.tmdb_id, media.media_type)
    if existing:
        for col in (
            "title", "original_title", "year", "season", "episode_start",
            "episode_end", "total_episodes", "quality", "audio", "overview",
            "poster_path",
        ):
            val = getattr(media, col)
            if val is not None:  # 跳过未显式设置的字段，避免 NOT NULL 违反
                setattr(existing, col, val)
        await session.commit()
        return existing
    session.add(media)
    await session.commit()
    await session.refresh(media)
    return media


# ---- TmdbCache ----
async def get_tmdb_cache(
    session: AsyncSession, tmdb_id: int, media_type: str
) -> models.TmdbCache | None:
    stmt = select(models.TmdbCache).where(
        (models.TmdbCache.tmdb_id == tmdb_id)
        & (models.TmdbCache.media_type == media_type)
    )
    return await session.scalar(stmt)


async def upsert_tmdb_cache(
    session: AsyncSession, cache: models.TmdbCache
) -> models.TmdbCache:
    """upsert 必须刷新 cached_at 时间戳（旧项目约束）。"""
    existing = await get_tmdb_cache(session, cache.tmdb_id, cache.media_type)
    if existing:
        existing.data = cache.data
        existing.ongoing = cache.ongoing
        existing.cached_at = cache.cached_at
        existing.expires_at = cache.expires_at
        await session.commit()
        return existing
    session.add(cache)
    await session.commit()
    await session.refresh(cache)
    return cache


async def delete_tmdb_cache_by_id(
    session: AsyncSession, tmdb_id: int
) -> int:
    """refresh 命令删除该 TMDB ID 的所有缓存（旧项目约束）。"""
    stmt = select(models.TmdbCache).where(models.TmdbCache.tmdb_id == tmdb_id)
    result = await session.execute(stmt)
    count = 0
    for c in result.scalars():
        await session.delete(c)
        count += 1
    await session.commit()
    return count


async def list_tmdb_cache(
    session: AsyncSession, limit: int = 50, offset: int = 0
) -> list[models.TmdbCache]:
    stmt = (
        select(models.TmdbCache)
        .order_by(models.TmdbCache.cached_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    return list(result.scalars())


async def count_tmdb_cache(session: AsyncSession) -> int:
    from sqlalchemy import func

    stmt = select(func.count()).select_from(models.TmdbCache)
    return (await session.execute(stmt)).scalar() or 0


# ---- TaskLog ----
async def create_task_log(
    session: AsyncSession, task_log: models.TaskLog
) -> models.TaskLog:
    session.add(task_log)
    await session.commit()
    await session.refresh(task_log)
    return task_log


async def update_task_log(
    session: AsyncSession, task_id: int, **fields
) -> None:
    log = await session.get(models.TaskLog, task_id)
    if log:
        for k, v in fields.items():
            setattr(log, k, v)
        await session.commit()


async def list_task_logs(
    session: AsyncSession, limit: int = 50
) -> list[models.TaskLog]:
    stmt = select(models.TaskLog).order_by(models.TaskLog.id.desc()).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars())
