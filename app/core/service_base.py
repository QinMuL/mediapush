"""轮询服务基础设施：统一后台循环骨架、退避状态机、admin 进度消息。

六个常驻轮询服务（LocalMedia / Ed2k / Ed2kPusher / Cd2 / ShareWatcher /
Inspector）原先各自实现同构的 start/_run_loop/stop（异常兜底 + 间隔 sleep +
_last_report 记录）与指数退避状态机，本模块将其收敛：

- PollingService：生命周期与循环骨架；子类只写 run_once 与钩子
- FailureTracker：failures/next_retry/first_seen 指数退避（StateStore 格式兼容）
- AdminProgressMessages：admin 进度消息组（首条 send、后续 edit、收尾 finalize）

兼容性约束（改造不破坏线上状态）：
- 退避状态持久化格式不变（failures/next_retry/first_seen + 各服务附加键），
  NAS 上已有 data/state.db 无需迁移
- 各服务 Report dataclass（summary/has_events）保持领域定制，不强行统一字段
- 循环日志走子类模块 logger（app.media.* 路由 media.log 的既有行为不变）
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

# 重试退避：1h 起指数翻倍，24h 封顶（原 app/media/service.py，向后兼容 re-export）
_RETRY_BASE_SECONDS = 3600
_RETRY_CAP_SECONDS = 24 * 3600


def retry_backoff_seconds(failures: int) -> int:
    """低置信重试退避：failures=1→1h, 2→2h, 3→4h … 封顶 24h。"""
    if failures <= 0:
        return 0
    return min(_RETRY_CAP_SECONDS, _RETRY_BASE_SECONDS * (1 << (failures - 1)))


class PollingService(ABC):
    """轮询服务基类：统一 start/stop 生命周期 + 后台循环骨架。

    子类契约：
    - ``self.interval: float``  轮询间隔（单位任意，配合 interval_scale 换算秒）
    - ``run_once() -> Report``  单轮；Report 需实现 summary() 与 has_events()
    - 可选钩子：
      * startup_delay   启动后先歇（避开启动高峰；ShareWatcher 60s / Inspector 120s）
      * before_round    每轮前置（刷 cookie / 健康检查）
      * after_round     每轮收尾（默认：has_events 时记 info 日志；通知类服务覆盖）
      * on_stopped      stop 时持久化钩子
      * _on_start       启动日志文案（各服务目录/DRY-RUN 标记不同）

    ``interval`` 为实例属性（子类 __init__ 赋值），/reload 热更新直接改值即可生效。
    类属性兜底（_task/_last_report）使子类无需显式调用 super().__init__()。
    """

    name: str = "service"        # StateStore 键名 / 标识
    log_prefix: str = "服务"      # 循环日志前缀（"本地媒体扫描" / "ed2k 推送"…）
    interval: float = 60.0
    interval_scale: float = 1.0  # 间隔单位换算（分钟×60 / 小时×3600）
    startup_delay: float = 0.0

    _task: asyncio.Task | None = None
    _last_report: str | None = None
    _log_obj: logging.Logger | None = None

    @property
    def log(self) -> logging.Logger:
        """子类模块的 logger（保持 app.media.* → media.log 的路由不变）。"""
        if self._log_obj is None:
            self._log_obj = logging.getLogger(type(self).__module__)
        return self._log_obj

    @property
    def sleep_seconds(self) -> float:
        return self.interval * self.interval_scale

    @abstractmethod
    async def run_once(self):
        """单轮领域逻辑，返回带 summary()/has_events() 的 Report。"""

    # ---------------- 钩子 ---------------- #
    async def before_round(self) -> None:  # noqa: B027 - 可选钩子（默认 no-op）
        """每轮前置（默认 no-op）。"""

    async def after_round(self, report) -> None:
        """每轮收尾（默认：有事件时记 info 日志）。"""
        if report.has_events():
            self.log.info("%s：%s", self.log_prefix, report.summary())

    def on_stopped(self) -> None:  # noqa: B027 - 可选钩子（默认 no-op）
        """stop 钩子（默认 no-op；有状态的服务覆盖为持久化 + 停止日志）。"""

    def _on_start(self) -> None:
        """启动日志（默认通用文案；子类可覆盖为带目录/DRY-RUN 的定制文案）。"""
        self.log.info("%s已启动（间隔 %.0fs）", self.log_prefix, self.sleep_seconds)

    # ---------------- 生命周期 ---------------- #
    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._on_start()
        self._task = asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        if self.startup_delay:
            await asyncio.sleep(self.startup_delay)
        while True:
            try:
                await self.before_round()
                report = await self.run_once()
                self._last_report = report.summary()
                await self.after_round(report)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.error("%s轮异常：%s", self.log_prefix, exc, exc_info=exc)
            await asyncio.sleep(self.sleep_seconds)

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self.on_stopped()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()


class FailureTracker:
    """指数退避状态机：failures/next_retry/first_seen + stuck（卡死）判定。

    持久化格式与既有 StateStore 状态完全一致（dict[str, dict]），
    各服务附加字段（name/size_bytes 等）经 record(**extra) 合并保留。
    """

    def __init__(self, stuck_days: float = 7.0) -> None:
        self.stuck_days = stuck_days
        self._state: dict[str, dict] = {}

    # ---------------- 持久化 ---------------- #
    def load(self, data: dict) -> None:
        """载入状态（过滤非 dict 条目与下划线元键，如 _offset）。"""
        self._state = {
            k: v for k, v in data.items()
            if isinstance(v, dict) and not k.startswith("_")
        }

    def dump(self) -> dict[str, dict]:
        return self._state

    # ---------------- 查询 ---------------- #
    def __contains__(self, key: str) -> bool:
        return key in self._state

    def __len__(self) -> int:
        return len(self._state)

    def get(self, key: str) -> dict | None:
        return self._state.get(key)

    def items(self):
        return self._state.items()

    def values(self):
        return self._state.values()

    def oldest_first_seen(self) -> float:
        """最老条目的 first_seen（无条目返回 0；卡死展示用）。"""
        return min((v.get("first_seen", 0) for v in self._state.values()), default=0.0)

    # ---------------- 状态机 ---------------- #
    def due(self, key: str, now: float) -> bool:
        """无状态=首轮即试；有状态=退避到期才试。"""
        st = self._state.get(key)
        return st is None or st.get("next_retry", 0) <= now

    def record(self, key: str, now: float, **extra) -> bool:
        """记一次失败；返回是否 stuck（超 stuck_days 仍未成功，需人工介入）。

        extra 附加字段（如 name/size_bytes）合并进条目；已有附加字段保留。
        """
        st = self._state.get(key, {"failures": 0, "first_seen": now})
        st["failures"] += 1
        st["next_retry"] = now + retry_backoff_seconds(st["failures"])
        st.update(extra)
        self._state[key] = st
        return now - st.get("first_seen", now) > self.stuck_days * 86400

    def register(self, key: str, now: float, **extra) -> None:
        """登记条目但不计失败（failures=0）：首见登记，附加字段随 extra。"""
        if key in self._state:
            self._state[key].update(extra)
            return
        self._state[key] = {"failures": 0, "first_seen": now, **extra}

    def clear(self, key: str) -> None:
        """成功即清退避状态。"""
        self._state.pop(key, None)


class AdminProgressMessages:
    """admin 进度消息组：首条 send、后续 edit（防同文 + 可选节流）、收尾 finalize。

    CD2 单任务进度（throttle=0，每轮一次）与 ed2k 批量进度（throttle=2s）
    共用；消息引用追踪、编辑失败剔除、首条全失败后本轮不再重试等细节收敛于此。

    bot_provider / admin_ids_provider 为 callable：
    - bot_provider() 返回 None → 全部操作静默跳过（开关未开 / bot 未就绪）
    - admin_ids_provider() 返回 admin id 列表（settings 热更新后即时生效）
    """

    def __init__(self, bot_provider, admin_ids_provider, *, throttle: float = 0.0) -> None:
        self._bot_provider = bot_provider
        self._admin_provider = admin_ids_provider
        self.throttle = throttle
        #: 当前消息引用 (chat_id, message_id)；空 = 无活跃消息
        self.msgs: list[tuple[int, int]] = []
        self._last_text = ""
        self._last_edit = 0.0
        self._send_failed = False  # 首条全失败 → 本周期不再重试

    @property
    def active(self) -> bool:
        return bool(self.msgs)

    def reset(self) -> None:
        """每轮开始时复位周期状态（保留/清空由调用方决定，等同全新一轮）。"""
        self.msgs = []
        self._last_text = ""
        self._send_failed = False

    async def send(self, text: str) -> bool:
        """给所有 admin 发首条进度消息；至少送达一个返回 True。"""
        if self.msgs or self._send_failed:
            return bool(self.msgs)
        bot = self._bot_provider()
        admins = list(self._admin_provider() or [])
        if bot is None or not admins:
            return False
        msgs: list[tuple[int, int]] = []
        for uid in admins:
            try:
                m = await bot.send_message(chat_id=uid, text=text)
                msgs.append((uid, m.message_id))
            except Exception as exc:  # noqa: BLE001 - 通知失败不影响主链路
                logger.warning("进度消息发送 admin %s 失败：%s", uid, exc)
        if not msgs:
            self._send_failed = True
            return False
        self.msgs = msgs
        self._last_text = text
        self._last_edit = time.monotonic()  # 节流窗口自首条发出开始
        return True

    async def edit(self, text: str) -> None:
        """编辑存活消息；同文跳过（防 Telegram not modified）；节流窗口内跳过。"""
        await self._edit(text, throttled=True)

    async def _edit(self, text: str, *, throttled: bool) -> None:
        if not self.msgs or text == self._last_text:
            return
        if throttled and self.throttle > 0 and time.monotonic() - self._last_edit < self.throttle:
            return
        bot = self._bot_provider()
        if bot is None:
            return
        alive: list[tuple[int, int]] = []
        for cid, mid in self.msgs:
            try:
                await bot.edit_message_text(chat_id=cid, message_id=mid, text=text)
                alive.append((cid, mid))
            except Exception as exc:  # noqa: BLE001
                logger.warning("进度消息编辑失败（%s）：%s", cid, exc)
        self.msgs = alive
        self._last_text = text
        self._last_edit = time.monotonic()

    async def finalize(self, text: str) -> bool:
        """编辑为最终文本（如 100% 或汇总）并复位，下轮可重新 send。

        终态必达：绕过节流窗口（对应原实现中汇总直接编辑进度消息的行为）。
        """
        if not self.msgs:
            return False
        await self._edit(text, throttled=False)
        had = bool(self.msgs)
        self.msgs = []
        self._last_text = ""
        self._send_failed = False
        return had
