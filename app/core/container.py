"""依赖注入容器：持有所有服务单例，提供公共接口。

旧项目约束（ARCHITECTURE.md 第 5.2 / 8 节）：
- 外部只通过公共接口访问服务，不访问私有属性
- reset_pan115 为 async，更新 cookie 后重建 client
- 配置变更触发对应组件重建（on_config_changed）
- 首次未配置 token/cookie/key 时对应服务为 None，不阻塞启动
"""
from __future__ import annotations

import logging

from app.db import repository
from app.db.base import async_session
from app.pan115.client import Pan115Client
from app.pan115.service import Pan115Service
from app.pipeline import Pipeline
from app.telegram.bot import TelegramService
from app.telegram.pusher import Pusher
from app.tmdb.client import TmdbClient
from app.tmdb.service import TmdbService

logger = logging.getLogger(__name__)


class Container:
    """服务注册表：组装 pan115 / tmdb / pipeline / telegram，统一生命周期。"""

    def __init__(self, session_factory=None):
        self.session_factory = session_factory or async_session
        self._pan115_client: Pan115Client | None = None
        self._pan115: Pan115Service | None = None
        self._tmdb_client: TmdbClient | None = None
        self._tmdb: TmdbService | None = None
        self._pipeline: Pipeline | None = None
        self._telegram: TelegramService | None = None
        self._pusher: Pusher | None = None

    @classmethod
    async def create(cls, session_factory=None) -> Container:
        c = cls(session_factory=session_factory)
        await c._init_from_config()
        return c

    async def _read_config(self) -> dict[str, str]:
        async with self.session_factory() as session:
            await repository.ensure_default_config(session)
            return await repository.get_all_config(session)

    async def _init_from_config(self) -> None:
        cfg = await self._read_config()

        # pan115
        cookie = cfg.get("pan115_cookie", "")
        if cookie:
            self._pan115_client = Pan115Client(cookie)
            self._pan115 = Pan115Service(self._pan115_client)
        else:
            self._pan115 = None
            self._pan115_client = None

        # tmdb
        api_key = cfg.get("tmdb_api_key", "")
        proxy_tmdb = await self._proxy_for(cfg, "tmdb")
        if api_key:
            self._tmdb_client = TmdbClient(api_key, proxy=proxy_tmdb)
            self._tmdb = TmdbService(self._tmdb_client)
        else:
            self._tmdb = None
            self._tmdb_client = None

        # pipeline（始终构造，pan115/tmdb 可为 None）
        self._pipeline = Pipeline(
            self._pan115, self._tmdb, self.session_factory
        )

        # telegram（构造但不启动，start_bot 由 lifecycle 调用）
        token = cfg.get("tg_bot_token", "")
        chat_id = cfg.get("tg_chat_id", "")
        proxy_tg = await self._proxy_for(cfg, "tg")
        if token:
            self._telegram = TelegramService(token, proxy_tg, container=self)
            self._pusher = Pusher(
                self._telegram, chat_id, self.session_factory
            )
            self._pipeline.set_pusher(self._pusher)
        else:
            self._telegram = None
            self._pusher = None

    async def _proxy_for(self, cfg: dict[str, str], target: str) -> str | None:
        """从已读配置判定代理（避免重复读 DB）。"""
        from app.core.proxy import _TRUE, _parse_targets

        if (cfg.get("proxy_enabled") or "").lower() not in _TRUE:
            return None
        if target.lower() not in _parse_targets(cfg.get("proxy_targets")):
            return None
        return cfg.get("proxy_url") or None

    # ---- 公共只读属性 ----
    @property
    def pan115(self) -> Pan115Service | None:
        return self._pan115

    @property
    def tmdb(self) -> TmdbService | None:
        return self._tmdb

    @property
    def pipeline(self) -> Pipeline | None:
        return self._pipeline

    @property
    def telegram(self) -> TelegramService | None:
        return self._telegram

    @property
    def pusher(self) -> Pusher | None:
        return self._pusher

    def pan115_ready(self) -> bool:
        return self._pan115 is not None

    # ---- 流水线操作（handler 公共接口）----
    def is_pipeline_running(self) -> bool:
        return self._pipeline is not None and self._pipeline.ctx.is_running

    def stop_pipeline(self) -> None:
        if self._pipeline is not None:
            self._pipeline.ctx.stop()

    async def run_pipeline_once(
        self, share_codes: list[tuple[str, str]], trigger: str = "manual"
    ) -> dict:
        if self._pipeline is None:
            return {"skipped": True, "error": "pipeline 未初始化"}
        return await self._pipeline.run(share_codes, trigger=trigger)

    async def refresh_tmdb(self, tmdb_id: int) -> int:
        if self._tmdb is None:
            return 0
        async with self.session_factory() as session:
            return await self._tmdb.refresh(session, tmdb_id)

    async def send_alert(self, text: str) -> bool:
        """通过 Telegram 向配置的频道发送告警（监控/watchdog 用）。

        bot 未运行或未配置 chat_id 时返回 False，不抛异常。
        """
        if self._telegram is None:
            return False
        async with self.session_factory() as session:
            chat_id = await repository.get_config(session, "tg_chat_id", "")
        if not chat_id:
            return False
        try:
            await self._telegram.send_message(chat_id, text)
            return True
        except Exception:  # noqa: BLE001  告警失败不应影响监控主流程
            logger.exception("告警发送失败")
            return False

    async def get_status(self) -> dict:
        """供 /status 与 Web 仪表盘使用。"""
        cfg = await self._read_config()
        bot_running = False
        if self._telegram is not None:
            bot_running = await self._telegram.is_running()
        unpushed = 0
        async with self.session_factory() as session:
            from sqlalchemy import func, select

            from app.db import models

            count_stmt = select(func.count()).select_from(models.Share).where(
                models.Share.pushed.is_(False)
            )
            unpushed = (await session.execute(count_stmt)).scalar() or 0
        return {
            "bot_running": bot_running,
            "pipeline_running": self.is_pipeline_running(),
            "unpushed": unpushed,
            "config_health": {
                "tg_token": bool(cfg.get("tg_bot_token")),
                "pan115_cookie": bool(cfg.get("pan115_cookie")),
                "tmdb_key": bool(cfg.get("tmdb_api_key")),
                "proxy": (cfg.get("proxy_enabled") or "").lower() in {"1", "true", "yes", "on"},
                "schedule_interval": cfg.get("schedule_interval", "?"),
            },
        }

    # ---- bot 生命周期 ----
    async def start_bot(self) -> bool:
        if self._telegram is None:
            logger.warning("未配置 tg_bot_token，跳过 bot 启动")
            return False
        try:
            await self._telegram.start()
            return True
        except Exception:  # noqa: BLE001
            logger.exception("Telegram bot 启动失败")
            return False

    async def stop_bot(self) -> None:
        if self._telegram is not None:
            await self._telegram.stop()

    # ---- pan115 健康检查 ----
    async def check_pan115_health(self) -> bool:
        if self._pan115_client is None:
            return False
        return await self._pan115_client.check_health()

    # ---- 监控分享列表 ----
    @staticmethod
    def parse_monitored_shares(text: str) -> list[tuple[str, str]]:
        """解析 monitored_shares 配置：逗号分隔，code:password 或 code。"""
        pairs: list[tuple[str, str]] = []
        for item in (text or "").split(","):
            item = item.strip()
            if not item:
                continue
            if ":" in item:
                code, pwd = item.split(":", 1)
                pairs.append((code.strip(), pwd.strip()))
            else:
                pairs.append((item, ""))
        return pairs

    async def get_monitored_shares(self) -> list[tuple[str, str]]:
        async with self.session_factory() as session:
            text = await repository.get_config(session, "monitored_shares", "")
        return self.parse_monitored_shares(text)

    # ---- 配置变更：重建对应组件 ----
    async def reset_pan115(self, cookie: str | None = None) -> None:
        """更新 cookie 后重建 pan115 client（async，需 await）。

        cookie=None → 从 DB 读；cookie="" → 清空；cookie="..." → 用给定值。
        """
        if cookie is None:
            async with self.session_factory() as session:
                new_cookie = await repository.get_config(
                    session, "pan115_cookie", ""
                )
        else:
            new_cookie = cookie
        if not new_cookie:
            # 清空
            if self._pan115_client is not None:
                await self._pan115_client.close()
            self._pan115_client = None
            self._pan115 = None
            if self._pipeline is not None:
                self._pipeline._pan115 = None  # noqa: SLF001  容器拥有 pipeline
            return
        if self._pan115_client is not None:
            await self._pan115_client.reset(new_cookie)
        else:
            self._pan115_client = Pan115Client(new_cookie)
            self._pan115 = Pan115Service(self._pan115_client)
            if self._pipeline is not None:
                self._pipeline._pan115 = self._pan115  # noqa: SLF001

    async def rebuild_tmdb(self) -> None:
        """tmdb_api_key / 代理变更后重建 tmdb client。"""
        if self._tmdb_client is not None:
            await self._tmdb_client.close()
        cfg = await self._read_config()
        api_key = cfg.get("tmdb_api_key", "")
        if not api_key:
            self._tmdb = None
            self._tmdb_client = None
            if self._pipeline is not None:
                self._pipeline._tmdb = None  # noqa: SLF001
            return
        proxy_tmdb = await self._proxy_for(cfg, "tmdb")
        self._tmdb_client = TmdbClient(api_key, proxy=proxy_tmdb)
        self._tmdb = TmdbService(self._tmdb_client)
        if self._pipeline is not None:
            self._pipeline._tmdb = self._tmdb  # noqa: SLF001

    async def rebuild_telegram(self, raise_on_error: bool = False) -> bool:
        """tg_bot_token / 代理变更后重建 bot（先停旧 bot）。

        raise_on_error=True 时失败重抛，供 watchdog 区分永久/瞬时故障；
        默认 False（Web/lifecycle 调用，吞掉异常返回 bool）。
        """
        await self.stop_bot()
        cfg = await self._read_config()
        token = cfg.get("tg_bot_token", "")
        chat_id = cfg.get("tg_chat_id", "")
        proxy_tg = await self._proxy_for(cfg, "tg")
        if not token:
            self._telegram = None
            self._pusher = None
            if self._pipeline is not None:
                self._pipeline.set_pusher(None)
            return False
        self._telegram = TelegramService(token, proxy_tg, container=self)
        self._pusher = Pusher(self._telegram, chat_id, self.session_factory)
        if self._pipeline is not None:
            self._pipeline.set_pusher(self._pusher)
        try:
            await self._telegram.start()
            return True
        except Exception:
            if raise_on_error:
                raise
            logger.exception("Telegram bot 重建启动失败")
            return False

    async def on_config_changed(self, key: str) -> None:
        """配置变更分发：通知对应组件重建（Web 改配置后调用）。"""
        if key in ("pan115_cookie",):
            await self.reset_pan115()
        elif key in ("tmdb_api_key", "proxy_enabled", "proxy_url", "proxy_targets"):
            # 代理变更可能同时影响 tmdb 与 telegram
            await self.rebuild_tmdb()
            if key in ("proxy_enabled", "proxy_url", "proxy_targets"):
                await self.rebuild_telegram()
        elif key in ("tg_bot_token", "tg_chat_id"):
            await self.rebuild_telegram()

    # ---- 关闭（lifecycle: reset_all 先于 close_db）----
    async def close(self) -> None:
        await self.stop_bot()
        if self._pan115_client is not None:
            await self._pan115_client.close()
        if self._tmdb_client is not None:
            await self._tmdb_client.close()
        logger.info("Container 已关闭")
