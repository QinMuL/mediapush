"""运维命令：/refresh /loglevel /reload /cookie /reset（admin only）。"""

from __future__ import annotations

import logging
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

from app.core.maintenance import ResetService
from app.logging_config import set_console_level
from app.telegram.handlers.common import _DENY_TEXT, _container, _is_admin

logger = logging.getLogger(__name__)

# /reset 二次确认关键词（防止误触一键清空）
_RESET_CONFIRM_WORDS = ("确认", "confirm", "yes", "y")


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
    from app.config import Settings, find_env_file

    try:
        new_settings = Settings.load(dotenv_override=True)
    except Exception as exc:  # 配置解析失败保持现状
        logger.exception("重读配置失败")
        await update.message.reply_text(f"❌ 重读配置失败：{exc}")
        return
    hot, restart = _container(context).reload_config(new_settings)

    env_file = find_env_file()
    if env_file is None:
        # 容器内无 .env（旧部署未挂载项目目录）：环境变量是 env_file 注入的
        # 创建时快照，/reload 永远读不到宿主机的修改——如实说明而非假报无变更
        lines = [
            "⚠️ 容器内未找到 .env 文件，/reload 无法感知宿主机 .env 的修改",
            "解决办法（二选一）：",
            "  • docker-compose.yml 挂载项目目录（新版已内置：- .:/app/deploy:ro）",
            "  • docker compose up -d 重建容器（每次改 .env 都需要）",
        ]
    else:
        lines = [f"🔄 配置已重读（{env_file}）"]
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
        summary = await ResetService(container).run()
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
