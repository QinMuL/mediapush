"""速率限制关键词监控：扫描日志缓冲，冷却告警管理员。

旧项目约束（ARCHITECTURE.md 第 8 节）：高频间隔下监控 frequent/rate limit 关键词，
考虑增大调度间隔到 10-15 分钟。本模块扫描内存日志缓冲，命中关键词且过冷却期则
通过 Telegram 告警管理员，避免刷屏。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from app.core.logging import memory_handler

logger = logging.getLogger(__name__)

# 命中即视为速率限制/风控征兆
KEYWORDS = [
    "rate limit", "too many requests", "frequent",
    "请求过于频繁", "风控", "频繁",
]
_ALERT_COOLDOWN_MIN = 30  # 告警冷却，避免刷屏
_SCAN_LINES = 500


class RateLimitMonitor:
    """扫描日志缓冲，命中 rate-limit 关键词时冷却告警。"""

    def __init__(self, container):
        self._container = container
        self._last_alert_at: datetime | None = None
        self._pattern = re.compile(
            "|".join(re.escape(k) for k in KEYWORDS), re.IGNORECASE
        )

    def scan(self, lines: list[str]) -> list[str]:
        """返回命中的日志行（可单测，不依赖全局缓冲）。"""
        return [ln for ln in lines if self._pattern.search(ln)]

    async def check(self) -> int:
        """扫描最近日志，命中且过冷却期则告警。返回命中条数。"""
        matched = self.scan(memory_handler.recent(_SCAN_LINES))
        if not matched:
            return 0

        now = datetime.now()
        if (
            self._last_alert_at is not None
            and now - self._last_alert_at < timedelta(minutes=_ALERT_COOLDOWN_MIN)
        ):
            return len(matched)  # 冷却中，不重复告警

        sample = matched[-1][:200]
        text = (
            "⚠️ 115 速率限制告警\n"
            f"近 {_SCAN_LINES} 条日志命中 {len(matched)} 条 rate-limit/风控关键词。\n"
            f"样例：{sample}\n"
            "建议：在 Web 后台增大调度间隔（10-15 分钟）或检查 115 cookie。"
        )
        sent = await self._container.send_alert(text)
        if sent:
            self._last_alert_at = now
            logger.warning("rate-limit 监控：已告警管理员（%d 条命中）", len(matched))
        return len(matched)

    def reset_cooldown(self) -> None:
        """单测辅助：重置冷却状态。"""
        self._last_alert_at = None
