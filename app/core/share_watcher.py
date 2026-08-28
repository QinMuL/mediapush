"""网盘目录监控 → 自动创建永久分享 → 推送卡片。

链路（用户决策：无转存环节，文件本就在自己网盘）：
1. 遍历 /dir add 登记的监控目录（cache.share_dirs）
2. pan115.list_dir(cid) 列子目录（nf=1 仅目录）
3. 未分享过的子目录 → pan115.create_share(fid) 建永久分享
   （share_send + duration=-1，margin 渐进重试内置于 provider）
4. processor.process(ParsedShare("115", share_code, receive_code)) 推卡片
   —— 完全复用手动推送管线（TMDB 匹配/卡片/分流/持久化去重/巡检撤卡）
5. mark_shared 记档；失败不标记 → 下轮重扫自动重试

- 需 115 cookie（建分享不支持匿名）；无 cookie 优雅跳过
- 后台每 SHARE_WATCH_INTERVAL_MINUTES 分钟一轮（挂 bot post_init）；
  /share 命令手动触发（两者都要，用户决策）
- 推送串行 + 2s 限速（与手动批处理同款，避免 flood control）
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from app.core.link_parser import ParsedShare
from app.providers.exceptions import Pan115Error

logger = logging.getLogger(__name__)


@dataclass
class WatchReport:
    """一轮目录监控结果。"""

    dirs: int = 0
    new_items: int = 0  # 发现的未分享子目录
    shared: int = 0  # 建分享+推送成功
    retried: int = 0  # 复用已建分享码重推成功（此前 pending）
    auditing: int = 0  # 115 审核中/快照生成中（新分享正常中间态，非失败）
    failed: int = 0  # 建分享/推送失败（下轮重试，复用已建的码）
    skipped: int = 0  # 已推送（ok）跳过
    items: list[dict] = field(default_factory=list)  # 成功明细

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
            s += f" · ⚠️ 失败 {self.failed}（下轮复用分享码重试）"
        if self.skipped:
            s += f" · ⏭️ 已分享 {self.skipped}"
        return s


class ShareWatcher:
    """目录监控服务：run_once 单轮；start/stop 后台循环。"""

    def __init__(self, container, settings) -> None:
        self.container = container
        self.settings = settings
        self.interval = max(1.0, settings.share_watch_interval_minutes)
        self._task: asyncio.Task | None = None

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
                subdirs = await pan115.list_dir(cid)
            except Exception as exc:  # noqa: BLE001 - 网络异常下轮再看（warning 摘要）
                report.failed += 1
                logger.warning("目录监控列目录失败（%s）：%s", path, exc)
                continue

            for item in subdirs:
                fid, name = item["fid"], item["name"]
                record = await cache.get_shared_item(dir_id, fid)
                if record is not None and record["status"] == "ok":
                    report.skipped += 1
                    continue

                if record is None:
                    report.new_items += 1
                # pending：复用已建的 share_code 重推（新分享常处审核/快照中，
                # 读取失败 → 登记 pending → 下轮复用同码重试，绝不重复建分享）
                try:
                    is_retry = record is not None
                    await self._share_and_push(
                        processor, cache, pan115, dir_id, fid, name, record
                    )
                except Pan115Error as exc:
                    # 审核中/快照生成中：新分享的正常中间态，非失败
                    # （分享码已登记 pending，下轮复用码重试即可）
                    msg = str(exc)
                    if "审核中" in msg or "快照" in msg:
                        report.auditing += 1
                        logger.info(
                            "目录监控：115 审核中（%s/%s），分享码已登记，下轮重试",
                            path, name,
                        )
                    else:
                        report.failed += 1
                        logger.warning(
                            "目录监控处理失败（%s/%s）：%s", path, name, exc
                        )
                except Exception:  # 失败保留 pending 记录，下轮复用码重试
                    report.failed += 1
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

    async def _share_and_push(
        self, processor, cache, pan115,
        dir_id: int, fid: int, name: str, record: dict | None,
    ) -> None:
        """单个子目录：建永久分享（或复用 pending 码）→ 推卡片 → 标记 ok。

        两阶段防重复建分享：
        1. record None → create_share → record_share(pending)【建分享即登记】
        2. record pending → 复用 share_code/password 重推 → 成功 mark_shared(ok)
        新分享常处"审核中/快照生成中"（share_snap 预检不过），首次推送易失败
        ——登记后下轮复用同码重试，不会在账户里堆重复分享。
        """
        if record is None:
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
        result = await processor.process(parsed)
        if not result.ok:
            # 推送失败但 is_pushed 命中（此前已推过）→ 视为完成
            if result.dup:
                await cache.mark_shared(dir_id, fid)
                logger.info("目录监控：分享已推送过，直接标记完成：%s", name)
                return
            raise RuntimeError(result.message)
        await cache.mark_shared(dir_id, fid)
        logger.info("目录监控推送成功：%s（分享码 %s）", name, share_code)

    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """后台循环（bot post_init 挂载）。"""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())
        logger.info("目录监控已启动（间隔 %.0f 分钟）", self.interval)

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _loop(self) -> None:
        # 启动先歇 1 分钟（等 bot/巡检/监控全部就绪，避开启动高峰）
        await asyncio.sleep(60)
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("目录监控轮失败（下轮重试）")
            await asyncio.sleep(self.interval * 60)
