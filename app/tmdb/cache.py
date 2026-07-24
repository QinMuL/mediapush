"""TMDB 缓存：连载中 3 天 / 已完结 30 天过期；upsert 刷新时间戳；refresh 删除。

旧项目约束：
- 30 天缓存导致连载剧集数不更新 → 连载中缩短为 3 天
- upsert 必须刷新 cached_at，否则缓存永不过期
- refresh 命令删除缓存以强制重拉
"""
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models, repository
from app.db.models import _now

ONGOING_TTL_DAYS = 3
FINISHED_TTL_DAYS = 30


def compute_expiry(ongoing: bool):
    days = ONGOING_TTL_DAYS if ongoing else FINISHED_TTL_DAYS
    return _now() + timedelta(days=days)


def is_expired(cache: models.TmdbCache) -> bool:
    return _now() >= cache.expires_at


async def get_valid_cache(
    session: AsyncSession, tmdb_id: int, media_type: str
) -> models.TmdbCache | None:
    """返回未过期缓存；过期或无则 None。"""
    cache = await repository.get_tmdb_cache(session, tmdb_id, media_type)
    if cache and not is_expired(cache):
        return cache
    return None


async def store_cache(
    session: AsyncSession,
    tmdb_id: int,
    media_type: str,
    data: dict,
    ongoing: bool,
) -> models.TmdbCache:
    cache = models.TmdbCache(
        tmdb_id=tmdb_id,
        media_type=media_type,
        data=data,
        ongoing=ongoing,
        cached_at=_now(),  # upsert 会刷新该时间戳
        expires_at=compute_expiry(ongoing),
    )
    return await repository.upsert_tmdb_cache(session, cache)


async def refresh_cache(session: AsyncSession, tmdb_id: int) -> int:
    """refresh 命令：删除该 TMDB ID 的缓存，下次取时重拉。"""
    return await repository.delete_tmdb_cache_by_id(session, tmdb_id)
