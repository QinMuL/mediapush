"""看门狗：pipeline 停滞告警 + bot 重建。

旧项目约束（ARCHITECTURE.md 第 5.10 / 8 节）：
- 超时阈值 max(interval*3, 30) 分钟
- 重建 bot 失败统一指数退避，禁止"网络正常就立即重建"分支跳过退避：
  永久性故障（ImportError/ModuleNotFoundError/AttributeError）下会形成每 30 秒
  狂试一次的死循环
- 区分永久性故障与瞬时故障：永久性抛 _PermanentRebuildFailure，
  上层 10 分钟冷却并告警管理员，不重试
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# 永久性故障异常类型（重建无意义，进入冷却）
_PERMANENT_ERRORS = (ImportError, ModuleNotFoundError, AttributeError)

# 退避参数
_BACKOFF_BASE = 30  # 秒
_BACKOFF_MAX = 1800  # 30 分钟上限
_PERMANENT_COOLDOWN_MIN = 10  # 永久故障冷却 10 分钟


class _PermanentRebuildFailure(Exception):
    """bot 重建永久性故障（配置/依赖问题，重试无意义）。"""


class Watchdog:
    """监控 pipeline 执行节奏与 bot 存活，按需重建 bot。"""

    def __init__(self, container):
        self._container = container
        self._rebuild_failures = 0
        self._cooldown_until: datetime | None = None

    @property
    def rebuild_failures(self) -> int:
        return self._rebuild_failures

    @property
    def cooldown_until(self) -> datetime | None:
        return self._cooldown_until

    async def check(
        self,
        last_pipeline_execution: datetime | None,
        interval_minutes: int,
    ) -> None:
        """每次 watchdog tick 调用：检查 pipeline 停滞 + bot 重建。"""
        self._check_pipeline_stall(last_pipeline_execution, interval_minutes)
        await self._try_rebuild_bot()

    # ---- pipeline 停滞告警 ----
    @staticmethod
    def _check_pipeline_stall(
        last_pipeline_execution: datetime | None, interval_minutes: int
    ) -> None:
        if last_pipeline_execution is None:
            return  # 尚未执行过，不告警
        threshold_min = max(interval_minutes * 3, 30)
        elapsed_min = (datetime.now() - last_pipeline_execution).total_seconds() / 60
        if elapsed_min > threshold_min:
            logger.warning(
                "watchdog: pipeline 已 %.1f 分钟未执行（阈值 %d 分钟），可能调度停滞",
                elapsed_min, threshold_min,
            )

    # ---- bot 重建 ----
    async def _should_bot_be_running(self) -> bool:
        """是否期望 bot 在运行（配置了 token）。"""
        async with self._container.session_factory() as session:
            from app.db import repository

            token = await repository.get_config(session, "tg_bot_token", "")
        return bool(token)

    async def _bot_running(self) -> bool:
        tg = self._container.telegram
        if tg is None:
            return False
        return await tg.is_running()

    async def _try_rebuild_bot(self) -> None:
        # 冷却期内不重试（统一退避，无"网络正常即重建"分支）
        now = datetime.now()
        if self._cooldown_until is not None and now < self._cooldown_until:
            return
        if not await self._should_bot_be_running():
            return  # 未配置 token，无需重建
        if await self._bot_running():
            return  # bot 正常运行
        try:
            ok = await self._container.rebuild_telegram(raise_on_error=True)
            if ok:
                self._rebuild_failures = 0
                self._cooldown_until = None
                logger.info("watchdog: bot 重建成功")
        except _PERMANENT_ERRORS as e:
            self._handle_permanent_failure(e)
        except Exception as e:  # noqa: BLE001  瞬时故障
            self._handle_transient_failure(e)

    def _handle_transient_failure(self, err: Exception) -> None:
        """瞬时故障：指数退避，绝不跳过。"""
        self._rebuild_failures += 1
        backoff = min(_BACKOFF_BASE * (2 ** (self._rebuild_failures - 1)), _BACKOFF_MAX)
        self._cooldown_until = datetime.now() + timedelta(seconds=backoff)
        logger.warning(
            "watchdog: bot 重建失败 #%d，%ds 后重试: %s",
            self._rebuild_failures, backoff, err,
        )

    def _handle_permanent_failure(self, err: Exception) -> None:
        """永久性故障：10 分钟冷却 + 告警，不重试。"""
        self._cooldown_until = datetime.now() + timedelta(minutes=_PERMANENT_COOLDOWN_MIN)
        logger.error(
            "watchdog: bot 重建永久性故障，%d 分钟冷却并告警管理员: %s",
            _PERMANENT_COOLDOWN_MIN, err,
        )
