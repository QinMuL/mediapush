"""Telegram handlers 包：命令处理器按领域拆分（原单文件 handlers.py 1862 行）。

- common.py      公共工具（容器/权限/会话/格式化/Pan115Error 容错导入）
- basic.py       /start /help + 菜单注册 + 全局错误处理
- status.py      /status /ed2k_status /upload_status
- push.py        /115 + 裸链接自动处理 + PushCoordinator（去重/串行/聚合）
- edit_flow.py   /edit /cancel + 预览/回调/确认推送
- admin.py       /refresh /loglevel /reload /cookie /reset
- monitor_cmds.py /mon /dir /share /inspect
"""

from __future__ import annotations

import time

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from app.telegram.handlers.admin import (
    cmd_cookie,
    cmd_loglevel,
    cmd_refresh,
    cmd_reload,
    cmd_reset,
)
from app.telegram.handlers.basic import (
    _BOT_COMMANDS,
    _error_handler,
    cmd_help,
    cmd_start,
    setup_commands,
)
from app.telegram.handlers.common import (
    _DENY_TEXT,
    _clear_session,
    _container,
    _get_session,
    _is_admin,
    _set_session,
)
from app.telegram.handlers.edit_flow import (
    cmd_cancel,
    cmd_edit,
    on_edit_callback,
)
from app.telegram.handlers.monitor_cmds import (
    cmd_dir,
    cmd_inspect,
    cmd_mon,
    cmd_share,
)
from app.telegram.handlers.push import (
    _episode_sort_key,
    cmd_115,
    coordinator,
    on_text,
)
from app.telegram.handlers.status import (
    cmd_ed2k_status,
    cmd_status,
    cmd_upload_status,
)

__all__ = [
    "cmd_115", "cmd_cancel", "cmd_cookie", "cmd_dir", "cmd_edit",
    "cmd_ed2k_status", "cmd_help", "cmd_inspect", "cmd_loglevel",
    "cmd_mon", "cmd_refresh", "cmd_reload", "cmd_reset", "cmd_share",
    "cmd_start", "cmd_status", "cmd_upload_status",
    "coordinator", "on_edit_callback", "on_text", "register", "setup_commands",
    # 公共工具（测试与内部模块复用）
    "_BOT_COMMANDS", "_episode_sort_key",
    "_DENY_TEXT", "_container", "_is_admin",
    "_get_session", "_set_session", "_clear_session",
]


# ---------------------------------------------------------------------- #
async def _touch_inbound(update: Update, context) -> None:
    """记录入站收包时间戳（group=-1 最先执行；/status 展示与排障用）。

    即使后续 handler 出错也已记录——"最后收包时间"是入站链路
    是否存活的直接证据（配合 bot.py 的入站看门狗）。
    """
    context.application.bot_data["_last_update_at"] = time.monotonic()


def register(application: Application) -> None:
    application.add_handler(TypeHandler(Update, _touch_inbound), group=-1)
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
