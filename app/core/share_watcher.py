"""网盘目录监控 → 自动创建永久分享 → 推送卡片。

链路（用户决策：无转存环节，文件本就在自己网盘）：
1. 遍历 /dir add 登记的监控目录（cache.share_dirs）
2. pan115.list_dir(cid) 列子目录（nf=1 仅目录）
3. 未分享过的子目录 → pan115.create_share(fid) 建永久分享
   （share_send + duration=-1，margin 渐进重试内置于 provider）
4. processor.process(ParsedShare("115", share_code, receive_code)) 推卡片
   —— 完全复用手动推送管线（TMDB 匹配/卡片/分流/持久化去重/巡检撤卡）
5. mark_shared 记档；失败不标记 → 下轮重扫自动重试
6. 推送成功后移入归档目录（SHARE_ARCHIVE_DIR，空=不移动）

- 归档时机：推送成功 = 快照已生成、审核已通过（process 内部先 list_share），
  此时移动安全——115 分享绑定文件快照而非路径，移动不失效；且失效巡检兜底。
  移动失败仅告警不影响推送；ok 状态仍在监控目录 → 下轮补移（幂等闭环）。

- 需 115 cookie（建分享不支持匿名）；无 cookie 优雅跳过
- 后台每 SHARE_WATCH_INTERVAL_MINUTES 分钟一轮（挂 bot post_init）；
  /share 命令手动触发（两者都要，用户决策）
- 推送串行 + 2s 限速（与手动批处理同款，避免 flood control）
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from app.core.link_parser import ParsedShare
from app.core.service_base import PollingService, retry_backoff_seconds
from app.core.share_normalizer import ShareNormalizer
from app.logging_config import make_trace_id, trace_id
from app.providers.exceptions import Pan115Error

logger = logging.getLogger(__name__)

# 不可恢复错误关键词（115 账号风控，重试无意义）→ 标记 blocked，不再自动重试
_BLOCKED_KEYWORDS: tuple[str, ...] = ("违规", "禁止分享")
# blocked 的 next_retry_at 设为 365 天后（实质不再自动重试，需人工复位）
_BLOCKED_RETRY_OFFSET = 365 * 86400
# next_retry_at 距当前超过此阈值视为 blocked（区分普通退避 vs 违规永久跳过）
_BLOCKED_THRESHOLD = 30 * 86400


@dataclass
class WatchReport:
    """一轮目录监控结果。"""

    dirs: int = 0
    new_items: int = 0  # 发现的未分享子目录
    shared: int = 0  # 建分享+推送成功
    retried: int = 0  # 复用已建分享码重推成功（此前 pending/failed）
    auditing: int = 0  # 115 审核中/快照生成中（新分享正常中间态，非失败）
    failed: int = 0  # 本轮新失败（含违规 blocked）
    skipped: int = 0  # 已推送（ok）跳过
    blocked: int = 0  # 不可恢复（违规等），不再自动重试
    backoff: int = 0  # 退避中跳过（未到期，含 blocked 永久跳过）
    items: list[dict] = field(default_factory=list)  # 成功明细
    failed_items: list[dict] = field(default_factory=list)  # 失败明细（含原因）
    audit_items: list[dict] = field(default_factory=list)  # 审核中明细

    def summary(self) -> str:
        s = (
            f"扫描 {self.dirs} 个目录：新 {self.new_items}"
            f" → ✅ 推送 {self.shared + self.retried}"
        )
        if self.retried:
            s += f"（含复用重试 {self.retried}）"
        if self.auditing:
            s += f" · ⏳ 115 审核中 {self.auditing}（下轮复用码重试）"
        if self.failed:
            s += f" · ⚠️ 失败 {self.failed}"
        if self.blocked:
            s += f" · 🚫 违规 {self.blocked}（不再自动重试）"
        if self.backoff:
            s += f" · ⏳ 退避中 {self.backoff}"
        if self.skipped:
            s += f" · ⏭️ 已分享 {self.skipped}"
        return s

    def has_events(self) -> bool:
        """本轮是否有值得通知的事件（静默轮不打扰）。"""
        return bool(self.items or self.failed_items or self.audit_items)


class ShareWatcher(PollingService):
    """目录监控服务：run_once 单轮；循环骨架见 PollingService。"""

    name = "share_watch"
    log_prefix = "目录监控"
    interval_scale = 60.0      # interval 单位为分钟
    startup_delay = 60.0       # 启动先歇 1 分钟（等 bot/巡检/监控就绪，避开启动高峰）

    def __init__(self, container, settings) -> None:
        self.container = container
        self.settings = settings
        self.interval = max(1.0, settings.share_watch_interval_minutes)
        self._archive_cid: int | None = None  # 归档目录 CID（进程内缓存）
        self._normalizer = ShareNormalizer(container, settings)

    async def before_round(self) -> None:
        # 每轮重读 cookie 文件：目录监控建分享需登录态，换 cookie 无需重启
        self.container.refresh_cookie_file()

    async def after_round(self, report) -> None:
        # 静默轮（无成功/失败/审核事件）不打扰；详情推送成功与失败都通知
        if report.has_events() and getattr(self.settings, "share_watch_notify", True):
            await self.notify_admin(report)

    # ------------------------------------------------------------------ #
    async def run_once(self) -> WatchReport:
        """单轮扫描：列目录 → 新子目录建分享 → 推卡片 → 标记。"""
        report = WatchReport()
        pan115 = self.container.pan115
        cache = self.container.cache
        processor = self.container.processor

        dirs = await cache.list_share_dirs()
        report.dirs = len(dirs)
        if not dirs:
            return report
        if pan115 is None or processor is None:
            logger.warning("目录监控跳过：pan115/processor 未就绪")
            return report
        if not pan115.cookie:
            logger.warning(
                "目录监控跳过：未配置 115 cookie（PAN115_COOKIE / PAN115_COOKIE_FILE，"
                "创建分享需登录态）"
            )
            return report

        for d in dirs:
            dir_id, path, cid = int(d["id"]), d["path"], int(d["cid"])
            try:
                subdirs = await pan115.list_dir(cid, nf=0)
            except Exception as exc:  # noqa: BLE001 - 网络异常下轮再看（warning 摘要）
                report.failed += 1
                logger.warning("目录监控列目录失败（%s）：%s", path, exc)
                continue

            for item in subdirs:
                fid, name = item["fid"], item["name"]
                is_dir = item.get("is_dir", True)
                record = await cache.get_shared_item(dir_id, fid)
                if record is not None and record["status"] == "ok":
                    report.skipped += 1
                    # ok 却仍在监控目录（上次移动失败/归档中途启用）→ 补移
                    await self._try_archive(pan115, fid, name)
                    continue
                # failed 状态：退避中或违规 blocked → 未到期跳过
                if record is not None and record["status"] == "failed":
                    now = time.time()
                    if record["next_retry_at"] > now:
                        if record["next_retry_at"] - now > _BLOCKED_THRESHOLD:
                            report.blocked += 1
                        else:
                            report.backoff += 1
                        continue
                    # 到期：放行重试（record 传下去；share_code 空则重建分享）

                if record is None:
                    report.new_items += 1
                    # 分享前目录结构标准化（幂等，失败不阻断）
                    if self._normalizer.enabled:
                        nr = await self._normalizer.normalize(
                            pan115, fid, name, is_dir, cid
                        )
                        if nr.changed:
                            logger.info(
                                "目录标准化：%s → %s\n  %s",
                                name, nr.name, "\n  ".join(nr.actions),
                            )
                        fid = nr.fid
                        name = nr.name
                # pending：复用已建的 share_code 重推（新分享常处审核/快照中，
                # 读取失败 → 登记 pending → 下轮复用同码重试，绝不重复建分享）
                try:
                    is_retry = record is not None and bool(record.get("share_code"))
                    await self._share_and_push(
                        processor, cache, pan115, dir_id, fid, name, record
                    )
                except Pan115Error as exc:
                    # 审核中/快照生成中：新分享的正常中间态，非失败
                    # （分享码已登记 pending，下轮复用码重试即可）
                    msg = str(exc)
                    if "审核中" in msg or "快照" in msg:
                        report.auditing += 1
                        report.audit_items.append({"dir": path, "name": name})
                        logger.info(
                            "目录监控：115 审核中（%s/%s），分享码已登记，下轮重试",
                            path, name,
                        )
                    else:
                        await self._record_failure(
                            cache, report, dir_id, fid, name, record, msg, path
                        )
                except Exception as exc:  # 推送失败等：登记退避，码留存下轮复用
                    msg = str(exc) or exc.__class__.__name__
                    await self._record_failure(
                        cache, report, dir_id, fid, name, record, msg, path
                    )
                    logger.exception("目录监控处理失败（%s/%s）", path, name)
                else:
                    if is_retry:
                        report.retried += 1
                    else:
                        report.shared += 1
                    report.items.append({"dir": path, "name": name})
                    # 限速：连续推送避免 TG 频道 flood control
                    await asyncio.sleep(2)

        if report.new_items or report.retried or report.auditing or report.failed:
            logger.info("目录监控完成：%s", report.summary())
        return report

    async def _record_failure(
        self, cache, report, dir_id: int, fid: int, name: str,
        record: dict | None, msg: str, path: str,
    ) -> None:
        """登记失败：违规→blocked（不再自动重试）；否则指数退避（1h→…→24h）。

        upsert 保留已有 share_code（pending 推送失败时码留存，下轮复用）。
        """
        now = time.time()
        fail_count = (record or {}).get("fail_count", 0)
        blocked = any(k in msg for k in _BLOCKED_KEYWORDS)
        if blocked:
            next_retry = now + _BLOCKED_RETRY_OFFSET
            count = fail_count
            report.blocked += 1
            suffix = "（已标记违规，不再自动重试）"
        else:
            count = fail_count + 1
            next_retry = now + retry_backoff_seconds(count)
            suffix = f"（{retry_backoff_seconds(count) / 3600:.0f}h 后重试）"
        await cache.record_share_failed(
            dir_id, fid, name, fail_count=count, next_retry_at=next_retry, reason=msg
        )
        report.failed += 1
        report.failed_items.append({"dir": path, "name": name, "reason": msg})
        logger.warning("目录监控处理失败（%s/%s）：%s%s", path, name, msg, suffix)

    async def _share_and_push(
        self, processor, cache, pan115,
        dir_id: int, fid: int, name: str, record: dict | None,
    ) -> None:
        """单个子目录：建永久分享（或复用已有码）→ 推卡片 → 标记 ok。

        三阶段防重复建分享：
        1. record None / failed 无码 → create_share → record_share(pending)
        2. record pending / failed 有码 → 复用 share_code/password 重推 → mark_shared(ok)
        3. record failed 退避中 → run_once 已跳过，不进入此方法
        新分享常处"审核中/快照生成中"（share_snap 预检不过），首次推送易失败
        ——登记后下轮复用同码重试，不会在账户里堆重复分享。
        """
        if record is None or not record.get("share_code"):
            share_code, receive_code = await pan115.create_share(fid)
            # 建分享成功立即登记：即使推送失败，下轮也复用此码
            await cache.record_share(
                dir_id, fid, name, share_code, receive_code or ""
            )
            # 新分享预检常在审核/快照中，等一会让 115 就绪再推
            await asyncio.sleep(5)
        else:
            share_code = record["share_code"]
            receive_code = record["password"] or ""
            logger.info("目录监控复用分享码重推：%s（%s）", name, share_code)

        parsed = ParsedShare("115", share_code, receive_code or None)
        # 链路 trace：与手动推送共用 tid 派生规则（分享码前缀），便于交叉排查
        with trace_id(make_trace_id(parsed)):
            result = await processor.process(parsed)
            if not result.ok:
                # 推送失败但 is_pushed 命中（此前已推过）→ 视为完成
                if result.dup:
                    await cache.mark_shared(dir_id, fid)
                    logger.info("目录监控：分享已推送过，直接标记完成：%s", name)
                    await self._try_archive(pan115, fid, name)
                    return
                raise RuntimeError(result.message)
            await cache.mark_shared(dir_id, fid)
            logger.info("目录监控推送成功：%s（分享码 %s）", name, share_code)
            await self._try_archive(pan115, fid, name)

    # ------------------------------------------------------------------ #
    # 归档：推送成功后移入全局归档目录（SHARE_ARCHIVE_DIR，空=不启用）
    @property
    def archive_enabled(self) -> bool:
        return bool(getattr(self.settings, "share_archive_dir", ""))

    async def _try_archive(self, pan115, fid: int, name: str) -> None:
        """移动已分享目录到归档目录；失败仅告警（不影响推送结果，下轮补移）。"""
        if not self.archive_enabled:
            return
        try:
            await self._move_to_archive(pan115, fid, name)
        except Exception as exc:  # noqa: BLE001 - 归档失败不影响推送主链路
            logger.warning("目录监控：移入归档失败（%s，下轮补移）：%s", name, exc)
            return
        # 115 移动为服务端异步执行：提交成功后留间隔，避免下一个移动
        # 撞上"上一个尚未执行完成"（errno 990009）；超大目录仍有重试兜底
        await asyncio.sleep(3)

    async def _move_to_archive(self, pan115, fid: int, name: str) -> None:
        cid = await self._archive_dir_cid(pan115)
        try:
            await pan115.fs_move(fid, cid)
        except Pan115Error:
            # 缓存 CID 可能已失效（session 过期/目录被删）→ 清缓存，下轮重解析
            self._archive_cid = None
            raise
        logger.info(
            "目录监控：%s 已移入归档目录 %s",
            name, getattr(self.settings, "share_archive_dir", ""),
        )

    async def _archive_dir_cid(self, pan115) -> int:
        """归档目录 CID：懒解析 + 进程内缓存（fs_makedirs 幂等，已存在返回现有值）。"""
        path = str(getattr(self.settings, "share_archive_dir", ""))
        if self._archive_cid is None:
            self._archive_cid = await pan115.fs_makedirs(path)
            logger.info("归档目录就绪：%s（CID %d）", path, self._archive_cid)
        return self._archive_cid

    # ------------------------------------------------------------------ #
    async def notify_admin(self, report: WatchReport) -> None:
        """任务详情通知 admin：按 推送成功/审核中/失败 分组（层级分明）。

        组间空行 + 组标题带计数；条目「名字 — 监控目录」缩进两格对齐，
        失败原因跟在条目后（超长截断）。
        """
        from app.telegram.notifier import format_round_report, notify_admins

        telegram = getattr(self.container, "telegram", None)
        admins = getattr(self.settings, "tg_admin_ids", []) or []
        if telegram is None or not admins:
            return

        def _group(icon: str, label: str, items: list[dict],
                   limit: int, render) -> list[str]:
            if not items:
                return []
            lines = [f"{icon} {label}（{len(items)}）"]
            lines.extend(f"  • {render(it)}" for it in items[:limit])
            if len(items) > limit:
                lines.append(f"  … 共 {len(items)} 个")
            return lines

        groups: list[list[str]] = [
            _group("✅", "推送成功", report.items, 20,
                   lambda it: f"{it['name']} — {it['dir']}"),
            _group("⏳", "115 审核中（下轮复用码重试）", report.audit_items, 10,
                   lambda it: f"{it['name']} — {it['dir']}"),
            _group("⚠️", "失败", report.failed_items, 10,
                   lambda it: f"{it['name']} — {it['dir']}：{it['reason'][:120]}"),
        ]
        details: list[str] = []
        for g in groups:
            if g:
                if details:
                    details.append("")  # 组间空行
                details.extend(g)
        text = format_round_report("📂", "目录监控扫描", report.summary(), details)
        await notify_admins(telegram.bot, admins, text)

    # ------------------------------------------------------------------ #
    # 生命周期钩子（循环骨架见 PollingService）
    # ------------------------------------------------------------------ #
    def _on_start(self) -> None:
        self.log.info("目录监控已启动（间隔 %.0f 分钟）", self.interval)
