"""集中式日志配置。

设计目标：让 `docker compose logs` 能读出一个"故事"，排障时文件日志有全量细节。

- 双通道分级：控制台 INFO（简洁故事线），文件 DEBUG（全量细节）
- 单一 stdout handler（Docker 友好，日志走 Docker json-file 驱动）
- 双文件分流：mediapush.log（核心系统）+ media.log（本地媒体流水线 app.media.*），
  两文件互不重复（按 logger 名过滤）
- 文件按固定字节轮转（默认 5MB/文件，LOG_MAX_BYTES 可配），旧文件 gzip 压缩
- 归档保留 7 天（LOG_RETENTION_DAYS 可配，按 mtime 到期即删，不受份数限制）
- 彩色级别 + 缩短模块名（app.telegram.handlers → handlers），便于扫读
- 第三方噪声库（telegram / httpx / urllib3 / asyncio）降级到 WARNING（两通道均抑制）
- LOG_LEVEL 控制控制台级别；文件恒为 DEBUG
- 处理链路 trace_id：入口处 trace_id(tid) 上下文内所有日志自动带 [tid=xxx]，
  批处理/巡检/监控多条链路交错时 grep tid 一步拉出全链路
- 敏感信息兜底脱敏：password=xxx / 访问码：xxx / cookie 等打码后再进任何通道

格式：2026-07-28 12:33:14 INFO  [handlers] [tid=abc12345] 消息内容
（无 tid 时省略该段）
"""

from __future__ import annotations

import gzip
import logging
import logging.handlers
import os
import re
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

# 日志时间戳固定用中国时区（容器 TZ 与本机无关，保证可读）
_SHANGHAI = ZoneInfo("Asia/Shanghai")

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
    "telethon": logging.WARNING,      # "Got difference for channel ..." 心跳刷屏
    "telethon.network.mtprotosender": logging.WARNING,
}

# ---------------------------------------------------------------------- #
# 处理链路 trace_id（contextvars 实现：异步任务链内自动传播，零函数签名改动）
# ---------------------------------------------------------------------- #
_trace_id_var: ContextVar[str] = ContextVar("mediapush_trace_id", default="")


@contextmanager
def trace_id(tid: str):
    """处理链路上下文：with 内所有日志自动附加 [tid=tid]。

    ContextVar 按 asyncio task 复制传播：入口 handler 设置后，
    同一处理链（processor/pusher/tmdb …）内的日志全部携带，
    并发链路互不串扰；with 结束自动恢复。
    """
    token = _trace_id_var.set(tid)
    try:
        yield
    finally:
        _trace_id_var.reset(token)


def make_trace_id(parsed) -> str:
    """从链接派生可 grep 的短标识：115 用分享码前缀，ed2k 用文件 hash 前缀。

    duck typing（provider/code 属性），不引入业务模块依赖。
    """
    code = getattr(parsed, "code", "") or ""
    provider = getattr(parsed, "provider", "") or ""
    if provider == "ed2k":
        # ed2k://|file|片名|大小|hash|/ → 取 hash 前 8 位
        parts = code.split("|")
        if len(parts) > 4 and parts[4]:
            return f"e{parts[4][:8]}"
        return "ed2k"
    return code[:8] or "share"


# ---------------------------------------------------------------------- #
# 敏感信息兜底脱敏：防访问码/cookie/密钥意外落入日志（文件留存多份压缩归档）
# ---------------------------------------------------------------------- #
_SENSITIVE_RE = re.compile(
    r"(?i)(password|passwd|passcode|token|api[_-]?key|secret|cookie"
    r"|访问码|提取码|密码|receive[_-]?code)"
    r"([=:：]\s*)[^\s，,]+"
)


class _SanitizeFilter(logging.Filter):
    """打码兜底：消息含 password=xxx / 访问码：xxx / cookie: xxx 时掩去值。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - args 异常时原样放行
            return True
        if not _SENSITIVE_RE.search(msg):
            return True
        record.msg = _SENSITIVE_RE.sub(r"\1\2***", msg)
        record.args = ()
        return True


def _is_media_logger(name: str) -> bool:
    """本地媒体流水线模块：pipeline.*（统一流水线）与 media.*/ed2k.*（历史路由兼容）。"""
    return (
        name == "app.pipeline" or name.startswith("app.pipeline.")
        or name == "app.media" or name.startswith("app.media.")
        or name == "app.ed2k" or name.startswith("app.ed2k.")
    )


class _MediaLogFilter(logging.Filter):
    """只放行本地媒体流水线（app.media.* / app.ed2k.*）日志 → media.log。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return _is_media_logger(record.name or "")


class _ExcludeMediaFilter(logging.Filter):
    """排除本地媒体流水线日志 → 核心日志只记系统内容，双文件互不重复。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return not _is_media_logger(record.name or "")


def set_console_level(level: str) -> bool:
    """运行时调整控制台 handler 级别（/loglevel 命令）。

    文件通道恒为 DEBUG 不动。级别无效或 handler 未找到返回 False。
    """
    try:
        lvl = logging._checkLevel(level.upper())
    except (ValueError, TypeError):
        return False
    for h in logging.getLogger().handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            h.setLevel(lvl)
            return True
    return False


class _ConsoleFormatter(logging.Formatter):
    """彩色控制台格式：时间 级别 [模块] [tid] 消息。模块名取末段，tid 可选。"""

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
        tid = _trace_id_var.get()
        if tid:
            return f"{ts} {tag} [{short}] [tid={tid}] {msg}"
        return f"{ts} {tag} [{short}] {msg}"


class _CompressedRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler + gzip 压缩 + 按时间清理归档。

    覆写 doRollover：先把已有 .N.gz 滚动（.N → .N+1），再把当前文件
    gzip 成 .1.gz；backupCount 为份数硬上限，另按 mtime 清理超过
    retention_days 的过期归档（时间优先，保证"保留 7 天"语义）。
    """

    def __init__(
        self,
        filename: str,
        *,
        max_bytes: int = 5 * 1024 * 1024,
        backup_count: int = 200,
        retention_days: float = 7.0,
        encoding: str = "utf-8",
    ) -> None:
        super().__init__(
            filename, maxBytes=max_bytes, backupCount=backup_count, encoding=encoding
        )
        self.retention_days = max(0.0, retention_days)

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None
        if self.backupCount > 0:
            # 滚动已有压缩文件：.(N-1).gz → .N.gz，最老的丢弃
            for i in range(self.backupCount - 1, 0, -1):
                sfn = f"{self.baseFilename}.{i}.gz"
                dfn = f"{self.baseFilename}.{i + 1}.gz"
                if os.path.exists(sfn):
                    if os.path.exists(dfn):
                        os.remove(dfn)
                    os.rename(sfn, dfn)
            # 压缩当前文件 → .1.gz（失败则保留原文件，下一轮再试）
            dfn = f"{self.baseFilename}.1.gz"
            if os.path.exists(dfn):
                os.remove(dfn)
            try:
                with open(self.baseFilename, "rb") as f_in, gzip.open(dfn, "wb") as f_out:
                    f_out.writelines(f_in)
                os.remove(self.baseFilename)
            except Exception:  # noqa: BLE001,S110
                pass
        # 按保留天数清理过期归档（启动时也调一次，见 prune_archives）
        self._prune_expired_archives()
        if not self.delay:
            self.stream = self._open()

    def _prune_expired_archives(self) -> None:
        """删除 mtime 超过 retention_days 的归档（.N.gz 与旧时间戳命名均覆盖）。"""
        if self.retention_days <= 0:
            return
        cutoff = time.time() - self.retention_days * 86400
        base = Path(self.baseFilename)
        for p in base.parent.glob(base.name + ".*"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass

    def prune_archives(self) -> None:
        """启动时主动清理一次过期归档（无需等到首次轮转）。"""
        self._prune_expired_archives()


def setup_logging(
    level: str = "INFO",
    *,
    use_color: bool | None = None,
    log_file: str | None = None,
    log_media_file: str | None = None,
    log_max_bytes: int = 5 * 1024 * 1024,
    log_retention_days: float = 7.0,
) -> None:
    """配置根日志：控制台 INFO 级 + 双文件 DEBUG 级（按字节轮转 + gzip + 7 天保留）。

    Args:
        level: 控制台日志级别字符串（DEBUG/INFO/WARNING/...），来自 LOG_LEVEL。
        use_color: 是否启用 ANSI 彩色（stdout）。None=按 stderr 是否 tty 自动判断。
        log_file: 核心日志文件（系统内容，排除 app.media.*）。传 None 或空串则不写。
        log_media_file: 媒体流水线日志文件（只记 app.media.*）。传 None 或空串则不写。
        log_max_bytes: 单文件轮转阈值（字节，达到即刻轮转），来自 LOG_MAX_BYTES。
        log_retention_days: 归档保留天数（按 mtime 到期即删），来自 LOG_RETENTION_DAYS。
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

    # 1) stdout handler（彩色，Docker 走 json-file 驱动捕获，控制台双份内容都看）——故事线
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(level.upper())
    sh.setFormatter(_ConsoleFormatter(use_color=use_color))
    sh.addFilter(_SanitizeFilter())  # 兜底脱敏（handler 级：覆盖所有子 logger 记录）
    root.addHandler(sh)

    # 2) 核心文件 handler（系统内容，排除媒体流水线）——按字节轮转 + gzip + 时间保留
    if log_file:
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            fh = _CompressedRotatingFileHandler(
                log_file,
                max_bytes=log_max_bytes,
                retention_days=log_retention_days,
            )
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(_ConsoleFormatter(use_color=False))
            fh.addFilter(_SanitizeFilter())       # 兜底脱敏
            fh.addFilter(_ExcludeMediaFilter())   # 媒体流水线日志只进 media.log
            fh.prune_archives()                   # 启动清理一次过期归档
            root.addHandler(fh)
        except Exception as exc:  # noqa: BLE001 - 文件不可写不阻断启动
            # 退化：只 stdout，不写文件
            logging.getLogger(__name__).warning("本地日志文件不可写 %s：%s", log_file, exc)

    # 3) 媒体流水线文件 handler（只记 app.media.*）——同样按字节轮转 + 7 天保留
    if log_media_file:
        try:
            Path(log_media_file).parent.mkdir(parents=True, exist_ok=True)
            mfh = _CompressedRotatingFileHandler(
                log_media_file,
                max_bytes=log_max_bytes,
                retention_days=log_retention_days,
            )
            mfh.setLevel(logging.DEBUG)
            mfh.setFormatter(_ConsoleFormatter(use_color=False))
            mfh.addFilter(_SanitizeFilter())
            mfh.addFilter(_MediaLogFilter())      # 只放行 app.media.*
            mfh.prune_archives()
            root.addHandler(mfh)
        except Exception as exc:  # noqa: BLE001 - 文件不可写不阻断启动
            logging.getLogger(__name__).warning(
                "媒体日志文件不可写 %s：%s", log_media_file, exc
            )

    for name, lvl in _NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(lvl)


def purge_log_files(
    level: str,
    *,
    use_color: bool | None = None,
    log_file: str | None = None,
    log_media_file: str | None = None,
    log_max_bytes: int = 5 * 1024 * 1024,
    log_retention_days: float = 7.0,
) -> list[str]:
    """清空本地日志（当前文件 + 全部归档）并重建 handler，返回删除的文件路径。

    /reset 一键清空数据用：先摘除并关闭 root 上全部 handler（释放文件句柄），
    再删除两个日志文件及其 .N.gz 归档，最后按原参数重建日志。
    """
    root = logging.getLogger()
    handlers = list(root.handlers)
    for h in handlers:
        root.removeHandler(h)
        try:
            h.close()
        except Exception:  # noqa: BLE001,S110
            pass

    removed: list[str] = []
    for path in (log_file, log_media_file):
        if not path:
            continue
        base = Path(path)
        candidates = list(base.parent.glob(base.name + ".*")) if base.parent.exists() else []
        if base.exists():
            candidates.append(base)
        for p in candidates:
            try:
                p.unlink()
                removed.append(str(p))
            except OSError:
                pass

    setup_logging(
        level,
        use_color=use_color,
        log_file=log_file,
        log_media_file=log_media_file,
        log_max_bytes=log_max_bytes,
        log_retention_days=log_retention_days,
    )
    return removed
