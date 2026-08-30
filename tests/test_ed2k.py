"""ed2k 模块与 Ed2kService 单测（纯 MD4 向量 + tmp_path 集成冒烟）。

不依赖网络/真实 ffmpeg/TMDB。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.media.ed2k import ED2K_CHUNK, ed2k_hash_file, ed2k_uri, md4_digest
from app.media.ed2k_service import Ed2kReport, Ed2kService


# ---------------- MD4 ---------------- #
def test_md4_rfc():
    """RFC 1320 A.3 三组关键向量。"""
    assert md4_digest(b"").hex() == "31d6cfe0d16ae931b73c59d7e0c089c0"
    assert md4_digest(b"abc").hex() == "a448017aaf21d8525fc10ae87aa6729d"
    assert md4_digest(b"message digest").hex() == "d9130a8164549fe818874806e1c7014b"


# ---------------- ed2k 分块哈希 ---------------- #
def test_ed2k_empty_file(tmp_path: Path):
    f = tmp_path / "empty.mkv"
    f.write_bytes(b"")
    h, size = asyncio.run(ed2k_hash_file(f))
    assert size == 0
    assert h.hex() == md4_digest(b"").hex()


def test_ed2k_single_chunk(tmp_path: Path):
    """< 9728000 字节：单块 → 直接返回块哈希。"""
    f = tmp_path / "s.mkv"
    data = b"x" * 1_234_567
    f.write_bytes(data)
    h, size = asyncio.run(ed2k_hash_file(f))
    assert size == len(data)
    assert h == md4_digest(data)


def test_ed2k_multi_chunk(tmp_path: Path):
    """>= 2 块：块哈希拼接后 MD4。"""
    chunk = ED2K_CHUNK
    data = b"A" * chunk + b"B" * chunk + b"C" * 100
    f = tmp_path / "big.mkv"
    f.write_bytes(data)
    h, size = asyncio.run(ed2k_hash_file(f))
    assert size == len(data)
    expected = md4_digest(
        md4_digest(b"A" * chunk) + md4_digest(b"B" * chunk) + md4_digest(b"C" * 100)
    )
    assert h == expected


def test_ed2k_uri_format():
    assert ed2k_uri("show.mkv", 12345, "aBcDef0123456789") == (
        "ed2k://|file|show.mkv|12345|abcdef0123456789|/"
    )


# ---------------- Ed2kService 冒烟 ---------------- #
class _FakeSettings:
    ed2k_interval_seconds = 30.0
    ed2k_stable_rounds = 2
    ed2k_stuck_days = 7.0
    ed2k_dry_run = True
    ed2k_input_dir = ""
    ed2k_output_dir = ""


@pytest.fixture()
def dirs(tmp_path: Path):
    b, c = tmp_path / "B", tmp_path / "C"
    b.mkdir()
    c.mkdir()
    return b, c


def _mk_service(b: Path, c: Path) -> Ed2kService:
    st = _FakeSettings()
    st.ed2k_input_dir = str(b)
    st.ed2k_output_dir = str(c)
    svc = Ed2kService(st)
    svc.state_file = b.parent / "_state_test.json"
    svc.results_file = b.parent / "_results_test.jsonl"
    return svc


def test_service_dry_run_hashes_and_logs(dirs):
    b, c = dirs
    sub = b / "狂怒追缉 (2026)" / "S01"
    sub.mkdir(parents=True)
    video = sub / "狂怒追缉.2026.S01E04.第04集.2160p.WEB-DL.H.265.mkv"
    video.write_bytes(b"x" * 5_000_000)
    video.with_suffix(".srt").write_text("srt")
    video.with_suffix(".nfo").write_text("nfo")

    svc = _mk_service(b, c)
    r1 = asyncio.run(svc.run_once())  # 快照 1
    assert r1.scanned == 1 and r1.hashed == 0
    r2 = asyncio.run(svc.run_once())  # 稳定 → 哈希 → dry-run
    assert r2.hashed == 1 and r2.dry_moved == 1
    assert video.exists()
    assert not list(c.rglob("*.mkv"))
    import json
    lines = svc.results_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["name"] == video.name
    assert rec["size_bytes"] == 5_000_000
    assert rec["ed2k"].startswith("ed2k://|file|") and "|5000000|" in rec["ed2k"]


def test_service_real_move_structure(dirs):
    """关 dry-run：视频/字幕/nfo 保留相对路径移入C + 空目录清理。"""
    b, c = dirs
    sub = b / "狂怒追缉 (2026)" / "S01"
    sub.mkdir(parents=True)
    video = sub / "狂怒追缉.2026.S01E04.第04集.2160p.WEB-DL.H.265.mkv"
    video.write_bytes(b"y" * 2_000_000)
    srt = video.with_suffix(".srt")
    srt.write_text("srt")
    nfo = video.with_suffix(".nfo")
    nfo.write_text("nfo")

    svc = _mk_service(b, c)
    svc.settings.ed2k_dry_run = False
    asyncio.run(svc.run_once())
    asyncio.run(svc.run_once())

    dest = c / "狂怒追缉 (2026)" / "S01" / video.name
    assert dest.is_file()
    assert dest.with_suffix(".srt").is_file()
    assert dest.with_suffix(".nfo").is_file()
    assert not video.exists() and not srt.exists() and not nfo.exists()
    assert not sub.exists() and not (b / "狂怒追缉 (2026)").exists()


def test_service_conflict_no_overwrite(dirs):
    b, c = dirs
    video = b / "f.mkv"
    video.write_bytes(b"v")
    (c / "f.mkv").write_bytes(b"EXISTING")

    svc = _mk_service(b, c)
    svc.settings.ed2k_dry_run = False
    asyncio.run(svc.run_once())
    asyncio.run(svc.run_once())

    assert (c / "f.mkv").read_bytes() == b"EXISTING"
    assert video.exists()


def test_service_stability_requires_rounds(dirs):
    """需要 stable_rounds=2 轮连续相同 size/mtime 才能哈希。

    r1 首轮 → count=1；中途改 → 重置；r2 新快照 → count=1；
    r3 与 r2 快照一致 → count=2（达阈值 2）→ 哈希。
    """
    b, c = dirs
    video = b / "s.mkv"
    video.write_bytes(b"a" * 1_000_000)
    svc = _mk_service(b, c)

    r1 = asyncio.run(svc.run_once())
    video.write_bytes(b"a" * 1_000_000 + b"MORE")  # 中途改，重置计数
    r2 = asyncio.run(svc.run_once())
    assert r1.hashed == 0 and r2.hashed == 0
    r3 = asyncio.run(svc.run_once())
    assert r3.hashed == 1


def test_service_temp_files_skipped(dirs):
    b, c = dirs
    (b / "f.tmp").write_bytes(b"t")
    (b / "f.mkv.!qb").write_bytes(b"t")
    (b / "f.crdownload").write_bytes(b"t")
    svc = _mk_service(b, c)
    r = asyncio.run(svc.run_once())
    assert r.scanned == 0


def test_report_summary():
    r = Ed2kReport(scanned=5, stable=3, hashed=3, moved=4,
                   failed=1, conflict=1, stuck=1)
    s = r.summary()
    assert "哈希 3" in s and "移入C 4" in s and "失败退避 1" in s
    assert "同名跳过 1" in s and "卡死 1" in s
