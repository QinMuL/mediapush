"""频道监控模块测试：store 持久化 / watcher 纯函数 / service 配置选择。"""

import asyncio
import time

from app.monitor.store import (
    KEY_BATCH,
    KEY_TARGET,
    KIND_EXCLUDE,
    KIND_INCLUDE,
    MonitorStore,
    link_hash,
)
from app.monitor.watcher import (
    FilterRules,
    extract_ed2k,
    format_ts,
    human_size,
    match_filters,
    parse_link,
    process_text,
    validate,
)

# 标准 ed2k 样例（name / size / hash 均合法）
_LINK_OK = "ed2k://|file|Movie.2026.1080p.mkv|4680847360|0123456789ABCDEF0123456789ABCDEF|h=ABCDEFGH|/"
_LINK_OK2 = "ed2k://|file|Show.S02E01.720p.mp4|734003200|FEDCBA9876543210FEDCBA9876543210|/"
_LINK_MIN = "ed2k://|file|A.mkv|1024|AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA|/"


# ------------------------------------------------------------------ #
# watcher：提取 / 验证 / 拆解
# ------------------------------------------------------------------ #
def test_extract_ordered_and_dedup():
    text = f"前缀 {_LINK_OK} 中间 {_LINK_OK} 重复 {_LINK_OK2} 尾部"
    assert extract_ed2k(text) == [_LINK_OK, _LINK_OK2]


def test_extract_empty_text():
    assert extract_ed2k("") == []
    assert extract_ed2k("普通文本无链接") == []


def test_validate_ok_and_broken():
    assert validate(_LINK_OK)
    assert validate(_LINK_MIN)  # 最简形态（hash 后直接 |/）
    assert not validate("ed2k://|file|A.mkv|1024|SHORT|/")  # hash 不足 32 位
    assert not validate("ed2k://|file|A.mkv|abc|0123456789ABCDEF0123456789ABCDEF|/")  # size 非数字
    assert not validate("ed2k://|file||1024|0123456789ABCDEF0123456789ABCDEF|/")  # 空文件名（提取层已挡）


def test_parse_link_fields():
    item = parse_link(_LINK_OK)
    assert item is not None
    assert item.filename == "Movie.2026.1080p.mkv"
    assert item.size == 4680847360
    assert parse_link("not-ed2k") is None


def test_human_size_binary_units():
    assert human_size(1024) == "1.00 KiB"
    assert human_size(4680847360) == "4.36 GiB"  # 1024 进制
    assert human_size(500) == "500 B"


# ------------------------------------------------------------------ #
# watcher：过滤 / 渲染 / 完整链路
# ------------------------------------------------------------------ #
def test_match_filters_exclude_wins():
    rules = FilterRules(include=[], exclude=["广告"])
    assert not match_filters("电影.广告.mkv", rules)
    assert match_filters("电影.mkv", rules)


def test_match_filters_include_requires_hit():
    rules = FilterRules(include=["1080p", "2160p"], exclude=[])
    assert match_filters("Movie.1080p.mkv", rules)
    assert not match_filters("Movie.720p.mkv", rules)
    # 未配置 include = 不限
    assert match_filters("Movie.720p.mkv", FilterRules(include=[], exclude=[]))


def test_match_filters_case_insensitive():
    rules = FilterRules(include=["1080P"], exclude=[])
    assert match_filters("movie.1080p.mkv", rules)


def test_render_and_process_text():
    text = f"发布\n{_LINK_OK}\n{_LINK_OK2}"
    html, items = process_text(text, "测试频道", FilterRules([], []), latest_ts=1756089600.0)
    assert len(items) == 2
    assert "📺 来源：测试频道" in html
    assert "🔗 ed2k 链接（2 条）" in html
    assert f"<code>{_LINK_OK}</code>" in html
    assert "4.36 GiB" in html
    # 北京时间渲染（UTC 2025-08-25 02:40 → 北京 10:40）
    assert format_ts(1756089600.0) == "2025-08-25 10:40:00"


def test_process_text_filtered_out():
    text = f"{_LINK_OK}"
    rules = FilterRules(include=["4K"], exclude=[])
    html, items = process_text(text, "频道", rules, latest_ts=1756089600.0)
    assert html == "" and items == []


def test_process_text_invalid_dropped():
    # 提取层产出的链接必带 /，手工构造一条缺尾杠的串验证防御
    html, items = process_text("ed2k://|file|A.mkv|1024|0123456789ABCDEF0123456789ABCDEF", "c", FilterRules([], []))
    assert html == "" and items == []


def test_process_text_html_escape():
    link = "ed2k://|file|A&B<测>.mkv|1024|0123456789ABCDEF0123456789ABCDEF|/"
    html, _ = process_text(link, "频<道>", FilterRules([], []), latest_ts=1756089600.0)
    assert "A&amp;B&lt;测&gt;.mkv" in html
    assert "频&lt;道&gt;" in html


# ------------------------------------------------------------------ #
# store：频道 / 设置 / 过滤 / 去重
# ------------------------------------------------------------------ #
def test_store_channels_crud(tmp_path):
    store = MonitorStore(str(tmp_path / "m.db"))

    async def run():
        assert await store.add_channel(-100123, "影视频道", "movie_ch")
        assert not await store.add_channel(-100123, "改名频道")  # 重复 → False 且刷新信息
        assert await store.add_channel(-100456, "剧集频道", "")

        chs = await store.list_channels()
        assert len(chs) == 2
        assert chs[0].chat_id == -100123
        assert chs[0].title == "改名频道"  # 重复添加刷新了标题
        assert chs[0].username == "movie_ch"

        # last_msg_id 只增不减
        await store.set_last_msg_id(-100123, 500)
        await store.set_last_msg_id(-100123, 200)
        chs = await store.list_channels()
        assert chs[0].last_msg_id == 500

        assert await store.remove_channel(-100456)
        assert not await store.remove_channel(-100456)  # 再删 → False
        assert len(await store.list_channels()) == 1

    asyncio.run(run())


def test_store_settings_kv(tmp_path):
    store = MonitorStore(str(tmp_path / "m.db"))

    async def run():
        assert await store.get_setting(KEY_TARGET) is None
        await store.set_setting(KEY_TARGET, "@push_ch")
        assert await store.get_setting(KEY_TARGET) == "@push_ch"
        await store.set_setting(KEY_TARGET, "-100999")  # 覆盖更新
        assert await store.get_setting(KEY_TARGET) == "-100999"
        await store.set_setting(KEY_BATCH, "30")
        assert await store.get_setting(KEY_BATCH) == "30"

    asyncio.run(run())


def test_store_filters(tmp_path):
    store = MonitorStore(str(tmp_path / "m.db"))

    async def run():
        assert await store.add_filter("1080p", KIND_INCLUDE)
        assert not await store.add_filter("1080p", KIND_EXCLUDE)  # 关键词已存在
        assert await store.add_filter("广告", KIND_EXCLUDE)
        rules = await store.list_filters()
        assert {r.keyword: r.kind for r in rules} == {"1080p": KIND_INCLUDE, "广告": KIND_EXCLUDE}

        assert await store.remove_filter("1080p")
        assert not await store.remove_filter("1080p")
        assert len(await store.list_filters()) == 1

        try:  # 非法类型抛错
            await store.add_filter("x", "bad")
            raise AssertionError("should raise")
        except ValueError:
            pass

    asyncio.run(run())


def test_store_seen_dedup_and_cleanup(tmp_path):
    store = MonitorStore(str(tmp_path / "m.db"))

    async def run():
        h1, h2 = link_hash(_LINK_OK), link_hash(_LINK_OK2)
        assert not await store.is_seen(h1)
        await store.mark_seen([h1, h2])
        assert await store.is_seen(h1)
        assert await store.is_seen(h2)
        # 重复标记幂等
        await store.mark_seen([h1])

        # TTL 清理：手工插入过期行
        await store._execute(
            "INSERT OR REPLACE INTO mon_seen (link_hash, first_seen) VALUES (?,?)",
            ("expired", time.time() - 31 * 86400),
        )
        n = await store.cleanup_seen(30)
        assert n == 1
        assert not await store.is_seen("expired")
        assert await store.is_seen(h1)  # 新记录保留

    asyncio.run(run())


def test_store_persistence_across_reopen(tmp_path):
    db = str(tmp_path / "m.db")

    async def run():
        store = MonitorStore(db)
        await store.add_channel(-1001, "频道", "ch")
        await store.add_filter("2160p", KIND_INCLUDE)
        await store.set_setting(KEY_BATCH, "60")
        await store.set_last_msg_id(-1001, 42)
        await store.close()

        store2 = MonitorStore(db)
        chs = await store2.list_channels()
        assert chs[0].last_msg_id == 42  # 水位持久化（重启补扫依赖）
        assert await store2.get_setting(KEY_BATCH) == "60"
        assert (await store2.list_filters())[0].keyword == "2160p"
        await store2.close()

    asyncio.run(run())


# ------------------------------------------------------------------ #
# service：代理解析 / 目标与窗口选择（不触网）
# ------------------------------------------------------------------ #
class _Settings:
    def __init__(self):
        self.tg_api_id = 123
        self.tg_api_hash = "hash"
        self.monitor_session = "./data/monitor.session"
        self.monitor_batch_seconds = 0
        self.proxy_url = ""
        self.tg_chat_id = "@default_ch"
        self.tg_chat_id_ed2k = "@ed2k_ch"


def test_parse_proxy_variants():
    from app.monitor.channel_monitor import parse_proxy

    assert parse_proxy("") is None
    assert parse_proxy("socks5://127.0.0.1:7890") == {
        "proxy_type": "socks5",
        "addr": "127.0.0.1",
        "port": 7890,
    }
    assert parse_proxy("127.0.0.1:7890")["proxy_type"] == "socks5"  # 无 scheme 默认 socks5
    assert parse_proxy("http://user:pw@10.0.0.1:8080") == {
        "proxy_type": "http",
        "addr": "10.0.0.1",
        "port": 8080,
        "username": "user",
        "password": "pw",
    }
    assert parse_proxy("ftp://1.2.3.4:21") is None  # 不支持的协议
    assert parse_proxy("socks5://nohost") is None  # 缺端口


def test_target_and_batch_selection(tmp_path):
    from app.monitor.channel_monitor import MonitorService

    class _Container:
        pusher = None

    async def run():
        s = _Settings()
        store = MonitorStore(str(tmp_path / "m.db"))
        svc = MonitorService(s, store, _Container())

        # 默认：ed2k 频道 > 默认频道
        assert await svc.target_chat_id() == "@ed2k_ch"
        s.tg_chat_id_ed2k = ""
        assert await svc.target_chat_id() == "@default_ch"

        # /mon target 覆盖一切
        await store.set_setting(KEY_TARGET, "@my_push")
        assert await svc.target_chat_id() == "@my_push"

        # batch：默认取 settings，/mon batch 覆盖，非法值回退
        assert await svc.batch_seconds() == 0
        s.monitor_batch_seconds = 30
        assert await svc.batch_seconds() == 30
        await store.set_setting(KEY_BATCH, "10")
        assert await svc.batch_seconds() == 10
        await store.set_setting(KEY_BATCH, "abc")
        assert await svc.batch_seconds() == 30

        # 过滤规则组装
        await store.add_filter("1080p", KIND_INCLUDE)
        await store.add_filter("广告", KIND_EXCLUDE)
        rules = await svc.filters()
        assert rules.include == ["1080p"] and rules.exclude == ["广告"]
        await store.close()

    asyncio.run(run())


def test_normalize_ref_variants():
    from app.monitor.channel_monitor import MonitorService

    n = MonitorService._normalize_ref
    assert n("@movie_ch") == "movie_ch"
    assert n("movie_ch") == "movie_ch"
    assert n("https://t.me/movie_ch") == "movie_ch"
    assert n("https://t.me/movie_ch/123") == "movie_ch"  # 帖子直达链接
    assert n("t.me/c/1234567") == -1001234567  # 私有频道链接 → marked id
    assert n("-1001234567") == -1001234567


# ------------------------------------------------------------------ #
# service：Bot 内交互式登录流（stub TelegramClient，不触网）
# ------------------------------------------------------------------ #
class _FakeLoginClient:
    """可编程 Telethon 客户端替身：模拟发码/验证码/两步密码各分支。"""

    def __init__(self, *, need_password=False, code_valid=True, password_valid=True):
        self.need_password = need_password
        self.code_valid = code_valid
        self.password_valid = password_valid
        self.connected = False
        self.handlers = []
        self.calls = []  # (method, kwargs) 调用记录

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    def is_connected(self):
        return self.connected

    async def is_user_authorized(self):
        return False  # 登录流程中恒为未授权

    def add_event_handler(self, handler, event):
        self.handlers.append(handler)

    async def get_me(self):
        from types import SimpleNamespace

        return SimpleNamespace(first_name="监控号", id=42, phone="+8613800138000")

    async def send_code_request(self, phone):
        self.calls.append(("send_code", phone))
        from types import SimpleNamespace

        return SimpleNamespace(phone_code_hash="hash123")

    async def sign_in(self, code=None, password=None, phone_code_hash=None):
        from telethon.errors import (
            PasswordHashInvalidError,
            PhoneCodeInvalidError,
            SessionPasswordNeededError,
        )

        self.calls.append(("sign_in", code, password))
        if code is not None:
            if not self.code_valid:
                raise PhoneCodeInvalidError(request=None)
            if self.need_password:
                raise SessionPasswordNeededError(request=None)
        if password is not None and not self.password_valid:
            raise PasswordHashInvalidError(request=None)


def _login_svc(tmp_path, monkeypatch, client_kwargs=None, settings=None):
    """构造登录测试用 MonitorService：_make_client 每次产生新 FakeClient（真实语义）。"""
    from app.monitor.channel_monitor import MonitorService

    store = MonitorStore(str(tmp_path / "m.db"))

    class _Container:
        pusher = None

    svc = MonitorService(settings or _Settings(), store, _Container())
    fakes: list[_FakeLoginClient] = []

    async def _fake_make_client(self):
        client = _FakeLoginClient(**(client_kwargs or {}))
        await client.connect()
        fakes.append(client)
        return client

    monkeypatch.setattr(MonitorService, "_make_client", _fake_make_client)
    return svc, store, fakes


def test_login_flow_with_password(tmp_path, monkeypatch):
    """完整登录流：/mon login → 手机号（自动补 +86）→ 验证码 → 两步密码 → 监控启动。"""
    from app.monitor.channel_monitor import STATE_RUNNING

    svc, store, fakes = _login_svc(tmp_path, monkeypatch, {"need_password": True})

    async def run():
        # 1) /mon login（无手机号）→ 等手机号
        msg = await svc.login_start(chat_id=100)
        assert "手机号" in msg
        assert await svc.login_stage(100) == "phone"

        # 2) 手机号（国内 11 位自动补 +86）→ 发码（创建客户端）
        ok, msg = await svc.login_phone("13800138000")
        assert ok and "验证码" in msg
        assert await svc.login_stage(100) == "code"
        assert ("send_code", "+8613800138000") in fakes[0].calls

        # 3) 验证码 → 触发两步验证
        status, msg = await svc.login_code("12345")
        assert status == "password" and "两步验证" in msg
        assert await svc.login_stage(100) == "password"

        # 4) 密码 → 登录成功，监控启动（同一客户端贯穿发码与登录）
        status, msg = await svc.login_password("secret-pwd")
        assert status == "ok" and "登录成功" in msg
        assert svc.state == STATE_RUNNING
        assert svc.is_running
        assert len(fakes) == 1  # 全程同一客户端
        assert len(fakes[0].handlers) == 1  # NewMessage 处理器已注册
        assert ("sign_in", "12345", None) in fakes[0].calls
        assert ("sign_in", None, "secret-pwd") in fakes[0].calls

        # 登录完成后会话清空，文本恢复链接解析
        assert await svc.login_stage(100) is None
        await asyncio.sleep(0)  # 让后台 _post_login 任务执行完毕
        await store.close()

    asyncio.run(run())


def test_login_flow_direct_code_ok(tmp_path, monkeypatch):
    """/mon login 带手机号直接发码；验证码即完成（无两步验证）。"""
    from app.monitor.channel_monitor import STATE_RUNNING

    svc, store, _ = _login_svc(tmp_path, monkeypatch)

    async def run():
        msg = await svc.login_start(chat_id=100, phone="+8613800138000")
        assert "验证码" in msg
        assert await svc.login_stage(100) == "code"
        status, msg = await svc.login_code("54321")
        assert status == "ok"
        assert svc.state == STATE_RUNNING
        await asyncio.sleep(0)
        await store.close()

    asyncio.run(run())


def test_login_retry_keeps_session(tmp_path, monkeypatch):
    """验证码错误 → retry 保留会话可重试；手机号阶段格式错误同样保留。"""
    svc, store, _ = _login_svc(
        tmp_path, monkeypatch, {"code_valid": False, "need_password": True}
    )

    async def run():
        # 手机号格式错误：会话保留在 phone 阶段
        await svc.login_start(100)
        ok, msg = await svc.login_phone("123")
        assert not ok and "格式" in msg
        assert await svc.login_stage(100) == "phone"

        # 发码后验证码错误：会话保留在 code 阶段
        await svc.login_phone("+8613800138000")
        status, msg = await svc.login_code("00000")
        assert status == "retry" and "验证码错误" in msg
        assert await svc.login_stage(100) == "code"
        await store.close()

    asyncio.run(run())


def test_login_timeout_discards_client(tmp_path, monkeypatch):
    """会话超时 → login_stage 惰性清理并断开客户端。"""
    svc, store, fakes = _login_svc(tmp_path, monkeypatch)

    async def run():
        import time as _time

        await svc.login_start(100, "+8613800138000")
        assert await svc.login_stage(100) == "code"
        assert fakes[0].connected

        svc._login.expires_at = _time.time() - 1  # 手工置过期
        assert await svc.login_stage(100) is None
        assert not fakes[0].connected  # 客户端已断开
        assert svc._login is None
        await store.close()

    asyncio.run(run())


def test_login_cancel_and_single_instance(tmp_path, monkeypatch):
    """/cancel 取消登录；新 /mon login 覆盖旧会话并断开旧客户端。"""
    svc, store, fakes = _login_svc(tmp_path, monkeypatch)

    async def run():
        assert "没有进行中的登录" in await svc.login_cancel()

        await svc.login_start(100, "+8613800138000")
        assert "已取消登录" in await svc.login_cancel()
        assert not fakes[0].connected

        # 新会话覆盖旧会话：旧客户端断开，新客户端接管
        await svc.login_start(100, "+8613800138000")
        assert fakes[1].connected
        await svc.login_start(200, "+8613912345678")
        assert not fakes[1].connected  # 旧客户端被覆盖废弃
        assert fakes[2].connected
        assert await svc.login_stage(200) == "code"
        assert await svc.login_stage(100) is None  # 旧 chat 不再有效
        await store.close()

    asyncio.run(run())


def test_login_rejected_when_running_or_no_api(tmp_path, monkeypatch):
    """监控运行中拒绝重复登录；缺 API 配置直接拒绝。"""
    from app.monitor.channel_monitor import STATE_NO_API

    class _NoApiSettings(_Settings):
        def __init__(self):
            super().__init__()
            self.tg_api_id = 0
            self.tg_api_hash = ""

    svc, store, _ = _login_svc(tmp_path, monkeypatch)
    svc_noapi, store2, _ = _login_svc(tmp_path, monkeypatch, settings=_NoApiSettings())

    async def run():
        # 正常配置 → 进入手机号阶段
        msg = await svc.login_start(100)
        assert "手机号" in msg

        # 缺 API 配置 → 直接拒绝
        msg = await svc_noapi.login_start(100)
        assert "TG_API_ID" in msg
        assert svc_noapi.state == STATE_NO_API

        # 已运行 → 拒绝（手工接管一个已连接客户端）
        client = _FakeLoginClient()
        await client.connect()
        await svc._setup(client)
        msg = await svc.login_start(100)
        assert "已在运行" in msg
        await store.close()
        await store2.close()

    asyncio.run(run())


def test_login_stage_desc_property(tmp_path, monkeypatch):
    """login_stage_desc：各阶段中文描述；无会话/过期返回空串。"""
    import time as _time

    svc, store, _ = _login_svc(tmp_path, monkeypatch)
    assert svc.login_stage_desc == ""  # 无会话

    async def run():
        await svc.login_start(100)  # phone 阶段
        assert svc.login_stage_desc == "等待手机号"
        await svc.login_phone("+8613800138000")  # code 阶段
        assert svc.login_stage_desc == "等待验证码"
        svc._login.stage = "password"
        assert svc.login_stage_desc == "等待两步密码"

        svc._login.expires_at = _time.time() - 1  # 过期
        assert svc.login_stage_desc == ""
        await store.close()

    asyncio.run(run())


# ------------------------------------------------------------------ #
# service：事件 → 推送 管线（stub Bot / Telethon 事件，不触网）
# ------------------------------------------------------------------ #
class _FakeBot:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[dict] = []
        self.fail = fail

    async def send_message(self, **kwargs) -> None:
        if self.fail:
            raise RuntimeError("network down")
        self.sent.append(kwargs)


class _FakePusher:
    def __init__(self, fail: bool = False) -> None:
        self.bot = _FakeBot(fail)


class _FakeContainer:
    def __init__(self, fail: bool = False) -> None:
        self.pusher = _FakePusher(fail)


class _FakeProcessor:
    """可编程 processor：按序弹出预设结果，记录 process 调用。"""

    def __init__(self, results: list) -> None:
        self.results = list(results)
        self.calls: list[tuple] = []

    async def process(self, parsed, *, chat_id: str | None = None):
        self.calls.append((parsed.provider, parsed.code, chat_id))
        return self.results.pop(0)


class _FakeDate:
    def __init__(self, ts: float) -> None:
        self._ts = ts

    def timestamp(self) -> float:
        return self._ts


class _FakeMsg:
    def __init__(self, text: str, msg_id: int = 100) -> None:
        self.message = text
        self.id = msg_id
        self.date = _FakeDate(1756089600.0)


class _FakeEvent:
    def __init__(self, chat_id: int, text: str, msg_id: int = 100) -> None:
        self.chat_id = chat_id
        self.message = _FakeMsg(text, msg_id)


def _make_svc(tmp_path, fail: bool = False):
    """构造真实 store + stub container 的 MonitorService（不触网）。"""
    from app.monitor.channel_monitor import MonitorService

    store = MonitorStore(str(tmp_path / "m.db"))
    container = _FakeContainer(fail)
    svc = MonitorService(_Settings(), store, container)
    return svc, store, container


def test_on_new_message_realtime_push_and_dedup(tmp_path):
    """batch=0：实时直推 → 标记去重 → 重复消息不再推。"""
    svc, store, container = _make_svc(tmp_path)
    svc._monitored[-100123] = "测试频道"
    event = _FakeEvent(-100123, f"新资源发布\n{_LINK_OK}")
    other = _FakeEvent(-100999, _LINK_OK)  # 非监控频道

    async def run():
        await svc._on_new_message(other)
        assert container.pusher.bot.sent == []  # 非监控频道忽略

        await svc._on_new_message(event)
        assert len(container.pusher.bot.sent) == 1
        sent = container.pusher.bot.sent[0]
        assert "📺 来源：测试频道" in sent["text"]
        assert f"<code>{_LINK_OK}</code>" in sent["text"]
        assert sent["parse_mode"] == "HTML"
        assert sent["disable_web_page_preview"] is True

        # 去重已落库：同链接再出现不重复推送
        assert await store.is_seen(link_hash(_LINK_OK))
        await svc._on_new_message(_FakeEvent(-100123, f"重发\n{_LINK_OK}", msg_id=101))
        assert len(container.pusher.bot.sent) == 1
        await store.close()

    asyncio.run(run())


def test_on_new_message_excludes_filtered_links(tmp_path):
    """exclude 规则命中的链接不推送。"""
    svc, store, container = _make_svc(tmp_path)
    svc._monitored[-100123] = "测试频道"

    async def run():
        await store.add_filter("1080p", KIND_EXCLUDE)
        await svc._on_new_message(_FakeEvent(-100123, _LINK_OK))
        assert container.pusher.bot.sent == []  # 文件名含 1080p 被排除
        assert not await store.is_seen(link_hash(_LINK_OK))  # 未推送不标记
        await store.close()

    asyncio.run(run())


def test_batch_window_merges_links(tmp_path, monkeypatch):
    """batch>0：窗口内同频道多条消息的链接合并为一条推送。"""
    import app.monitor.channel_monitor as svc_mod

    monkeypatch.setattr(svc_mod, "_PUSH_INTERVAL", 0)  # 免去推送限速等待
    svc, store, container = _make_svc(tmp_path)
    svc._monitored[-100123] = "测试频道"

    async def run():
        await store.set_setting(KEY_BATCH, "3600")  # 长窗口：测试期内定时器不触发
        await svc._on_new_message(_FakeEvent(-100123, _LINK_OK, msg_id=100))
        await svc._on_new_message(_FakeEvent(-100123, _LINK_OK2, msg_id=101))
        assert container.pusher.bot.sent == []  # 窗口内不推

        pend = svc._pending.get(-100123)
        assert pend is not None and pend.task is not None
        assert len(pend.items) == 2  # 两条消息的链接已合并

        pend.task.cancel()  # 模拟窗口到期前终止定时器，手动冲刷
        await svc._flush(-100123)
        assert len(container.pusher.bot.sent) == 1
        text = container.pusher.bot.sent[0]["text"]
        assert "ed2k 链接（2 条）" in text
        assert _LINK_OK in text and _LINK_OK2 in text
        await store.close()

    asyncio.run(run())


def test_flush_failure_rolls_back_seen(tmp_path, monkeypatch):
    """推送失败：不落去重 + 回滚进程内占位（下次出现重试）。"""
    import app.monitor.channel_monitor as svc_mod

    async def _fast_sleep(_secs):
        pass

    monkeypatch.setattr(svc_mod.asyncio, "sleep", _fast_sleep)
    svc, store, container = _make_svc(tmp_path, fail=True)
    h = link_hash(_LINK_OK)

    async def run():
        from app.monitor.channel_monitor import _Pending

        item = parse_link(_LINK_OK)
        svc._pending[-100123] = _Pending(
            title="频道", items=[item], hashes=[h], latest_ts=1756089600.0
        )
        svc._seen_mem = {h}
        await svc._flush(-100123)

        assert container.pusher.bot.sent == []  # 全部失败（内部已重试 3 次）
        assert not await store.is_seen(h)  # 未标记去重
        assert h not in svc._seen_mem  # 占位回滚，可重试
        await store.close()

    asyncio.run(run())


# ------------------------------------------------------------------ #
# service：卡片模式（有 processor 时走主链路，模板与手动推送一致）
# ------------------------------------------------------------------ #
def _pr(ok: bool, message: str = "", dup: bool = False):
    from app.core.processor import ProcessResult

    return ProcessResult(ok=ok, message=message, dup=dup)


def _make_card_svc(tmp_path, results, fail: bool = False):
    """构造带 FakeProcessor 的监控服务（卡片模式）。"""
    from app.monitor.channel_monitor import MonitorService, _Pending

    store = MonitorStore(str(tmp_path / "m.db"))
    container = _FakeContainer(fail)
    processor = _FakeProcessor(results)
    container.processor = processor
    svc = MonitorService(_Settings(), store, container)
    items = [parse_link(_LINK_OK), parse_link(_LINK_OK2)]
    hashes = [link_hash(_LINK_OK), link_hash(_LINK_OK2)]
    svc._pending[-100123] = _Pending(
        title="频道", items=items, hashes=hashes, latest_ts=1756089600.0
    )
    svc._seen_mem = set(hashes)
    return svc, store, container, processor


def test_flush_card_mode_uses_processor_pipeline(tmp_path, monkeypatch):
    """有 processor：逐链接走 process（与手动推送同一管线），不再发合并文本。"""
    import app.monitor.channel_monitor as svc_mod

    monkeypatch.setattr(svc_mod, "_PUSH_INTERVAL", 0)
    svc, store, container, processor = _make_card_svc(
        tmp_path, [_pr(True), _pr(True)]
    )

    async def run():
        await svc._flush(-100123)
        # 两条链接各走一次主链路，provider=ed2k、code=完整 URL、目标=ed2k 频道
        assert [(p, c, t) for p, c, t in processor.calls] == [
            ("ed2k", _LINK_OK, "@ed2k_ch"),
            ("ed2k", _LINK_OK2, "@ed2k_ch"),
        ]
        assert container.pusher.bot.sent == []  # 卡片由 pusher.push_share 发出
        assert await store.is_seen(link_hash(_LINK_OK))
        assert await store.is_seen(link_hash(_LINK_OK2))
        await store.close()

    asyncio.run(run())


def test_flush_card_mode_dup_marks_seen_without_fallback(tmp_path):
    """已推送过（dup）：视为完成并标记 seen，不回退纯文本。"""
    svc, store, container, processor = _make_card_svc(
        tmp_path, [_pr(True), _pr(False, "分享 xxx 已推送过，跳过", dup=True)]
    )

    async def run():
        await svc._flush(-100123)
        assert len(processor.calls) == 2
        assert container.pusher.bot.sent == []  # dup 不回退纯文本
        assert await store.is_seen(link_hash(_LINK_OK2))  # 仍标记 seen
        await store.close()

    asyncio.run(run())


def test_flush_card_mode_tmdb_miss_falls_back_to_text(tmp_path):
    """TMDB 未匹配：卡片失败但回退纯文本中继成功 → 标记 seen。"""
    svc, store, container, _ = _make_card_svc(
        tmp_path, [_pr(True), _pr(False, "TMDB 未匹配到：Unknown")]
    )

    async def run():
        await svc._flush(-100123)
        # 第二条回退为单链接纯文本
        assert len(container.pusher.bot.sent) == 1
        text = container.pusher.bot.sent[0]["text"]
        assert f"<code>{_LINK_OK2}</code>" in text
        assert await store.is_seen(link_hash(_LINK_OK))
        assert await store.is_seen(link_hash(_LINK_OK2))  # 回退成功也算完成
        await store.close()

    asyncio.run(run())


def test_flush_card_mode_partial_failure_rolls_back(tmp_path, monkeypatch):
    """部分失败：成功的标记 seen，失败的回滚占位（可重试）。"""
    import app.monitor.channel_monitor as svc_mod

    async def _fast_sleep(_secs):
        pass

    monkeypatch.setattr(svc_mod.asyncio, "sleep", _fast_sleep)
    svc, store, _, _ = _make_card_svc(
        tmp_path, [_pr(True), _pr(False, "TMDB 未匹配到：X")], fail=True
    )

    async def run():
        await svc._flush(-100123)
        # 第一条卡片成功；第二条卡片失败且回退纯文本也失败（bot.fail=True）
        assert await store.is_seen(link_hash(_LINK_OK))
        assert not await store.is_seen(link_hash(_LINK_OK2))
        assert link_hash(_LINK_OK2) not in svc._seen_mem  # 占位回滚
        await store.close()

    asyncio.run(run())
