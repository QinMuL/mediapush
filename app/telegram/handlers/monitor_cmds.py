"""自动化命令：/mon 频道监控、/dir 目录监控、/share 手动扫描、/inspect 手动巡检。"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from app.monitor.store import (
    KEY_BATCH,
    KEY_TARGET,
    KIND_EXCLUDE,
    KIND_INCLUDE,
)
from app.telegram.handlers.common import (
    _DENY_TEXT,
    Pan115Error,
    _container,
    _edit,
    _is_admin,
)

logger = logging.getLogger(__name__)

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
    from app.monitor.channel_monitor import STATE_NO_API, STATE_NO_LOGIN, STATE_RUNNING

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
