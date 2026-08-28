"""SQLite 轻量缓存：TMDB 元数据缓存 + 已推送分享去重。

承接前序经验：
- TMDB 缓存统一 24 小时（ttl_days=1），不区分连载/完结
- upsert 时刷新 fetched_at（前序 bug：缓存时间戳不刷新导致不过期）
- get_tmdb 读到过期行时物理删除（自动清除，非惰性残留）
- 持久连接：长连接 + 启动建表一次，避免每次操作重连+executescript 开销
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
CREATE TABLE IF NOT EXISTS share_dirs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT NOT NULL UNIQUE,   -- 网盘路径（如 /媒体/新剧）
    cid         INTEGER NOT NULL,       -- 路径解析出的目录 id
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS shared_items (
    file_id     INTEGER NOT NULL,       -- 子目录 fid（去重 key）
    dir_id      INTEGER NOT NULL,       -- 所属监控目录 id
    name        TEXT NOT NULL,
    share_code  TEXT NOT NULL,
    pushed_at   REAL NOT NULL,
    PRIMARY KEY (file_id, dir_id)
);
CREATE TABLE IF NOT EXISTS pushed_shares (
    share_code  TEXT PRIMARY KEY,
    pushed_at   REAL NOT NULL,
    provider    TEXT NOT NULL DEFAULT '115',
    password    TEXT NOT NULL DEFAULT '',
    chat_id     TEXT NOT NULL DEFAULT '',
    message_id  INTEGER,
    title       TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'ok',
    last_checked_at REAL
);
"""

# 旧库迁移：pushed_shares 新增列（巡检/撤卡用），CREATE IF NOT EXISTS 不加列需 ALTER
_PUSHED_MIGRATE = [
    ("provider", "TEXT NOT NULL DEFAULT '115'"),
    ("password", "TEXT NOT NULL DEFAULT ''"),
    ("chat_id", "TEXT NOT NULL DEFAULT ''"),
    ("message_id", "INTEGER"),
    ("title", "TEXT NOT NULL DEFAULT ''"),
    ("status", "TEXT NOT NULL DEFAULT 'ok'"),
    ("last_checked_at", "REAL"),
]


class Cache:
    def __init__(self, db_path: str = "./data/cache.db") -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    async def connect(self) -> None:
        """建长连接 + 建表一次（幂等）。首次操作时懒连接，避免改 build 同步签名。"""
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.executescript(_SCHEMA)
        await self._migrate_pushed_shares()
        await self._db.commit()

    async def _migrate_pushed_shares(self) -> None:
        """旧库 pushed_shares 补列（幂等）：PRAGMA 检查缺失列后 ALTER ADD。"""
        cursor = await self._db.execute("PRAGMA table_info(pushed_shares)")
        cols = {row[1] for row in await cursor.fetchall()}
        for col, ddl in _PUSHED_MIGRATE:
            if col not in cols:
                await self._db.execute(
                    f"ALTER TABLE pushed_shares ADD COLUMN {col} {ddl}"
                )
                logger.info("迁移 pushed_shares：新增列 %s", col)

    async def _ensure(self) -> aiosqlite.Connection:
        if self._db is None:
            await self.connect()
        return self._db

    async def _execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        db = await self._ensure()
        cur = await db.execute(sql, params)
        await db.commit()
        return cur

    async def _fetchone(self, sql: str, params: tuple = ()) -> tuple | None:
        db = await self._ensure()
        cur = await db.execute(sql, params)
        return await cur.fetchone()

    async def _fetchall(self, sql: str, params: tuple = ()) -> list[tuple]:
        db = await self._ensure()
        cur = await db.execute(sql, params)
        return await cur.fetchall()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

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
            # 过期：物理删除该行（自动清除），下次访问重拉
            await self._execute(
                "DELETE FROM tmdb_cache WHERE tmdb_id=? AND media_type=?",
                (tmdb_id, media_type),
            )
            return None
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

    async def mark_pushed(
        self,
        share_code: str,
        *,
        provider: str = "115",
        password: str | None = None,
        chat_id: str = "",
        message_id: int | None = None,
        title: str = "",
    ) -> None:
        """标记已推送（upsert）：重推时刷新引用信息并复位 status='ok'。

        - password/chat_id/message_id：巡检（check_share_status）与撤卡（delete_message）用
        - title：失效告警文案用
        """
        await self._execute(
            "INSERT INTO pushed_shares "
            "(share_code, pushed_at, provider, password, chat_id, message_id, title) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(share_code) DO UPDATE SET "
            "pushed_at=excluded.pushed_at, provider=excluded.provider, "
            "password=excluded.password, chat_id=excluded.chat_id, "
            "message_id=excluded.message_id, title=excluded.title, "
            "status='ok', last_checked_at=NULL",
            (share_code, time.time(), provider, password or "", chat_id, message_id, title),
        )

    # ------------------------------------------------------------------ #
    # 分享失效巡检（借 P115-Share：定期检查已推送卡片，失效撤卡/告警）
    # ------------------------------------------------------------------ #
    async def list_pushed_shares(
        self, *, provider: str = "115", limit: int = 100
    ) -> list[dict]:
        """巡检候选：status='ok' 的指定 provider 分享，最久未检查的优先。

        ed2k 双重排除（code 是完整 ed2k:// URL，非 115 分享码，无失效概念）：
        - provider 不符的行（新数据显式存 'ed2k'）
        - 旧数据（迁移默认 provider='115' 但 code 是 ed2k:// 开头）
        """
        cursor = await self._execute(
            "SELECT share_code, password, chat_id, message_id, title, "
            "last_checked_at, pushed_at FROM pushed_shares "
            "WHERE provider=? AND status='ok' AND share_code NOT LIKE 'ed2k://%' "
            "ORDER BY (last_checked_at IS NULL) DESC, last_checked_at ASC "
            "LIMIT ?",
            (provider, limit),
        )
        rows = await cursor.fetchall()
        keys = (
            "share_code", "password", "chat_id", "message_id",
            "title", "last_checked_at", "pushed_at",
        )
        return [dict(zip(keys, r)) for r in rows]

    async def touch_checked(self, share_code: str) -> None:
        """记录检查时间（活着/暂不可判均记录，减少重复巡检）。"""
        await self._execute(
            "UPDATE pushed_shares SET last_checked_at=? WHERE share_code=?",
            (time.time(), share_code),
        )

    async def mark_dead(self, share_code: str) -> None:
        """标记失效（巡检确认死亡后；撤卡完成后由巡检器调用）。"""
        await self._execute(
            "UPDATE pushed_shares SET status='dead', last_checked_at=? "
            "WHERE share_code=?",
            (time.time(), share_code),
        )

    # ------------------------------------------------------------------ #
    # 目录监控 → 自动建分享（/dir add 管理，watcher 消费）
    # ------------------------------------------------------------------ #
    async def add_share_dir(self, path: str, cid: int) -> None:
        """添加（或刷新）监控目录：路径唯一，已存在则更新 cid。"""
        await self._execute(
            "INSERT INTO share_dirs (path, cid, created_at) VALUES (?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET cid=excluded.cid",
            (path, cid, time.time()),
        )

    async def remove_share_dir(self, path: str) -> int:
        """移除监控目录（连同其已分享记录）。返回删除的行数。"""
        row = await self._fetchone(
            "SELECT id FROM share_dirs WHERE path=?", (path,)
        )
        if not row:
            return 0
        dir_id = int(row[0])
        await self._execute("DELETE FROM shared_items WHERE dir_id=?", (dir_id,))
        cur = await self._execute("DELETE FROM share_dirs WHERE id=?", (dir_id,))
        return cur.rowcount

    async def list_share_dirs(self) -> list[dict]:
        rows = await self._fetchall(
            "SELECT d.id, d.path, d.cid, "
            "(SELECT COUNT(*) FROM shared_items s WHERE s.dir_id = d.id) AS shared "
            "FROM share_dirs d ORDER BY d.id"
        )
        keys = ("id", "path", "cid", "shared")
        return [dict(zip(keys, r)) for r in rows]

    async def is_shared(self, dir_id: int, file_id: int) -> bool:
        row = await self._fetchone(
            "SELECT 1 FROM shared_items WHERE dir_id=? AND file_id=?",
            (dir_id, file_id),
        )
        return row is not None

    async def mark_shared(
        self, dir_id: int, file_id: int, name: str, share_code: str
    ) -> None:
        """记录已建分享推送（失败不标记 → 下轮重扫自动重试）。"""
        await self._execute(
            "INSERT OR REPLACE INTO shared_items "
            "(file_id, dir_id, name, share_code, pushed_at) VALUES (?,?,?,?,?)",
            (file_id, dir_id, name, share_code, time.time()),
        )
