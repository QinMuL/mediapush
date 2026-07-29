"""Bot 命令菜单注册测试 + 编辑模式（/edit）流程测试。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.core.link_parser import ParsedShare
from app.core.processor import PrepareResult, ProcessResult
from app.parser.media_parser import AggregatedMedia
from app.providers.base import ShareFile
from app.telegram.edit_session import EditSession, EditState
from app.telegram.handlers import (
    _BOT_COMMANDS,
    _get_session,
    _set_session,
    cmd_edit,
    on_edit_callback,
    on_text,
    setup_commands,
)


# ---------------------------------------------------------------------- #
# 菜单注册（原有）
# ---------------------------------------------------------------------- #
class _FakeBot:
    def __init__(self) -> None:
        self.deleted = False
        self.set_commands = None

    async def delete_my_commands(self, *args, **kwargs) -> bool:
        self.deleted = True
        return True

    async def set_my_commands(self, commands, *args, **kwargs) -> bool:
        self.set_commands = commands
        return True


class _FakeApp:
    def __init__(self) -> None:
        self.bot = _FakeBot()


def test_bot_commands_structure():
    """命令清单顺序与描述非空。"""
    cmds = [c.command for c in _BOT_COMMANDS]
    assert cmds == ["start", "help", "115", "edit", "cancel", "status", "refresh"]
    for c in _BOT_COMMANDS:
        assert c.description, f"{c.command} 描述为空"


def test_setup_commands_clears_then_sets():
    """setup_commands 先删除旧菜单，再设置新菜单。"""
    app = _FakeApp()
    asyncio.run(setup_commands(app))
    assert app.bot.deleted is True  # 先清除残留
    assert app.bot.set_commands is _BOT_COMMANDS  # 再注册新菜单


# ---------------------------------------------------------------------- #
# 编辑模式（/edit）流程 mock 测试
# ---------------------------------------------------------------------- #
class _FakeSettings:
    def is_admin(self, _uid) -> bool:
        return True


class _FakeProcessor:
    def __init__(self, prepare_result=None) -> None:
        self._pr = prepare_result
        self.prepare_calls: list = []
        self.process_calls: list = []

    async def prepare(self, parsed):
        self.prepare_calls.append(parsed)
        return self._pr

    async def process(self, parsed):
        self.process_calls.append(parsed)
        return ProcessResult(True, "已推送（测试）", file_count=1, title="X")


class _FakePusher:
    def __init__(self) -> None:
        self.push_calls: list = []
        self.push_return = (True, "已推送（测试）")

    async def push_share(self, *args, **kwargs):
        self.push_calls.append((args, kwargs))
        return self.push_return


class _FakeCache:
    def __init__(self, pushed=False) -> None:
        self._pushed = pushed
        self.marked: list = []

    async def is_pushed(self, _code) -> bool:
        return self._pushed

    async def mark_pushed(self, code):
        self.marked.append(code)


class _FakeContainer:
    def __init__(self, processor=None, pusher=None, cache=None) -> None:
        self.processor = processor
        self._pusher = pusher
        self.cache = cache
        self.settings = _FakeSettings()

    @property
    def pusher(self):
        return self._pusher


def _make_context(container, user_data=None):
    ctx = MagicMock()
    ctx.application.bot_data = {"container": container}
    ctx.user_data = user_data if user_data is not None else {}
    ctx.bot = MagicMock()
    ctx.bot.edit_message_text = AsyncMock()
    ctx.bot.edit_message_caption = AsyncMock()
    ctx.args = []
    return ctx


def _make_message(text=""):
    msg = MagicMock()
    msg.text = text
    msg.chat_id = 456
    msg.message_id = 100
    # 所有被 await 的方法用 AsyncMock；reply_text/reply_photo 返回 msg 自身
    msg.reply_text = AsyncMock(return_value=msg)
    msg.reply_photo = AsyncMock(return_value=msg)
    msg.delete = AsyncMock(return_value=True)
    msg.edit_text = AsyncMock(return_value=True)
    return msg


def _make_callback(data, message_id=100, user_id=123):
    q = MagicMock()
    q.data = data
    q.from_user.id = user_id
    q.message.message_id = message_id
    q.answer = AsyncMock()
    return q


def _make_update(message=None, callback_query=None, user_id=123):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = 456
    update.message = message
    update.callback_query = callback_query
    return update


def _movie_details():
    return {
        "tmdb_id": 1, "media_type": "movie", "title": "X", "year": 2020,
        "release_date": "2020-01-01", "overview": "", "poster_path": None,
        "vote_average": 7.0, "vote_count": 10, "status": "Released",
        "genres": ["剧情"], "runtime": 120, "cast": [], "countries": [],
    }


def _movie_media():
    return AggregatedMedia(title="X", year=2020, media_type="movie", quality="1080P")


def _movie_files():
    return [ShareFile(name="X.2020.1080p.mkv", size=100, is_dir=False)]


# -------------------- cmd_edit -------------------- #
def test_cmd_edit_success_builds_session():
    """prepare 成功 → 建立 EditSession + 发预览（无海报走 reply_text）。"""
    details = _movie_details()
    media = _movie_media()
    files = _movie_files()
    pr = PrepareResult(
        True, "", file_count=1, title="X", year=2020, media_type="movie",
        details=details, media=media, files=files,
    )
    proc = _FakeProcessor(pr)
    container = _FakeContainer(processor=proc)
    ctx = _make_context(container)
    ctx.args = ["https://115.com/s/abc12345"]
    msg = _make_message("/edit https://115.com/s/abc12345")
    update = _make_update(message=msg)

    asyncio.run(cmd_edit(update, ctx))

    assert len(proc.prepare_calls) == 1
    assert proc.prepare_calls[0].provider == "115"
    session = _get_session(ctx)
    assert session is not None
    assert session.details is details
    assert session.media is media
    assert session.preview_message_id == 100
    assert session.preview_is_photo is False
    assert session.state == EditState.PREVIEW


def test_cmd_edit_prepare_fails_no_session():
    """prepare 失败（已推送）→ 不建 session，placeholder 显示 ⚠️。"""
    pr = PrepareResult(False, "分享 abc 已推送过，跳过")
    proc = _FakeProcessor(pr)
    container = _FakeContainer(processor=proc)
    ctx = _make_context(container)
    ctx.args = ["https://115.com/s/abc12345"]
    msg = _make_message()
    update = _make_update(message=msg)

    asyncio.run(cmd_edit(update, ctx))

    assert _get_session(ctx) is None
    msg.edit_text.assert_called()  # placeholder edit 显示 ⚠️


# -------------------- on_edit_callback -------------------- #
def test_callback_toggle_premium_flips_and_refreshes():
    session = EditSession(
        parsed=ParsedShare("115", "abc"), details=_movie_details(),
        media=_movie_media(), files=_movie_files(), provider="115",
        preview_message_id=100, preview_is_photo=False,
    )
    ctx = _make_context(_FakeContainer())
    _set_session(ctx, session)
    update = _make_update(callback_query=_make_callback("toggle_premium", 100))

    asyncio.run(on_edit_callback(update, ctx))

    assert session.is_premium is True
    ctx.bot.edit_message_text.assert_called()  # 刷新预览


def test_callback_confirm_push_marks_pushed_and_clears():
    parsed = ParsedShare("115", "abc", "p")
    session = EditSession(
        parsed=parsed, details=_movie_details(), media=_movie_media(),
        files=_movie_files(), provider="115",
        preview_message_id=100, preview_is_photo=False,
        quality_extra="推荐语", is_premium=True,
    )
    pusher = _FakePusher()
    cache = _FakeCache(pushed=False)
    container = _FakeContainer(pusher=pusher, cache=cache)
    ctx = _make_context(container)
    from app.telegram.handlers import _set_session
    _set_session(ctx, session)
    update = _make_update(callback_query=_make_callback("confirm_push", 100))

    asyncio.run(on_edit_callback(update, ctx))

    assert len(pusher.push_calls) == 1
    _args, kwargs = pusher.push_calls[0]
    assert kwargs.get("quality_extra") == "推荐语"
    assert kwargs.get("is_premium") is True
    assert cache.marked == ["abc"]
    assert _get_session(ctx) is None  # 推送后清空


def test_callback_cancel_clears_session():
    session = EditSession(
        parsed=ParsedShare("115", "abc"), details=_movie_details(),
        media=_movie_media(), files=_movie_files(), provider="115",
        preview_message_id=100, preview_is_photo=False,
    )
    ctx = _make_context(_FakeContainer())
    from app.telegram.handlers import _set_session
    _set_session(ctx, session)
    update = _make_update(callback_query=_make_callback("cancel_edit", 100))

    asyncio.run(on_edit_callback(update, ctx))

    assert _get_session(ctx) is None


def test_callback_stale_button_rejected():
    """按钮不属于当前预览消息 → 拦截，不修改 session。"""
    session = EditSession(
        parsed=ParsedShare("115", "abc"), details=_movie_details(),
        media=_movie_media(), files=_movie_files(), provider="115",
        preview_message_id=100, preview_is_photo=False,
    )
    ctx = _make_context(_FakeContainer())
    from app.telegram.handlers import _set_session
    _set_session(ctx, session)
    # callback 来自 message_id=999（陈旧）
    update = _make_update(callback_query=_make_callback("toggle_premium", 999))

    asyncio.run(on_edit_callback(update, ctx))

    assert session.is_premium is False  # 未翻转
    ctx.bot.edit_message_text.assert_not_called()


def test_callback_confirm_push_dedup_again():
    """confirm 时二次去重命中 → 不推送，清 session。"""
    parsed = ParsedShare("115", "abc")
    session = EditSession(
        parsed=parsed, details=_movie_details(), media=_movie_media(),
        files=_movie_files(), provider="115",
        preview_message_id=100, preview_is_photo=False,
    )
    pusher = _FakePusher()
    cache = _FakeCache(pushed=True)  # 已被并发推送
    container = _FakeContainer(pusher=pusher, cache=cache)
    ctx = _make_context(container)
    from app.telegram.handlers import _set_session
    _set_session(ctx, session)
    update = _make_update(callback_query=_make_callback("confirm_push", 100))

    asyncio.run(on_edit_callback(update, ctx))

    assert pusher.push_calls == []  # 未推送
    assert cache.marked == []  # 未标记
    assert _get_session(ctx) is None  # 仍清空


# -------------------- on_text 分发 -------------------- #
def test_on_text_awaiting_quality_consumes_as_extra():
    """AWAITING_QUALITY 状态下文本作为推荐语，不走链接处理。"""
    session = EditSession(
        parsed=ParsedShare("115", "abc"), details=_movie_details(),
        media=_movie_media(), files=_movie_files(), provider="115",
        preview_message_id=100, preview_is_photo=False,
        state=EditState.AWAITING_QUALITY,
    )
    proc = _FakeProcessor()  # prepare/process 都不应被调
    container = _FakeContainer(processor=proc)
    ctx = _make_context(container)
    from app.telegram.handlers import _set_session
    _set_session(ctx, session)
    msg = _make_message("原盘内封中字 · 国配音轨")
    update = _make_update(message=msg)

    asyncio.run(on_text(update, ctx))

    assert session.quality_extra == "原盘内封中字 · 国配音轨"
    assert session.state == EditState.PREVIEW
    assert proc.prepare_calls == []
    assert proc.process_calls == []
    ctx.bot.edit_message_text.assert_called()  # 刷新预览


def test_on_text_no_session_routes_to_process():
    """无 session 时 115 链接仍走 _process（编辑分发不遮蔽自动直推）。"""
    proc = _FakeProcessor()
    container = _FakeContainer(processor=proc)
    ctx = _make_context(container)
    msg = _make_message("https://115.com/s/abc12345")  # 115 URL
    update = _make_update(message=msg)

    asyncio.run(on_text(update, ctx))

    assert len(proc.process_calls) == 1  # 走 process
    assert _get_session(ctx) is None


# -------------------- EditSession 辅助 -------------------- #
def test_edit_session_import():
    """EditSession/EditState 可正常构造。"""
    from app.telegram.edit_session import EditSession

    s = EditSession(
        parsed=ParsedShare("115", "c"), details={}, media=_movie_media(),
        files=[], provider="115",
    )
    assert s.quality_extra == ""
    assert s.is_premium is False
    assert s.state == EditState.PREVIEW
