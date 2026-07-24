"""115 分享扫描服务。

旧项目约束：
- share_iterdir_walk 用 app='web'（android 端已移除失效），receive_code 第三位置参数
- share_snap 已废弃（405），统一用 share_iterdir_walk
- 内存缓存键含 password（receive_code），避免同 share_code 不同访问码命中错误缓存
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from app.pan115.client import Pan115Client, Pan115PermanentError


class Pan115Service:
    def __init__(self, client: Pan115Client):
        self._client = client
        # 缓存键含 receive_code（password），避免同 share_code 不同访问码命中错误缓存
        self._info_cache: dict[tuple[str, str], dict] = {}

    async def get_share_info(self, share_code: str, receive_code: str = "") -> dict:
        key = (share_code, receive_code)
        if key in self._info_cache:
            return self._info_cache[key]
        info = await self._client.share_info(share_code, receive_code)
        self._info_cache[key] = info
        return info

    async def iter_share_files(
        self, share_code: str, receive_code: str = ""
    ) -> AsyncIterator[dict]:
        """遍历分享文件（app='web'，receive_code 第三位置参数）。"""
        async for f in self._client.iter_share_files(share_code, receive_code):
            yield f

    def clear_cache(self) -> None:
        self._info_cache.clear()

    @staticmethod
    def is_permanent_error(err: Exception) -> bool:
        """405 等永久错误，调用方不应重试。"""
        return isinstance(err, Pan115PermanentError)
