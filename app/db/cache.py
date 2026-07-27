"""SQLite 轻量缓存：TMDB 元数据缓存 + 已推送分享去重。

承接前序经验：
- ongoing 剧集缓存 3 天，已完结 30 天
- upsert 时刷新 fetched_at（前序 bug：缓存时间戳不刷新导致不过期）
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tmdb_cache (
    tmdb_id     INTEGER NOT NULL,
    media_type  TEXT    NOT NULL,
    payload     TEXT    NOT NULL,
    fetched_at  REAL    NOT NULL,
    ttl_days    INTEGER NOT NULL,
    PRIMARY KEY (tmdb_id, media_type)
);
CREATE TABLE IF NOT EXISTS pushed_shares (
    share_code  TEXT PRIMARY KEY,
    pushed_at   REAL NOT NULL
);
"""


class Cache:
    def __init__(self, db_path: str = "./data/cache.db") -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    async def _execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_SCHEMA)
            cur = await db.execute(sql, params)
            await db.commit()
            return cur

    async def _fetchone(self, sql: str, params: tuple = ()) -> tuple | None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_SCHEMA)
            cur = await db.execute(sql, params)
            return await cur.fetchone()

    # ------------------------------------------------------------------ #
    # TMDB 缓存
    # ------------------------------------------------------------------ #
    async def get_tmdb(self, tmdb_id: int, media_type: str) -> dict | None:
        row = await self._fetchone(
            "SELECT payload, fetched_at, ttl_days FROM tmdb_cache "
            "WHERE tmdb_id=? AND media_type=?",
            (tmdb_id, media_type),
        )
        if not row:
            return None
        payload, fetched_at, ttl_days = row
        if (time.time() - fetched_at) > ttl_days * 86400:
            return None  # 过期
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return None

    async def set_tmdb(
        self, tmdb_id: int, media_type: str, payload: dict, *, ttl_days: int
    ) -> None:
        # upsert：ON CONFLICT 刷新 fetched_at + ttl_days + payload
        await self._execute(
            "INSERT INTO tmdb_cache (tmdb_id, media_type, payload, fetched_at, ttl_days) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(tmdb_id, media_type) DO UPDATE SET "
            "payload=excluded.payload, fetched_at=excluded.fetched_at, ttl_days=excluded.ttl_days",
            (tmdb_id, media_type, json.dumps(payload, ensure_ascii=False), time.time(), ttl_days),
        )

    async def delete_tmdb(self, tmdb_id: int) -> int:
        cur = await self._execute(
            "DELETE FROM tmdb_cache WHERE tmdb_id=?", (tmdb_id,)
        )
        return cur.rowcount

    # ------------------------------------------------------------------ #
    # 已推送分享去重
    # ------------------------------------------------------------------ #
    async def is_pushed(self, share_code: str) -> bool:
        row = await self._fetchone(
            "SELECT 1 FROM pushed_shares WHERE share_code=?", (share_code,)
        )
        return row is not None

    async def mark_pushed(self, share_code: str) -> None:
        await self._execute(
            "INSERT OR IGNORE INTO pushed_shares (share_code, pushed_at) VALUES (?,?)",
            (share_code, time.time()),
        )
