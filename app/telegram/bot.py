"""TelegramService：PTB Application 封装。

承接前序硬约束：
- concurrent_updates(True)：PTB v20+ 默认串行，长 handler 会阻塞队列拖垮交互
- 代理：.proxy(url) + .get_updates_proxy(url)（TG Bot API 走代理）
- 无 uvicorn/Web 共享 loop，直接 run_polling()（最简，生命周期由 PTB 管理）
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# 心跳文件：Bot 主循环每 _HEARTBEAT_INTERVAL 秒 touch 一次，
# Docker HEALTHCHECK 检查 mtime 判断 Bot 是否存活（卡死检测）。
_HEARTBEAT_FILE = Path("/tmp/.heartbeat")
_HEARTBEAT_INTERVAL = 30  # 秒
# 自愈探活：每 _PROBE_EVERY 个心跳周期调一次 TG API，连续 _PROBE_MAX_FAIL 次失败则强制退出。
# 进程退出后 Docker restart 策略自动重启，无需人工干预。
_PROBE_EVERY = 3  # 每 3 个心跳周期（90s）探活一次
_PROBE_MAX_FAIL = 3  # 连续 3 次失败（~4.5min）→ os._exit(1)


async def _heartbeat_loop(app) -> None:
    """心跳 + 自愈探活（模块级，便于单测）。

    每 30s 写心跳文件（Docker HEALTHCHECK 用）；
    每 90s 双重探活，异常时 os._exit 触发 Docker 自动重启：
    1) polling 任务活性：PTB 的 network_retry_loop 因意外异常退出时
       updater.running 标志仍为 True（进程"健康"但永远收不到消息），
       检查内部 polling task 是否 done 能捕获此场景；
    2) 网络连通：get_me 探测 bot client（代理/httpx 连接池卡死）。
    """
    fail_count = 0
    tick = 0
    while True:
        # 心跳文件
        try:
            _HEARTBEAT_FILE.touch()
        except OSError:
            pass
        # 探活（每 _PROBE_EVERY 个周期）
        tick += 1
        if tick >= _PROBE_EVERY:
            tick = 0
            # 1) polling 任务活性（done 且 running 标志仍 True = 静默死亡）
            task = getattr(app.updater, "_Updater__polling_task", None)
            if task is not None and task.done() and app.updater.running:
                exc = None
                try:
                    exc = task.exception()
                except asyncio.CancelledError:
                    exc = "cancelled"
                logger.error("polling 任务已静默死亡（%s），强制退出以触发 Docker 重启", exc)
                os._exit(1)
            # 2) 网络连通性
            try:
                await app.bot.get_me()
                fail_count = 0
            except Exception as exc:  # noqa: BLE001
                fail_count += 1
                logger.warning("自愈探活失败（第 %d/%d 次）：%s", fail_count, _PROBE_MAX_FAIL, exc)
                if fail_count >= _PROBE_MAX_FAIL:
                    logger.error("连续 %d 次探活失败，强制退出以触发 Docker 重启", fail_count)
                    os._exit(1)
        await asyncio.sleep(_HEARTBEAT_INTERVAL)


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
            # 频道监控：取消启动任务 + 停止服务（冲刷待推送批次后断开）
            monitor_task = app.bot_data.pop("_monitor_task", None)
            if monitor_task and not monitor_task.done():
                monitor_task.cancel()
            # 分享失效巡检 / 目录监控：停止后台循环
            if self.container.inspector is not None:
                await self.container.inspector.stop()
            if self.container.share_watcher is not None:
                await self.container.share_watcher.stop()
            if self.container.monitor is not None:
                await self.container.monitor.stop()
            if self.container.monitor_store is not None:
                await self.container.monitor_store.close()
            # 关闭时清理 TMDB/缓存资源（telegram 生命周期由 PTB 自管，不在此 stop）
            logger.info("Bot 关闭：清理 TMDB/缓存资源")
            if self.container.tmdb is not None:
                await self.container.tmdb.close()
            if self.container.cache is not None:
                await self.container.cache.close()

        async def _start_monitor():
            """频道监控启动（Telethon 用户账号）；失败不影响 Bot 主链路。"""
            try:
                ok = await self.container.monitor.start()
                if not ok:
                    logger.warning(
                        "频道监控未启动（/mon 可查看原因；发送 /mon login 交互式登录）"
                    )
            except Exception as exc:
                logger.error("频道监控启动失败：%s", exc, exc_info=exc)

        async def _post_init(app):
            await setup_commands(app)
            app.bot_data["_heartbeat_task"] = asyncio.create_task(_heartbeat_loop(app))
            # 频道监控：独立任务启动（连接 + 补扫耗时，不阻塞 polling）
            if self.container.monitor is not None:
                app.bot_data["_monitor_task"] = asyncio.create_task(_start_monitor())
            # 分享失效巡检：后台循环（独立任务，失败不影响主链路）
            if self.container.inspector is not None:
                app.bot_data["_inspector_task"] = asyncio.create_task(
                    self.container.inspector.start()
                )
            # 目录监控 → 自动建分享：后台循环（独立任务）
            if self.container.share_watcher is not None:
                app.bot_data["_share_watcher_task"] = asyncio.create_task(
                    self.container.share_watcher.start()
                )

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
