"""流水线编排：扫描新分享 → 解析 → TMDB 匹配 → 去重入库 → 推送。

取消机制（旧项目约束）：
- asyncio.wait(FIRST_COMPLETED) 竞速 gather_task 与 stop_task
- _stop_event 在 _fetch_one 入口及每个文件后检查
- 停止时取消剩余任务，1-2 秒内中断
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import models, repository
from app.db.models import _now
from app.parser import parse_filename
from app.pipeline.context import PipelineContext

logger = logging.getLogger(__name__)


class Pipeline:
    def __init__(
        self,
        pan115_service,
        tmdb_service,
        session_factory: async_sessionmaker,
        context: PipelineContext | None = None,
        concurrency: int = 5,
        pusher=None,
    ):
        self._pan115 = pan115_service
        self._tmdb = tmdb_service
        self._session_factory = session_factory
        self.ctx = context or PipelineContext()
        self._concurrency = concurrency
        self._pusher = pusher

    def set_pusher(self, pusher) -> None:
        """注入/更换推送器（Web 配置 TG token 后由 container 调用）。"""
        self._pusher = pusher

    async def run(
        self,
        share_codes: list[tuple[str, str]],
        trigger: str = "scheduler",
    ) -> dict:
        """处理一批 (share_code, receive_code)。返回统计。"""
        if not self.ctx.start():
            logger.info("pipeline 已在运行，跳过本次")
            return {"skipped": True}
        try:
            return await self._run_inner(share_codes, trigger)
        finally:
            self.ctx.finish()

    async def _run_inner(self, share_codes, trigger):
        async with self._session_factory() as session:
            task_log = await repository.create_task_log(
                session, models.TaskLog(task_type="pipeline", trigger=trigger)
            )

            # 去重：批量 200 对/批
            existing = await repository.find_existing_shares(session, share_codes)
            new_codes = [p for p in share_codes if p not in existing]
            logger.info(
                "pipeline: 共 %d, 已存在 %d, 新增 %d",
                len(share_codes), len(existing), len(new_codes),
            )

            new_count = 0
            pushed_count = 0
            errors: list[dict] = []
            if new_codes:
                results = await self._fetch_details_concurrent(new_codes)
                for r in results:
                    if not r:
                        continue
                    if r.get("error"):
                        errors.append(r)
                    else:
                        new_count += 1
                        if r.get("pushed"):
                            pushed_count += 1

            status = "cancelled" if self.ctx.stop_requested else "success"
            await repository.update_task_log(
                session, task_log.id,
                status=status,
                finished_at=_now(),
                shares_new=new_count,
                shares_pushed=pushed_count,
                error="; ".join(str(e.get("error", "")) for e in errors)[:1000],
            )

            return {
                "total": len(share_codes),
                "existing": len(existing),
                "new": new_count,
                "pushed": pushed_count,
                "errors": errors,
                "cancelled": self.ctx.stop_requested,
            }

    async def _fetch_details_concurrent(self, codes):
        """并发抓取，_stop_event 可中断（FIRST_COMPLETED）。"""
        sem = asyncio.Semaphore(self._concurrency)
        tasks = [
            asyncio.create_task(self._fetch_one(sem, code, pwd))
            for code, pwd in codes
        ]
        if not tasks:
            return []
        gather_task = asyncio.ensure_future(
            asyncio.gather(*tasks, return_exceptions=True)
        )
        stop_task = asyncio.ensure_future(self.ctx.stop_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {gather_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if stop_task in done:
                # 停止：取消 gather 与所有子任务
                gather_task.cancel()
                for t in tasks:
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                logger.info("pipeline 已在抓取阶段停止")
                results = []
                for t in tasks:
                    if t.done() and not t.cancelled() and t.exception() is None:
                        results.append(t.result())
                return results
            return list(gather_task.result())
        finally:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)

    async def _fetch_one(self, sem, share_code, receive_code):
        """抓取单个分享：文件列表 → 解析 → TMDB 匹配 → 入库。

        入口与每个文件后检查 _stop_event。使用独立 session（async session 非并发安全）。
        """
        if self.ctx.stop_requested:
            return None
        async with sem:
            if self.ctx.stop_requested:
                return None
            try:
                # 1. 抓取文件列表（pan115 IO，无 DB）
                files = []
                async for f in self._pan115.iter_share_files(share_code, receive_code):
                    if self.ctx.stop_requested:
                        return None
                    files.append(f)
                if not files:
                    return {"share_code": share_code, "error": "无文件"}

                # 2. 解析第一个文件名
                first_name = files[0].get("name", "") or files[0].get("n", "")
                md = parse_filename(first_name)

                # 3. TMDB 匹配 + 入库（独立 session）
                async with self._session_factory() as session:
                    media = await self._match_and_save_media(session, md)

                    share = models.Share(
                        share_code=share_code,
                        share_password=receive_code,
                        title=md.title or first_name,
                        status="",  # 显式字符串（旧项目约束）
                        create_time="",
                        file_count=len(files),
                        size=sum(
                            int(f.get("size", 0) or f.get("s", 0) or 0) for f in files
                        ),
                        raw_files={"files": files[:50]},
                        media_id=media.id if media else None,
                    )
                    await repository.add_share(session, share)

                # 4. 推送（可选；停止时不推，留作 push_pending 扫尾）
                pushed = False
                if self._pusher and not self.ctx.stop_requested:
                    pushed = await self._pusher.push_share(share, media)
                    if pushed:
                        async with self._session_factory() as session:
                            await repository.mark_share_pushed(session, share.id)
                return {
                    "share_code": share_code,
                    "files": len(files),
                    "media_id": media.id if media else None,
                    "pushed": pushed,
                }
            except Exception as e:
                logger.exception("抓取失败: %s", share_code)
                return {"share_code": share_code, "error": str(e)}

    async def _match_and_save_media(self, session, md):
        """TMDB 搜索匹配 + upsert media。返回 Media 对象；失败不阻断入库。"""
        if not md.title:
            return None
        try:
            media_type = "tv" if md.season is not None else "tv"
            results = await self._tmdb.search(md.title, media_type, year=md.year)
            if not results:
                return None
            tmdb_id = results[0].get("id")
            if not tmdb_id:
                return None
            details = await self._tmdb.get_details(session, tmdb_id, media_type)
            if md.is_whole_season:
                total_eps = await self._tmdb.fill_episodes_from_season(
                    session, tmdb_id, md.season
                )
            else:
                total_eps = await self._tmdb.get_total_episodes(
                    session, tmdb_id, fallback=md.total_episodes
                )
            media = models.Media(
                tmdb_id=tmdb_id,
                media_type=media_type,
                title=details.get("name") or details.get("title") or md.title,
                original_title=details.get("original_name")
                or details.get("original_title", ""),
                year=md.year,
                season=md.season,
                episode_start=md.episode,
                episode_end=md.episode_end,
                total_episodes=total_eps,
                quality=md.quality,
                audio=md.audio,
                overview=details.get("overview", ""),
                poster_path=details.get("poster_path", ""),
            )
            return await repository.upsert_media(session, media)
        except Exception:
            logger.exception("TMDB 匹配失败: %s", md.title)
            return None
