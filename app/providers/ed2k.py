"""ed2k (eMule/eDonkey) 单文件链接 provider。

ed2k 链接形态：ed2k://|file|<文件名>|<字节数>|<hash>|...|/
- 无网络请求：文件名/大小/hash 全部内嵌在链接里
- 无访问码：链接本身即资源标识
- 单文件：list_share 返回单个 ShareFile，直接喂给 analyze_share
"""

from __future__ import annotations

import re

from app.providers.base import BaseShareProvider, ShareFile
from app.providers.exceptions import Pan115Error

# 复用 link_parser 的正则形态；独立编译避免循环导入
_ED2K_RE = re.compile(
    r"ed2k://\|file\|([^|]+)\|(\d+)\|([0-9A-Fa-f]{32})\|[^ ]*?/",
    re.IGNORECASE,
)


class Ed2kProvider(BaseShareProvider):
    """ed2k 单文件链接解析（纯字符串，无网络）。"""

    name = "ed2k"

    async def list_share(self, code: str, password: str | None) -> list[ShareFile]:
        """code = 完整 ed2k URL；解析出文件名 + 大小，返回单元素列表。"""
        m = _ED2K_RE.search(code)
        if not m:
            raise Pan115Error("ed2k 链接格式无效", code=code)
        filename = m.group(1)
        size = int(m.group(2))
        return [ShareFile(name=filename, size=size, is_dir=False)]

    async def check_health(self) -> bool | None:
        """无需配置，恒可用。"""
        return None
