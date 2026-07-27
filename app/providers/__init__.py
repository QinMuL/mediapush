"""网盘 Provider 抽象层。

新增网盘只需：
1. 继承 BaseShareProvider 实现 list_share / check_health
2. 在 core/link_parser.py 注册链接解析
3. 在 core/container.py 注册实例
上层（pipeline/telegram）零改动。
"""

from app.providers.base import BaseShareProvider, ShareFile
from app.providers.ed2k import Ed2kProvider
from app.providers.exceptions import Pan115Error
from app.providers.pan115 import Pan115Provider

__all__ = [
    "BaseShareProvider",
    "Ed2kProvider",
    "Pan115Error",
    "Pan115Provider",
    "ShareFile",
]
