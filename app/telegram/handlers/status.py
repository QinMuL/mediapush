"""状态类命令：/status（五块总览）/ed2k_status /upload_status。"""

from __future__ import annotations

import logging
import time

from telegram import Update
from telegram.ext import ContextTypes

from app.telegram.handlers.common import (
    _DENY_TEXT,
    _STARTED_AT,
    _container,
    _fmt_uptime,
    _is_admin,
)

logger = logging.getLogger(__name__)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update, context):
        await update.message.reply_text(_DENY_TEXT)
        return
    container = _container(context)
    s = container.settings
    lines: list[str] = []

    # 1) 运行概览（动态统计）
    lines.append("🤖 运行概览")
    lines.append(f"• 运行时长：{_fmt_uptime(time.monotonic() - _STARTED_AT)}")
    # 入站收包新鲜度（配合 bot.py 入站看门狗；None=启动后尚无任何消息）
    last_in = context.application.bot_data.get("_last_update_at")
    if last_in is not None:
        lines.append(f"• 入站收包：{_fmt_uptime(time.monotonic() - last_in)}前")
    else:
        lines.append("• 入站收包：启动后暂无消息")
    try:
        st = await container.cache.stats()
        lines.append(
            f"• 已推送分享 {st['pushed']} · 失效撤卡 {st['dead']}"
            f" · TMDB 缓存 {st['tmdb_cache']} 条"
        )
        if st["share_dirs"]:
            lines.append(
                f"• 监控目录 {st['share_dirs']} 个 · 已分享子目录 {st['shared_items']} 个"
            )
    except Exception:  # noqa: BLE001 - 统计失败不阻断整体展示
        lines.append("• 运行统计：暂不可用")
    lines.append("")

    # 2) 健康与配置（静态 + 实时探活）
    lines.append("🩺 健康与配置")
    lines.append(
        f"• TG Bot：{'✅' if s.tg_bot_token else '❌ 未配置'}"
        f" · TMDB Key：{'✅' if s.tmdb_api_key else '❌ 未配置'}"
    )
    if container.pan115 is not None:
        try:
            ok = await container.pan115.check_health()
            if ok is None:
                lines.append("• 115 健康：✅ 匿名读取可用（cookie 未配置）")
            else:
                lines.append(f"• 115 健康：{'✅ 正常' if ok else '❌ cookie 失效'}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"• 115 健康：❌ {exc}")
    # cookie 状态按 provider 运行时状态显示（文件方式热加载后 .env 字段仍为空，
    # 只读配置字段会误报"未配置"）
    if container.pan115 is not None and container.pan115.cookie:
        source = (
            "直配" if s.pan115_cookie_direct
            else f"文件 {s.pan115_cookie_file}" if s.pan115_cookie_file
            else "运行时"
        )
        lines.append(f"• 115 Cookie：✅ {source}（UID {container.pan115.uid or '-'}）")
    else:
        lines.append("• 115 Cookie：未配置（匿名读取，可用）")
    lines.append(f"• 代理：{s.proxy_url or '未配置'}")
    lines.append("")

    # 3) 频道
    lines.append("📡 频道")
    lines.append(f"• 默认：{s.tg_chat_id or '❌ 未配置'}")
    lines.append(f"• 网盘：{s.tg_chat_id_115 or '⬇️ 同默认'}")
    lines.append(f"• ed2k：{s.tg_chat_id_ed2k or '⬇️ 同默认'}")
    lines.append("")

    # 4) 常驻任务（115 侧 + 频道侧）
    lines.append("⚙️ 常驻任务")
    lines.append(
        f"• 失效巡检：{'✅' if s.inspect_enabled else '⬜ 未启用'}"
        + (f"（每 {s.inspect_interval_hours:g} 小时，/inspect 手动触发）"
           if s.inspect_enabled else "（INSPECT_ENABLED=false）")
    )
    lines.append(
        f"• 目录监控：{'✅' if s.share_watch_enabled else '⬜ 未启用'}"
        + (f"（每 {s.share_watch_interval_minutes:g} 分钟，/share 手动触发）"
           if s.share_watch_enabled else "（SHARE_WATCH_ENABLED=false）")
    )
    monitor = getattr(container, "monitor", None)
    if monitor is None:
        lines.append("• 频道监控：⬜ 未启用（MONITOR_ENABLED=false）")
    elif monitor.login_active:
        desc = monitor.login_stage_desc or "会话已过期"
        lines.append(f"• 频道监控：⏳ 登录进行中（{desc}，/cancel 可中止）")
    else:
        lines.append(f"• 频道监控：{await _monitor_state_line(monitor)}")
    console_lvl = logging.getLogger().handlers[0].level if logging.getLogger().handlers else "?"
    lines.append(f"• 控制台日志：{logging.getLevelName(console_lvl)}（/loglevel 调整）")
    lines.append("")

    # 5) 本地媒体流水线（统一服务：A → B 资源库 → 哈希/推卡片 → CD2 上传）
    lines.append("🎬 媒体流水线")
    if not s.pipeline_enabled:
        lines.append("⬜ 未启用（PIPELINE_ENABLED=false）")
    else:
        svc = getattr(container, "pipeline", None)
        if svc is not None:
            lines.extend(svc.overview_lines())
        else:
            lines.append("⬜ 服务未构建（检查目录配置）")

    await update.message.reply_text("\n".join(lines))


async def cmd_ed2k_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """流水线推送侧详细状态 + pending（admin only）。"""
    if not _is_admin(update, context):
        await update.message.reply_text(_DENY_TEXT)
        return
    container = _container(context)
    svc = getattr(container, "pipeline", None)
    if svc is None:
        await update.message.reply_text(
            "⬜ 媒体流水线未启用（PIPELINE_ENABLED=false），或尚未完成容器 build。"
        )
        return
    await update.message.reply_text(svc.status_push_text(), disable_web_page_preview=True)


async def cmd_upload_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """流水线上传侧详细状态 + 失败退避明细（admin only）。"""
    if not _is_admin(update, context):
        await update.message.reply_text(_DENY_TEXT)
        return
    container = _container(context)
    svc = getattr(container, "pipeline", None)
    if svc is None:
        await update.message.reply_text(
            "⬜ 媒体流水线未启用（PIPELINE_ENABLED=false），或尚未完成容器 build。"
        )
        return
    await update.message.reply_text(svc.status_upload_text(), disable_web_page_preview=True)


async def _monitor_state_line(monitor) -> str:
    """/status 用的一行式监控状态。"""
    from app.monitor.channel_monitor import STATE_NO_API, STATE_NO_LOGIN, STATE_RUNNING

    if monitor.state == STATE_RUNNING and monitor.is_running:
        return "✅ 运行中（/mon 查看详情）"
    if monitor.state == STATE_NO_API:
        return "❌ 未配置 TG_API_ID/TG_API_HASH"
    if monitor.state == STATE_NO_LOGIN:
        return "❌ 账号未登录（/mon login 登录）"
    return "❌ 未运行（/mon login 重新登录）"
