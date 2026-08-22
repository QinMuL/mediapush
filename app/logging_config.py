"""集中式日志配置。

设计目标：让 `docker compose logs` 能读出一个"故事"，排障时文件日志有全量细节。

- 双通道分级：控制台 INFO（简洁故事线），文件 DEBUG（全量细节）
- 单一 stdout handler（Docker 友好，日志走 Docker json-file 驱动）
- 文件按天轮转（TimedRotatingFileHandler），保留 14 天，自动清理过期文件
- 彩色级别 + 缩短模块名（app.telegram.handlers → handlers），便于扫读
- 第三方噪声库（telegram / httpx / urllib3 / asyncio）降级到 WARNING（两通道均抑制）
- LOG_LEVEL 控制控制台级别；文件恒为 DEBUG

格式：2026-07-28 12:33:14 INFO  [handlers] 消息内容
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

# 日志时间戳固定用中国时区（容器 TZ 与本机无关，保证可读）
_SHANGHAI = ZoneInfo("Asia/Shanghai")

# 本地文件轮转：按天（午夜切分），保留 14 天
_FILE_BACKUP_DAYS = 14

# ANSI 颜色
_RESET = "\x1b[0m"
_COLORS = {
    logging.DEBUG: "\x1b[36m",      # cyan
    logging.INFO: "\x1b[32m",       # green
    logging.WARNING: "\x1b[33m",   # yellow
    logging.ERROR: "\x1b[31m",     # red
    logging.CRITICAL: "\x1b[35m",  # magenta
}
# 固定宽度级别标签（5 字符），对齐整齐
_LEVEL_TAG = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO ",
    logging.WARNING: "WARN ",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "FATAL",
}

# 噪声库降级（PTB/httpx 等默认 INFO 太啰嗦；httpcore 为 httpx 底层，DEBUG 刷屏）
_NOISY_LOGGERS: dict[str, int] = {
    "telegram": logging.WARNING,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "urllib3": logging.WARNING,
    "asyncio": logging.WARNING,
    "p115client": logging.WARNING,
}


class _ConsoleFormatter(logging.Formatter):
    """彩色控制台格式：时间 级别 [模块] 消息。模块名取末段。"""

    def __init__(self, *, use_color: bool = True) -> None:
        super().__init__()
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        short = record.name.rsplit(".", 1)[-1] if record.name else "root"
        tag = _LEVEL_TAG.get(record.levelno, record.levelname)
        if self.use_color:
            color = _COLORS.get(record.levelno, "")
            tag = f"{color}{tag}{_RESET}" if color else tag
        msg = record.getMessage()
        if record.exc_info:
            msg = msg + "\n" + self.formatException(record.exc_info)
        ts = datetime.fromtimestamp(record.created, tz=_SHANGHAI).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        return f"{ts} {tag} [{short}] {msg}"


def setup_logging(
    level: str = "INFO",
    *,
    use_color: bool | None = None,
    log_file: str | None = None,
) -> None:
    """配置根日志：控制台 INFO 级 + 文件 DEBUG 级（按天轮转）。

    Args:
        level: 控制台日志级别字符串（DEBUG/INFO/WARNING/...），来自 LOG_LEVEL。
        use_color: 是否启用 ANSI 彩色（stdout）。None=按 stderr 是否 tty 自动判断。
        log_file: 本地日志文件路径，启用文件持久化 + 按天轮转（保留 14 天）。
                  传 None 或空串则只输出 stdout。来自 LOG_FILE。
    """
    root = logging.getLogger()
    # 清理已有 handler，避免容器重启/重复调用时叠加
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:  # noqa: BLE001,S110 - 清理旧 handler 失败可忽略
            pass

    if use_color is None:
        use_color = sys.stderr.isatty()

    # root 放开到 DEBUG，由各 handler 自行过滤（控制台 level / 文件 DEBUG）
    root.setLevel(logging.DEBUG)

    # 1) stdout handler（彩色，Docker 走 json-file 驱动捕获）——故事线
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(level.upper())
    sh.setFormatter(_ConsoleFormatter(use_color=use_color))
    root.addHandler(sh)

    # 2) 本地文件 handler（纯文本无颜色，按天轮转，持久化到挂载卷）——全量细节
    if log_file:
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            fh = TimedRotatingFileHandler(
                log_file,
                when="midnight",           # 每天午夜切分（容器 TZ）
                backupCount=_FILE_BACKUP_DAYS,  # 保留 14 天，过期自动删除
                encoding="utf-8",
            )
            fh.suffix = "%Y-%m-%d"         # 备份文件名：mediapush.log.2026-08-22
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(_ConsoleFormatter(use_color=False))
            root.addHandler(fh)
        except Exception as exc:  # noqa: BLE001 - 文件不可写不阻断启动
            # 退化：只 stdout，不写文件
            logging.getLogger(__name__).warning("本地日志文件不可写 %s：%s", log_file, exc)

    for name, lvl in _NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(lvl)
