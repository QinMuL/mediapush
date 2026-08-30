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
        self.share_watcher = None
        self.local_media = None
        self.ed2k_service = None   # B→C 哈希流水线（app.media.ed2k_service.Ed2kService）
        self.ed2k_pusher = None    # JSONL → 频道推送（app.media.ed2k_pusher.Ed2kPusherService）
        self.pan115_limiter = None
        self._built = False

    # ------------------------------------------------------------------ #
    def refresh_cookie_file(self) -> bool:
        """重读 PAN115_COOKIE_FILE 并热更新 provider cookie。

        供 /reload 立即生效 + 巡检/目录监控周期调用（统一入口）。
        返回是否有更新（cookie 内容变化才算）。
        """
        cookie_file = self.settings.pan115_cookie_file
        if not cookie_file or self.pan115 is None:
            return False
        try:
            from pathlib import Path

            new_cookie = Path(cookie_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("cookie 文件读取失败：%s", exc)
            return False
        if not new_cookie or new_cookie == self.pan115.cookie:
            return False
        self.pan115.update_cookie(new_cookie)
        logger.info("115 cookie 已从文件热更新（长度 %d）", len(new_cookie))
        return True

    # ------------------------------------------------------------------ #
    def build(self) -> None:
        if self._built:
            return

        # 缓存
        from app.db.cache import Cache

        self.cache = Cache(self.settings.db_path)

        # 115 —— cookie 可选（读取分享走匿名 web；cookie 仅用于健康检查）
        from app.core.rate_limiter import AdaptiveLimiter

        self.pan115_limiter = AdaptiveLimiter(
            self.settings.pan115_request_interval, name="pan115"
        )
        try:
            from app.providers.pan115 import Pan115Provider

            self.pan115 = Pan115Provider(
                self.settings.pan115_cookie,
                use_proxy=self.settings.pan115_use_proxy,
                proxy_url=self.settings.proxy_url,
                limiter=self.pan115_limiter,
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

        # 目录监控 → 自动建永久分享（依赖 pan115 cookie + processor，start 在 bot post_init）
        if self.settings.share_watch_enabled:
            from app.core.share_watcher import ShareWatcher

            self.share_watcher = ShareWatcher(self, self.settings)
            if not self.settings.pan115_cookie and not self.settings.pan115_cookie_file:
                logger.warning(
                    "目录监控已创建但暂无 115 cookie：创建分享需登录态"
                    "（PAN115_COOKIE / PAN115_COOKIE_FILE），配置后 /dir add 即可用"
                )
        else:
            logger.info("SHARE_WATCH_ENABLED=false，目录监控未启用")

        # 本地媒体流水线（目录A → 重命名 → 目录B，start 在 bot post_init）
        if self.settings.local_media_enabled:
            from app.media.service import LocalMediaService

            self.local_media = LocalMediaService(self, self.settings)
            logger.info(
                "本地媒体流水线已创建：A=%s → B=%s（%s）",
                self.settings.local_media_input_dir,
                self.settings.local_media_output_dir,
                "DRY-RUN 模拟" if self.settings.local_media_dry_run else "实际移动",
            )
        else:
            logger.info("LOCAL_MEDIA_ENABLED=false，本地媒体流水线未启用")

        # ed2k 流水线（目录B → 哈希 → 目录C，start 在 bot post_init）
        if self.settings.ed2k_enabled:
            from app.media.ed2k_service import Ed2kService

            self.ed2k_service = Ed2kService(self.settings)
            logger.info(
                "ed2k 流水线已创建：B=%s → C=%s（%s）",
                self.settings.ed2k_input_dir,
                self.settings.ed2k_output_dir,
                "DRY-RUN 模拟" if self.settings.ed2k_dry_run else "实际移动",
            )
        else:
            logger.info("ED2K_ENABLED=false，ed2k 流水线未启用")

        # ed2k 推送（JSONL → ShareProcessor → 频道卡片，start 在 bot post_init）
        if self.settings.ed2k_push_enabled:
            from app.media.ed2k_pusher import Ed2kPusherService

            self.ed2k_pusher = Ed2kPusherService(self, self.settings)
            logger.info(
                "ed2k 推送已创建：追读 data/ed2k_results.jsonl（%s）",
                "DRY-RUN 模拟" if self.settings.ed2k_push_dry_run else "实际推送",
            )
        else:
            logger.info("ED2K_PUSH_ENABLED=false，ed2k 推送未启用")

        self._built = True

    # ------------------------------------------------------------------ #
    def reload_config(self, new_settings) -> tuple[list[str], list[str]]:
        """/reload：应用新配置到运行中的服务，返回 (已热加载, 需重启) 字段名。

        - 热加载项原地更新 self.settings（各服务持同一引用，即刻生效）
        - 服务 __init__ 缓存的字段（巡检/扫描间隔、限速基准、控制台日志级别）
          需单独同步
        - cookie 文件重读立即生效；其余变更仅报告需重启
        """
        import dataclasses

        from app.config import HOT_RELOAD_FIELDS

        old = self.settings
        changed = [
            f.name
            for f in dataclasses.fields(new_settings)
            if getattr(old, f.name) != getattr(new_settings, f.name)
        ]
        hot = [n for n in changed if n in HOT_RELOAD_FIELDS]
        restart = [n for n in changed if n not in HOT_RELOAD_FIELDS]

        # 1) 原地更新热加载字段（服务引用同对象即刻可见）
        for name in hot:
            setattr(old, name, getattr(new_settings, name))

        # 2) 同步服务缓存的派生值
        if "inspect_interval_hours" in hot and self.inspector is not None:
            self.inspector.interval = max(0.5, old.inspect_interval_hours)
        if "share_watch_interval_minutes" in hot and self.share_watcher is not None:
            self.share_watcher.interval = max(1.0, old.share_watch_interval_minutes)
        if "local_media_interval_seconds" in hot and self.local_media is not None:
            self.local_media.interval = max(1.0, old.local_media_interval_seconds)
        if "ed2k_interval_seconds" in hot and self.ed2k_service is not None:
            self.ed2k_service.interval = max(1.0, old.ed2k_interval_seconds)
        if "ed2k_push_interval_seconds" in hot and self.ed2k_pusher is not None:
            self.ed2k_pusher.interval = max(1.0, old.ed2k_push_interval_seconds)
        if "pan115_request_interval" in hot and self.pan115_limiter is not None:
            self.pan115_limiter.set_base_interval(old.pan115_request_interval)
        if "log_level" in hot:
            from app.logging_config import set_console_level

            set_console_level(old.log_level)
        # 3) cookie 热更新：
        #    - PAN115_COOKIE 直配变化 → provider.update_cookie（重建内部 client）
        #    - PAN115_COOKIE_FILE 文件内容变化 → refresh_cookie_file（统一入口）
        if (
            "pan115_cookie" in hot
            and self.pan115 is not None
            and new_settings.pan115_cookie
            and new_settings.pan115_cookie != self.pan115.cookie
        ):
            self.pan115.update_cookie(new_settings.pan115_cookie)
            logger.info(
                "PAN115_COOKIE 直配已热更新（长度 %d）", len(new_settings.pan115_cookie)
            )
        self.refresh_cookie_file()

        if hot:
            logger.info("配置热加载：%s", ", ".join(hot))
        if restart:
            logger.info("配置变更需重启生效：%s", ", ".join(restart))
        return hot, restart

    # ------------------------------------------------------------------ #
    @property
    def pusher(self):
        return self.telegram.pusher if self.telegram is not None else None

    @property
    def tg_ready(self) -> bool:
        return self.telegram is not None

    async def close(self) -> None:
        if self.ed2k_pusher is not None:
            await self.ed2k_pusher.stop()
        if self.ed2k_service is not None:
            await self.ed2k_service.stop()
        if self.local_media is not None:
            await self.local_media.stop()
        if self.share_watcher is not None:
            await self.share_watcher.stop()
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
