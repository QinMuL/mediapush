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
# TMDB 缓存统一 24 小时（定时刷新，及时捕捉 TMDB 数据修正，如地区/季集变更）
_TTL_DAYS = 1


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
            headers = {"Accept": "application/json"}
            # v4 read access token（eyJ 开头 JWT）走 Bearer header，避免 URL/日志泄露；
            # v3 API key（32 位 hex）不支持 Bearer，改走 query 参数（_get 中注入）。
            if self.api_key.startswith("eyJ"):
                headers["Authorization"] = f"Bearer {self.api_key}"
            # trust_env=True：同时尊重 PROXY_URL（显式）和系统/进程的
            # HTTP_PROXY/HTTPS_PROXY 环境变量——用户开了代理工具时 env 通常有，
            # 避免"明明开代理 Python 还连不上 TMDB"。
            self._client = httpx.AsyncClient(
                proxy=self.proxy_url or None,
                timeout=httpx.Timeout(15.0),
                headers=headers,
                trust_env=True,
            )
        return self._client

    async def _get(self, path: str, params: dict, language: str | None = None) -> dict:
        params = {**params, "language": language or self.language}
        # v3 key 走 query 参数（v4 token 已走 Bearer header，不重复注入）
        if not self.api_key.startswith("eyJ"):
            params["api_key"] = self.api_key
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
    async def search(
        self,
        title: str,
        year: int | None = None,
        media_type: str = "auto",
        language: str | None = None,
    ) -> list[dict]:
        """搜索候选列表。带年无果回退无年。

        language：覆盖实例默认语言（如英文查询词用 en-US 搜，匹配英文名）。
        """
        if media_type == "auto":
            res = await self._search_one(title, year, "movie", language)
            res += await self._search_one(title, year, "tv", language)
            return res
        return await self._search_one(title, year, media_type, language)

    async def _search_one(
        self, title: str, year: int | None, media_type: str, language: str | None = None
    ) -> list[dict]:
        path = "/search/movie" if media_type == "movie" else "/search/tv"
        params: dict = {"query": title}
        year_key = "year" if media_type == "movie" else "first_air_date_year"
        if year:
            params[year_key] = year
        data = await self._get(path, params, language)
        results = data.get("results", []) or []
        if not results and year:
            # 回退：不带年重搜
            data = await self._get(path, {"query": title}, language)
            results = data.get("results", []) or []
        return results

    async def search_best(
        self, title: str, year: int | None, media_type: str = "auto"
    ) -> tuple[int, str] | None:
        """返回最佳匹配 (tmdb_id, media_type)，无则 None。

        匹配优先级（借 P115-Share 的思路）：
        1. 年份吻合（文件名年份可信度最高，防同名老片/新片混淆）
        2. 标题相似度：精确（zh title）> 原名精确（original_title，别名兜底）> 互相包含
        3. 评分 + 海报（流行度兜底，即原行为）
        media_type 显式时过滤异型候选（电影/剧集同名命名空间冲突兜底）。
        """
        candidates = await self.search(title, year, media_type)
        if not candidates:
            return None

        def kind(c: dict) -> str:
            return "movie" if "title" in c else "tv"

        pool = candidates
        if media_type in ("movie", "tv"):
            same = [c for c in candidates if kind(c) == media_type]
            pool = same or candidates
        t = (title or "").strip().lower()

        def score(c: dict) -> tuple:
            d = c.get("release_date") or c.get("first_air_date") or ""
            cy = int(d[:4]) if d[:4].isdigit() else None
            year_ok = 1 if (year is None or cy == year) else 0
            ct = (c.get("title") or c.get("name") or "").strip().lower()
            ot = (c.get("original_title") or c.get("original_name") or "").strip().lower()
            if t and ct == t:
                ts = 3
            elif t and ot == t:
                ts = 2
            elif t and ((ct and (t in ct or ct in t)) or (ot and (t in ot or ot in t))):
                ts = 1
            else:
                ts = 0
            return (year_ok, ts, c.get("vote_average") or 0, bool(c.get("poster_path")))

        best = max(pool, key=score)
        return int(best["id"]), kind(best)

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
        # translations（20 种语言标题）+ alternative_titles（AKA），是韩剧/
        # 外语片名跨语种命中的关键（zh-CN 搜出中文标题≠英文名发布名）。
        params = {"append_to_response": "credits,images,translations,alternative_titles"}
        data = await self._get(path, params)
        normalized = self._normalize(data, media_type)

        if self.cache:
            await self.cache.set_tmdb(tmdb_id, media_type, normalized, ttl_days=_TTL_DAYS)
        return normalized

    def _normalize(self, data: dict, media_type: str) -> dict:
        alt_titles: list[str] = self._collect_alt_titles(data)
        if media_type == "movie":
            return {
                "tmdb_id": data.get("id"),
                "media_type": "movie",
                "title": data.get("title") or data.get("original_title") or "",
                "original_title": data.get("original_title") or "",
                "alt_titles": alt_titles,
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
                "countries": data.get("origin_country")
                or [c["iso_3166_1"] for c in data.get("production_countries", [])],
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
            "alt_titles": alt_titles,
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
            "countries": data.get("origin_country")
            or [c["iso_3166_1"] for c in data.get("production_countries", [])],
        }

    @staticmethod
    def _collect_alt_titles(data: dict) -> list[str]:
        """从 translations + alternative_titles 收集别名，给跨语种标题匹配兜底。

        只取英文（发布组通用）+ 中文（中文搜索）+ 无地区 AKA，过滤空串
        与 title/original_name 的重复交给调用方去重。
        """
        titles: list[str] = []
        for t in data.get("translations", {}).get("translations", []) or []:
            lang = t.get("iso_639_1", "")
            d = t.get("data") or {}
            name = d.get("name") or d.get("title") or ""
            if lang in ("en", "zh", "ko") and name:
                titles.append(name.strip())
        # TV: alternative_titles.results[], Movie: alternative_titles.titles[]
        at = data.get("alternative_titles") or {}
        for item in at.get("results", []) or at.get("titles", []) or []:
            if isinstance(item, dict):
                n = item.get("title") or ""
            else:
                n = str(item or "")
            if n:
                titles.append(n.strip())
        # 去重保序
        seen: set[str] = set()
        uniq: list[str] = []
        for n in titles:
            if n and n not in seen:
                seen.add(n)
                uniq.append(n)
        return uniq

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
