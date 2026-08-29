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
    cmd_cookie,
    cmd_edit,
    cmd_status,
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
    assert cmds == [
        "start", "help", "115", "edit", "cancel", "status", "refresh",
        "loglevel", "reload", "cookie", "mon", "inspect", "dir", "share",
    ]
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

    async def prepare(self, parsed, *, skip_dedup: bool = False):
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
        return True, self.push_return, 555, "@chan"


class _FakeCache:
    def __init__(self, pushed=False) -> None:
        self._pushed = pushed
        self.marked: list = []

    async def is_pushed(self, _code) -> bool:
        return self._pushed

    async def mark_pushed(self, code, **kwargs):
        self.marked.append(code)


class _FakeContainer:
    def __init__(self, processor=None, pusher=None, cache=None) -> None:
        self.processor = processor
        self._pusher = pusher
        self.cache = cache if cache is not None else _FakeCache()
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
    """prepare 失败（如 TMDB 未匹配）→ 不建 session，placeholder 显示 ⚠️。"""
    pr = PrepareResult(False, "❌ TMDB 未匹配到：X")
    proc = _FakeProcessor(pr)
    container = _FakeContainer(processor=proc)
    ctx = _make_context(container)
    ctx.args = ["https://115.com/s/abc12345"]
    msg = _make_message()
    update = _make_update(message=msg)

    asyncio.run(cmd_edit(update, ctx))

    assert _get_session(ctx) is None
    msg.edit_text.assert_called()  # placeholder edit 显示 ⚠️


def test_cmd_edit_repush_already_pushed():
    """已推送过的链接 /edit 仍能进入预览（重推：跳过去重，标记 already_pushed）。"""
    pr = PrepareResult(
        True, "", file_count=1, title="X", year=2020, media_type="movie",
        details=_movie_details(), media=_movie_media(), files=_movie_files(),
    )
    proc = _FakeProcessor(pr)
    cache = _FakeCache(pushed=True)  # 已推送过
    container = _FakeContainer(processor=proc, cache=cache)
    ctx = _make_context(container)
    ctx.args = ["https://115.com/s/abc12345"]
    msg = _make_message()
    update = _make_update(message=msg)

    asyncio.run(cmd_edit(update, ctx))

    session = _get_session(ctx)
    assert session is not None  # 仍建 session（未被去重拦截）
    assert session.already_pushed is True
    assert len(proc.prepare_calls) == 1  # prepare 被调（skip_dedup）


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


def _ed2k(name: str) -> ParsedShare:
    return ParsedShare("ed2k", f"ed2k://|file|{name}|123|0123456789ABCDEF0123456789ABCDEF|/", None)


def test_episode_sort_key_orders_by_season_episode():
    """ed2k 链接按 SxxExy 排序：乱序输入 → E01..E25 有序输出。"""
    from app.telegram.handlers import _episode_sort_key

    shares = [
        _ed2k("Show.2026.S01E25.2160p.mp4"),
        _ed2k("Show.2026.S01E03.2160p.mp4"),
        _ed2k("Show.2026.S01E10.2160p.mp4"),
        _ed2k("Show.2026.S01E02.2160p.mp4"),
        _ed2k("Show.2026.S01E01.2160p.mp4"),
    ]
    ordered = sorted(shares, key=_episode_sort_key)
    eps = [_episode_sort_key(s)[2] for s in ordered]
    assert eps == [1, 2, 3, 10, 25]


def test_episode_sort_key_no_episode_and_115_sort_last_stable():
    """无集数 ed2k 与 115 排最后，且彼此保持原序（稳定排序）。"""
    from app.telegram.handlers import _episode_sort_key

    ed2k_ep = _ed2k("Show.S01E05.2160p.mp4")
    ed2k_none = _ed2k("Movie.2026.2160p.mp4")  # 无 SxxExx
    p115 = ParsedShare("115", "abc12345", None)
    ordered = sorted([ed2k_none, ed2k_ep, p115], key=_episode_sort_key)
    assert ordered[0] is ed2k_ep  # 有集数排第一
    assert ordered[1] is ed2k_none  # 无集数保持原序
    assert ordered[2] is p115


# ---------------------------------------------------------------------- #
# /cookie：查看状态 / 写文件热更新 / 直配互斥保护
# ---------------------------------------------------------------------- #
class _CookiePan115:
    def __init__(self, cookie="", health=True) -> None:
        self.cookie = cookie
        self.uid = 309130782
        self._health = health

    def update_cookie(self, cookie):
        self.cookie = cookie

    async def check_health(self):
        return self._health


class _CookieContainer:
    def __init__(self, pan115, *, env_cookie="", cookie_file=""):
        self.pan115 = pan115
        self.refreshed: list = []
        self.settings = MagicMock()
        self.settings.pan115_cookie = env_cookie
        self.settings.pan115_cookie_direct = bool(env_cookie)
        self.settings.pan115_cookie_file = cookie_file

    def refresh_cookie_file(self):
        self.refreshed.append(True)
        # 模拟 Container 行为：文件内容变化 → provider 热更新
        if self.settings.pan115_cookie_file:
            from pathlib import Path

            try:
                new = Path(self.settings.pan115_cookie_file).read_text(
                    encoding="utf-8"
                ).strip()
            except OSError:
                return False
            if new and new != self.pan115.cookie:
                self.pan115.update_cookie(new)
                return True
        return False


def test_cmd_cookie_status_view():
    """无参数：显示来源/长度/UID，不回显 cookie 原文。"""
    pan = _CookiePan115(cookie="UID=1;CID=2;")
    container = _CookieContainer(pan, cookie_file="./data/ck.txt")
    ctx = _make_context(container)
    msg = _make_message("/cookie")
    update = _make_update(message=msg)

    asyncio.run(cmd_cookie(update, ctx))

    text = msg.reply_text.await_args.args[0]
    assert "UID=1" not in text  # 不回显原文
    assert "309130782" in text
    assert "data/ck.txt" in text


def test_cmd_cookie_set_writes_file_and_probes(tmp_path):
    """带参数：写入 cookie 文件 → 热更新 provider → 探活反馈。"""
    f = tmp_path / "ck.txt"
    f.write_text("UID=old;", encoding="utf-8")
    pan = _CookiePan115(cookie="UID=old;", health=True)
    container = _CookieContainer(pan, cookie_file=str(f))
    ctx = _make_context(container)
    ctx.args = ["UID=9;CID=8;"]
    msg = _make_message("/cookie UID=9;CID=8;")
    update = _make_update(message=msg)

    asyncio.run(cmd_cookie(update, ctx))

    assert f.read_text(encoding="utf-8") == "UID=9;CID=8;"  # 持久化
    assert pan.cookie == "UID=9;CID=8;"  # provider 热更新
    assert container.refreshed == [True]
    # 探活结果经 edit_text 反馈
    edited = msg.reply_text.return_value.edit_text.await_args.args[0]
    assert "探活通过" in edited


def test_cmd_cookie_env_direct_blocks_setting():
    """PAN115_COOKIE 直配非空：拒绝 bot 内设置（重启会回到旧值，防不一致）。"""
    pan = _CookiePan115(cookie="UID=old;")
    container = _CookieContainer(pan, env_cookie="UID=old;", cookie_file="./x.txt")
    ctx = _make_context(container)
    ctx.args = ["UID=9;"]
    msg = _make_message("/cookie UID=9;")
    update = _make_update(message=msg)

    asyncio.run(cmd_cookie(update, ctx))

    text = msg.reply_text.await_args.args[0]
    assert "直配" in text
    assert pan.cookie == "UID=old;"  # 未变更


def test_cmd_cookie_no_file_configured():
    """未配置 PAN115_COOKIE_FILE：提示先配置再 /reload。"""
    pan = _CookiePan115()
    container = _CookieContainer(pan)
    ctx = _make_context(container)
    ctx.args = ["UID=9;"]
    msg = _make_message("/cookie UID=9;")
    update = _make_update(message=msg)

    asyncio.run(cmd_cookie(update, ctx))

    text = msg.reply_text.await_args.args[0]
    assert "PAN115_COOKIE_FILE" in text


# ---------------------------------------------------------------------- #
# /status：115 cookie 状态行按 provider 运行时状态显示（文件方式不误报）
# ---------------------------------------------------------------------- #
class _StatusPan115:
    def __init__(self, cookie="") -> None:
        self.cookie = cookie
        self.uid = 309130782

    async def check_health(self):
        return bool(self.cookie) or None


class _StatusCache:
    async def stats(self):
        return {"pushed": 0, "dead": 0, "tmdb_cache": 0, "share_dirs": 0, "shared_items": 0}


class _StatusContainer:
    def __init__(self, pan115, *, env_cookie="", cookie_file="") -> None:
        self.pan115 = pan115
        self.cache = _StatusCache()
        self.monitor = None
        self.settings = MagicMock()
        self.settings.pan115_cookie = env_cookie
        self.settings.pan115_cookie_direct = bool(env_cookie)
        self.settings.pan115_cookie_file = cookie_file
        self.settings.tg_bot_token = "t"
        self.settings.tg_chat_id = "@c"
        self.settings.tg_chat_id_115 = ""
        self.settings.tg_chat_id_ed2k = ""
        self.settings.tmdb_api_key = "k"
        self.settings.proxy_url = ""
        self.settings.inspect_enabled = False
        self.settings.inspect_interval_hours = 6
        self.settings.share_watch_enabled = False
        self.settings.share_watch_interval_minutes = 10


def _run_status(env_cookie="", cookie_file="", provider_cookie=""):
    pan = _StatusPan115(provider_cookie)
    container = _StatusContainer(pan, env_cookie=env_cookie, cookie_file=cookie_file)
    ctx = _make_context(container)
    msg = _make_message("/status")
    update = _make_update(message=msg)
    asyncio.run(cmd_status(update, ctx))
    return msg.reply_text.await_args.args[0]


def test_cmd_status_cookie_file_mode_not_misreported():
    """文件方式：.env 直配为空但 provider 已加载 → 显示来源+UID，不误报未配置。"""
    text = _run_status(
        env_cookie="", cookie_file="./data/115cookie.txt", provider_cookie="UID=1;"
    )
    assert "115 Cookie：未配置" not in text
    assert "115 Cookie：✅ 文件 ./data/115cookie.txt（UID 309130782）" in text


def test_cmd_status_cookie_env_direct_shown():
    """直配方式：显示“直配”来源 + UID。"""
    text = _run_status(env_cookie="UID=1;", provider_cookie="UID=1;")
    assert "直配" in text
    assert "309130782" in text


def test_cmd_status_cookie_absent_shows_anonymous():
    """无任何 cookie：维持“未配置（匿名读取，可用）”。"""
    text = _run_status()
    assert "未配置（匿名读取，可用）" in text


def test_cmd_status_cookie_file_backfilled_not_direct():
    """回归（NAS 实测场景）：文件方式启动时 config 把文件内容回填进
    pan115_cookie 字段——须按 direct 标记判来源，不得误标“直配”。"""
    pan = _StatusPan115("UID=1;")
    container = _StatusContainer(pan, env_cookie="", cookie_file="./data/115cookie.txt")
    # 模拟 config.load 文件回填：字段有值但 direct=False
    container.settings.pan115_cookie = "UID=1;"
    container.settings.pan115_cookie_direct = False
    ctx = _make_context(container)
    msg = _make_message("/status")
    update = _make_update(message=msg)
    asyncio.run(cmd_status(update, ctx))

    text = msg.reply_text.await_args.args[0]
    assert "115 Cookie：✅ 文件 ./data/115cookie.txt（UID 309130782）" in text
    assert "直配" not in text


def test_cmd_cookie_file_backfilled_allows_update(tmp_path):
    """回归：文件回填场景下 /cookie 设置不被“直配保护”误拒。"""
    f = tmp_path / "ck.txt"
    f.write_text("UID=old;", encoding="utf-8")
    pan = _CookiePan115(cookie="UID=old;", health=True)
    container = _CookieContainer(pan, cookie_file=str(f))
    # 模拟文件回填：pan115_cookie 有值但非直配
    container.settings.pan115_cookie = "UID=old;"
    container.settings.pan115_cookie_direct = False
    ctx = _make_context(container)
    ctx.args = ["UID=9;CID=8;"]
    msg = _make_message("/cookie UID=9;CID=8;")
    update = _make_update(message=msg)

    asyncio.run(cmd_cookie(update, ctx))

    assert pan.cookie == "UID=9;CID=8;"  # 未被直配保护拦截
    assert f.read_text(encoding="utf-8") == "UID=9;CID=8;"


# -------------------- A4 处理中 60s 去重 -------------------- #
def test_processing_dedup_blocks_duplicate_concurrent():
    """同一链接处理中再次发送 → 提示且不再重复 process。"""
    proc = _FakeProcessor()
    container = _FakeContainer(processor=proc)
    ctx = _make_context(container)
    msg = _make_message("https://115.com/s/abc12345")
    update = _make_update(message=msg)

    from app.telegram.handlers import _mark_processing, _processing

    # 第一次正常处理（结束后标记已被 finally 清除）
    asyncio.run(on_text(update, ctx))
    assert len(proc.process_calls) == 1

    # 模拟"处理中"（另一并发任务尚未完成）→ 跳过并提示
    _mark_processing(ParsedShare("115", "abc12345"))
    try:
        asyncio.run(on_text(update, ctx))
        assert len(proc.process_calls) == 1  # 未重复处理
        texts = [c.args[0] for c in msg.reply_text.call_args_list]
        assert any("正在处理中" in t for t in texts)
    finally:
        _processing.clear()


def test_processing_ttl_expires():
    """超过 60s 的陈旧标记视为不在处理中（顺手清理）。"""
    import time as _t

    from app.telegram.handlers import (
        _PROCESSING_TTL,
        _is_processing,
        _processing,
    )

    _processing["115:xyz"] = _t.monotonic() - _PROCESSING_TTL - 1  # 已过期
    try:
        assert _is_processing(ParsedShare("115", "xyz")) is False
        assert "115:xyz" not in _processing  # 已被清理
    finally:
        _processing.clear()
