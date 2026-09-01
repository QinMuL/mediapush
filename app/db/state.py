"""统一服务状态存储：data/state.db 单表，取代散落各处的 JSON 状态文件。

设计：
- 一行一服务：state(service TEXT PRIMARY KEY, payload TEXT, updated_at REAL)
- payload 为该服务完整状态 dict 的 JSON 序列化（结构与旧 JSON 文件一致，零语义变化）
- 每次操作短连接（状态读写频率低、体积小；避免长连接的跨线程与文件删除锁问题）
- 旧 JSON 状态文件首次加载时自动迁移入库并改名 .migrated（load_with_legacy）

已接入服务（service 键）：
- local_media  ← data/local_media_state.json（低置信退避状态）
- ed2k         ← data/ed2k_state.json（哈希失败退避状态）
- ed2k_push    ← data/ed2k_push_state.json（JSONL offset + 推送退避状态）
- cd2          ← data/cd2_state.json（completed + 上传退避状态）
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS state (
    service    TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


class StateStore:
    """SQLite 键值状态存储：一行一服务，payload 为整份状态 dict。"""

    def __init__(self, db_path: str | Path = "./data/state.db") -> None:
        self.db_path = str(db_path)

    # ------------------------------------------------------------------ #
    def _connect(self) -> sqlite3.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.executescript(_SCHEMA)
        return conn

    def load(self, service: str) -> dict | None:
        """读取服务状态；无记录返回 None，损坏时按无记录处理（不抛错）。"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT payload FROM state WHERE service = ?", (service,)
            ).fetchone()
            if row is None:
                return None
            data = json.loads(row[0])
            return data if isinstance(data, dict) else None
        except (sqlite3.Error, ValueError) as exc:
            logger.warning("状态加载失败（service=%s，按空启动）：%s", service, exc)
            return None
        finally:
            conn.close()

    def save(self, service: str, data: dict) -> bool:
        """整份覆盖保存；失败只记日志不抛错（不阻断服务主链路）。"""
        try:
            payload = json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            logger.warning("状态序列化失败（service=%s）：%s", service, exc)
            return False
        conn = self._connect()
        try:
            with conn:  # 事务提交
                conn.execute(
                    "INSERT INTO state (service, payload, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(service) DO UPDATE SET "
                    "payload = excluded.payload, updated_at = excluded.updated_at",
                    (service, payload, time.time()),
                )
            return True
        except sqlite3.Error as exc:
            logger.warning("状态保存失败（service=%s）：%s", service, exc)
            return False
        finally:
            conn.close()

    def clear(self, service: str | None = None) -> None:
        """清空指定服务（或全部）的状态行。"""
        conn = self._connect()
        try:
            with conn:
                if service is None:
                    conn.execute("DELETE FROM state")
                else:
                    conn.execute("DELETE FROM state WHERE service = ?", (service,))
        except sqlite3.Error as exc:
            logger.warning("状态清空失败（service=%s）：%s", service or "*", exc)
        finally:
            conn.close()

    def services(self) -> list[str]:
        """已登记的服务键列表（/reset 结果展示用）。"""
        conn = self._connect()
        try:
            rows = conn.execute("SELECT service FROM state").fetchall()
            return [r[0] for r in rows]
        except sqlite3.Error:
            return []
        finally:
            conn.close()


def load_with_legacy(store: StateStore, service: str, legacy_path: str | Path) -> dict:
    """读服务状态；DB 无记录且旧 JSON 文件存在 → 导入迁移并改名 .migrated。"""
    data = store.load(service)
    if data is not None:
        return data
    p = Path(legacy_path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            store.save(service, raw)
            p.rename(p.with_name(p.name + ".migrated"))
            logger.info("旧状态文件已迁移入库：%s（service=%s）", p, service)
            return raw
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        logger.warning("旧状态文件迁移失败（%s）：%s", p, exc)
    return {}
