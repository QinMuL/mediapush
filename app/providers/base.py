"""Provider 抽象基类与数据结构。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ShareFile:
    """分享内的一个条目（文件或目录）。"""

    name: str
    size: int = 0
    is_dir: bool = False
    sha1: str | None = None

    @property
    def is_video(self) -> bool:
        return (not self.is_dir) and _has_video_ext(self.name)


_VIDEO_EXTS = {
    "mkv", "mp4", "avi", "rmvb", "ts", "m2ts", "mov", "wmv", "flv", "iso", "mpg", "mpeg",
}


def _has_video_ext(name: str) -> bool:
    dot = name.rfind(".")
    if dot < 0:
        return False
    return name[dot + 1:].lower() in _VIDEO_EXTS


class BaseShareProvider(ABC):
    """网盘分享读取抽象。"""

    name: str = "base"

    @abstractmethod
    async def list_share(self, code: str, password: str | None) -> list[ShareFile]:
        """读取分享内容，返回扁平文件/目录列表。"""

    @abstractmethod
    async def check_health(self) -> bool | None:
        """健康检查。None=未配置（匿名可用）；True/False=已配置且校验结果。"""
