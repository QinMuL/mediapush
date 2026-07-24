"""Telegram Bot 服务：构建 PTB Application，非阻塞启动/停止，公共接口。

旧项目约束（ARCHITECTURE.md 第 5.11 / 8 节）：
- concurrent_updates(True)：PTB v20+ 默认串行，长 handler 会阻塞整个 update 队列
  导致 TG 交互断联
- 代理注入：ApplicationBuilder().proxy(url).get_updates_proxy(url)
- 与 uvicorn 共享 loop，不用 run_polling（会阻塞并自管 loop），
  改用 initialize → start → updater.start_polling 的手动生命周期
- 公共接口：is_running / get_me / send_message / send_photo，
  外部不访问 _app 私有属性
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class TelegramService:
    """PTB Application 的封装，提供公共接口与生命周期管理。"""

    def __init__(
        self,
        token: str,
        proxy_url: str | None = None,
        container=None,
    ):
        self._token = token
        self._proxy = proxy_url
        self._container = container
        self._app = None  # PTB Application

    def _build_app(self):
        from telegram.ext import ApplicationBuilder

        builder = ApplicationBuilder().token(self._token).concurrent_updates(True)
        if self._proxy:
            builder = builder.proxy(self._proxy).get_updates_proxy(self._proxy)
        app = builder.build()
        if self._container is not None:
            from app.telegram.handlers import register_handlers

            register_handlers(app, self._container)
        return app

    # ---- 生命周期 ----
    async def start(self) -> None:
        """启动 polling（与 uvicorn 同 loop，非阻塞）。重复调用安全。

        失败时清理半成品状态并重新抛出，供上层（watchdog）区分永久/瞬时故障。
        """
        if self._app is not None:
            return
        app = self._build_app()
        try:
            await app.initialize()
            await app.start()
            await app.updater.start_polling(
                allowed_updates=None,
                drop_pending_updates=True,
            )
        except Exception:
            # 清理半成品状态，避免下次 stop 操作未初始化的对象
            try:
                if app.updater and getattr(app.updater, "running", False):
                    await app.updater.stop()
                if getattr(app, "running", False):
                    await app.stop()
                await app.shutdown()
            except Exception:  # noqa: BLE001
                pass
            raise
        self._app = app
        logger.info("Telegram bot 已启动 polling")

    async def stop(self) -> None:
        """停止 polling。重复调用安全。"""
        if self._app is None:
            return
        try:
            if self._app.updater and self._app.updater.running:
                await self._app.updater.stop()
            if self._app.running:
                await self._app.stop()
            await self._app.shutdown()
        except Exception:  # noqa: BLE001
            logger.exception("Telegram bot 停止异常")
        finally:
            self._app = None

    # ---- 公共接口（外部只读，不访问 _app 私有属性）----
    async def is_running(self) -> bool:
        return self._app is not None and bool(self._app.running)

    async def get_me(self):
        if self._app is None:
            return None
        return await self._app.bot.get_me()

    async def send_message(self, chat_id: str, text: str) -> None:
        if self._app is None:
            raise RuntimeError("Telegram bot 未启动")
        await self._app.bot.send_message(
            chat_id=chat_id, text=text, parse_mode="HTML",
            disable_web_page_preview=True,
        )

    async def send_photo(self, chat_id: str, photo_url: str, caption: str) -> None:
        if self._app is None:
            raise RuntimeError("Telegram bot 未启动")
        await self._app.bot.send_photo(
            chat_id=chat_id, photo=photo_url, caption=caption,
            parse_mode="HTML",
        )
