"""StateStore（data/state.db 统一状态存储）单测。"""
from __future__ import annotations

import json

from app.db.state import StateStore, load_with_legacy


def test_save_load_roundtrip(tmp_path):
    db = tmp_path / "state.db"
    store = StateStore(db)
    assert store.load("cd2") is None  # 无记录

    store.save("cd2", {"completed": ["/a.mkv"], "retry": {}})
    data = store.load("cd2")
    assert data == {"completed": ["/a.mkv"], "retry": {}}


def test_save_overwrites(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.save("ed2k", {"a": 1})
    store.save("ed2k", {"b": 2})
    assert store.load("ed2k") == {"b": 2}


def test_clear_scoped_and_all(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.save("cd2", {"x": 1})
    store.save("ed2k", {"y": 2})

    store.clear("cd2")
    assert store.load("cd2") is None
    assert store.load("ed2k") == {"y": 2}

    store.clear()
    assert store.load("ed2k") is None
    assert store.services() == []


def test_corrupt_payload_treated_as_missing(tmp_path):
    db = tmp_path / "state.db"
    store = StateStore(db)
    store.save("cd2", {"ok": True})
    # 直接写坏 payload
    import sqlite3

    conn = sqlite3.connect(str(db))
    with conn:
        conn.execute("UPDATE state SET payload = 'not-json' WHERE service = 'cd2'")
    conn.close()
    assert store.load("cd2") is None  # 损坏按空启动，不抛错


def test_load_with_legacy_migrates_json(tmp_path):
    legacy = tmp_path / "cd2_state.json"
    legacy.write_text(
        json.dumps({"completed": ["/old.mkv"]}), encoding="utf-8"
    )
    store = StateStore(tmp_path / "state.db")

    data = load_with_legacy(store, "cd2", legacy)
    assert data == {"completed": ["/old.mkv"]}
    assert store.load("cd2") == {"completed": ["/old.mkv"]}
    # 旧文件迁移后改名 .migrated（不再二次导入）
    assert not legacy.exists()
    assert legacy.with_name(legacy.name + ".migrated").exists()

    # 第二次：DB 已有记录 → 返回 DB 值（幂等）
    again = load_with_legacy(store, "cd2", legacy)
    assert again == {"completed": ["/old.mkv"]}


def test_load_with_legacy_no_file_returns_empty(tmp_path):
    store = StateStore(tmp_path / "state.db")
    data = load_with_legacy(store, "local_media", tmp_path / "nope.json")
    assert data == {}


def test_load_with_legacy_corrupt_file_returns_empty(tmp_path):
    legacy = tmp_path / "bad.json"
    legacy.write_text("{broken", encoding="utf-8")
    store = StateStore(tmp_path / "state.db")
    assert load_with_legacy(store, "ed2k", legacy) == {}
