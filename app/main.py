"""程序入口：加载配置 → 构建容器 → 启动 Telegram Bot（run_polling）。"""

from __future__ import annotations

import logging
import signal

from app.config import Settings
from app.core.container import Container


def main() -> None:
    settings = Settings.load()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    # 静默 PTB 过于啰嗦的日志
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

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
