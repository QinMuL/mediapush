"""DI 容器：懒加载所有服务单例。

- 空配置（缺 token/cookie/key）时不阻塞启动，对应服务为 None
- pusher 懒取：bot 启动（build）后才就绪
- on_config_changed：配置变更后按需重建（轻量版仅日志提示重启）
"""

from __future__ import annotations

import logging

from app.providers.exceptions import Pan115Error

logger = logging.getLogger(__name__)


class Container:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.cache = None
        self.pan115 = None
        self.ed2k = None
        self.tmdb = None
        self.processor = None
        self.telegram = None
        self.monitor_store = None
        self.monitor = None
        self.inspector = None
        self._built = False

    # ------------------------------------------------------------------ #
    def build(self) -> None:
        if self._built:
            return

        # 缓存
        from app.db.cache import Cache

        self.cache = Cache(self.settings.db_path)

        # 115 —— cookie 可选（读取分享走匿名 web；cookie 仅用于健康检查）
        try:
            from app.providers.pan115 import Pan115Provider

            self.pan115 = Pan115Provider(
                self.settings.pan115_cookie,
                use_proxy=self.settings.pan115_use_proxy,
                proxy_url=self.settings.proxy_url,
            )
        except Pan115Error as exc:
            logger.error("Pan115 构造失败：%s", exc)

        # ed2k —— 无依赖配置，恒可用（纯字符串解析）
        from app.providers.ed2k import Ed2kProvider

        self.ed2k = Ed2kProvider()

        # TMDB
        if self.settings.tmdb_api_key:
            from app.tmdb.client import TMDBHelper

            self.tmdb = TMDBHelper(
                self.settings.tmdb_api_key,
                language=self.settings.tmdb_language,
                proxy_url=self.settings.proxy_url,
                cache=self.cache,
            )
        else:
            logger.warning("TMDB_API_KEY 未配置，TMDB 不可用")

        # 编排器（pusher 懒取，故可先建）
        from app.core.processor import ShareProcessor

        self.processor = ShareProcessor(self.pan115, self.ed2k, self.tmdb, self.cache, self)

        # Telegram Bot
        if self.settings.tg_bot_token:
            from app.telegram.bot import TelegramService

            self.telegram = TelegramService(self.settings, self)
        else:
            logger.error("TG_BOT_TOKEN 未配置，Bot 不启动")

        # 频道监控（Telethon 用户账号）——store 恒可建，登录态在 service.start 校验
        if self.settings.monitor_enabled:
            from app.monitor.service import MonitorService
            from app.monitor.store import MonitorStore

            self.monitor_store = MonitorStore(self.settings.monitor_db_path)
            self.monitor = MonitorService(self.settings, self.monitor_store, self)
        else:
            logger.info("MONITOR_ENABLED=false，频道监控未启用")

        # 分享失效巡检（依赖 pan115/cache + telegram.bot，start 在 bot post_init）
        if self.settings.inspect_enabled:
            from app.telegram.inspector import ShareInspector

            self.inspector = ShareInspector(self, self.settings)
        else:
            logger.info("INSPECT_ENABLED=false，分享失效巡检未启用")

        self._built = True

    # ------------------------------------------------------------------ #
    @property
    def pusher(self):
        return self.telegram.pusher if self.telegram is not None else None

    @property
    def tg_ready(self) -> bool:
        return self.telegram is not None

    async def close(self) -> None:
        if self.inspector is not None:
            await self.inspector.stop()
        if self.monitor is not None:
            await self.monitor.stop()
        if self.monitor_store is not None:
            await self.monitor_store.close()
        if self.tmdb is not None:
            await self.tmdb.close()
        if self.cache is not None:
            await self.cache.close()
        if self.telegram is not None:
            await self.telegram.stop()
