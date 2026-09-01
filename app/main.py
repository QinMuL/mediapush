"""程序入口：加载配置 → 构建容器 → 启动 Telegram Bot（run_polling）。"""

from __future__ import annotations

import logging
import os

from app.config import Settings
from app.core.container import Container
from app.logging_config import setup_logging

# 115 域名列表：p115client 内部 requests/urllib3 会读 NO_PROXY/no_proxy env，
# 命中这些域名的请求不走代理（115 走代理易触发风控）。
_115_DOMAINS = "115.com,.115.com"


def setup_proxy_env() -> None:
    """设置 NO_PROXY 让 115 走直连，同时保留系统代理供 TMDB 自动检测。

    - 不再清除 HTTP_PROXY/HTTPS_PROXY 环境变量（旧方案 _clear_proxy_env 会
      导致 TMDB httpx trust_env=True 拿不到系统代理）
    - 改为设置 NO_PROXY=115.com,.115.com：p115client 内部 requests 看到后
      对 115 域名走直连，其它域名（如 api.themoviedb.org）仍可用系统代理
    - TG Bot 走 PTB 显式 .proxy(PROXY_URL)，不受 env 影响
    - PROXY_URL 仍为 TMDB 的首选代理（显式 > 系统代理 env）
    """
    existing = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    parts = [p.strip() for p in existing.split(",") if p.strip()]
    for d in _115_DOMAINS.split(","):
        if d and d not in parts:
            parts.append(d)
    no_proxy = ",".join(parts)
    os.environ["NO_PROXY"] = no_proxy
    os.environ["no_proxy"] = no_proxy
    logging.getLogger("app").info(
        "已设置 NO_PROXY=%s（115 走直连防风控；TMDB 可用系统代理或 PROXY_URL）",
        no_proxy,
    )


def main() -> None:
    settings = Settings.load()
    setup_logging(
        settings.log_level,
        use_color=settings.log_color,
        log_file=settings.log_file,
    )

    logger = logging.getLogger("app")
    logger.info("配置加载完成，DB=%s", settings.db_path)
    setup_proxy_env()

    warns = settings.validate()
    for w in warns:
        logger.warning("配置告警：%s", w)

    container = Container(settings)
    container.build()

    if not container.tg_ready:
        logger.error("TG_BOT_TOKEN 未配置，无法启动 Bot。请填写 .env 后重试。")
        return

    logger.info("启动 Telegram Bot ...")
    container.telegram.run()


if __name__ == "__main__":
    main()
