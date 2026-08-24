"""监控配置持久化（SQLite，独立 monitor.db）。

表结构：
- mon_channels：监控频道（chat_id 为 Telethon marked id，如 -100xxxx）
- mon_settings：KV 配置（推送目标 target / 聚合窗口 batch 秒）
- mon_filters ：关键词过滤规则（include 命中才推 / exclude 命中不推）
- mon_seen    ：已推送 ed2k 去重（md5(链接)），启动时按 TTL 清理防膨胀

沿用 Cache 的连接约定：长连接 + 启动建表一次，懒连接，close() 清理。
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

KIND_INCLUDE = "include"  # 命中任一 include 才推送（配置了 include 时生效）
KIND_EXCLUDE = "exclude"  # 命中任一 exclude 即丢弃
VALID_KINDS = {KIND_INCLUDE, KIND_EXCLUDE}

# settings KV 键名
KEY_TARGET = "target"  # 推送目标频道（chat_id 或 @username）
KEY_BATCH = "batch"  # 聚合窗口（秒），0 = 实时逐条

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mon_channels (
    chat_id     INTEGER PRIMARY KEY,
    title       TEXT    NOT NULL DEFAULT '',
    username    TEXT    NOT NULL DEFAULT '',
    added_at    REAL    NOT NULL,
    last_msg_id INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS mon_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mon_filters (
    keyword  TEXT PRIMARY KEY,
    kind     TEXT NOT NULL,
    added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS mon_seen (
    link_hash  TEXT PRIMARY KEY,
    first_seen REAL NOT NULL
);
"""


def link_hash(link: str) -> str:
    """ed2k 链接去重键：md5（链接可能很长，hash 让索引保持定长）。"""
    return hashlib.md5(link.encode("utf-8")).hexdigest()


@dataclass
class MonChannel:
    chat_id: int  # Telethon marked id（event.chat_id 同形式）
    title: str
    username: str
    added_at: float
    last_msg_id: int


@dataclass
class FilterRule:
    keyword: str
    kind: str  # include | exclude


class MonitorStore:
    def __init__(self, db_path: str = "./data/monitor.db") -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # 连接管理（同 Cache：懒连接 + 建表一次）
    # ------------------------------------------------------------------ #
    async def connect(self) -> None:
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

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
        return list(await cur.fetchall())

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # ------------------------------------------------------------------ #
    # 监控频道
    # ------------------------------------------------------------------ #
    async def add_channel(self, chat_id: int, title: str, username: str = "") -> bool:
        """新增监控频道；已存在返回 False（幂等）。"""
        cur = await self._execute(
            "INSERT OR IGNORE INTO mon_channels (chat_id, title, username, added_at) "
            "VALUES (?,?,?,?)",
            (chat_id, title, username, time.time()),
        )
        if cur.rowcount == 0:
            # 已存在：刷新 title（频道可能改名）；username 仅在新值非空时覆盖（保留解析回退）
            await self._execute(
                "UPDATE mon_channels SET title=?, "
                "username=CASE WHEN ?='' THEN username ELSE ? END WHERE chat_id=?",
                (title, username, username, chat_id),
            )
            return False
        return True

    async def remove_channel(self, chat_id: int) -> bool:
        cur = await self._execute(
            "DELETE FROM mon_channels WHERE chat_id=?", (chat_id,)
        )
        return cur.rowcount > 0

    async def list_channels(self) -> list[MonChannel]:
        rows = await self._fetchall(
            "SELECT chat_id, title, username, added_at, last_msg_id "
            "FROM mon_channels ORDER BY added_at"
        )
        return [
            MonChannel(chat_id=r[0], title=r[1], username=r[2], added_at=r[3], last_msg_id=r[4])
            for r in rows
        ]

    async def set_last_msg_id(self, chat_id: int, msg_id: int) -> None:
        """推进频道已处理水位（只增不减，防乱序回退）。"""
        await self._execute(
            "UPDATE mon_channels SET last_msg_id=MAX(last_msg_id, ?) WHERE chat_id=?",
            (msg_id, chat_id),
        )

    # ------------------------------------------------------------------ #
    # KV 设置（推送目标 / 聚合窗口）
    # ------------------------------------------------------------------ #
    async def get_setting(self, key: str) -> str | None:
        row = await self._fetchone("SELECT value FROM mon_settings WHERE key=?", (key,))
        return row[0] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self._execute(
            "INSERT INTO mon_settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ------------------------------------------------------------------ #
    # 关键词过滤规则
    # ------------------------------------------------------------------ #
    async def add_filter(self, keyword: str, kind: str) -> bool:
        """新增过滤规则；关键词已存在（任意类型）返回 False。"""
        if kind not in VALID_KINDS:
            raise ValueError(f"非法过滤类型：{kind}")
        keyword = keyword.strip()
        if not keyword:
            return False
        cur = await self._execute(
            "INSERT OR IGNORE INTO mon_filters (keyword, kind, added_at) VALUES (?,?,?)",
            (keyword, kind, time.time()),
        )
        return cur.rowcount > 0

    async def remove_filter(self, keyword: str) -> bool:
        cur = await self._execute("DELETE FROM mon_filters WHERE keyword=?", (keyword,))
        return cur.rowcount > 0

    async def list_filters(self) -> list[FilterRule]:
        rows = await self._fetchall(
            "SELECT keyword, kind FROM mon_filters ORDER BY added_at"
        )
        return [FilterRule(keyword=r[0], kind=r[1]) for r in rows]

    # ------------------------------------------------------------------ #
    # 已推送 ed2k 去重
    # ------------------------------------------------------------------ #
    async def is_seen(self, hash_: str) -> bool:
        row = await self._fetchone("SELECT 1 FROM mon_seen WHERE link_hash=?", (hash_,))
        return row is not None

    async def mark_seen(self, hashes: list[str]) -> None:
        """批量标记已推送（推送成功后调用；失败不标记，下次出现自动重试）。"""
        if not hashes:
            return
        now = time.time()
        await self._execute(
            "INSERT OR IGNORE INTO mon_seen (link_hash, first_seen) VALUES "
            + ",".join(["(?,?)"] * len(hashes)),
            tuple(v for h in hashes for v in (h, now)),
        )

    async def cleanup_seen(self, days: int = 30) -> int:
        """清理 N 天前的去重记录（防表无限膨胀），返回删除行数。"""
        cur = await self._execute(
            "DELETE FROM mon_seen WHERE first_seen < ?", (time.time() - days * 86400,)
        )
        return cur.rowcount
