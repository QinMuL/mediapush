"""handlers 包公共工具：容器取用 / 权限 / 会话辅助 / 通用格式化。"""

from __future__ import annotations

import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.telegram.edit_session import EditSession
from app.telegram.pusher import _send_with_retry

# Pan115Error 容错导入：p115client 装坏时退化为 Exception，保留 except 语义
try:
    from app.providers import Pan115Error
except Exception:  # noqa: BLE001
    Pan115Error = Exception  # type: ignore[misc,assignment]

logger = logging.getLogger(__name__)

_SESSION_KEY = "edit_session"

# 权限拒绝统一文案（所有 admin 命令共用，风格一致）
_DENY_TEXT = "⛔ 仅管理员可用（在 .env 的 TG_ADMIN_IDS 中配置）"

# 进程启动时刻（模块首次导入≈进程启动，供 /status 展示运行时长）
_STARTED_AT = time.monotonic()


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


def _count_dir_files(path: str) -> int | None:
    """目录内普通文件数（队列深度参考）；目录不可访问返回 None。"""
    from pathlib import Path

    try:
        return sum(1 for p in Path(path).iterdir() if p.is_file())
    except OSError:
        return None


def _fmt_kb(n: int | None) -> str:
    return "?" if n is None else str(n)


async def _edit(message, text: str) -> None:
    """编辑消息；编辑失败（非 flood）兜底重发。"""
    try:
        await _send_with_retry(lambda: message.edit_text(text))
    except Exception:  # noqa: BLE001
        try:
            await _send_with_retry(lambda: message.reply_text(text))
        except Exception:  # noqa: BLE001
            logger.warning("回复失败")
