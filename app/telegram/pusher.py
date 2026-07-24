"""推送卡片渲染 + Telegram 投递。

卡片两态：
- 有海报：send_photo + caption（caption 上限 1024 字符，用紧凑版）
- 无海报：send_message（文本上限 4096 字符，用完整版）

渲染为纯函数（render_text / render_caption），便于单测；push_share/push_pending 负责 IO。
"""
from __future__ import annotations

import html
import logging

from app.db import models, repository

logger = logging.getLogger(__name__)

TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
SHARE_LINK_BASE = "https://115.com/s/"

# Telegram 文本上限
TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024


def _fmt_ep_range(season: int | None, ep_start: int | None, ep_end: int | None) -> str:
    """季集区间：第 1 季 E02-05 / 第 1 季 全季 / 电影无。"""
    if season is None and ep_start is None:
        return ""
    if season is not None:
        if ep_start is None:
            return f"第 {season} 季 全季"
        rng = f"E{ep_start:02d}"
        if ep_end and ep_end != ep_start:
            rng += f"-{ep_end:02d}"
        return f"第 {season} 季 {rng}"
    # 仅集号，无季
    rng = f"E{ep_start:02d}"
    if ep_end and ep_end != ep_start:
        rng += f"-{ep_end:02d}"
    return rng


def render_text(share: models.Share, media: models.Media | None) -> str:
    """完整卡片（≤4096），用于纯文本消息。"""
    parts: list[str] = []
    title = (media.title if media and media.title else share.title) or "未知资源"
    year = media.year if media else None
    is_movie = bool(media and media.media_type == "movie")
    type_label = "电影" if is_movie else "剧集"

    header = f"🎬 <b>{html.escape(title)}</b>"
    if year:
        header += f" ({year})"
    parts.append(header)
    parts.append("")

    meta: list[str] = [f"📺 <b>类型</b>：{type_label}"]
    if media and not is_movie:
        rng = _fmt_ep_range(media.season, media.episode_start, media.episode_end)
        if rng:
            meta.append(f"🔢 <b>集数</b>：{rng}")
        if media.total_episodes:
            meta.append(f"📦 <b>总集数</b>：{media.total_episodes}")
    spec_bits = []
    if media and media.quality:
        spec_bits.append(media.quality)
    if media and media.audio:
        spec_bits.append(media.audio)
    if spec_bits:
        meta.append("🎞 <b>规格</b>：" + " | ".join(spec_bits))
    parts.append("\n".join(meta))

    if media and media.overview:
        overview = media.overview.strip()
        if len(overview) > 500:
            overview = overview[:500].rstrip() + "…"
        parts.append("")
        parts.append(f"<blockquote>{html.escape(overview)}</blockquote>")

    parts.append("")
    parts.append(f"🔗 <b>115 分享</b>：{SHARE_LINK_BASE}{share.share_code}")
    if share.share_password:
        parts.append(f"🔑 <b>访问码</b>：<code>{html.escape(share.share_password)}</code>")
    if share.file_count:
        parts.append(f"📁 <b>文件数</b>：{share.file_count}")

    text = "\n".join(parts)
    if len(text) > TEXT_LIMIT:
        text = text[: TEXT_LIMIT - 3] + "…"
    return text


def render_caption(share: models.Share, media: models.Media | None) -> str:
    """紧凑卡片（≤1024），用于海报图片 caption。"""
    parts: list[str] = []
    title = (media.title if media and media.title else share.title) or "未知资源"
    year = media.year if media else None
    is_movie = bool(media and media.media_type == "movie")

    header = f"🎬 <b>{html.escape(title)}</b>"
    if year:
        header += f" ({year})"
    parts.append(header)

    bits: list[str] = []
    if media and not is_movie:
        rng = _fmt_ep_range(media.season, media.episode_start, media.episode_end)
        if rng:
            bits.append(rng)
        if media.total_episodes:
            bits.append(f"共{media.total_episodes}集")
    if media and media.quality:
        bits.append(media.quality)
    if media and media.audio:
        bits.append(media.audio)
    if bits:
        parts.append(" | ".join(bits))

    parts.append(f"🔗 {SHARE_LINK_BASE}{share.share_code}")
    if share.share_password:
        parts.append(f"🔑 <code>{html.escape(share.share_password)}</code>")

    text = "\n".join(parts)
    if len(text) > CAPTION_LIMIT:
        text = text[: CAPTION_LIMIT - 3] + "…"
    return text


def poster_url(media: models.Media | None) -> str | None:
    if media and media.poster_path:
        return f"{TMDB_IMAGE_BASE}{media.poster_path}"
    return None


class Pusher:
    """推送器：渲染卡片并投递到 Telegram 频道。"""

    def __init__(self, telegram_service, chat_id: str, session_factory=None):
        self._tg = telegram_service
        self._chat_id = chat_id
        self._session_factory = session_factory

    async def push_share(self, share: models.Share, media: models.Media | None) -> bool:
        """推送单个分享。返回是否成功（失败仅记日志，不抛出）。"""
        try:
            poster = poster_url(media)
            if poster:
                caption = render_caption(share, media)
                await self._tg.send_photo(self._chat_id, poster, caption)
            else:
                text = render_text(share, media)
                await self._tg.send_message(self._chat_id, text)
            return True
        except Exception:
            logger.exception("推送失败: share_code=%s", share.share_code)
            return False

    async def push_pending(self, limit: int = 20) -> int:
        """扫尾：推送所有 pushed=False 的分享。返回成功推送数。

        由 scheduler 周期调用，或 /find 触发；捕获取消前中断的未推送分享。
        """
        if not self._session_factory:
            return 0
        pushed = 0
        async with self._session_factory() as session:
            shares = await repository.list_unpushed_shares(session, limit=limit)
        for share in shares:
            try:
                ok = await self.push_share(share, share.media)
                if ok:
                    async with self._session_factory() as session:
                        await repository.mark_share_pushed(session, share.id)
                    pushed += 1
            except Exception:  # noqa: BLE001
                logger.exception("push_pending 失败: id=%s", share.id)
        return pushed
