"""Telegram 命令处理。

旧项目核心约束（见 ARCHITECTURE.md 第 5.11 / 8 节）：
- 顶部 Pan115Error 必须 try/except 容错导入：pan115_service 顶部硬依赖 p115client，
  p115client 装坏时整条 import 链崩，连带 TelegramService.start() 失败导致 bot 完全不可用；
  fallback 到 Exception 子类保留 except 分支语义。
- handler 只通过 container 公共接口访问服务，不访问私有属性。
- 长耗时操作（/115 解析+TMDB+推送）依赖 concurrent_updates(True) 不阻塞队列。

命令：/start /help /status /stop /115 <链接> /refresh <tmdb_id>
"""
from __future__ import annotations

import logging
import re

# ---- 容错导入：Pan115Error（p115client 装坏时不能拖垮 bot）----
try:
    from app.pan115.client import Pan115Error, Pan115PermanentError
except Exception:  # noqa: BLE001  容错导入，保留 except 分支语义

    class Pan115Error(Exception):
        pass

    class Pan115PermanentError(Pan115Error):
        pass


logger = logging.getLogger(__name__)

# 115 分享链接解析：https://115.com/s/{code}?password={pwd} 或 纯 code [pwd]
_LINK_RE = re.compile(r"115\.com/s/([A-Za-z0-9]+)", re.IGNORECASE)
_PW_PARAM_RE = re.compile(r"[?&]password=([A-Za-z0-9]+)", re.IGNORECASE)
# 裸分享码回退：115 码通常 8+ 字符，避免误把普通单词（如 hello）当作码
_CODE_RE = re.compile(r"^[A-Za-z0-9]{8,}$")
# 访问码可较短（4+），仅在裸码形式作为第二 token 校验
_PWD_RE = re.compile(r"^[A-Za-z0-9]{3,}$")

HELP_TEXT = (
    "<b>MediaPush 机器人</b>\n\n"
    "<b>/find</b> — 立即扫描监控分享列表并推送\n"
    "<b>/115</b> &lt;分享链接&gt; [访问码] — 解析并推送单个 115 分享\n"
    "<b>/status</b> — 查看运行状态与配置健康\n"
    "<b>/stop</b> — 停止正在运行的流水线\n"
    "<b>/refresh</b> &lt;tmdb_id&gt; — 删除 TMDB 缓存，下次拉取最新\n"
    "<b>/help</b> — 显示本帮助\n"
)


def parse_115_link(text: str) -> tuple[str, str] | None:
    """从文本解析 (share_code, receive_code)。无法解析返回 None。"""
    text = text.strip()
    if not text:
        return None
    m = _LINK_RE.search(text)
    if m:
        code = m.group(1)
        pwd = ""
        pm = _PW_PARAM_RE.search(text)
        if pm:
            pwd = pm.group(1)
        else:
            # 链接后跟的独立 token 视作访问码
            tail = text[m.end():].lstrip()
            tail = tail.split()[0] if tail else ""
            if tail and _PWD_RE.fullmatch(tail):
                pwd = tail
        return code, pwd
    # 纯 code [pwd] 形式
    tokens = text.split()
    if tokens and _CODE_RE.fullmatch(tokens[0]):
        code = tokens[0]
        pwd = tokens[1] if len(tokens) > 1 and _PWD_RE.fullmatch(tokens[1]) else ""
        return code, pwd
    return None


# ---- 命令处理（container 通过 _bind 注入，便于单测直接传参）----
async def handle_start(update, context, container):
    await update.message.reply_text(HELP_TEXT)


async def handle_help(update, context, container):
    await update.message.reply_text(HELP_TEXT)


async def handle_status(update, context, container):
    status = await container.get_status()
    lines = ["<b>📊 MediaPush 状态</b>", ""]
    lines.append(
        f"🤖 Bot：{'运行中' if status['bot_running'] else '未运行'}"
    )
    cfg = status["config_health"]
    lines.append(f"🔑 TG Token：{'✅' if cfg['tg_token'] else '❌'}")
    lines.append(f"🍪 115 Cookie：{'✅' if cfg['pan115_cookie'] else '❌'}")
    lines.append(f"🎞 TMDB Key：{'✅' if cfg['tmdb_key'] else '❌'}")
    lines.append(f"🌐 代理：{'✅' if cfg['proxy'] else '❌'}")
    lines.append("")
    lines.append(
        f"⚙️ 流水线：{'运行中' if status['pipeline_running'] else '空闲'}"
    )
    lines.append(f"📥 未推送：{status['unpushed']} 条")
    lines.append(f"🕒 调度间隔：{cfg['schedule_interval']} 分钟")
    await update.message.reply_text("\n".join(lines))


async def handle_stop(update, context, container):
    if not container.is_pipeline_running():
        await update.message.reply_text("当前没有正在运行的流水线。")
        return
    container.stop_pipeline()
    await update.message.reply_text("⏹ 已请求停止流水线，1-2 秒内中断。")


async def handle_find(update, context, container):
    """立即扫描监控分享列表（monitored_shares）并推送。"""
    codes = await container.get_monitored_shares()
    if not codes:
        await update.message.reply_text("⚠️ 未配置监控分享列表（monitored_shares）。")
        return
    if not container.pan115_ready():
        await update.message.reply_text("❌ 115 Cookie 未配置，无法扫描。")
        return
    await update.message.reply_text(f"⏳ 开始扫描 {len(codes)} 个监控分享...")
    try:
        result = await container.run_pipeline_once(codes, trigger="manual")
    except Pan115Error as e:
        await update.message.reply_text(f"❌ 115 错误：{e}")
        return
    if result.get("skipped"):
        await update.message.reply_text("⚠️ 已有流水线在运行，请稍后再试或 /stop。")
        return
    await update.message.reply_text(
        f"✅ 扫描完成：新增 {result.get('new', 0)} 条，"
        f"已推送 {result.get('pushed', 0)} 条，已存在 {result.get('existing', 0)} 条。"
    )


async def handle_115(update, context, container):
    args = context.args if hasattr(context, "args") else []
    if not args:
        await update.message.reply_text(
            "用法：/115 &lt;分享链接&gt; [访问码]\n"
            "示例：/115 https://115.com/s/abc123?password=xyz"
        )
        return
    parsed = parse_115_link(" ".join(args))
    if not parsed:
        await update.message.reply_text("❌ 无法解析 115 分享链接。")
        return
    code, pwd = parsed
    if not container.pan115_ready():
        await update.message.reply_text("❌ 115 Cookie 未配置，无法扫描。")
        return
    await update.message.reply_text(f"⏳ 正在处理：{code}")
    try:
        result = await container.run_pipeline_once([(code, pwd)], trigger="manual")
    except Pan115PermanentError as e:
        await update.message.reply_text(f"❌ 永久错误（不重试）：{e}")
        return
    except Pan115Error as e:
        await update.message.reply_text(f"❌ 115 错误：{e}")
        return
    if result.get("skipped"):
        await update.message.reply_text("⚠️ 已有流水线在运行，请稍后再试或 /stop。")
        return
    new = result.get("new", 0)
    pushed = result.get("pushed", 0)
    if result.get("cancelled"):
        await update.message.reply_text("⏹ 处理已被停止。")
    elif new == 0:
        await update.message.reply_text("ℹ️ 该分享已存在，未新增。")
    else:
        await update.message.reply_text(
            f"✅ 新增 {new} 条，已推送 {pushed} 条。"
        )


async def handle_refresh(update, context, container):
    args = context.args if hasattr(context, "args") else []
    if not args:
        await update.message.reply_text("用法：/refresh &lt;tmdb_id&gt;")
        return
    try:
        tmdb_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ tmdb_id 必须是数字。")
        return
    count = await container.refresh_tmdb(tmdb_id)
    if count > 0:
        await update.message.reply_text(
            f"✅ 已删除 {count} 条 TMDB 缓存，下次流水线将拉取最新数据。"
        )
    else:
        await update.message.reply_text("ℹ️ 未找到对应的 TMDB 缓存。")


def _bind(fn, container):
    """把 (update, context, container) 适配为 PTB 的 (update, context)。"""

    async def wrapper(update, context):
        await fn(update, context, container)

    return wrapper


def register_handlers(app, container) -> None:
    """注册命令处理器到 PTB Application。"""
    from telegram.ext import CommandHandler

    app.add_handler(CommandHandler("start", _bind(handle_start, container)))
    app.add_handler(CommandHandler("help", _bind(handle_help, container)))
    app.add_handler(CommandHandler("status", _bind(handle_status, container)))
    app.add_handler(CommandHandler("stop", _bind(handle_stop, container)))
    app.add_handler(CommandHandler("find", _bind(handle_find, container)))
    app.add_handler(CommandHandler("115", _bind(handle_115, container)))
    app.add_handler(CommandHandler("refresh", _bind(handle_refresh, container)))
