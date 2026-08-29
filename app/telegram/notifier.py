"""admin 私信通知器：统一发送 + 节流 + 开关，取代各处手写 for-loop。

- notify_admins(bot, admins, text)：群发，单 admin 失败不影响其他（只记 warning）
- AdminNotifier：带 per-key 节流的告警（同一 key 在窗口内只发一次，避免异常风暴轰炸）
  静默恢复时可选发恢复通知（记 state，恢复且曾告警过才发）
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# TG 消息上限 4096，统一留余量
_MAX_TEXT = 3800


def _truncate(text: str) -> str:
    if len(text) > _MAX_TEXT:
        return text[:_MAX_TEXT] + "\n…（超长已截断）"
    return text


async def notify_admins(bot, admin_ids: list[int], text: str) -> None:
    """群发私信给所有 admin；失败只记日志不影响调用方主链路。"""
    if not admin_ids or not text:
        return
    text = _truncate(text)
    for uid in admin_ids:
        try:
            await bot.send_message(chat_id=uid, text=text)
        except Exception as exc:  # noqa: BLE001 - 通知失败不影响主链路
            logger.warning("私信 admin %s 失败：%s", uid, exc)


class AdminNotifier:
    """带节流的 admin 告警器（巡检/监控循环共用）。

    - alert(key, text)：同 key 在 throttle_seconds 内只发一次；
      恢复（resolve）后再次触发可重新告警
    - resolve(key, text=None)：标记恢复正常；text 非空则发恢复通知
      （仅当此前确实告警过，否则静默）
    """

    def __init__(self, bot, admin_ids: list[int], throttle_seconds: float = 3600.0) -> None:
        self.bot = bot
        self.admin_ids = admin_ids
        self.throttle_seconds = throttle_seconds
        self._last_alert: dict[str, float] = {}  # key -> 上次告警时刻
        self._active: dict[str, str] = {}  # key -> 告警文本（未恢复，用于恢复通知判断）

    async def alert(self, key: str, text: str) -> bool:
        """发送告警（节流）。返回是否实际发送。"""
        now = time.monotonic()
        last = self._last_alert.get(key)
        if last is not None and now - last < self.throttle_seconds:
            return False
        self._last_alert[key] = now
        self._active[key] = text
        await notify_admins(self.bot, self.admin_ids, text)
        return True

    async def resolve(self, key: str, text: str | None = None) -> bool:
        """标记恢复；曾告警过且 text 给定时发恢复通知。

        同时清除该 key 的节流记录：新一轮故障（如再次断连）应立即告警，
        不受上一次的窗口限制。
        """
        was_active = key in self._active
        self._active.pop(key, None)
        self._last_alert.pop(key, None)
        if was_active and text:
            await notify_admins(self.bot, self.admin_ids, text)
        return was_active
