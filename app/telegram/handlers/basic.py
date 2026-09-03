"""入口命令（/start /help）+ 菜单注册 + 全局错误处理。"""

from __future__ import annotations

import logging

from telegram import BotCommand, Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import Application, ContextTypes

logger = logging.getLogger(__name__)


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
        "【媒体流水线】\n"
        "• /ed2k_status — 查看推送侧状态（账本/pending/卡死告警）\n"
        "• /upload_status — 查看上传侧状态（进度/退避/卡死告警）\n"
        "• 统一流水线：A 下载落地 → TMDB 重命名 → B 资源库\n"
        "• B：ed2k 哈希（MD4 Merkle）→ 推频道卡片 → CD2 传 115（秒传命中秒完成）→ 删源\n"
        "• 文件名格式：片名 (年份) - 画质标签 {tmdb-ID}.ext\n"
        "• 配置见 .env 的 PIPELINE_* 段（三个阶段各有 DRY_RUN 模拟开关）"
    )


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
