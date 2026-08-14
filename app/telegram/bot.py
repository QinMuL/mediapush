"""TelegramService：PTB Application 封装。

承接前序硬约束：
- concurrent_updates(True)：PTB v20+ 默认串行，长 handler 会阻塞队列拖垮交互
- 代理：.proxy(url) + .get_updates_proxy(url)（TG Bot API 走代理）
- 无 uvicorn/Web 共享 loop，直接 run_polling()（最简，生命周期由 PTB 管理）
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 心跳文件：Bot 主循环每 _HEARTBEAT_INTERVAL 秒 touch 一次，
# Docker HEALTHCHECK 检查 mtime 判断 Bot 是否存活（卡死检测）。
_HEARTBEAT_FILE = Path("/tmp/.heartbeat")
_HEARTBEAT_INTERVAL = 30  # 秒


class TelegramService:
    def __init__(self, settings, container) -> None:
        self.settings = settings
        self.container = container
        self._app = None  # telegram.ext.Application
        self._pusher = None

    @property
    def is_running(self) -> bool:
        return self._app is not None and bool(getattr(self._app, "running", False))

    @property
    def pusher(self):
        return self._pusher

    @property
    def bot(self):
        return self._app.bot if self._app is not None else None

    # ------------------------------------------------------------------ #
    def build(self) -> None:
        from telegram.ext import ApplicationBuilder

        from app.telegram.handlers import register, setup_commands
        from app.telegram.pusher import Pusher

        async def _post_stop(app):
            logger.info("Bot polling 已停止")

        async def _post_shutdown(app):
            # 取消心跳任务
            task = app.bot_data.pop("_heartbeat_task", None)
            if task and not task.done():
                task.cancel()
            # 关闭时清理 TMDB/缓存资源（telegram 生命周期由 PTB 自管，不在此 stop）
            logger.info("Bot 关闭：清理 TMDB/缓存资源")
            if self.container.tmdb is not None:
                await self.container.tmdb.close()
            if self.container.cache is not None:
                await self.container.cache.close()

        async def _heartbeat_loop():
            """定期写心跳文件，供 Docker HEALTHCHECK 检测 Bot 是否存活。"""
            while True:
                try:
                    _HEARTBEAT_FILE.touch()
                except OSError:
                    pass
                await asyncio.sleep(_HEARTBEAT_INTERVAL)

        async def _post_init(app):
            await setup_commands(app)
            app.bot_data["_heartbeat_task"] = asyncio.create_task(_heartbeat_loop())

        builder = (
            ApplicationBuilder()
            .token(self.settings.tg_bot_token)
            .concurrent_updates(True)
            .post_init(_post_init)  # 注册命令 + 启动心跳
            .post_stop(_post_stop)  # 停止时记录状态
            .post_shutdown(_post_shutdown)  # 关闭时清理 TMDB/缓存资源
            # 代理场景下默认超时（read=2s）太短，长轮询/发送容易超时拖死轮询循环
            .read_timeout(30)
            .write_timeout(30)
            .connect_timeout(30)
            .pool_timeout(30)
            .get_updates_read_timeout(30)
            .get_updates_connect_timeout(30)
            .get_updates_write_timeout(30)
            .get_updates_pool_timeout(30)
        )
        if self.settings.proxy_url:
            builder = builder.proxy(self.settings.proxy_url).get_updates_proxy(
                self.settings.proxy_url
            )
        app = builder.build()
        app.bot_data["container"] = self.container

        register(app)
        self._pusher = Pusher(
            app.bot,
            self.settings.tg_chat_id,
            self.settings.tg_chat_id_115,
            self.settings.tg_chat_id_ed2k,
        )
        self._app = app

    async def get_me(self):
        if self._app is None:
            return None
        try:
            return await self._app.bot.get_me()
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_me 失败：%s", exc)
            return None

    async def send_message(self, *args, **kwargs):
        return await self._app.bot.send_message(*args, **kwargs)

    def run(self) -> None:
        """阻塞运行（main 调用）。run_polling 内部管理 initialize/start/stop。"""
        self.build()
        logger.info("Telegram Bot 启动 polling ...")
        # bootstrap_retries=-1: 启动期 getMe 无限重试（代理波动时不放弃启动）
        self._app.run_polling(close_loop=False, bootstrap_retries=-1)

    async def stop(self) -> None:
        if self._app is None:
            return
        try:
            await self._app.stop()
        finally:
            await self._app.shutdown()
            self._app = None
