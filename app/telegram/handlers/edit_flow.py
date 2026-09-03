"""编辑模式：/edit 预览 → 编辑画质模块 → 确认推送；/cancel 取消。"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from app.core.link_parser import parse_share
from app.logging_config import make_trace_id, trace_id
from app.telegram.edit_session import MAX_QUALITY_EXTRA, EditSession, EditState
from app.telegram.handlers.common import (
    _DENY_TEXT,
    Pan115Error,
    _clear_session,
    _container,
    _edit,
    _edit_keyboard,
    _get_session,
    _is_admin,
    _set_session,
)
from app.telegram.pusher import render_caption, render_text

logger = logging.getLogger(__name__)


async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/edit <链接>：进入预览编辑模式（不直接推送频道）。

    流程：prepare（去重+读取+TMDB）→ 预览卡片+编辑键盘 → 按钮编辑 → 确认推送。
    去重检查在 prepare；标记已推送在确认推送成功后（取消不标记）。
    """
    if not _is_admin(update, context):
        await update.message.reply_text(_DENY_TEXT)
        return
    text = " ".join(context.args) if context.args else (update.message.text or "")
    parsed = parse_share(text)
    if not parsed:
        await update.message.reply_text(
            "❌ 无法识别链接。用法：/edit <115 分享链接 / 裸码 / ed2k 链接>"
        )
        return

    container = _container(context)
    # 已有 session：覆盖前先清理（旧预览按钮因 preview_message_id 校验而失效）
    if _get_session(context) is not None:
        _clear_session(context)

    # /edit 允许重推：查已推送状态仅用于预览提示，prepare 跳过去重
    already_pushed = await container.cache.is_pushed(parsed.code)

    loading = (
        "⏳ 正在解析 ed2k 资源 ..."
        if parsed.provider == "ed2k"
        else f"⏳ 正在读取分享 `{parsed.code}` ..."
    )
    placeholder = await update.message.reply_text(loading, parse_mode="Markdown")
    try:
        pr = await container.processor.prepare(parsed, skip_dedup=True)
    except Pan115Error as exc:
        await _edit(placeholder, f"❌ 115 错误：{exc}")
        return
    except Exception as exc:
        logger.exception("prepare 失败")
        await _edit(placeholder, f"❌ 处理失败：{exc}")
        return

    if not pr.ok:
        await _edit(placeholder, f"⚠️ {pr.message}")
        return

    session = EditSession(
        parsed=parsed,
        details=pr.details,
        media=pr.media,
        files=pr.files,
        provider=parsed.provider,
        already_pushed=already_pushed,
    )
    await _send_preview(update, context, session)
    hint = (
        "⚠️ 此资源已推送过，本次为重新推送。\n" if already_pushed else ""
    )
    hint += (
        "👆 预览已生成，点上方卡片按钮编辑画质模块后确认推送。\n"
        "• ✏️ 追加画质：发送推荐语/精品说明\n"
        "• 💎 精品：切换精品资源标记\n"
        "• /cancel 或 ❌ 取消"
    )
    await _edit(placeholder, hint)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cancel：取消当前编辑会话 / 登录会话（不推送、不标记已推送）。"""
    if not _is_admin(update, context):
        await update.message.reply_text(_DENY_TEXT)
        return
    monitor = getattr(_container(context), "monitor", None)
    if monitor is not None and monitor.login_active:
        await update.message.reply_text(await monitor.login_cancel())
        return
    session = _get_session(context)
    if session is None:
        await update.message.reply_text("ℹ️ 当前没有进行中的编辑或登录会话。")
        return
    _clear_session(context)
    await _edit_preview_text(context.bot, session, "❌ 已取消编辑，未推送频道。")


async def _send_preview(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: EditSession
) -> None:
    """发送预览卡片（带编辑键盘），记录预览消息引用到 session。"""
    from app.tmdb.client import TMDBHelper

    details = session.details
    media = session.media
    code = session.parsed.code
    password = session.parsed.password
    files = session.files
    provider = session.provider
    keyboard = _edit_keyboard(session)

    image_url = TMDBHelper.image_url(details)
    if image_url:
        caption = render_caption(
            details, media, code, password, files, provider,
            quality_extra=session.quality_extra, is_premium=session.is_premium,
        )
        try:
            msg = await update.message.reply_photo(
                photo=image_url,
                caption=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            session.preview_is_photo = True
        except Exception as exc:  # noqa: BLE001 - 配图发送失败回退文本
            logger.warning("预览 reply_photo 失败，回退文本：%s", exc)
            image_url = None

    if not image_url:
        text = render_text(
            details, media, code, password, files, provider,
            quality_extra=session.quality_extra, is_premium=session.is_premium,
        )
        msg = await update.message.reply_text(
            text, parse_mode="HTML", reply_markup=keyboard,
        )
        session.preview_is_photo = False

    session.preview_chat_id = msg.chat_id
    session.preview_message_id = msg.message_id
    _set_session(context, session)


async def _refresh_preview(bot, session: EditSession) -> None:
    """重新渲染并 edit 预览消息（内容+键盘）。吞 not-modified/限流。"""
    details = session.details
    media = session.media
    code = session.parsed.code
    password = session.parsed.password
    files = session.files
    provider = session.provider
    keyboard = _edit_keyboard(session)

    try:
        if session.preview_is_photo:
            caption = render_caption(
                details, media, code, password, files, provider,
                quality_extra=session.quality_extra, is_premium=session.is_premium,
            )
            await bot.edit_message_caption(
                chat_id=session.preview_chat_id,
                message_id=session.preview_message_id,
                caption=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            text = render_text(
                details, media, code, password, files, provider,
                quality_extra=session.quality_extra, is_premium=session.is_premium,
            )
            await bot.edit_message_text(
                chat_id=session.preview_chat_id,
                message_id=session.preview_message_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
    except Exception as exc:  # noqa: BLE001 - "message is not modified"/限流兜底
        logger.debug("刷新预览失败（可忽略）：%s", exc)


async def _edit_preview_text(bot, session: EditSession, text: str) -> None:
    """把预览消息 edit 成纯文本（无键盘）。用于编辑等待/取消/推送完成。"""
    try:
        if session.preview_is_photo:
            await bot.edit_message_caption(
                chat_id=session.preview_chat_id,
                message_id=session.preview_message_id,
                caption=text,
            )
        else:
            await bot.edit_message_text(
                chat_id=session.preview_chat_id,
                message_id=session.preview_message_id,
                text=text,
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("edit 预览文本失败（可忽略）：%s", exc)


async def on_edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """编辑键盘回调：edit_quality / toggle_premium / confirm_push / cancel_edit。

    Telegram 每个 callback query 只能 answer 一次：校验分支（无权限/会话失效）
    用弹窗 answer 并 return，正常路径过校验后统一 answer 一次。
    先 answer 再校验会吞掉弹窗（二次 answer 抛 BadRequest）。
    """
    q = update.callback_query
    if not _is_admin(update, context):
        await q.answer("⛔ 仅管理员可用", show_alert=True)
        return

    session = _get_session(context)
    # 陈旧按钮拦截：session 不存在或按钮不属于当前预览消息
    if session is None or session.preview_message_id != q.message.message_id:
        await q.answer("会话已失效，请重新 /edit", show_alert=True)
        return

    await q.answer()  # 正常路径 ack
    bot = context.bot
    data = q.data

    if data == "edit_quality":
        session.state = EditState.AWAITING_QUALITY
        _set_session(context, session)
        await _edit_preview_text(
            bot,
            session,
            "✏️ 请直接发送要追加到画质模块的推荐语/精品说明文本。\n"
            "（发送的文本将作为推荐语；如需推送新链接请先 /cancel）",
        )
        return

    if data == "toggle_premium":
        session.is_premium = not session.is_premium
        _set_session(context, session)
        await _refresh_preview(bot, session)
        return

    if data == "cancel_edit":
        _clear_session(context)
        await _edit_preview_text(bot, session, "❌ 已取消编辑，未推送频道。")
        return

    if data == "confirm_push":
        await _confirm_push(context, session)


async def _confirm_push(
    context: ContextTypes.DEFAULT_TYPE, session: EditSession
) -> None:
    """确认推送：二次去重 → push_share（带编辑覆写）→ mark_pushed。"""
    container = _container(context)
    bot = context.bot
    parsed = session.parsed

    # 二次去重：仅首次推送防并发；重推（already_pushed）跳过
    if not session.already_pushed and await container.cache.is_pushed(parsed.code):
        _clear_session(context)
        await _edit_preview_text(bot, session, "⚠️ 该链接已被推送过，已取消。")
        return

    pusher = container.pusher
    if pusher is None:
        await _edit_preview_text(bot, session, "⚠️ 推送器未就绪")
        return

    # 链路 trace：编辑模式确认推送与直推共用 tid 派生规则
    with trace_id(make_trace_id(parsed)):
        try:
            ok, msg, message_id, push_chat_id = await pusher.push_share(
                session.details, session.media, parsed.code, parsed.password,
                session.files, provider=session.provider,
                quality_extra=session.quality_extra, is_premium=session.is_premium,
            )
        except Exception as exc:
            logger.exception("确认推送失败")
            await _edit_preview_text(bot, session, f"⚠️ 推送失败：{exc}")
            return

        title = session.details.get("title") or session.media.title
        if ok:
            await container.cache.mark_pushed(
                parsed.code,
                provider=session.provider,
                password=parsed.password,
                chat_id=push_chat_id,
                message_id=message_id,
                title=title,
            )
            logger.info("编辑模式推送成功：%s", title)
            _clear_session(context)
            await _edit_preview_text(bot, session, f"✅ 已推送：{title}")
        else:
            await _edit_preview_text(bot, session, f"⚠️ 推送失败：{msg}")


async def _handle_quality_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: EditSession
) -> None:
    """AWAITING_QUALITY 状态：把用户文本作为推荐语，回到预览并刷新。"""
    text = (update.message.text or "").strip()
    if not text:
        return
    session.quality_extra = text[:MAX_QUALITY_EXTRA]
    session.state = EditState.PREVIEW
    _set_session(context, session)
    # 删除用户输入消息保持整洁（无权限则忽略）
    try:
        await update.message.delete()
    except Exception as exc:  # noqa: BLE001
        logger.debug("删除用户输入消息失败（可忽略）：%s", exc)
    await _refresh_preview(context.bot, session)
