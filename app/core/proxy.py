"""代理管理：从 app_config 读取代理配置，按目标分发。

target 取值：tg / tmdb / 115。默认 tg+tmdb 走代理（境外必须），115 不走（国内服务）。
详见 ARCHITECTURE.md 第 5.13 / 8 节。
"""
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repository

_TRUE = {"1", "true", "yes", "on"}


def _parse_targets(targets_str: str | None) -> set[str]:
    if not targets_str:
        return set()
    return {t.strip().lower() for t in targets_str.split(",") if t.strip()}


async def get_proxy_for(session: AsyncSession, target: str) -> str | None:
    """返回 target 应使用的代理 URL；不使用代理返回 None。

    判定：proxy_enabled 为真 且 target 在 proxy_targets 中。
    """
    enabled = (await repository.get_config(session, "proxy_enabled") or "").lower()
    if enabled not in _TRUE:
        return None
    targets = _parse_targets(await repository.get_config(session, "proxy_targets"))
    if target.lower() not in targets:
        return None
    url = await repository.get_config(session, "proxy_url")
    return url or None


async def is_proxy_enabled_for(session: AsyncSession, target: str) -> bool:
    return await get_proxy_for(session, target) is not None
