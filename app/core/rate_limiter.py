"""自适应限速器（令牌桶）：统一管控 115 请求节奏。

背景：此前限速散落各处且互不知情（巡检 1s/条、推送 2s、快照退避），
多任务并发时叠加触发 115 风控（margin 限速 / 405 封 IP）。

设计：
- 令牌桶：base_interval 控制稳态速率（默认 1 req/s），burst 允许小突发
- 自适应：hit_limit()（margin 响应等限速信号）→ interval 翻倍（上限
  max_interval）；连续 success streak 后逐步衰减回 base —— 115 限流时
  自动放慢，风控解除后自动恢复，无需人工调参
- acquire() 在锁内重算令牌（惰性补充），无后台任务

TG 侧推送限速（flood control）不属于此层，仍由 pusher 串行 + 间隔处理。
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class AdaptiveLimiter:
    """按 interval 节流的令牌桶，限速信号驱动 interval 自适应升降。"""

    def __init__(
        self,
        base_interval: float = 1.0,
        *,
        burst: int = 2,
        max_interval: float = 60.0,
        name: str = "115",
    ) -> None:
        self.base_interval = max(0.0, base_interval)
        self.interval = self.base_interval
        self.max_interval = max(self.interval, max_interval)
        self._capacity = max(1, burst)
        self._tokens = float(self._capacity)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()
        self._success_streak = 0
        self._name = name

    async def acquire(self) -> None:
        """取一个令牌；不足则等到可用（锁内计算等待，锁外 sleep 防长持锁）。"""
        while True:
            async with self._lock:
                now = time.monotonic()
                # 惰性补充令牌（按当前 interval）
                if self.interval > 0:
                    self._tokens = min(
                        self._capacity,
                        self._tokens + (now - self._last) / self.interval,
                    )
                else:
                    self._tokens = self._capacity
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) * self.interval
            await asyncio.sleep(wait)

    def hit_limit(self, reason: str = "") -> None:
        """限速信号（margin 响应等）：interval 翻倍，重置成功计数。"""
        new = min(self.interval * 2, self.max_interval)
        if new != self.interval:
            logger.warning(
                "[%s] 限速信号%s：%s → %.1fs（连续成功后将逐步回落）",
                self._name, f"（{reason}）" if reason else "", self.interval, new,
            )
        self.interval = new
        self._success_streak = 0

    def success(self, streak_to_decay: int = 20) -> None:
        """成功信号：连续 streak_to_decay 次后 interval 减半（向 base 衰减）。"""
        self._success_streak += 1
        if self._success_streak >= streak_to_decay:
            self._success_streak = 0
            new = max(self.base_interval, self.interval / 2)
            if new != self.interval:
                logger.info(
                    "[%s] 限速恢复：%.1fs → %.1fs", self._name, self.interval, new
                )
            self.interval = new

    def set_base_interval(self, interval: float) -> None:
        """/reload 热更新基础速率（当前值超新基准时立即收敛）。"""
        self.base_interval = max(0.0, interval)
        self.interval = max(self.base_interval, min(self.interval, self.max_interval))
        self.max_interval = max(self.base_interval, self.max_interval)
