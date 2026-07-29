"""TelegramService：PTB Application 封装。

承接前序硬约束：
- concurrent_updates(True)：PTB v20+ 默认串行，长 handler 会阻塞队列拖垮交互
- 代理：.proxy(url) + .get_updates_proxy(url)（TG Bot API 走代理）
- 无 uvicorn/Web 共享 loop，直接 run_polling()（最简，生命周期由 PTB 管理）
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


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

        builder = (
            ApplicationBuilder()
            .token(self.settings.tg_bot_token)
            .concurrent_updates(True)
            .post_init(setup_commands)  # 启动时清除旧菜单并注册新命令
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
        self._pusher = Pusher(app.bot, self.settings.tg_chat_id)
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
        self._app.run_polling(close_loop=False)

    async def stop(self) -> None:
        if self._app is None:
            return
        try:
            await self._app.stop()
        finally:
            await self._app.shutdown()
            self._app = None
