"""程序入口：加载配置 → 构建容器 → 启动 Telegram Bot（run_polling）。"""

from __future__ import annotations

import logging
import os

from app.config import Settings
from app.core.container import Container
from app.logging_config import setup_logging

# 启动时清除进程级代理环境变量（借 P115-Share）：docker-compose 注入的
# HTTP_PROXY 等会被 httpx/aiohttp 自动读取，导致 115（须直连防风控）与
# TMDB 意外走代理。代理一律显式配置（PROXY_URL → PTB/TMDB；115 默认直连）。
_PROXY_ENV_KEYS = (
    "HTTP_PROXY", "http_proxy",
    "HTTPS_PROXY", "https_proxy",
    "ALL_PROXY", "all_proxy",
)


def _clear_proxy_env() -> None:
    cleared = [k for k in _PROXY_ENV_KEYS if os.environ.pop(k, None) is not None]
    if cleared:
        logging.getLogger("app").warning(
            "已清除进程级代理环境变量（%s）：代理请用 PROXY_URL 显式配置；"
            "115 走直连防风控",
            ", ".join(cleared),
        )


def main() -> None:
    settings = Settings.load()
    # 集中式日志配置：彩色控制台 + 缩短模块名 + 噪声库降级 + 本地文件轮转（见 app/logging_config.py）
    setup_logging(
        settings.log_level,
        use_color=settings.log_color,
        log_file=settings.log_file,
    )

    logger = logging.getLogger("app")
    logger.info("配置加载完成，DB=%s", settings.db_path)
    _clear_proxy_env()

    warns = settings.validate()
    for w in warns:
        logger.warning("配置告警：%s", w)

    container = Container(settings)
    container.build()

    if not container.tg_ready:
        logger.error("TG_BOT_TOKEN 未配置，无法启动 Bot。请填写 .env 后重试。")
        return

    # SIGTERM 由 PTB run_polling 内置处理 → 触发 post_stop/post_shutdown
    # 优雅清理 TMDB/缓存资源（见 bot.py _post_shutdown）。勿覆盖 signal.signal，
    # 否则 PTB 收不到 SIGTERM，container.close() 不执行。
    logger.info("启动 Telegram Bot ...")
    container.telegram.run()


if __name__ == "__main__":
    main()
