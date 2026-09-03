"""ed2k 根哈希（MD4 分块 Merkle）+ 纯 Python MD4（RFC 1320）。

OpenSSL 3 默认弃用 MD4（Python 3.10+ 在 Ubuntu 22.04 / Win 3.14 已不可用），
这里直接提供 RFC 1320 的参考实现，单文件 < 150 行，不用额外依赖。

ed2k 算法规范：
- 分块大小 ED2K_CHUNK = 9728000 字节（9.28 MB，eMule 标准）
- 每块 MD4；多个块 → 把所有块哈希拼起来再 MD4 取根
- 单块文件：root = 该块的 MD4（不再包一层）
- 空文件：root = 空字节的 MD4（与 eMule 行为一致）
- 链接格式：ed2k://|file|<文件名>|<大小>|<hash_hex>|/
"""

from __future__ import annotations

import asyncio
import logging
import struct
from pathlib import Path

logger = logging.getLogger(__name__)

ED2K_CHUNK = 9_728_000
# 流式读取缓冲（比块小，减少内存峰值）
_READ_BUF = 32 * 1024 * 1024

# ---------------------------------------------------------------------- #
# MD4：优先使用 pycryptodome 的 C 实现（~300-800 MB/s，2.4GB ≤ 10s）
# 没装再 fallback 到下面纯 Python RFC 1320 参考实现（~3 MB/s，仅供兼容）。
# ---------------------------------------------------------------------- #
try:  # pragma: no cover - 环境中只要装了 pycryptodome 就会走这条
    from Crypto.Hash import MD4 as _MD4  # type: ignore[import-not-found]

    def md4_digest(data: bytes) -> bytes:
        """MD4（C 实现，优先）。"""
        return _MD4.new(data).digest()

    _MD4_FAST = True
except Exception:  # noqa: BLE001 - pragma: no cover - 纯 Python fallback
    _MD4_FAST = False


# ---------------------------------------------------------------------- #
# 纯 Python MD4（RFC 1320）—— pycryptodome 未安装时的 fallback。
# 参考：https://www.rfc-editor.org/rfc/rfc1320 / amule/CryptoPP 向量验证。
# ---------------------------------------------------------------------- #
if not _MD4_FAST:  # pragma: no cover - 仅在没有 Crypto 时执行
    _MASK32 = 0xFFFFFFFF

    def _lrot(x: int, n: int) -> int:
        return ((x << n) | (x >> (32 - n))) & _MASK32

    def _f(x, y, z): return (x & y) | (~x & z)
    def _g(x, y, z): return (x & y) | (x & z) | (y & z)
    def _h(x, y, z): return x ^ y ^ z

    def _md4_compress(state: list[int], block: bytes) -> None:
        a, b, c, d = state
        x = list(struct.unpack("<16I", block))
        # Round 1
        for i, s in [(0, 3), (1, 7), (2, 11), (3, 19),
                     (4, 3), (5, 7), (6, 11), (7, 19),
                     (8, 3), (9, 7), (10, 11), (11, 19),
                     (12, 3), (13, 7), (14, 11), (15, 19)]:
            a = _lrot((a + _f(b, c, d) + x[i]) & _MASK32, s)
            a, d, c, b = d, c, b, a
        # Round 2
        for i, s in [(0, 3), (4, 5), (8, 9), (12, 13),
                     (1, 3), (5, 5), (9, 9), (13, 13),
                     (2, 3), (6, 5), (10, 9), (14, 13),
                     (3, 3), (7, 5), (11, 9), (15, 13)]:
            a = _lrot((a + _g(b, c, d) + x[i] + 0x5A827999) & _MASK32, s)
            a, d, c, b = d, c, b, a
        # Round 3
        for i, s in [(0, 3), (8, 9), (4, 11), (12, 15),
                     (2, 3), (10, 9), (6, 11), (14, 15),
                     (1, 3), (9, 9), (5, 11), (13, 15),
                     (3, 3), (11, 9), (7, 11), (15, 15)]:
            a = _lrot((a + _h(b, c, d) + x[i] + 0x6ED9EBA1) & _MASK32, s)
            a, d, c, b = d, c, b, a
        state[0] = (state[0] + a) & _MASK32
        state[1] = (state[1] + b) & _MASK32
        state[2] = (state[2] + c) & _MASK32
        state[3] = (state[3] + d) & _MASK32

    def md4_digest(data: bytes) -> bytes:
        """MD4 摘要（字节串，16 字节）。"""
        state = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476]
        size = len(data)
        pad = b"\x80" + b"\x00" * ((55 - size % 64) % 64) + struct.pack("<Q", size * 8)
        msg = data + pad
        for off in range(0, len(msg), 64):
            _md4_compress(state, msg[off:off + 64])
        return struct.pack("<4I", *state)


# ---------------------------------------------------------------------- #
# ed2k 根哈希：流式分块 MD4
# ---------------------------------------------------------------------- #
def _ed2k_hash_file_sync(path: Path, chunk_size: int, read_buf: int) -> tuple[bytes, int]:
    """同步实现（大块顺序 I/O，C 级 MD4 = 数百 MB/s）。跑在 executor 里供 async 包装。"""
    size = path.stat().st_size
    if size == 0:
        return md4_digest(b""), 0
    chunks: list[bytes] = []
    with path.open("rb") as f:
        pending = bytearray()
        while True:
            buf = f.read(read_buf)
            if not buf:
                break
            pending.extend(buf)
            while len(pending) >= chunk_size:
                chunks.append(md4_digest(bytes(pending[:chunk_size])))
                del pending[:chunk_size]
        if pending:
            chunks.append(md4_digest(bytes(pending)))
    if not chunks:
        return md4_digest(b""), 0
    if len(chunks) == 1:
        return chunks[0], size
    return md4_digest(b"".join(chunks)), size


async def ed2k_hash_file(path: str | Path, chunk_size: int = ED2K_CHUNK) -> tuple[bytes, int]:
    """对文件做 ed2k 分块哈希。返回 (root_hash_bytes, size_bytes)。

    核心 I/O + MD4 全部丢到线程池跑同步版（避免逐次 executor 调度的巨大开销），
    2.4GB 文件在 C 级 MD4 下 ≈ 5-10s 完成。
    """
    p = Path(path)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _ed2k_hash_file_sync, p, chunk_size, _READ_BUF)


def ed2k_uri(file_name: str, size_bytes: int, root_hash_hex: str) -> str:
    """生成 ed2k 链接。"""
    return f"ed2k://|file|{file_name}|{size_bytes}|{root_hash_hex.lower()}|/"
