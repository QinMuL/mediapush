"""ed2k 哈希单测（纯 MD4 向量，不依赖网络/TMDB）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.ed2k.hasher import ED2K_CHUNK, ed2k_hash_file, ed2k_uri, md4_digest


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
