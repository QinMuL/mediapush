"""程序入口：加载配置 → 构建容器 → 启动 Telegram Bot（run_polling）。"""

from __future__ import annotations

import logging
import signal

from app.config import Settings
from app.core.container import Container
from app.logging_config import setup_logging


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

    warns = settings.validate()
    for w in warns:
        logger.warning("配置告警：%s", w)

    container = Container(settings)
    container.build()

    if not container.tg_ready:
        logger.error("TG_BOT_TOKEN 未配置，无法启动 Bot。请填写 .env 后重试。")
        return

    # SIGTERM 优雅退出（Docker stop 用）
    signal.signal(signal.SIGTERM, lambda *_: None)

    logger.info("启动 Telegram Bot ...")
    container.telegram.run()


if __name__ == "__main__":
    main()
