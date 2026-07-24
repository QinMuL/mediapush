"""日志配置：文件 + 控制台双输出，内存环形缓冲供 Web 日志页读取。

Web 日志页通过 memory_handler.recent() 取最近 N 条（决策记录第 5 条）。
"""
import logging
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path


class MemoryLogHandler(logging.Handler):
    """内存环形缓冲，Web 日志页通过 recent() 读取最近 N 条。"""

    def __init__(self, capacity: int = 2000):
        super().__init__()
        self._buf: deque[str] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        self._buf.append(self.format(record))

    def recent(self, n: int = 200) -> list[str]:
        items = list(self._buf)
        if n <= 0:
            return items
        return items[-n:]


memory_handler = MemoryLogHandler()


def setup_logging(log_file: str = "data/mediapush.log", level: str = "INFO") -> None:
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    console = logging.StreamHandler()
    console.setFormatter(fmt)

    memory_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console)
    root.addHandler(memory_handler)
