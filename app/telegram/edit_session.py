"""编辑模式会话状态：暂存 /edit 预览阶段的草稿与交互状态。

context.user_data["edit_session"] 单键存 EditSession，避免散落键不一致。
无 JobQueue（未装 [job-queue] extra），草稿清理靠 /cancel 与新 /edit 覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.link_parser import ParsedShare
    from app.parser.media_parser import AggregatedMedia
    from app.providers.base import ShareFile


class EditState(Enum):
    """编辑模式状态机。"""

    PREVIEW = "preview"  # 预览展示中，等待按钮操作
    AWAITING_QUALITY = "awaiting_quality"  # 等待用户发送推荐语/精品说明文本


@dataclass
class EditSession:
    """单条链接的编辑草稿。

    - parsed/details/media/files/provider：prepare 阶段产物（不可变）
    - quality_extra/is_premium：用户编辑覆写（渲染到画质模块）
    - already_pushed：该链接此前是否已推送过（/edit 重推场景，用于预览提示与跳过二次去重）
    - state：交互状态机
    - preview_*：预览消息引用，用于 edit_message 更新预览内容与键盘
    """

    parsed: ParsedShare
    details: dict
    media: AggregatedMedia
    files: list[ShareFile]
    provider: str
    quality_extra: str = ""
    is_premium: bool = False
    already_pushed: bool = False
    state: EditState = EditState.PREVIEW
    preview_chat_id: int | None = None
    preview_message_id: int | None = None
    preview_is_photo: bool = False


# 推荐语/精品说明最大长度（与 pusher._QUALITY_EXTRA_LIMIT 一致，handler 入口截断）
MAX_QUALITY_EXTRA = 200
