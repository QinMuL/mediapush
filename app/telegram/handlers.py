"""Telegram Bot 命令处理。

- /start /help /status /115 <链接> [密码] /refresh <tmdb_id>
- 裸链接消息自动当 /115 处理
- 仅 TG_ADMIN_IDS 可用
- Pan115Error 顶部容错导入（p115client 装坏不拖垮 bot）
- 通过 context.application.bot_data["container"] 注入，不访问私有属性
"""

from __future__ import annotations

import logging

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.core.link_parser import parse_share, parse_shares

# Pan115Error 容错导入：p115client 装坏时退化为 Exception，保留 except 语义
try:
    from app.providers import Pan115Error
except Exception:  # noqa: BLE001
    Pan115Error = Exception  # type: ignore[misc,assignment]

logger = logging.getLogger(__name__)


def _container(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data["container"]


def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    return bool(user) and _container(context).settings.is_admin(user.id)


# ---------------------------------------------------------------------- #
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
        "• /refresh <tmdb_id> — 清除该 TMDB 缓存后重拉\n"
        "• /status — 查看配置与 115 健康状态"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update, context):
        return
    container = _container(context)
    s = container.settings
    lines = [
        f"TG Bot：{'✅ 已配置' if s.tg_bot_token else '❌ 未配置'}",
        f"推送频道：{s.tg_chat_id or '❌ 未配置'}",
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
    await update.message.reply_text("\n".join(lines))


async def cmd_115(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update, context):
        await update.message.reply_text("⛔ 无权限")
        return
    text = " ".join(context.args) if context.args else (update.message.text or "")
    parsed = parse_share(text)
    if not parsed:
        await update.message.reply_text("❌ 无法识别链接，请发送 115 分享链接、8+ 位裸码或 ed2k 链接。")
        return
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


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """裸链接消息自动触发处理（支持单链接 / 多链接批处理）。"""
    if not _is_admin(update, context):
        return
    text = update.message.text or ""
    shares = parse_shares(text)
    if not shares:
        return
    if len(shares) == 1:
        await _process(update, context, shares[0])
    else:
        await _process_batch(update, context, shares)


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
        await message.edit_text(text)
    except Exception:  # noqa: BLE001 - 编辑失败兜底重发
        try:
            await message.reply_text(text)
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
    if "已推送" in result.message:
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


async def _process_batch(update: Update, context, shares) -> None:
    """多链接串行处理：逐个推送，单条汇总消息实时更新，失败继续。"""
    container = _container(context)
    total = len(shares)
    placeholder = await update.message.reply_text(
        f"⏳ 正在处理 {total} 个链接，逐个推送中 ..."
    )
    lines: list[str] = []
    done = 0
    for parsed in shares:
        done += 1
        try:
            result = await container.processor.process(parsed)
        except Pan115Error as exc:
            lines.append(f"⚠️ {_short_id(parsed)}：{exc}".replace("\n", " "))
        except Exception as exc:  # noqa: BLE001
            logger.exception("处理分享失败")
            lines.append(f"⚠️ {_short_id(parsed)}：{exc}".replace("\n", " "))
        else:
            lines.append(_summarize_line(parsed, result))
        await _edit(
            placeholder,
            _build_batch_summary(f"⏳ 处理中 ({done}/{total})", lines),
        )

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
# 快捷菜单命令（启动时通过 setMyCommands 注册，覆盖旧项目残留）
# 顺序即菜单显示顺序；描述简短，Telegram 会作为 "/" 命令提示展示
_BOT_COMMANDS: list[BotCommand] = [
    BotCommand("start", "开始使用"),
    BotCommand("help", "用法说明"),
    BotCommand("115", "推送 115/ed2k 链接"),
    BotCommand("status", "查看配置与健康"),
    BotCommand("refresh", "清除 TMDB 缓存"),
]


async def setup_commands(application: Application) -> None:
    """启动时注册快捷菜单命令（post_init 钩子）。

    先 delete_my_commands 清除上个项目残留的菜单，再 set_my_commands 设置新菜单。
    默认 scope（对所有用户生效）。失败不阻断启动。
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


# ---------------------------------------------------------------------- #
def register(application: Application) -> None:
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("115", cmd_115))
    application.add_handler(CommandHandler("refresh", cmd_refresh))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_text)
    )
