"""TMDB 官方 API 客户端。

承接前序策略：
- 搜索带年无果回退无年（文件名年份可能是资源年）
- get_details：电影/剧集详情 + 演职员 + 季集数
- 整季文件夹从 TMDB 季 episode_count 补集数（保留 season 0 特别篇）
- HTTP：429/5xx/超时指数退避重试，4xx 立即抛出不重试
- 缓存：ongoing 剧 3 天 / 已完结 30 天；upsert 刷新时间戳
- TMDB API 走代理
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.db.cache import Cache

logger = logging.getLogger(__name__)

_API_BASE = "https://api.themoviedb.org/3"
_IMG_BASE = "https://image.tmdb.org/t/p/w500"
# 横屏剧照（backdrop，16:9）：w780 清晰度与体积平衡，适合 TG 卡片
_BACKDROP_BASE = "https://image.tmdb.org/t/p/w780"
_TTL_ONGOING = 3
_TTL_FINISHED = 30


class TMDBError(Exception):
    """TMDB API 永久性错误（4xx），不重试。"""


def _is_transient(resp: httpx.Response) -> bool:
    return resp.status_code == 429 or resp.status_code >= 500


class TMDBHelper:
    def __init__(
        self,
        api_key: str,
        *,
        language: str = "zh-CN",
        proxy_url: str = "",
        cache: Cache | None = None,
    ) -> None:
        if not api_key:
            raise TMDBError("TMDB_API_KEY 未配置")
        self.api_key = api_key
        self.language = language
        self.proxy_url = proxy_url
        self.cache = cache
        self._client: httpx.AsyncClient | None = None

    async def _aclose_client(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                proxy=self.proxy_url or None,
                timeout=httpx.Timeout(15.0),
                headers={"Accept": "application/json"},
            )
        return self._client

    async def _get(self, path: str, params: dict) -> dict:
        params = {**params, "api_key": self.api_key, "language": self.language}
        client = self._ensure_client()
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                resp = await client.get(f"{_API_BASE}{path}", params=params)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                await asyncio.sleep(min(2 ** attempt, 8))
                continue
            if _is_transient(resp):
                last_exc = TMDBError(f"TMDB {resp.status_code} transient")
                await asyncio.sleep(min(2 ** attempt, 8))
                continue
            if 400 <= resp.status_code < 500:
                # 永久错误，立即抛出不重试
                raise TMDBError(f"TMDB {resp.status_code}: {resp.text[:200]}")
            return resp.json()
        raise TMDBError(f"TMDB 请求重试耗尽：{last_exc}")

    # ------------------------------------------------------------------ #
    # 搜索
    # ------------------------------------------------------------------ #
    async def search(self, title: str, year: int | None = None, media_type: str = "auto") -> list[dict]:
        """搜索候选列表。带年无果回退无年。"""
        if media_type == "auto":
            res = await self._search_one(title, year, "movie")
            res += await self._search_one(title, year, "tv")
            return res
        return await self._search_one(title, year, media_type)

    async def _search_one(self, title: str, year: int | None, media_type: str) -> list[dict]:
        path = "/search/movie" if media_type == "movie" else "/search/tv"
        params: dict = {"query": title}
        year_key = "year" if media_type == "movie" else "first_air_date_year"
        if year:
            params[year_key] = year
        data = await self._get(path, params)
        results = data.get("results", []) or []
        if not results and year:
            # 回退：不带年重搜
            data = await self._get(path, {"query": title})
            results = data.get("results", []) or []
        return results

    async def search_best(
        self, title: str, year: int | None, media_type: str = "auto"
    ) -> tuple[int, str] | None:
        """返回最佳匹配 (tmdb_id, media_type)，无则 None。"""
        candidates = await self.search(title, year, media_type)
        if not candidates:
            return None
        # 优先选有 poster / 评分高的
        best = max(
            candidates,
            key=lambda c: (c.get("vote_average") or 0, bool(c.get("poster_path"))),
        )
        return int(best["id"]), "movie" if "title" in best else "tv"

    # ------------------------------------------------------------------ #
    # 详情
    # ------------------------------------------------------------------ #
    async def get_details(self, tmdb_id: int, media_type: str) -> dict:
        """取详情（含演职员/季）。带缓存。"""
        if self.cache:
            cached = await self.cache.get_tmdb(tmdb_id, media_type)
            if cached:
                logger.debug("TMDB 缓存命中 %s/%s", media_type, tmdb_id)
                return cached

        path = f"/{'movie' if media_type == 'movie' else 'tv'}/{tmdb_id}"
        params = {"append_to_response": "credits,images"}
        data = await self._get(path, params)
        normalized = self._normalize(data, media_type)

        if self.cache:
            ttl = _TTL_ONGOING if self._is_ongoing(normalized, media_type) else _TTL_FINISHED
            await self.cache.set_tmdb(tmdb_id, media_type, normalized, ttl_days=ttl)
        return normalized

    @staticmethod
    def _is_ongoing(info: dict, media_type: str) -> bool:
        if media_type != "tv":
            return False
        status = (info.get("status") or "").lower()
        return status not in {"ended", "canceled", "cancelled"}

    def _normalize(self, data: dict, media_type: str) -> dict:
        if media_type == "movie":
            return {
                "tmdb_id": data.get("id"),
                "media_type": "movie",
                "title": data.get("title") or data.get("original_title") or "",
                "original_title": data.get("original_title") or "",
                "year": self._year_of(data.get("release_date")),
                "release_date": data.get("release_date") or "",
                "overview": data.get("overview") or "",
                "poster_path": data.get("poster_path"),
                "backdrop_path": data.get("backdrop_path"),
                "vote_average": data.get("vote_average") or 0,
                "vote_count": data.get("vote_count") or 0,
                "genres": [g["name"] for g in data.get("genres", [])],
                "runtime": data.get("runtime"),  # 分钟
                "status": data.get("status") or "",
                "cast": [c["name"] for c in (data.get("credits", {}).get("cast", []))[:5]],
                "directors": [
                    c["name"] for c in data.get("credits", {}).get("crew", [])
                    if c.get("job") == "Director"
                ][:3],
                "countries": [c["iso_3166_1"] for c in data.get("production_countries", [])],
            }
        # tv
        seasons = []
        for s in data.get("seasons", []):
            # 保留 season 0（特别篇/specials），让 S00 文件能正确匹配
            seasons.append({
                "season": s.get("season_number"),
                "episode_count": s.get("episode_count") or 0,
                "name": s.get("name") or "",
            })
        return {
            "tmdb_id": data.get("id"),
            "media_type": "tv",
            "title": data.get("name") or data.get("original_name") or "",
            "original_title": data.get("original_name") or "",
            "year": self._year_of(data.get("first_air_date")),
            "release_date": data.get("first_air_date") or "",
            "overview": data.get("overview") or "",
            "poster_path": data.get("poster_path"),
            "backdrop_path": data.get("backdrop_path"),
            "vote_average": data.get("vote_average") or 0,
            "vote_count": data.get("vote_count") or 0,
            "genres": [g["name"] for g in data.get("genres", [])],
            "number_of_seasons": data.get("number_of_seasons") or 0,
            "number_of_episodes": data.get("number_of_episodes") or 0,
            "status": data.get("status") or "",
            "seasons": seasons,
            "cast": [c["name"] for c in (data.get("credits", {}).get("cast", []))[:5]],
            "creators": [c["name"] for c in data.get("created_by", [])][:3],
            "countries": [c["iso_3166_1"] for c in data.get("production_countries", [])],
        }

    @staticmethod
    def _year_of(date_str: str | None) -> int | None:
        if date_str and len(date_str) >= 4 and date_str[:4].isdigit():
            return int(date_str[:4])
        return None

    # ------------------------------------------------------------------ #
    # 海报
    # ------------------------------------------------------------------ #
    @staticmethod
    def poster_url(poster_path: str | None) -> str | None:
        if not poster_path:
            return None
        return f"{_IMG_BASE}{poster_path}"

    @staticmethod
    def backdrop_url(backdrop_path: str | None) -> str | None:
        """横屏剧照（16:9），无 backdrop 返回 None（由调用方回退 poster）。"""
        if not backdrop_path:
            return None
        return f"{_BACKDROP_BASE}{backdrop_path}"

    @staticmethod
    def image_url(details: dict) -> str | None:
        """卡片配图：优先横屏 backdrop，回退竖屏 poster，再无则 None。"""
        return (
            TMDBHelper.backdrop_url(details.get("backdrop_path"))
            or TMDBHelper.poster_url(details.get("poster_path"))
        )

    async def close(self) -> None:
        await self._aclose_client()
