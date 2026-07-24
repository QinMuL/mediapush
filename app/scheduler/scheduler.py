"""调度器：pipeline / full_scan / pan115_health / watchdog。

旧项目约束（ARCHITECTURE.md 第 5.9 / 8 节）：
- max_instances=1 + coalesce=True，防止并发执行与任务积压风暴
- misfire_grace_time = max(60, interval*60)，防止高频调度丢任务
- 跳过执行（is_running）时也要更新 _last_pipeline_execution，防止 watchdog 误报超时
- 间隔联动自适应：schedule_interval 变化时自动重算并持久化
  full_scan_interval_runs / pan115_health_interval，update_interval 返回 (full_scan, health)
- schedule_interval < 3 分钟告警；推荐 5-10 分钟
- 启动时 _validate_interval_on_start 校验间隔合理性 + 联动参数匹配度，仅告警不覆盖
"""
from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.monitor import RateLimitMonitor
from app.db import repository
from app.scheduler.watchdog import Watchdog

logger = logging.getLogger(__name__)

PIPELINE_JOB_ID = "pipeline"
HEALTH_JOB_ID = "pan115_health"
WATCHDOG_JOB_ID = "watchdog"
MONITOR_JOB_ID = "rate_limit_monitor"

_MIN_INTERVAL = 3  # 分钟，低于此告警
_FULL_SCAN_TARGET_MIN = 120  # 目标 2 小时全量一次
_HEALTH_MIN_SEC = 180
_HEALTH_MAX_SEC = 900


class SchedulerService:
    """封装 AsyncIOScheduler：注册定时任务、间隔联动、watchdog。"""

    def __init__(self, container, scheduler: AsyncIOScheduler | None = None):
        self._container = container
        self._scheduler = scheduler or AsyncIOScheduler()
        self._watchdog = Watchdog(container)
        self._monitor = RateLimitMonitor(container)
        self._last_pipeline_execution: datetime | None = None
        self._full_scan_counter = 0
        self._started = False

    @property
    def last_pipeline_execution(self) -> datetime | None:
        return self._last_pipeline_execution

    @property
    def watchdog(self) -> Watchdog:
        return self._watchdog

    @property
    def scheduler(self) -> AsyncIOScheduler:
        return self._scheduler

    # ---- 启动 / 停止 ----
    async def start(self) -> None:
        cfg = await self._read_config()
        interval = self._parse_int(cfg.get("schedule_interval", "5"), 5)
        health_interval = self._parse_int(cfg.get("pan115_health_interval", "300"), 300)

        await self._validate_interval_on_start(interval, cfg)

        self._scheduler.add_job(
            self._pipeline_tick, "interval", minutes=interval,
            id=PIPELINE_JOB_ID, max_instances=1, coalesce=True,
            misfire_grace_time=max(60, interval * 60),
            replace_existing=True,
        )
        self._scheduler.add_job(
            self._health_tick, "interval", seconds=health_interval,
            id=HEALTH_JOB_ID, max_instances=1, coalesce=True,
            replace_existing=True,
        )
        self._scheduler.add_job(
            self._watchdog_tick, "interval", minutes=1,
            id=WATCHDOG_JOB_ID, max_instances=1, coalesce=True,
            replace_existing=True,
        )
        self._scheduler.add_job(
            self._monitor_tick, "interval", minutes=5,
            id=MONITOR_JOB_ID, max_instances=1, coalesce=True,
            replace_existing=True,
        )
        self._scheduler.start()
        self._started = True
        logger.info(
            "scheduler 已启动：pipeline=%dmin, health=%ds", interval, health_interval
        )

    async def stop(self) -> None:
        if self._started:
            self._scheduler.shutdown(wait=False)
            self._started = False
            logger.info("scheduler 已停止")

    # ---- pipeline tick ----
    async def _pipeline_tick(self) -> None:
        # 必须最先更新，即使跳过也更新，防止 watchdog 误报超时（旧项目约束）
        self._last_pipeline_execution = datetime.now()

        if self._container.is_pipeline_running():
            logger.info("pipeline 已在运行，调度跳过")
            return

        codes = await self._container.get_monitored_shares()
        if not codes:
            logger.debug("无监控分享，跳过本次 pipeline")
            return

        self._full_scan_counter += 1
        full_scan_every = self._parse_int(
            (await self._read_config()).get("full_scan_interval_runs", "24"), 24
        )
        full_scan_every = max(1, full_scan_every)
        is_full = self._full_scan_counter % full_scan_every == 0
        trigger = "full_scan" if is_full else "scheduler"
        try:
            result = await self._container.run_pipeline_once(codes, trigger=trigger)
            logger.info(
                "pipeline(%s): new=%s pushed=%s existing=%s",
                trigger,
                result.get("new"), result.get("pushed"), result.get("existing"),
            )
        except Exception:  # noqa: BLE001  调度内异常不应崩 scheduler
            logger.exception("pipeline tick 异常")

    # ---- health tick ----
    async def _health_tick(self) -> None:
        if not self._container.pan115_ready():
            return
        try:
            ok = await self._container.check_pan115_health()
            if not ok:
                logger.warning("pan115 健康检查失败：cookie 可能已失效，请在 Web 后台更新")
        except Exception:  # noqa: BLE001
            logger.exception("health tick 异常")

    # ---- watchdog tick ----
    async def _watchdog_tick(self) -> None:
        interval = self._parse_int(
            (await self._read_config()).get("schedule_interval", "5"), 5
        )
        try:
            await self._watchdog.check(self._last_pipeline_execution, interval)
        except Exception:  # noqa: BLE001
            logger.exception("watchdog tick 异常")

    # ---- rate-limit 监控 tick ----
    async def _monitor_tick(self) -> None:
        try:
            await self._monitor.check()
        except Exception:  # noqa: BLE001
            logger.exception("monitor tick 异常")

    # ---- 间隔联动自适应 ----
    async def update_interval(self, new_interval: int) -> tuple[int, int]:
        """schedule_interval 变化时重算联动参数并持久化，重排任务。

        返回 (full_scan_interval_runs, pan115_health_interval) 供展示。
        用户单独改某项后，只要不再改间隔就不会被联动覆盖。
        """
        new_interval = max(1, new_interval)
        full_scan = max(1, round(_FULL_SCAN_TARGET_MIN / new_interval))
        health = min(max(new_interval * 60, _HEALTH_MIN_SEC), _HEALTH_MAX_SEC)

        async with self._container.session_factory() as session:
            await repository.set_config(session, "schedule_interval", str(new_interval))
            await repository.set_config(session, "full_scan_interval_runs", str(full_scan))
            await repository.set_config(session, "pan115_health_interval", str(health))

        if self._started:
            self._scheduler.reschedule_job(
                PIPELINE_JOB_ID, trigger="interval", minutes=new_interval,
            )
            self._scheduler.reschedule_job(
                HEALTH_JOB_ID, trigger="interval", seconds=health,
            )
        logger.info(
            "间隔联动：interval=%dmin → full_scan_every=%d, health=%ds",
            new_interval, full_scan, health,
        )
        return full_scan, health

    # ---- 启动校验 ----
    async def _validate_interval_on_start(
        self, interval: int, cfg: dict[str, str]
    ) -> None:
        if interval < _MIN_INTERVAL:
            logger.warning(
                "schedule_interval=%d 分钟 < %d，流水线耗时可能超过间隔 + 115 风控压力",
                interval, _MIN_INTERVAL,
            )
        # 联动参数匹配度校验（仅告警不覆盖）
        expected_full = max(1, round(_FULL_SCAN_TARGET_MIN / interval))
        expected_health = min(max(interval * 60, _HEALTH_MIN_SEC), _HEALTH_MAX_SEC)
        stored_full = self._parse_int(cfg.get("full_scan_interval_runs", ""), expected_full)
        stored_health = self._parse_int(cfg.get("pan115_health_interval", ""), expected_health)
        if stored_full != expected_full:
            logger.warning(
                "full_scan_interval_runs=%d 与 interval=%d 联动期望 %d 不符"
                "（改间隔后自动联动；如单独覆盖此项，不再改间隔即保留）",
                stored_full, interval, expected_full,
            )
        if stored_health != expected_health:
            logger.warning(
                "pan115_health_interval=%d 与 interval=%d 联动期望 %d 不符",
                stored_health, interval, expected_health,
            )

    # ---- 工具 ----
    async def _read_config(self) -> dict[str, str]:
        async with self._container.session_factory() as session:
            return await repository.get_all_config(session)

    @staticmethod
    def _parse_int(value: str, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
