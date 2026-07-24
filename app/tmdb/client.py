"""TMDB API 客户端（httpx + 代理）。

代理在构造时注入（由 service 从 app_config 读取后传入），改代理后由 container 重建 client。
瞬时错误（429 / 5xx / 超时 / 连接）指数退避重试，4xx 永久错误立即抛出。
"""
import httpx

from app.core.retry import retry_async

TMDB_BASE = "https://api.themoviedb.org/3"


def _is_transient(err: Exception) -> bool:
    """429 / 5xx / 网络超时连接视为瞬时；其余 4xx 永久不重试。"""
    if isinstance(err, httpx.HTTPStatusError):
        code = err.response.status_code
        return code == 429 or code >= 500
    return isinstance(err, (httpx.TimeoutException, httpx.TransportError))


class TmdbClient:
    def __init__(self, api_key: str, proxy: str | None = None, timeout: float = 30.0):
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=TMDB_BASE, proxy=proxy, timeout=timeout
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, **params) -> dict:
        query = {"api_key": self._api_key, **params}

        async def _call():
            resp = await self._client.get(path, params=query)
            resp.raise_for_status()
            return resp.json()

        return await retry_async(
            _call, retries=3, base_delay=1.0,
            is_transient=_is_transient, label=f"tmdb {path}",
        )

    async def search_tv(self, query: str, year: int | None = None) -> dict:
        params: dict = {"query": query, "language": "zh-CN"}
        if year:
            params["first_air_date_year"] = year
        return await self._get("/search/tv", **params)

    async def search_movie(self, query: str, year: int | None = None) -> dict:
        params: dict = {"query": query, "language": "zh-CN"}
        if year:
            params["year"] = year
        return await self._get("/search/movie", **params)

    async def get_tv_details(self, tv_id: int) -> dict:
        return await self._get(f"/tv/{tv_id}", language="zh-CN")

    async def get_movie_details(self, movie_id: int) -> dict:
        return await self._get(f"/movie/{movie_id}", language="zh-CN")

    async def get_tv_season(self, tv_id: int, season_number: int) -> dict:
        return await self._get(f"/tv/{tv_id}/season/{season_number}", language="zh-CN")
