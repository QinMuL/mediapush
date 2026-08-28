"""已推送分享失效巡检（借 P115-Share 定期 link check + 撤卡思路）。

背景：推送出去的 115 分享卡片会因分享者取消/违规/审核而失效，频道里
留着死链影响体验。巡检器定期：

1. cache.list_pushed_shares：provider=115、status='ok'，最久未检查优先
2. pan115.check_share_status：share_snap 单次快照（margin/快照渐进重试内置于 provider）
3. 失效（share_state=7 / 已取消 / 不存在）→ 撤卡（删除频道卡片消息）+ cache.mark_dead
4. 审核中/快照中/访问码问题 → touch_checked（暂不可判，下轮再看）
5. 正常 → touch_checked

- 网络异常不计死亡（下轮重试）；撤卡失败（消息已被删/无权限）也 mark_dead 防重复巡检
- ed2k 不巡检（分享的是磁力链 hash，无失效概念）
- /inspect 手动触发一轮；INSPECT_ENABLED=true 时后台每 INSPECT_INTERVAL_HOURS 小时跑
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class InspectReport:
    """一轮巡检结果。"""

    total: int = 0
    ok: int = 0
    dead: int = 0  # 判定失效并处理（撤卡）
    pending: int = 0  # 快照/审核等暂不可判
    errors: int = 0  # 网络/接口异常
    need_code: int = 0  # 存活但缺/失访问码（无法深读，非死链）
    dead_items: list[dict] = field(default_factory=list)  # 撤卡明细（告警用）
    code_items: list[dict] = field(default_factory=list)  # 访问码问题明细（提醒补档）

    def summary(self) -> str:
        s = f"巡检 {self.total}：✅ 存活 {self.ok}"
        if self.need_code:
            s += f"（含 {self.need_code} 条缺访问码）"
        s += f" · ⚰️ 失效撤卡 {self.dead} · ⏳ 待定 {self.pending}"
        if self.errors:
            s += f" · ⚠️ 异常 {self.errors}"
        return s


class ShareInspector:
    """巡检器：run_once 单轮；start/stop 后台循环（bot post_init 挂载）。"""

    def __init__(self, container, settings) -> None:
        self.container = container
        self.settings = settings
        self.interval = max(0.5, settings.inspect_interval_hours)
        self._task: asyncio.Task | None = None
        self._last_cookie_alert = float("-inf")  # 上次 cookie 告警时刻（24h 节流；-inf 保证首次必告警）

    # ------------------------------------------------------------------ #
    # Cookie 文件热更新 + 失效告警（借 P115-Share _check_cookie_freshness）
    # ------------------------------------------------------------------ #
    def _refresh_cookie_file(self) -> None:
        """重读 PAN115_COOKIE_FILE：内容变化 → provider.update_cookie 热生效。"""
        path = self.settings.pan115_cookie_file
        pan115 = self.container.pan115
        if not path or not pan115:
            return
        try:
            new_cookie = Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("cookie 文件读取失败（%s）：%s", path, exc)
            return
        if new_cookie and new_cookie != pan115.cookie:
            pan115.update_cookie(new_cookie)
            logger.info("cookie 文件已更新，热加载生效")

    async def _check_cookie_health(self) -> None:
        """cookie 健康检查：失效 → admin 告警（24h 节流）。匿名（无 cookie）跳过。"""
        pan115 = self.container.pan115
        telegram = self.container.telegram
        if pan115 is None or not pan115.cookie or telegram is None:
            return
        try:
            ok = await pan115.check_health()
        except Exception:  # noqa: BLE001
            ok = False
        if ok is not False:  # None=匿名可用；True=健康
            return
        logger.warning("115 cookie 已失效（仅影响健康检查与转存，匿名读取不受影响）")
        now = time.monotonic()
        if now - self._last_cookie_alert < 86400:
            return
        self._last_cookie_alert = now
        text = (
            "⚠️ 115 Cookie 已失效（已 24h 告警节流）。\n"
            "仅影响 /status 健康检查；匿名读取分享不受影响。\n"
            "更新方式：改 PAN115_COOKIE_FILE 文件内容（自动热加载）或 PAN115_COOKIE 环境变量后重启。"
        )
        for uid in self.settings.tg_admin_ids:
            try:
                await telegram.bot.send_message(chat_id=uid, text=text)
            except Exception as exc:  # noqa: BLE001
                logger.warning("cookie 告警发送失败（admin=%s）：%s", uid, exc)

    # ------------------------------------------------------------------ #
    async def run_once(self, limit: int = 50) -> InspectReport:
        """单轮巡检：检查 → 失效撤卡 → 汇总。"""
        report = InspectReport()
        pan115 = self.container.pan115
        cache = self.container.cache
        if pan115 is None or cache is None:
            logger.warning("巡检跳过：pan115/cache 未就绪")
            return report

        rows = await cache.list_pushed_shares(provider="115", limit=limit)
        report.total = len(rows)
        if not rows:
            logger.info("巡检：暂无可检查的已推送分享")
            return report

        bot = self.container.telegram.bot if self.container.telegram else None
        for row in rows:
            code = row["share_code"]
            title = row["title"] or code
            try:
                status = await pan115.check_share_status(code, row["password"] or None)
            except Exception as exc:  # noqa: BLE001 - 网络/限速异常下轮再看
                report.errors += 1
                logger.warning("巡检查询失败（%s）：%s", code, exc)
                await cache.touch_checked(code)
                continue

            if status.readable:
                report.ok += 1
                if status.need_code:
                    # 分享活着但缺/失访问码（errno 4100012/4100008）：
                    # 不撤卡；提醒补档（/edit 重推会存新码并重置状态）
                    report.need_code += 1
                    reason = "访问码已变更（卡片旧码失效）" if status.code_changed else "访问码未存档"
                    report.code_items.append(dict(row, title=title, reason=reason))
                    logger.info("巡检存活但缺访问码（%s）：%s", code, reason)
                await cache.touch_checked(code)
                continue

            if status.state == 7:
                # 确认失效：撤卡 + 标记（P115-Share 同款语义：7=失效/已取消）
                item = dict(row, title=title, reason=status.message)
                report.dead += 1
                report.dead_items.append(item)
                await self._revoke_card(bot, row, title, status.message)
                await cache.mark_dead(code)
            else:
                # 快照/审核/访问码等：暂不可判，下轮再看
                report.pending += 1
                logger.info("巡检待定（%s）：%s", code, status.message)
                await cache.touch_checked(code)

        logger.info("巡检完成：%s", report.summary())
        return report

    # ------------------------------------------------------------------ #
    async def _revoke_card(self, bot, row: dict, title: str, reason: str) -> None:
        """撤卡：删除频道卡片消息。无引用（旧数据）或删除失败仅告警。"""
        chat_id = row.get("chat_id") or ""
        message_id = row.get("message_id")
        if bot is None or not chat_id or message_id is None:
            logger.warning("失效分享无法撤卡（缺消息引用）：%s — %s", title, reason)
            return
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
            logger.info("撤卡成功：%s（%s）", title, reason)
        except Exception as exc:  # noqa: BLE001 - 消息可能已被删/无权限
            logger.warning("撤卡失败（%s）：%s", title, exc)

    # ------------------------------------------------------------------ #
    async def notify_admin(self, report: InspectReport) -> None:
        """撤卡明细通知 admin（每 admin 一条汇总）。"""
        telegram = self.container.telegram
        if telegram is None or not report.dead_items or not self.settings.tg_admin_ids:
            return
        lines = [f"⚰️ 分享失效巡检：{report.summary()}"]
        for it in report.dead_items[:20]:
            t = it["title"] or it["share_code"]
            lines.append(f"• {t}（{it['reason']}）")
        if len(report.dead_items) > 20:
            lines.append(f"… 共 {len(report.dead_items)} 条")
        text = "\n".join(lines)
        bot = telegram.bot
        for uid in self.settings.tg_admin_ids:
            try:
                await bot.send_message(chat_id=uid, text=text)
            except Exception as exc:  # noqa: BLE001
                logger.warning("巡检通知 admin %s 失败：%s", uid, exc)

    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """后台循环（bot post_init 挂载，与 monitor 同模式）。"""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "分享失效巡检已启动（间隔 %.1f 小时）", self.interval
        )

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _loop(self) -> None:
        # 启动先歇 2 分钟（避开启动高峰，等首批推送/巡检数据落库）
        await asyncio.sleep(120)
        while True:
            try:
                self._refresh_cookie_file()
                await self._check_cookie_health()
                report = await self.run_once()
                if report.dead and self.settings.inspect_notify:
                    await self.notify_admin(report)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("巡检轮失败（下轮重试）")
            await asyncio.sleep(self.interval * 3600)
