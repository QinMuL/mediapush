"""Telegram Bot 命令处理。

- /start /help /status — 入口、帮助、运行状态一览（5 大块 + 流水线 4 阶段）
- /115 <链接> [访问码] — 推送 115/ed2k 分享（裸链接消息自动当 /115 处理）
- /edit <链接> — 预览编辑模式（追加推荐语/精品标记后推送）/cancel 取消
- /refresh <tmdb_id> — 清除 TMDB 缓存重拉
- /loglevel <级别> — 运行时调整控制台日志级别
- /reload — 重读 .env 热加载配置（间隔/开关/cookie；连接层变更提示需重启）
- /cookie — 在 bot 里查看/设置 115 cookie（写 PAN115_COOKIE_FILE + 热更新 + 探活）
- /reset — 一键清空业务数据（缓存/去重/状态/日志，保留配置；/reset 确认 执行）
- /mon — 频道监控管理（login/add/del/target/batch/filter）
- /inspect [数量] — 手动巡检失效分享并撤卡
- /dir add|del|list — 目录监控登记（新子目录自动建永久分享）
- /share — 立即扫描一轮监控目录
- /ed2k_status /upload_status — 本地媒体流水线状态（哈希推送/CD2 上传）
- 仅 TG_ADMIN_IDS 可用
- Pan115Error 顶部容错导入（p115client 装坏不拖垮 bot）
- 通过 context.application.bot_data["container"] 注入，不访问私有属性
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.core.link_parser import ParsedShare, parse_share, parse_shares
from app.logging_config import make_trace_id, set_console_level, trace_id
from app.monitor.store import (
    KEY_BATCH,
    KEY_TARGET,
    KIND_EXCLUDE,
    KIND_INCLUDE,
)
from app.telegram.edit_session import (
    MAX_QUALITY_EXTRA,
    EditSession,
    EditState,
)
from app.telegram.pusher import _send_with_retry, render_caption, render_text

# Pan115Error 容错导入：p115client 装坏时退化为 Exception，保留 except 语义
try:
    from app.providers import Pan115Error
except Exception:  # noqa: BLE001
    Pan115Error = Exception  # type: ignore[misc,assignment]

logger = logging.getLogger(__name__)

_SESSION_KEY = "edit_session"

# 权限拒绝统一文案（所有 admin 命令共用，风格一致）
_DENY_TEXT = "⛔ 仅管理员可用（在 .env 的 TG_ADMIN_IDS 中配置）"


def _container(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data["container"]


def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    return bool(user) and _container(context).settings.is_admin(user.id)


# ---------------------------------------------------------------------- #
# 编辑会话状态辅助（单键存 context.user_data，避免散落）
# ---------------------------------------------------------------------- #
def _get_session(context: ContextTypes.DEFAULT_TYPE) -> EditSession | None:
    return context.user_data.get(_SESSION_KEY)


def _set_session(context: ContextTypes.DEFAULT_TYPE, session: EditSession) -> None:
    context.user_data[_SESSION_KEY] = session


def _clear_session(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(_SESSION_KEY, None)


def _edit_keyboard(session: EditSession) -> InlineKeyboardMarkup:
    """编辑模式预览键盘：追加画质 / 切换精品 / 确认推送 / 取消。"""
    premium_label = "💎 精品:开" if session.is_premium else "💎 精品:关"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✏️ 追加画质", callback_data="edit_quality"),
                InlineKeyboardButton(premium_label, callback_data="toggle_premium"),
            ],
            [
                InlineKeyboardButton("✅ 确认推送", callback_data="confirm_push"),
                InlineKeyboardButton("❌ 取消", callback_data="cancel_edit"),
            ],
        ]
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 我是网盘影视资源推送 Bot。\n\n"
        "🚀 快速上手：\n"
        "1️⃣ 发送 115 分享链接或 ed2k 链接，自动匹配 TMDB 推送卡片\n"
        "2️⃣ 频道监控自动捕获 ed2k 链接并推送\n"
        "3️⃣ 本地媒体流水线：重命名 → ed2k 哈希 → 推频道 → CD2 传 115\n\n"
        "输入 /help 查看全部用法。"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📖 用法\n\n"
        "【推送】\n"
        "• 直接发送 115 分享链接，例如：\n"
        "  https://115.com/s/xxxx?password=yyyy\n"
        "• 或发送裸码（8 位以上）\n"
        "• 发送 ed2k 单文件链接，例如：\n"
        "  ed2k://|file|片名.mkv|大小|hash|/\n"
        "• 链接未带访问码时，正文写「访问码：xxxx」自动提取\n"
        "• 一条消息含多个链接会自动批量：按集数排序逐个推送，实时进度 + 汇总\n"
        "• /115 <链接> [访问码] — 显式触发\n\n"
        "【编辑】\n"
        "• /edit <链接> — 预览编辑模式：追加推荐语/精品标记后推送（可重推补档）\n"
        "• /cancel — 取消当前编辑或登录\n\n"
        "【运维】\n"
        "• /status — 运行状态、配置与健康一览\n"
        "• /refresh <tmdb_id> — 清除该 TMDB 缓存后重拉（剧集更新集数时用）\n"
        "• /loglevel <DEBUG|INFO|WARNING|ERROR> — 运行时调整控制台日志级别\n"
        "• /reload — 改 .env 后热加载配置（间隔/开关/cookie 等，无需重启）\n"
        "• /cookie — 查看状态；/cookie <串> 直接更新 115 cookie（写文件+探活）\n"
        "• /reset — 一键清空数据（缓存/去重/状态/日志，保留配置；/reset 确认 执行）\n\n"
        "【自动化】\n"
        "• /mon — 频道监控（/mon login 交互式登录，自动捕获 ed2k 推送）\n"
        "• /inspect [数量] — 手动巡检已推送分享，失效撤卡（默认每 6 小时自动跑）\n"
        "• /dir add <网盘路径> — 目录监控：新子目录自动建永久分享并推送\n"
        "• /share — 立即扫描一轮监控目录\n\n"
        "【本地媒体流水线】\n"
        "• /ed2k_status — 查看 ed2k 推送状态（pending 队列/进度/卡死告警）\n"
        "• /upload_status — 查看 CD2 上传状态（进度/退避/卡死告警）\n"
        "• A→B：监控本地目录，TMDB 高置信重命名 + ffprobe 实测画质标签\n"
        "• B→C：ed2k 哈希生成（MD4 Merkle），算完移入归档目录\n"
        "• C→频道：自动推送 ed2k 资源卡片到指定频道\n"
        "• C→115：CD2 上传到 115 网盘（115 秒传命中秒完成），完成后删本地源\n"
        "• 文件名格式：片名 (年份) - 画质标签 {tmdb-ID}.ext\n"
        "• 配置见 .env 的 LOCAL_MEDIA_* / ED2K_* / CD2_* 段"
    )


def _fmt_uptime(seconds: float) -> str:
    """运行时长人性化（X 秒 / X 分 X 秒 / X 小时 X 分 / X 天 X 小时）。"""
    s = int(seconds)
    if s < 60:
        return f"{s} 秒"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m} 分 {sec} 秒"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h} 小时 {m} 分"
    d, h = divmod(h, 24)
    return f"{d} 天 {h} 小时"


# 进程启动时刻（模块首次导入≈进程启动，供 /status 展示运行时长）
_STARTED_AT = time.monotonic()


def _count_dir_files(path: str) -> int | None:
    """目录内普通文件数（队列深度参考）；目录不可访问返回 None。"""
    from pathlib import Path

    try:
        return sum(1 for p in Path(path).iterdir() if p.is_file())
    except OSError:
        return None


def _fmt_kb(n: int | None) -> str:
    return "?" if n is None else str(n)


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

    # 5) 本地媒体流水线（A→B→C→频道→115，每阶段：状态 + 队列深度 + 最近一轮）
    lines.append("🎬 本地媒体流水线")
    if not (s.local_media_enabled or s.ed2k_enabled or s.ed2k_push_enabled or s.cd2_enabled):
        lines.append("⬜ 未启用（LOCAL_MEDIA / ED2K / ED2K_PUSH / CD2 的 *_ENABLED 均为 false）")
    else:
        # ① A→B：TMDB 重命名
        if not s.local_media_enabled:
            lines.append("① A→B 重命名：⬜ 未启用（LOCAL_MEDIA_ENABLED=false）")
        else:
            mode = "模拟" if s.local_media_dry_run else "实际移动"
            svc = getattr(container, "local_media", None)
            queue = _count_dir_files(s.local_media_input_dir) if svc else None
            retrying = len(getattr(svc, "_retry_state", {})) if svc else 0
            lines.append(
                f"① A→B 重命名：✅ 每 {s.local_media_interval_seconds:g}s · {mode}"
                f" · A 待处理 {_fmt_kb(queue)} · 低置信退避 {retrying}"
            )
            if svc:
                lines.append(f"   A={s.local_media_input_dir} → B={s.local_media_output_dir}")
                last = getattr(svc, "_last_report", None)
                if last:
                    lines.append(f"   最近一轮：{last}")
        # ② B→C：ed2k 哈希
        if not s.ed2k_enabled:
            lines.append("② B→C 哈希：⬜ 未启用（ED2K_ENABLED=false）")
        else:
            mode = "模拟" if s.ed2k_dry_run else "实际移动"
            svc = getattr(container, "ed2k_service", None)
            queue = _count_dir_files(s.ed2k_input_dir) if svc else None
            retrying = len(getattr(svc, "_retry_state", {})) if svc else 0
            lines.append(
                f"② B→C 哈希：✅ 每 {s.ed2k_interval_seconds:g}s · {mode}"
                f" · B 待处理 {_fmt_kb(queue)} · 失败退避 {retrying}"
            )
            if svc:
                lines.append(f"   B={s.ed2k_input_dir} → C={s.ed2k_output_dir}")
                last = getattr(svc, "_last_report", None)
                if last:
                    lines.append(f"   最近一轮：{last}")
        # ③ C→频道：ed2k 推送
        if not s.ed2k_push_enabled:
            lines.append("③ C→频道推送：⬜ 未启用（ED2K_PUSH_ENABLED=false）")
        else:
            mode = "模拟推送" if s.ed2k_push_dry_run else "实际推送"
            pusher = getattr(container, "ed2k_pusher", None)
            extra = ""
            if pusher is not None:
                pending, stuck = _ed2k_pending(pusher)
                extra = f" · 追读 {_ed2k_progress(pusher):.0f}% · 待推 {pending}（🚨 卡死 {stuck}）"
            lines.append(
                f"③ C→频道推送：✅ 每 {s.ed2k_push_interval_seconds:g}s · {mode}{extra}"
            )
            last = getattr(pusher, "_last_report", None) if pusher else None
            if last:
                lines.append(f"   最近一轮：{last}")
        # ④ C→115：CD2 上传
        if not s.cd2_enabled:
            lines.append("④ C→115 上传（CD2）：⬜ 未启用（CD2_ENABLED=false）")
        else:
            mode = "模拟上传" if s.cd2_upload_dry_run else "实际上传"
            note = " · Admin 汇总开" if s.cd2_report_admin else ""
            lines.append(
                f"④ C→115 上传（CD2）：✅ 每 {s.cd2_upload_interval_seconds:g}s · {mode}{note}"
            )
            lines.append(f"   {s.cd2_upload_src} → {s.cd2_upload_dst}")
            up = getattr(container, "cd2_uploader", None)
            if up is not None:
                now = time.time()
                for info in list(up._tasks.values())[:3]:
                    pct = info.uploaded_bytes / max(1, info.size) * 100
                    mins = (now - info.submitted_at) / 60
                    lines.append(
                        f"   🔄 传输中：{info.name} {pct:.0f}%"
                        f"（{mins:.0f} 分钟）"
                    )
                lines.append(
                    f"   已完成 {len(up._completed)} · 退避中 {len(up._retry_state)}"
                )
                last = getattr(up, "_last_report", None)
                if last:
                    lines.append(f"   最近一轮：{last}")

    await update.message.reply_text("\n".join(lines))


def _ed2k_pending(pusher) -> tuple[int, int]:
    """(待推/退避中条数, 卡死条数)。"""
    now = time.time()
    pending = stuck = 0
    for key, st in list(pusher._state.items()):
        if key.startswith("_"):
            continue
        pending += 1
        if now - float(st.get("first_seen", now)) > pusher.settings.ed2k_push_stuck_days * 86400:
            stuck += 1
    return pending, stuck


def _ed2k_progress(pusher) -> float:
    """JSONL 追读百分比（0-100）。"""
    try:
        size = pusher.results_file.stat().st_size if pusher.results_file.is_file() else 0
    except OSError:
        size = 0
    offset = pusher._state.get("_offset", 0)
    return (offset / size * 100.0) if size else 0.0




async def cmd_ed2k_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ed2k 推送端详细状态 + pending（admin only）。"""
    if not _is_admin(update, context):
        await update.message.reply_text(_DENY_TEXT)
        return
    container = _container(context)
    pusher = getattr(container, "ed2k_pusher", None)
    if pusher is None:
        await update.message.reply_text(
            "⬜ ed2k 推送未启用（ED2K_PUSH_ENABLED=false），或尚未完成容器 build。"
        )
        return
    text = pusher.status_text()
    # pending 细节最多列出前 N 条
    N = 8
    pending_lines = []
    import time as _t
    now = _t.time()
    pendings = []
    for key, st in list(pusher._state.items()):
        if key.startswith("_"):
            continue
        age_h = max(0.0, (now - float(st.get("first_seen", now))) / 3600.0)
        pendings.append((age_h, key, st))
    pendings.sort(key=lambda x: x[0], reverse=True)
    for age_h, key, st in pendings[:N]:
        f = st.get("failures", 0)
        nr = float(st.get("next_retry", 0) or 0)
        left = max(0.0, (nr - now) / 3600.0)
        snippet = key if len(key) <= 72 else key[:69] + "..."
        flag = " 🚨" if age_h / 24.0 >= container.settings.ed2k_push_stuck_days else ""
        pending_lines.append(
            f"• {age_h:.1f}h｜失败{f}｜下次{left:.1f}h{flag}｜{snippet}"
        )
    if pending_lines:
        text += f"\n\n⏳ Pending Top {min(N, len(pendings))}（按等待时长倒序）：\n" + "\n".join(pending_lines)
        if len(pendings) > N:
            text += f"\n（剩 {len(pendings) - N} 条未列出）"
    await update.message.reply_text(text, disable_web_page_preview=True)


async def cmd_upload_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """CD2 上传详细状态 + 失败退避明细（admin only）。"""
    if not _is_admin(update, context):
        await update.message.reply_text(_DENY_TEXT)
        return
    container = _container(context)
    uploader = getattr(container, "cd2_uploader", None)
    if uploader is None:
        await update.message.reply_text(
            "⬜ CD2 上传未启用（CD2_ENABLED=false），或尚未完成容器 build。"
        )
        return
    text = "\n".join(uploader.status_lines())
    import time as _t

    now = _t.time()
    retry_items = []
    for src, st in uploader._retry_state.items():
        age_h = max(0.0, (now - float(st.get("first_seen", now))) / 3600.0)
        retry_items.append((age_h, src, st))
    retry_items.sort(key=lambda x: x[0], reverse=True)
    N = 8
    if retry_items:
        retry_lines = []
        for age_h, src, st in retry_items[:N]:
            f = st.get("failures", 0)
            nr = float(st.get("next_retry", 0) or 0)
            left = max(0.0, (nr - now) / 3600.0)
            name = src.rsplit("/", 1)[-1]
            snippet = name if len(name) <= 72 else name[:69] + "..."
            flag = (
                " 🚨" if age_h / 24.0 >= container.settings.cd2_stuck_days else ""
            )
            retry_lines.append(
                f"• {age_h:.1f}h｜失败{f}｜下次{left:.1f}h{flag}｜{snippet}"
            )
        text += (
            f"\n\n⏳ 失败退避 Top {min(N, len(retry_items))}"
            f"（按等待时长倒序）：\n" + "\n".join(retry_lines)
        )
        if len(retry_items) > N:
            text += f"\n（剩 {len(retry_items) - N} 条未列出）"
    await update.message.reply_text(text, disable_web_page_preview=True)


async def _monitor_state_line(monitor) -> str:
    """/status 用的一行式监控状态。"""
    from app.monitor.service import STATE_NO_API, STATE_NO_LOGIN, STATE_RUNNING

    if monitor.state == STATE_RUNNING and monitor.is_running:
        return "✅ 运行中（/mon 查看详情）"
    if monitor.state == STATE_NO_API:
        return "❌ 未配置 TG_API_ID/TG_API_HASH"
    if monitor.state == STATE_NO_LOGIN:
        return "❌ 账号未登录（/mon login 登录）"
    return "❌ 未运行（/mon login 重新登录）"


async def cmd_115(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update, context):
        await update.message.reply_text(_DENY_TEXT)
        return
    text = " ".join(context.args) if context.args else (update.message.text or "")
    parsed = parse_share(text)
    if not parsed:
        await update.message.reply_text("❌ 无法识别链接，请发送 115 分享链接、8+ 位裸码或 ed2k 链接。")
        return
    logger.info("收到 /115 命令：%s", parsed.provider)
    await _process(update, context, parsed)


async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update, context):
        await update.message.reply_text(_DENY_TEXT)
        return
    if not context.args:
        await update.message.reply_text("用法：/refresh <tmdb_id>")
        return
    try:
        tmdb_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ tmdb_id 必须是整数")
        return
    n = await _container(context).cache.delete_tmdb(tmdb_id)
    await update.message.reply_text(f"🗑 已清除 TMDB 缓存 {tmdb_id}（{n} 条）。下次匹配将重新拉取。")


async def cmd_loglevel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/loglevel <级别>：运行时调整控制台日志级别（文件恒为 DEBUG 全量）。

    出问题时无需重启即可切 DEBUG 看细节；只影响 stdout handler。
    """
    if not _is_admin(update, context):
        await update.message.reply_text(_DENY_TEXT)
        return
    if not context.args:
        await update.message.reply_text(
            "用法：/loglevel <DEBUG|INFO|WARNING|ERROR>（控制台级别；文件恒为 DEBUG）"
        )
        return
    level = context.args[0].upper()
    if set_console_level(level):
        logger.info("控制台日志级别已切换为 %s", level)
        await update.message.reply_text(f"✅ 控制台日志级别：{level}（文件恒为 DEBUG）")
    else:
        await update.message.reply_text(
            "⚠️ 无效级别，可选：DEBUG / INFO / WARNING / ERROR / CRITICAL"
        )


async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/reload：重读 .env 热加载配置（无需重启容器）。

    可热加载：各类间隔/通知开关/115 限速/日志级别/cookie 文件；
    TG token、chat_id、代理等连接层变更仍需重启（回复中会列出）。
    """
    if not _is_admin(update, context):
        await update.message.reply_text(_DENY_TEXT)
        return
    from app.config import Settings

    try:
        new_settings = Settings.load(dotenv_override=True)
    except Exception as exc:  # 配置解析失败保持现状
        logger.exception("重读配置失败")
        await update.message.reply_text(f"❌ 重读配置失败：{exc}")
        return
    hot, restart = _container(context).reload_config(new_settings)

    lines = ["🔄 配置已重读（.env）"]
    if hot:
        lines.append("✅ 已热加载生效：\n" + "\n".join(f"  • {n}" for n in hot))
    if restart:
        lines.append(
            "⚠️ 以下变更需重启容器才生效：\n"
            + "\n".join(f"  • {n}" for n in restart)
        )
    if not hot and not restart:
        lines.append("ℹ️ 无变更（与当前运行配置一致）")
    lines.append("\nℹ️ cookie 文件（PAN115_COOKIE_FILE）内容变化已即时生效")
    await update.message.reply_text("\n".join(lines))


# /reset 二次确认关键词（防止误触一键清空）
_RESET_CONFIRM_WORDS = ("确认", "confirm", "yes", "y")


async def _stop_quietly(svc) -> None:
    """/reset 停后台服务：失败只记日志（不阻断清理流程）。"""
    if svc is None:
        return
    try:
        await svc.stop()
    except Exception as exc:  # noqa: BLE001
        logger.warning("/reset 停止服务 %s 失败：%s", type(svc).__name__, exc)


async def _reset_all_data(container) -> list[str]:
    """一键清空业务数据：停服务 → 清 DB/状态/文件/日志 → 重建重启。

    保留（配置）：.env、115 cookie 文件、监控频道（monitor.db）、
    /dir add 的监控目录（share_dirs 表）。
    清空（数据）：TMDB 缓存、推送去重历史、分享登记、
    统一状态存储（state.db）、ed2k_results.jsonl、全部日志。
    """
    from pathlib import Path

    from app.db.state import StateStore
    from app.logging_config import purge_log_files

    s = container.settings

    # 1) 停后台服务（必须先停：内存态会把已清空的数据写回 DB）
    for svc in (
        container.cd2_uploader,
        container.ed2k_pusher,
        container.ed2k_service,
        container.local_media,
        container.share_watcher,
        container.inspector,
    ):
        await _stop_quietly(svc)

    summary: list[str] = []

    # 2) 清业务数据库（share_dirs 为用户配置，保留）
    if container.cache is not None:
        counts = await container.cache.clear_all()
        summary.append(
            "数据库：tmdb_cache {tmdb_cache} · pushed_shares {pushed_shares} · shared_items {shared_items} 行已清".format(**counts)
        )

    # 3) 清统一状态存储（data/state.db 全部服务行）
    StateStore(s.state_db_path).clear()
    summary.append(f"状态存储：{s.state_db_path} 已清空")

    # 4) 删散落数据文件（ed2k 结果 JSONL + 旧状态 JSON 残留/迁移备份）
    data_dir = Path(s.state_db_path).resolve().parent
    removed = 0
    for pattern in ("ed2k_results.jsonl", "*_state.json", "*.migrated"):
        for p in data_dir.glob(pattern):
            try:
                p.unlink()
                removed += 1
            except OSError as exc:
                logger.warning("/reset 删除文件失败 %s：%s", p, exc)
    if removed:
        summary.append(f"数据文件：{removed} 个已删除（ed2k 结果/旧状态残留）")

    # 5) 清日志（当前 + 全部归档，随后重建 handler）
    removed_logs = purge_log_files(
        s.log_level,
        use_color=s.log_color,
        log_file=s.log_file,
        log_media_file=s.log_media_file,
        log_max_bytes=s.log_max_bytes,
        log_retention_days=s.log_retention_days,
    )
    summary.append(f"日志：{len(removed_logs)} 个文件已清空（含归档）")

    # 6) 重建媒体流水线（全新实例 = 全新内存态）并重启
    restarted: list[str] = []
    if s.local_media_enabled:
        from app.media.service import LocalMediaService

        container.local_media = LocalMediaService(container, s)
        await container.local_media.start()
        restarted.append("本地媒体 A→B")
    if s.ed2k_enabled:
        from app.media.ed2k_service import Ed2kService

        container.ed2k_service = Ed2kService(s)
        await container.ed2k_service.start()
        restarted.append("ed2k 哈希 B→C")
    if s.ed2k_push_enabled:
        from app.media.ed2k_pusher import Ed2kPusherService

        container.ed2k_pusher = Ed2kPusherService(container, s)
        await container.ed2k_pusher.start()
        restarted.append("ed2k 推送")
    if s.cd2_enabled:
        from app.media.cd2_uploader import Cd2UploaderService

        container.cd2_uploader = Cd2UploaderService(container, s)
        await container.cd2_uploader.start()
        restarted.append("CD2 上传 C→115")
    # 巡检/目录监控无内存态缓存，直接重启原实例
    if container.inspector is not None:
        await container.inspector.start()
        restarted.append("分享巡检")
    if container.share_watcher is not None:
        await container.share_watcher.start()
        restarted.append("目录监控")
    summary.append("已重启：" + " · ".join(restarted))
    logger.warning("/reset 数据清空完成：%s", "；".join(summary))
    return summary


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/reset：一键清空除配置外的所有数据（二次确认防误触）。

    /reset        — 查看将清空/保留的内容
    /reset 确认    — 执行清空（不可恢复）
    """
    if not _is_admin(update, context):
        await update.message.reply_text(_DENY_TEXT)
        return
    if not context.args or context.args[0].lower() not in _RESET_CONFIRM_WORDS:
        await update.message.reply_text(
            "⚠️ <b>一键重置（不可恢复）</b>\n\n"
            "<b>将清空：</b>\n"
            "  • TMDB 元数据缓存\n"
            "  • 已推送分享去重历史（同资源可能被重新推送）\n"
            "  • 分享登记与失败退避状态\n"
            "  • 流水线状态存储（completed/offset/退避）\n"
            "  • ed2k 哈希结果（ed2k_results.jsonl）\n"
            "  • 全部本地日志（含归档）\n\n"
            "<b>保留（配置）：</b>\n"
            "  • .env 与 115 cookie 文件\n"
            "  • 频道监控（/mon）与目录监控（/dir）配置\n\n"
            "确认执行请发送：<code>/reset 确认</code>",
            parse_mode="HTML",
        )
        return

    container = _container(context)
    try:
        summary = await _reset_all_data(container)
    except Exception as exc:
        logger.exception("/reset 执行失败")
        await update.message.reply_text(f"❌ 重置失败：{exc}\n建议重启容器后重试")
        return
    await update.message.reply_text(
        "🧹 <b>重置完成</b>（服务已自动重启）\n" + "\n".join(f"• {line}" for line in summary),
        parse_mode="HTML",
    )


async def cmd_cookie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cookie：在 bot 里查看/设置 115 cookie（写文件持久化 + 立即生效）。

    - /cookie — 查看当前 cookie 状态（来源/长度/UID，不回显原文）
    - /cookie <cookie串> — 写入 PAN115_COOKIE_FILE + 热更新 + 实时探活
    - PAN115_COOKIE 直配优先级高于文件：直配非空时禁止用本命令设置
      （否则重启后回到旧值，状态不一致），提示改用文件方式
    """
    if not _is_admin(update, context):
        await update.message.reply_text(_DENY_TEXT)
        return
    from pathlib import Path

    container = _container(context)
    pan115 = container.pan115
    if pan115 is None:
        await update.message.reply_text("❌ 115 provider 未初始化")
        return

    # ---- 查看模式：来源/长度/UID（不回显原文，cookie 是敏感凭证） ----
    if not context.args:
        source = (
            "PAN115_COOKIE 直配（.env）" if container.settings.pan115_cookie_direct
            else f"文件 {container.settings.pan115_cookie_file}"
            if container.settings.pan115_cookie_file else "未配置（匿名模式）"
        )
        uid = pan115.uid or "-"
        await update.message.reply_text(
            f"🍪 115 Cookie 状态\n"
            f"• 来源：{source}\n"
            f"• 长度：{len(pan115.cookie) or '-'}\n"
            f"• UID：{uid}\n\n"
            f"更新方式：/cookie <新cookie串>（需已配置 PAN115_COOKIE_FILE）\n"
            f"获取方式：浏览器登录 115 → F12 开发者工具 → Network 任一请求的 "
            f"Cookie 头整串复制"
        )
        return

    # ---- 设置模式：仅 PAN115_COOKIE 真直配时拒绝（文件内容回填不算直配） ----
    if container.settings.pan115_cookie_direct:
        await update.message.reply_text(
            "⚠️ 当前使用 PAN115_COOKIE 直配（优先级高于文件，重启后会覆盖此处设置）。\n"
            "请先在 .env 中清空 PAN115_COOKIE、填好 PAN115_COOKIE_FILE 后 /reload，"
            "再用本命令更新。"
        )
        return
    cookie_file = container.settings.pan115_cookie_file
    if not cookie_file:
        await update.message.reply_text(
            "❌ 未配置 PAN115_COOKIE_FILE（.env），无法持久化。\n"
            "容器内建议挂载卷后配置，如：PAN115_COOKIE_FILE=./data/115cookie.txt，"
            "再 /reload 后重试。"
        )
        return

    new_cookie = " ".join(context.args).strip()
    # 容错：常见整行带前缀的粘贴（"Cookie: UID=..."）
    if new_cookie.lower().startswith("cookie:"):
        new_cookie = new_cookie[7:].strip()
    try:
        # 父目录可能不存在（首次部署 data/ 未建），自动创建
        Path(cookie_file).parent.mkdir(parents=True, exist_ok=True)
        Path(cookie_file).write_text(new_cookie, encoding="utf-8")
    except OSError as exc:
        await update.message.reply_text(f"❌ cookie 文件写入失败：{exc}")
        return
    container.refresh_cookie_file()  # 内容变化 → provider.update_cookie 热生效

    # 实时探活：给 admin 直接的成败反馈（而不是等巡检告警）
    probe = "⏳ 已保存并热加载生效，正在探活…"
    msg = await update.message.reply_text(probe)
    try:
        ok = await pan115.check_health()
    except Exception as exc:  # noqa: BLE001 - 探活失败也是有效反馈
        ok = None
        logger.warning("/cookie 探活异常：%s", exc)
    if ok:
        result = (
            f"✅ cookie 已保存并热加载生效（探活通过，UID {pan115.uid or '-'}）\n"
            f"📁 持久化：{cookie_file}（重启后仍有效）\n"
            f"🗑 建议删除本条含 cookie 的消息"
        )
    elif ok is False:
        result = (
            "⚠️ 已保存并热加载，但探活失败（cookie 可能无效）。\n"
            "请确认复制的是完整 Cookie 头；重新获取后可再次 /cookie 覆盖。"
        )
    else:
        result = (
            "✅ 已保存并热加载生效（探活请求异常，稍后巡检会再检查）。\n"
            f"📁 持久化：{cookie_file}"
        )
    try:
        await msg.edit_text(result)
    except Exception:  # noqa: BLE001 - edit 失败（消息过旧等）退化为新消息
        await update.message.reply_text(result)


# ---------------------------------------------------------------------- #
# 频道监控管理（/mon）：add/del 监控频道、target 推送目标、batch 聚合窗口、filter 关键词
# ---------------------------------------------------------------------- #
_MON_USAGE = (
    "📡 频道监控用法：\n"
    "• /mon — 查看监控状态\n"
    "• /mon login [手机号] — 交互式登录监控账号（验证码/两步密码在对话中完成）\n"
    "• /mon add <@频道> — 添加监控频道（t.me 链接/chat_id 亦可，自动加入）\n"
    "• /mon del <@频道> — 移除监控频道\n"
    "• /mon target <频道ID> — 设置推送目标（默认 ed2k 频道）\n"
    "• /mon batch <秒> — 聚合窗口（0=实时逐条）\n"
    "• /mon filter — 查看过滤规则\n"
    "• /mon filter +<关键词> — 仅推送命中关键词的链接\n"
    "• /mon filter -<关键词> — 丢弃命中关键词的链接\n"
    "• /mon filter del <关键词> — 删除规则\n"
    "首次使用：.env 配置 TG_API_ID/TG_API_HASH → /mon login 登录"
)


async def _mon_status(container) -> str:
    """组装 /mon 状态文本（服务状态 + 频道 + 目标 + 窗口 + 规则）。"""
    from app.monitor.service import STATE_NO_API, STATE_NO_LOGIN, STATE_RUNNING

    monitor, store = container.monitor, container.monitor_store
    if monitor is None or store is None:
        return "📡 频道监控：未启用（MONITOR_ENABLED=false）"

    if monitor.login_active:
        desc = monitor.login_stage_desc or "会话已过期"
        state = f"⏳ 登录进行中（{desc}，/cancel 可中止）"
    elif monitor.state == STATE_RUNNING and monitor.is_running:
        state = "✅ 运行中"
    elif monitor.state == STATE_NO_LOGIN:
        state = "❌ 账号未登录（发送 /mon login 开始登录）"
    elif monitor.state == STATE_NO_API:
        state = "❌ 未配置 TG_API_ID/TG_API_HASH"
    else:
        state = "❌ 未运行（/mon login 重新登录）"

    channels = await store.list_channels()
    rules = await store.list_filters()
    batch = await monitor.batch_seconds()
    target = await monitor.target_chat_id()

    lines = [f"📡 频道监控：{state}", f"监控频道（{len(channels)}）："]
    for ch in channels:
        uname = f"，@{ch.username}" if ch.username else ""
        lines.append(f"• {ch.title}（{ch.chat_id}{uname}）")
    if not channels:
        lines.append("（空，/mon add @频道 添加）")
    lines.append(f"推送目标：{target or '❌ 未配置（/mon target <频道ID> 设置）'}")
    lines.append(f"聚合窗口：{batch} 秒（{'实时逐条' if batch == 0 else '同频道合并推送'}）")
    inc = [r.keyword for r in rules if r.kind == KIND_INCLUDE]
    exc = [r.keyword for r in rules if r.kind == KIND_EXCLUDE]
    lines.append(f"仅推送关键词：{'、'.join(inc) if inc else '无'}")
    lines.append(f"排除关键词：{'、'.join(exc) if exc else '无'}")
    return "\n".join(lines)


async def cmd_mon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update, context):
        await update.message.reply_text(_DENY_TEXT)
        return
    container = _container(context)
    monitor, store = container.monitor, container.monitor_store
    args = context.args or []
    sub = args[0].lower() if args else "status"

    if sub in ("", "status", "list", "状态"):
        await update.message.reply_text(await _mon_status(container))
        return

    if monitor is None or store is None:
        await update.message.reply_text("❌ 频道监控未启用（MONITOR_ENABLED=false）")
        return

    if sub == "login":  # 交互式登录监控账号
        chat_id = update.effective_chat.id if update.effective_chat else 0
        phone = " ".join(args[1:]).strip()
        await update.message.reply_text(await monitor.login_start(chat_id, phone))
        if phone:  # 命令中含手机号，属敏感内容，私聊中 Bot 可删除
            try:
                await update.message.delete()
            except Exception:
                logger.debug("登录命令消息删除失败（可能无权限）", exc_info=True)
        return

    if sub == "add":  # 添加监控频道（自动加入）
        if len(args) < 2:
            await update.message.reply_text("用法：/mon add @频道用户名")
            return
        ok, msg = await monitor.add_channel(" ".join(args[1:]))
        await update.message.reply_text(msg if ok else f"❌ {msg}")
        return

    if sub in ("del", "remove", "rm"):  # 移除监控频道
        if len(args) < 2:
            await update.message.reply_text("用法：/mon del @频道用户名（或 chat_id）")
            return
        ok, msg = await monitor.remove_channel(" ".join(args[1:]))
        await update.message.reply_text(msg if ok else f"❌ {msg}")
        return

    if sub == "target":  # 推送目标频道
        if len(args) < 2:
            current = await monitor.target_chat_id()
            await update.message.reply_text(
                f"用法：/mon target <频道ID或@用户名>\n当前：{current or '未配置'}"
            )
            return
        await store.set_setting(KEY_TARGET, args[1].strip())
        await update.message.reply_text(f"✅ 推送目标已设置为：{args[1].strip()}")
        return

    if sub == "batch":  # 聚合窗口（秒）
        if len(args) < 2 or not args[1].isdigit():
            await update.message.reply_text(
                f"用法：/mon batch <秒>（0=实时逐条）\n当前：{await monitor.batch_seconds()} 秒"
            )
            return
        await store.set_setting(KEY_BATCH, str(int(args[1])))
        await update.message.reply_text(f"✅ 聚合窗口已设置为 {int(args[1])} 秒")
        return

    if sub == "filter":  # 关键词过滤规则
        rest = args[1:]
        if not rest:
            rules = await store.list_filters()
            if not rules:
                await update.message.reply_text(
                    "过滤规则（空）：\n• /mon filter +关键词 → 仅推送命中\n"
                    "• /mon filter -关键词 → 排除命中"
                )
                return
            lines = ["过滤规则："]
            for r in rules:
                lines.append(f"• [{'仅推送' if r.kind == KIND_INCLUDE else '排除'}] {r.keyword}")
            await update.message.reply_text("\n".join(lines))
            return
        op = rest[0]
        if op.startswith("+") and len(op) > 1:
            ok = await store.add_filter(op[1:], KIND_INCLUDE)
            await update.message.reply_text("✅ 已添加仅推送规则" if ok else "❌ 规则已存在")
        elif op.startswith("-") and len(op) > 1:
            ok = await store.add_filter(op[1:], KIND_EXCLUDE)
            await update.message.reply_text("✅ 已添加排除规则" if ok else "❌ 规则已存在")
        elif op in ("del", "rm") and len(rest) > 1:
            ok = await store.remove_filter(rest[1])
            await update.message.reply_text("✅ 已删除规则" if ok else "❌ 规则不存在")
        else:
            await update.message.reply_text("用法：/mon filter +关键词 | -关键词 | del 关键词")
        return

    await update.message.reply_text(_MON_USAGE)


# 多链接消息聚合：TG 长消息自动拆分时，缓冲短时间内的多链接消息合并处理
_pending_shares: list[ParsedShare] = []
_pending_timer: asyncio.Task | None = None
_pending_update: Update | None = None
_pending_context = None
_AGGREGATE_WINDOW = 3  # 聚合窗口（秒）

# 处理中 60s 去重（借 P115-Share）：网络读取慢时，重复发送的同一链接直接跳过，
# 防止并发处理绕过 pushed 去重造成双推。TTL 兜底防泄漏（进程内内存态）。
_processing: dict[str, float] = {}  # "provider:code" -> 标记时刻（monotonic）
_PROCESSING_TTL = 60.0


def _processing_key(parsed) -> str:
    return f"{parsed.provider}:{parsed.code}"


def _is_processing(parsed) -> bool:
    """该链接是否正在处理（60s 内）。顺手清理过期标记。"""
    now = time.monotonic()
    for k, t in list(_processing.items()):
        if now - t >= _PROCESSING_TTL:
            _processing.pop(k, None)
    key = _processing_key(parsed)
    started = _processing.get(key)
    return started is not None and now - started < _PROCESSING_TTL


def _mark_processing(parsed) -> None:
    _processing[_processing_key(parsed)] = time.monotonic()


def _unmark_processing(parsed) -> None:
    _processing.pop(_processing_key(parsed), None)


async def _handle_login_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE, monitor, stage: str
) -> None:
    """登录会话中的文本输入按阶段解释（手机号/验证码/密码），并删除敏感消息。"""
    text = (update.message.text or "").strip()
    if not text:
        return
    if stage == "phone":
        ok, msg = await monitor.login_phone(text)
    elif stage == "code":
        status, msg = await monitor.login_code(text)
        ok = status != "error"  # retry 保留会话；error/ok/password 均终止输入流
    else:  # password
        status, msg = await monitor.login_password(text)
        ok = status != "error"
    await update.message.reply_text(msg)
    # 手机号/验证码/密码属敏感内容：私聊中 Bot 可删，失败（群组无权限）仅记录
    if ok:
        try:
            await update.message.delete()
        except Exception:
            logger.debug("登录敏感消息删除失败（可能无权限）", exc_info=True)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """裸链接消息自动触发处理（单链接直推 / 多链接聚合批处理）。

    单链接且无聚合中 → 立即直推（保持原 UX）。
    多链接或聚合中 → 缓冲聚合：TG 会把超长消息拆成多条，此处等 3s 合并
    成一个批量，统一按集数排序推送，避免拆分破坏顺序。
    顶部优先处理编辑模式 AWAITING_QUALITY 状态，其次监控登录输入流。
    """
    global _pending_update, _pending_context, _pending_timer
    if not _is_admin(update, context):
        return
    monitor = getattr(_container(context), "monitor", None)
    if monitor is not None:
        chat_id = update.effective_chat.id if update.effective_chat else 0
        stage = await monitor.login_stage(chat_id)
        if stage is not None:
            await _handle_login_input(update, context, monitor, stage)
            return
    session = _get_session(context)
    if session is not None and session.state == EditState.AWAITING_QUALITY:
        await _handle_quality_input(update, context, session)
        return
    text = update.message.text or ""
    shares = parse_shares(text)
    if not shares:
        return
    uid = update.effective_user.id if update.effective_user else "?"
    # 单链接且无聚合中 → 立即直推
    if len(shares) == 1 and not _pending_shares:
        logger.info("收到链接：%s（user=%s）", shares[0].provider, uid)
        await _process(update, context, shares[0])
        return
    # 多链接或聚合中 → 缓冲聚合
    logger.info("收到链接：%d 个（user=%s，进入聚合）", len(shares), uid)
    _pending_shares.extend(shares)
    if _pending_update is None:
        _pending_update = update
        _pending_context = context
    logger.info("聚合 +%d（累计 %d）", len(shares), len(_pending_shares))
    if _pending_timer is not None and not _pending_timer.done():
        _pending_timer.cancel()
    _pending_timer = asyncio.create_task(_flush_pending())


async def _flush_pending() -> None:
    """聚合窗口到期：跨消息去重后批量推送。"""
    global _pending_shares, _pending_update, _pending_context, _pending_timer
    await asyncio.sleep(_AGGREGATE_WINDOW)
    shares = _pending_shares
    update, context = _pending_update, _pending_context
    _pending_shares = []
    _pending_update = None
    _pending_context = None
    _pending_timer = None
    seen: set[tuple[str, str]] = set()
    unique: list[ParsedShare] = []
    for p in shares:
        k = (p.provider, p.code)
        if k in seen:
            continue
        seen.add(k)
        unique.append(p)
    logger.info("聚合完成 %d 个链接，开始批处理", len(unique))
    try:
        await _process_batch(update, context, unique)
    except Exception:
        logger.exception("聚合批处理失败")


# ---------------------------------------------------------------------- #
async def _process(update: Update, context: ContextTypes.DEFAULT_TYPE, parsed) -> None:
    container = _container(context)
    # 处理中 60s 去重：同一链接并发处理会造成双推（读取慢时用户易重发）
    if _is_processing(parsed):
        await update.message.reply_text("⏳ 该链接正在处理中，请稍候（勿在 1 分钟内重复发送）")
        return
    _mark_processing(parsed)
    # 链路 trace：prepare/读取/TMDB/推送全程日志带 [tid=xxx]，grep 即拉全链路
    with trace_id(make_trace_id(parsed)):
        try:
            await _process_locked(update, context, container, parsed)
        finally:
            _unmark_processing(parsed)


async def _process_locked(update: Update, context, container, parsed) -> None:
    # ed2k 链接很长，placeholder 用简短文案；115 显示分享码
    if parsed.provider == "ed2k":
        loading = "⏳ 正在解析 ed2k 资源 ..."
    else:
        loading = f"⏳ 正在读取分享 `{parsed.code}` ..."
    placeholder = await update.message.reply_text(loading, parse_mode="Markdown")
    try:
        result = await container.processor.process(parsed)
    except Pan115Error as exc:
        await _edit(placeholder, f"❌ 115 错误：{exc}")
        return
    except Exception as exc:
        logger.exception("处理分享失败")
        await _edit(placeholder, f"❌ 处理失败：{exc}")
        return

    if result.ok:
        await _edit(
            placeholder,
            f"✅ {result.message}\n"
            f"📁 文件 {result.file_count} · 🎬 {result.title}"
            + (f" ({result.year})" if result.year else ""),
        )
    else:
        await _edit(placeholder, f"⚠️ {result.message}")


async def _edit(message, text: str) -> None:
    try:
        await _send_with_retry(lambda: message.edit_text(text))
    except Exception:  # noqa: BLE001 - 编辑失败（非 flood）兜底重发
        try:
            await _send_with_retry(lambda: message.reply_text(text))
        except Exception:  # noqa: BLE001
            logger.warning("回复失败")


# ---------------------------------------------------------------------- #
# 多链接批处理
# ---------------------------------------------------------------------- #
def _short_id(parsed) -> str:
    """汇总行短标识（无标题时兜底）。"""
    return "ed2k 资源" if parsed.provider == "ed2k" else parsed.code


def _summarize_line(parsed, result) -> str:
    """单条结果 → 汇总行（单行，去换行防排版错乱）。"""
    msg = (result.message or "").replace("\n", " ")
    if result.ok:
        title = (result.title or _short_id(parsed)).replace("\n", " ")
        year = f" ({result.year})" if result.year else ""
        return f"✅ {title}{year}"
    if "已推送" in msg:
        title = (result.title or _short_id(parsed)).replace("\n", " ")
        return f"⏭️ {title}（已推送，跳过）"
    return f"⚠️ {_short_id(parsed)}：{msg}"


def _build_batch_summary(header: str, lines: list[str]) -> str:
    """组装批处理汇总，超 4096 截断。"""
    body = "\n".join(lines)
    msg = f"{header}\n\n{body}"
    if len(msg) <= 4000:
        return msg
    kept: list[str] = []
    total = len(header) + 2
    for line in lines:
        if total + len(line) + 1 > 3990:
            break
        kept.append(line)
        total += len(line) + 1
    return f"{header}\n\n" + "\n".join(kept) + "\n…（更多已截断）"


_EP_RE = re.compile(r"[Ss](\d+)[Ee](\d+)")


def _episode_sort_key(parsed) -> tuple:
    """按季集排序 ed2k 链接：从文件名提取 SxxExx；无法提取的排最后（保持原序）。

    115 分享码不含文件名，无法预排序，统一排后并保持原文顺序。
    """
    if parsed.provider == "ed2k":
        parts = parsed.code.split("|")
        if len(parts) > 2:
            m = _EP_RE.search(parts[2])
            if m:
                return (0, int(m.group(1)), int(m.group(2)))
    return (1, 0, 0)


_batch_lock = asyncio.Lock()  # 串行化批量推送，避免多消息并发加剧 TG flood


async def _process_batch(update: Update, context, shares) -> None:
    """多链接串行处理：逐个推送，单条汇总消息实时更新，失败继续。"""
    container = _container(context)
    shares = sorted(shares, key=_episode_sort_key)
    total = len(shares)
    placeholder = await update.message.reply_text(
        f"⏳ 正在处理 {total} 个链接，逐个推送中 ..."
    )
    lines: list[str] = []
    done = 0
    async with _batch_lock:  # 串行化批量，避免多消息并发推送加剧 flood
        for parsed in shares:
            done += 1
            # 处理中 60s 去重：跳过批外正在处理的同一链接（防双推）
            if _is_processing(parsed):
                lines.append(f"⏭️ {_short_id(parsed)}（正在处理中，跳过）")
                await _edit(
                    placeholder,
                    _build_batch_summary(f"⏳ 处理中 ({done}/{total})", lines),
                )
                continue
            _mark_processing(parsed)
            # 链路 trace：批内逐条独立 tid，日志交错也可按条 grep
            with trace_id(make_trace_id(parsed)):
                try:
                    try:
                        result = await container.processor.process(parsed)
                    except Pan115Error as exc:
                        lines.append(f"⚠️ {_short_id(parsed)}：{exc}".replace("\n", " "))
                    except Exception as exc:
                        uid = update.effective_user.id if update.effective_user else None
                        logger.exception("处理分享失败：user=%s", uid)
                        lines.append(f"⚠️ {_short_id(parsed)}：{exc}".replace("\n", " "))
                    else:
                        lines.append(_summarize_line(parsed, result))
                finally:
                    _unmark_processing(parsed)
            await _edit(
                placeholder,
                _build_batch_summary(f"⏳ 处理中 ({done}/{total})", lines),
            )
            if done < total:
                await asyncio.sleep(2)  # 限速避免 TG 频道 flood control

    ok_count = sum(1 for ln in lines if ln.startswith("✅"))
    skip_count = sum(1 for ln in lines if ln.startswith("⏭️"))
    fail_count = sum(1 for ln in lines if ln.startswith("⚠️"))
    header = f"✅ 完成 {ok_count}/{total}"
    if skip_count:
        header += f"，⏭️ 跳过 {skip_count}"
    if fail_count:
        header += f"，⚠️ 失败 {fail_count}"
    await _edit(placeholder, _build_batch_summary(header, lines))


# ---------------------------------------------------------------------- #
# 编辑模式：/edit 预览 → 编辑画质模块 → 确认推送
# ---------------------------------------------------------------------- #
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


# ---------------------------------------------------------------------- #
# /dir：目录监控管理（add/del/list）；/share：手动触发一轮
# ---------------------------------------------------------------------- #
_DIR_USAGE = (
    "📁 目录监控用法：\n"
    "• /dir add <网盘路径> — 添加监控目录（如 /媒体/新剧）\n"
    "• /dir del <网盘路径> — 移除（连同已分享记录）\n"
    "• /dir list — 查看监控目录与已推送数\n"
    "• /share — 立即扫描一轮：新子目录建永久分享并推送\n"
    "说明：监控目录下每个新子目录 = 一张卡片（一部剧/电影）；"
    "需已配置 115 cookie（创建分享要登录态）。"
)


async def cmd_dir(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/dir add|del|list：目录监控管理（admin only）。"""
    if not _is_admin(update, context):
        await update.message.reply_text(_DENY_TEXT)
        return
    container = _container(context)
    args = context.args or []

    async def reply(text: str) -> None:
        await update.message.reply_text(text)

    if not args or args[0] not in ("add", "del", "list"):
        await reply(_DIR_USAGE)
        return

    sub = args[0]
    if sub == "list":
        dirs = await container.cache.list_share_dirs()
        if not dirs:
            await reply("📁 暂无监控目录。用 /dir add <网盘路径> 添加（如 /dir add /媒体/新剧）")
            return
        lines = ["📁 监控目录："]
        for d in dirs:
            lines.append(f"• {d['path']}（已推送 {d['shared']} 个子目录）")
        lines.append("\n/share 立即扫描一轮")
        await reply("\n".join(lines))
        return

    # add / del 都需要路径参数（路径含空格时合并剩余参数）
    target = " ".join(args[1:]).strip().strip("/")
    if not target:
        await reply(f"❌ 缺少路径。用法：/dir {sub} <网盘路径>")
        return
    path = "/" + target

    if sub == "del":
        removed = await container.cache.remove_share_dir(path)
        if removed:
            await reply(f"✅ 已移除监控目录：{path}")
        else:
            await reply(f"❌ 未找到监控目录：{path}（/dir list 查看）")
        return

    # add：解析路径 → cid（校验存在性，防拼写错误）
    if container.pan115 is None:
        await reply("❌ 115 服务未就绪")
        return
    if not container.pan115.cookie:
        await reply(
            "❌ 未配置 115 cookie（PAN115_COOKIE / PAN115_COOKIE_FILE），"
            "无法创建分享。请先配置后再添加监控目录。"
        )
        return
    loading = await update.message.reply_text(f"⏳ 正在校验网盘路径 `{path}` ...", parse_mode="Markdown")
    try:
        cid = await container.pan115.resolve_path(path)
    except Pan115Error as exc:
        await _edit(loading, f"❌ {exc}")
        return
    except Exception as exc:
        logger.exception("路径解析失败")
        await _edit(loading, f"❌ 路径解析失败：{exc}")
        return
    await container.cache.add_share_dir(path, cid)
    await _edit(loading, f"✅ 已添加监控目录：{path}\n新增子目录将自动建永久分享并推送（/share 立即触发）")


async def cmd_share(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/share：手动扫描一轮监控目录（admin only）。"""
    if not _is_admin(update, context):
        await update.message.reply_text(_DENY_TEXT)
        return
    container = _container(context)
    watcher = getattr(container, "share_watcher", None)
    if watcher is None:
        await update.message.reply_text("❌ 目录监控未启用（SHARE_WATCH_ENABLED=false）")
        return
    placeholder = await update.message.reply_text("⏳ 正在扫描监控目录 ...")
    try:
        report = await watcher.run_once()
    except Exception as exc:  # noqa: BLE001
        await _edit(placeholder, f"❌ 扫描失败：{exc}")
        return
    lines = [report.summary()]
    for it in report.items[:20]:
        lines.append(f"✅ {it['name']}（{it['dir']}）")
    if len(report.items) > 20:
        lines.append(f"… 共 {len(report.items)} 个")
    if not report.items and report.new_items == 0 and report.failed == 0:
        lines.append("ℹ️ 本轮无新子目录（均已分享或目录为空）")
    lines.append(f"⏱ 下一轮自动扫描：{watcher.interval:g} 分钟后（SHARE_WATCH_INTERVAL_MINUTES）")
    await _edit(placeholder, "\n".join(lines))


# ---------------------------------------------------------------------- #
# /inspect：手动触发分享失效巡检
# ---------------------------------------------------------------------- #
async def cmd_inspect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """手动巡检一轮已推送分享：失效撤卡 + 汇总（admin only）。

    用法：/inspect [数量]（默认 50 条，最久未检查优先）
    """
    if not _is_admin(update, context):
        await update.message.reply_text(_DENY_TEXT)
        return
    container = _container(context)
    inspector = getattr(container, "inspector", None)
    if inspector is None:
        await update.message.reply_text("❌ 巡检未启用（INSPECT_ENABLED=false）")
        return
    limit = 50
    if context.args:
        try:
            limit = max(1, min(int(context.args[0]), 200))
        except ValueError:
            pass
    placeholder = await update.message.reply_text(f"⏳ 正在巡检最多 {limit} 条已推送分享 ...")
    try:
        report = await inspector.run_once(limit=limit)
    except Exception as exc:  # noqa: BLE001
        await _edit(placeholder, f"❌ 巡检失败：{exc}")
        return
    lines = [report.summary()]
    for it in report.dead_items[:20]:
        t = it["title"] or it["share_code"]
        lines.append(f"⚰️ {t}（{it['reason']}）已撤卡")
    if len(report.dead_items) > 20:
        lines.append(f"… 共失效 {len(report.dead_items)} 条")
    for it in report.code_items[:10]:
        t = it["title"] or it["share_code"]
        lines.append(f"🔑 {t}（{it['reason']}）—— /edit 重推可补档")
    if len(report.code_items) > 10:
        lines.append(f"… 共 {len(report.code_items)} 条缺访问码")
    lines.append(
        f"⏱ 下一轮自动巡检：{inspector.interval:g} 小时后（INSPECT_INTERVAL_HOURS）"
    )
    await _edit(placeholder, "\n".join(lines))


# ---------------------------------------------------------------------- #
# 快捷菜单命令（启动时通过 setMyCommands 注册，覆盖旧项目残留）
# 顺序即菜单显示顺序；描述简短，Telegram 会作为 "/" 命令提示展示
_BOT_COMMANDS: list[BotCommand] = [
    BotCommand("start", "开始使用"),
    BotCommand("help", "用法说明"),
    BotCommand("115", "推送 115/ed2k 链接"),
    BotCommand("edit", "编辑画质后推送"),
    BotCommand("cancel", "取消当前编辑"),
    BotCommand("status", "运行状态与健康一览"),
    BotCommand("refresh", "清除 TMDB 缓存"),
    BotCommand("loglevel", "调整控制台日志级别"),
    BotCommand("reload", "重读 .env 热加载配置"),
    BotCommand("cookie", "查看/设置 115 cookie"),
    BotCommand("mon", "频道监控（login 登录/add 添加）"),
    BotCommand("inspect", "巡检失效分享并撤卡"),
    BotCommand("dir", "目录监控管理（add/del/list）"),
    BotCommand("share", "扫描目录建永久分享并推送"),
    BotCommand("ed2k_status", "ed2k 推送状态与 pending 队列"),
    BotCommand("upload_status", "CD2 上传状态（进度/退避）"),
    BotCommand("reset", "一键清空数据（二次确认）"),
]


async def setup_commands(application: Application) -> None:
    """启动时注册快捷菜单命令（post_init 钩子）。

    先 delete_my_commands 清除上个项目残留的菜单，再 set_my_commands 设置新菜单。
    默认 scope（对所有用户生效）。失败不阻断启动。

    额外：发一次短超时 get_updates 抢占会话，终止上一实例残留的长轮询
    （容器重启时代理可能保持旧连接，导致 Conflict 循环 → CPU 飙高 + 断联）。
    """
    try:
        await application.bot.delete_my_commands()
        await application.bot.set_my_commands(_BOT_COMMANDS)
        logger.info(
            "已注册 Bot 命令菜单：%s",
            ", ".join(c.command for c in _BOT_COMMANDS),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("注册命令菜单失败：%s", exc)

    # 清理上一实例残留的 getUpdates 长轮询会话
    # 代理保活旧连接时 TG 仍认为旧 getUpdates 活跃，新实例每次 getUpdates 都 Conflict
    # 这里主动发一次短超时请求抢占会话（TG 会终止旧的），失败不阻断启动
    try:
        await application.bot.get_updates(timeout=1)
        logger.info("getUpdates 会话已就绪")
    except Exception as exc:  # noqa: BLE001
        logger.warning("清理残留 getUpdates 会话（可忽略，轮询循环会自动重试）：%s", exc)


# ---------------------------------------------------------------------- #
# 网络异常降噪：异常风暴时 60s 窗口内只打 1 次详情，其余计数，恢复打汇总
_NET_WARN_WINDOW = 60.0
_NET_ALERT_COUNT = 20  # 窗口内次数达到该值 → 私信 admin（疑似代理/网络故障）
_net_warn: dict[str, tuple[float, int]] = {}  # 类型名 -> (窗口起点, 计数)


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """全局错误处理：网络/冲突异常降级 WARN（PTB 自动重连），其他 ERROR + 用户上下文。

    - Conflict(409)：进程/网络重启后旧 getUpdates 在 TG 侧残留 ~1 分钟，属预期自愈，WARN
    - NetworkError/TimedOut：风暴降噪（60s 窗口计数），恢复时打汇总
    """
    import time as _time

    from telegram.error import Conflict

    err = context.error
    if isinstance(err, Conflict):
        logger.warning(
            "getUpdates 会话冲突（多为进程/网络重启后旧轮询在 TG 侧残留，"
            "约 1 分钟内自愈；持续超 5 分钟请检查是否有第二实例同 token）：%s",
            err,
        )
        return
    if isinstance(err, (NetworkError, TimedOut)):
        now = _time.monotonic()
        kind = type(err).__name__
        # 未见过的类型：起点设为远古 → 首条必然打详情
        start, count = _net_warn.get(kind, (now - _NET_WARN_WINDOW - 1, 0))
        if now - start > _NET_WARN_WINDOW:
            # 上一窗口结束：若曾风暴，先打汇总再开新窗口
            if count > 1:
                logger.warning("TG 网络异常风暴已恢复：近 %ds 内 %s ×%d", _NET_WARN_WINDOW, kind, count)
            _net_warn[kind] = (now, 1)
            # 代理不可达（连接被拒/代理握手失败）明确指向代理排查
            msg = str(err)
            hint = ""
            if "refused" in msg.lower() or "proxy" in msg.lower():
                hint = "（疑似代理不可达：检查 PROXY_URL 指向的代理是否运行、容器能否访问宿主机端口）"
            logger.warning("TG 网络异常（PTB 将自动重连）%s：%s", hint, err)
        else:
            _net_warn[kind] = (start, count + 1)
            # 持续风暴：私信 admin（== 阈值时只发一次，窗口滑动天然限频）
            if count + 1 == _NET_ALERT_COUNT:
                try:
                    from app.telegram.notifier import notify_admins

                    container = context.application.bot_data.get("container")
                    admins = container.settings.tg_admin_ids if container else []
                    if admins:
                        await notify_admins(
                            context.bot, admins,
                            f"🔴 TG Bot 持续网络异常：60 秒内 {kind} 已 {count + 1} 次。\n"
                            f"Bot 正在自动重连，但疑似代理不可达或网络故障，请尽快检查。\n"
                            f"最近错误：{err}",
                        )
                except Exception:
                    logger.exception("网络风暴告警发送失败")
        return
    user_id = None
    if isinstance(update, Update) and update.effective_user:
        user_id = update.effective_user.id
    logger.error("处理异常：user=%s %s", user_id, err, exc_info=err)


# ---------------------------------------------------------------------- #
def register(application: Application) -> None:
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("115", cmd_115))
    application.add_handler(CommandHandler("edit", cmd_edit))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    application.add_handler(CommandHandler("refresh", cmd_refresh))
    application.add_handler(CommandHandler("loglevel", cmd_loglevel))
    application.add_handler(CommandHandler("reload", cmd_reload))
    application.add_handler(CommandHandler("cookie", cmd_cookie))
    application.add_handler(CommandHandler("mon", cmd_mon))
    application.add_handler(CommandHandler("inspect", cmd_inspect))
    application.add_handler(CommandHandler("dir", cmd_dir))
    application.add_handler(CommandHandler("share", cmd_share))
    application.add_handler(CommandHandler("ed2k_status", cmd_ed2k_status))
    application.add_handler(CommandHandler("upload_status", cmd_upload_status))
    application.add_handler(CommandHandler("reset", cmd_reset))
    application.add_handler(
        CallbackQueryHandler(
            on_edit_callback,
            pattern="^(edit_quality|toggle_premium|confirm_push|cancel_edit)$",
        )
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_text)
    )
    application.add_error_handler(_error_handler)
