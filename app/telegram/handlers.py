"""Telegram Bot 命令处理。

- /start /help /status /115 <链接> [密码] /refresh <tmdb_id>
- /edit <链接> — 预览编辑模式（追加推荐语/精品标记后推送）/cancel 取消
- 裸链接消息自动当 /115 处理
- 仅 TG_ADMIN_IDS 可用
- Pan115Error 顶部容错导入（p115client 装坏不拖垮 bot）
- 通过 context.application.bot_data["container"] 注入，不访问私有属性
"""

from __future__ import annotations

import asyncio
import logging
import re

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
        "👋 我是网盘影视资源推送 Bot。\n"
        "发送一个 115 分享链接（或裸码）或 ed2k 链接，我会读取内容、匹配 TMDB 并推送到频道。\n"
        "输入 /help 查看用法。"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📖 用法：\n"
        "• 直接发送 115 分享链接，例如：\n"
        "  https://115.com/s/xxxx?password=yyyy\n"
        "• 或发送裸码（8 位以上）\n"
        "• 发送 ed2k 单文件链接，例如：\n"
        "  ed2k://|file|片名.mkv|大小|hash|/\n"
        "• /115 <链接> [访问码] — 显式触发\n"
        "• /edit <链接> — 预览编辑模式：追加推荐语/精品标记后推送（精品资源区分）\n"
        "• /cancel — 取消当前编辑\n"
        "• /refresh <tmdb_id> — 清除该 TMDB 缓存后重拉\n"
        "• /status — 查看配置与 115 健康状态\n"
        "• /mon — 频道监控管理（/mon login 交互式登录，自动捕获 ed2k 推送）"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update, context):
        return
    container = _container(context)
    s = container.settings
    lines = [
        f"TG Bot：{'✅ 已配置' if s.tg_bot_token else '❌ 未配置'}",
        f"默认频道：{s.tg_chat_id or '❌ 未配置'}",
        f"网盘频道：{s.tg_chat_id_115 or '⬇️ 同默认'}",
        f"ed2k 频道：{s.tg_chat_id_ed2k or '⬇️ 同默认'}",
        f"TMDB Key：{'✅' if s.tmdb_api_key else '❌ 未配置'}",
        f"115 Cookie：{'✅ 已配置（可选）' if s.pan115_cookie else '未配置（匿名读取，可用）'}",
        f"代理：{s.proxy_url or '未配置'}",
    ]
    if container.pan115 is not None:
        try:
            ok = await container.pan115.check_health()
            if ok is None:
                lines.append("115：✅ 匿名读取可用（cookie 未配置）")
            else:
                lines.append(f"115 健康：{'✅' if ok else '❌ cookie 失效'}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"115：❌ {exc}")
    monitor = getattr(container, "monitor", None)
    if monitor is None:
        lines.append("频道监控：未启用（MONITOR_ENABLED=false）")
    elif monitor.login_active:
        lines.append(f"频道监控：⏳ 登录进行中（{monitor.login_stage_desc or '会话已过期'}）")
    else:
        lines.append(f"频道监控：{await _monitor_state_line(monitor)}")
    await update.message.reply_text("\n".join(lines))


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
        await update.message.reply_text("⛔ 无权限")
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
        await update.message.reply_text("⛔ 无权限")
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
    lines.append(f"推送目标：{target or '❌ 未配置'}")
    lines.append(f"聚合窗口：{batch} 秒（{'实时逐条' if batch == 0 else '同频道合并推送'}）")
    inc = [r.keyword for r in rules if r.kind == KIND_INCLUDE]
    exc = [r.keyword for r in rules if r.kind == KIND_EXCLUDE]
    lines.append(f"仅推送关键词：{'、'.join(inc) if inc else '无'}")
    lines.append(f"排除关键词：{'、'.join(exc) if exc else '无'}")
    return "\n".join(lines)


async def cmd_mon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update, context):
        await update.message.reply_text("⛔ 无权限")
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
        await update.message.reply_text("⛔ 无权限")
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
        await update.message.reply_text("⛔ 无权限")
        return
    monitor = getattr(_container(context), "monitor", None)
    if monitor is not None and monitor.login_active:
        await update.message.reply_text(await monitor.login_cancel())
        return
    session = _get_session(context)
    if session is None:
        await update.message.reply_text("当前没有进行中的编辑。")
        return
    _clear_session(context)
    await _edit_preview_text(context.bot, session, "已取消编辑，未推送频道。")
    await update.message.reply_text("已取消编辑。")


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
        await q.answer("⛔ 无权限", show_alert=True)
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
        await _edit_preview_text(bot, session, "已取消编辑，未推送频道。")
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

    try:
        ok, msg = await pusher.push_share(
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
        await container.cache.mark_pushed(parsed.code)
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
# 快捷菜单命令（启动时通过 setMyCommands 注册，覆盖旧项目残留）
# 顺序即菜单显示顺序；描述简短，Telegram 会作为 "/" 命令提示展示
_BOT_COMMANDS: list[BotCommand] = [
    BotCommand("start", "开始使用"),
    BotCommand("help", "用法说明"),
    BotCommand("115", "推送 115/ed2k 链接"),
    BotCommand("edit", "编辑画质后推送"),
    BotCommand("cancel", "取消当前编辑"),
    BotCommand("status", "查看配置与健康"),
    BotCommand("refresh", "清除 TMDB 缓存"),
    BotCommand("mon", "频道监控（login 登录/add 添加）"),
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


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """全局错误处理：网络异常降级 WARN（PTB 自动重连），其他 ERROR + 用户上下文。"""
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        logger.warning("TG 网络异常（PTB 将自动重连）：%s", err)
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
    application.add_handler(CommandHandler("mon", cmd_mon))
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
