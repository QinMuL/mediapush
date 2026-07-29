"""分享处理编排：读取 → 解析 → TMDB 匹配 → 推送 → 去重标记。

无 Web/调度/watchdog，由 handlers 在 Bot 命令/裸链接中调用。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.parser.media_parser import analyze_share

logger = logging.getLogger(__name__)


@dataclass
class ProcessResult:
    ok: bool
    message: str
    file_count: int = 0
    title: str = ""
    year: int | None = None
    media_type: str = ""


class ShareProcessor:
    def __init__(self, pan115, ed2k, tmdb, cache, container) -> None:
        self.pan115 = pan115
        self.ed2k = ed2k
        self.tmdb = tmdb
        self.cache = cache
        self.container = container  # 懒取 pusher（bot 启动后才就绪）

    def _select_provider(self, provider: str):
        """按 provider 名选网盘读取器。"""
        if provider == "ed2k":
            return self.ed2k
        return self.pan115

    async def process(self, parsed) -> ProcessResult:
        provider = self._select_provider(parsed.provider)
        if provider is None:
            return ProcessResult(False, f"{parsed.provider} 网盘未配置")
        if self.tmdb is None:
            return ProcessResult(False, "TMDB 未配置（TMDB_API_KEY）")

        # 1. 去重
        if await self.cache.is_pushed(parsed.code):
            return ProcessResult(False, f"分享 {parsed.code} 已推送过，跳过")

        # 2. 读取分享内容（ed2k 为纯字符串解析，115 为联网读取）
        files = await provider.list_share(parsed.code, parsed.password)
        logger.info("读取分享：%d 个条目（%s）", len(files), parsed.provider)

        # 3. 文件名聚合
        media = analyze_share(files)
        if not media or not media.title:
            return ProcessResult(
                False, "未能从文件名解析出媒体信息", file_count=len(files)
            )

        # 4. TMDB 匹配
        # 优先用文件名/目录名标注的 {tmdb-XXX}（分享者明确指定，最可靠）
        tmdb_id = None
        mtype = media.media_type
        details = None
        if media.tmdb_id:
            try:
                details = await self.tmdb.get_details(media.tmdb_id, mtype)
                tmdb_id = media.tmdb_id
                logger.info("命中标注 TMDB ID：%s（%s）", tmdb_id, mtype)
            except Exception as exc:  # noqa: BLE001 - 标注 ID 失效时回退搜索
                logger.warning("标注 TMDB ID %s 获取失败，回退搜索：%s", media.tmdb_id, exc)

        if tmdb_id is None:
            best = await self.tmdb.search_best(media.title, media.year, media.media_type)
            if not best:
                return ProcessResult(
                    False,
                    f"TMDB 未匹配到：{media.title}"
                    + (f" ({media.year})" if media.year else ""),
                    file_count=len(files),
                    title=media.title,
                    year=media.year,
                )
            tmdb_id, mtype = best
            details = await self.tmdb.get_details(tmdb_id, mtype)

        logger.info(
            "TMDB 命中：%s（id=%s，%s）",
            details.get("title") or media.title, tmdb_id, mtype,
        )

        # 6. 推送
        pusher = self.container.pusher
        if pusher is None:
            return ProcessResult(
                False, "推送器未就绪", file_count=len(files), title=media.title
            )
        ok, msg = await pusher.push_share(
            details, media, parsed.code, parsed.password, files, provider=parsed.provider
        )
        title = details.get("title") or media.title
        if ok:
            logger.info("推送频道成功：%s", title)
        else:
            logger.warning("推送频道失败：%s — %s", title, msg)

        # 7. 标记已推送
        if ok:
            await self.cache.mark_pushed(parsed.code)

        return ProcessResult(
            ok=ok,
            message=msg,
            file_count=len(files),
            title=details.get("title") or media.title,
            year=details.get("year") or media.year,
            media_type=mtype,
        )
