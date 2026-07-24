"""TMDB 业务层：搜索（年份回退）、季集数补充、缓存优先、refresh。

旧项目约束：
- 搜索带年份无结果需回退不带年份重搜（文件名年份可能是制作/资源年份）
- 整季文件夹（S01）从 TMDB 季 episode_count 补集数，跳过 season 0 特别篇，取第一个正剧季
- total_episodes 用 TMDB number_of_episodes，回退文件名推断
"""
from sqlalchemy.ext.asyncio import AsyncSession

from app.tmdb import cache as cache_mod
from app.tmdb.client import TmdbClient


class TmdbService:
    def __init__(self, client: TmdbClient):
        self._client = client

    async def close(self) -> None:
        await self._client.close()

    # ---- 搜索 ----
    async def search(
        self, query: str, media_type: str = "tv", year: int | None = None
    ) -> list[dict]:
        """搜索；带年份无结果时回退不带年份重搜。"""
        if media_type == "tv":
            res = await self._client.search_tv(query, year)
            if year and not res.get("results"):
                res = await self._client.search_tv(query, None)  # 回退
        else:
            res = await self._client.search_movie(query, year)
            if year and not res.get("results"):
                res = await self._client.search_movie(query, None)
        return res.get("results", [])

    # ---- 详情（缓存优先）----
    async def get_details(
        self, session: AsyncSession, tmdb_id: int, media_type: str
    ) -> dict:
        cached = await cache_mod.get_valid_cache(session, tmdb_id, media_type)
        if cached:
            return cached.data
        if media_type == "tv":
            data = await self._client.get_tv_details(tmdb_id)
        else:
            data = await self._client.get_movie_details(tmdb_id)
        await cache_mod.store_cache(
            session, tmdb_id, media_type, data, self._is_ongoing(data, media_type)
        )
        return data

    @staticmethod
    def _is_ongoing(data: dict, media_type: str) -> bool:
        if media_type == "tv":
            status = (data.get("status") or "").lower()
            return status == "returning series"
        return False  # 电影不连载

    # ---- 整季集数补充 ----
    async def fill_episodes_from_season(
        self, session: AsyncSession, tv_id: int, season_number: int | None
    ) -> int | None:
        """整季文件夹：从 TMDB 季 episode_count 补集数。

        season 0 是特别篇，取第一个正剧季（旧项目约束）。
        """
        if season_number is None or season_number == 0:
            details = await self.get_details(session, tv_id, "tv")
            for s in details.get("seasons", []):
                if s.get("season_number", 0) > 0:
                    return s.get("episode_count")
            return None
        season_data = await self._client.get_tv_season(tv_id, season_number)
        return season_data.get("episode_count")

    # ---- 总集数 ----
    async def get_total_episodes(
        self,
        session: AsyncSession,
        tv_id: int,
        fallback: int | None = None,
    ) -> int | None:
        """total_episodes：TMDB number_of_episodes 优先，回退文件名推断。"""
        details = await self.get_details(session, tv_id, "tv")
        tmdb_eps = details.get("number_of_episodes")
        if tmdb_eps:
            return int(tmdb_eps)
        return fallback

    # ---- refresh ----
    async def refresh(self, session: AsyncSession, tmdb_id: int) -> int:
        return await cache_mod.refresh_cache(session, tmdb_id)
