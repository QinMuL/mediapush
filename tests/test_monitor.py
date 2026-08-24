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
    from app.monitor.service import parse_proxy

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
    from app.monitor.service import MonitorService

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
    from app.monitor.service import MonitorService

    n = MonitorService._normalize_ref
    assert n("@movie_ch") == "movie_ch"
    assert n("movie_ch") == "movie_ch"
    assert n("https://t.me/movie_ch") == "movie_ch"
    assert n("https://t.me/movie_ch/123") == "movie_ch"  # 帖子直达链接
    assert n("t.me/c/1234567") == -1001234567  # 私有频道链接 → marked id
    assert n("-1001234567") == -1001234567


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
    from app.monitor.service import MonitorService

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
    import app.monitor.service as svc_mod

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
    import app.monitor.service as svc_mod

    async def _fast_sleep(_secs):
        pass

    monkeypatch.setattr(svc_mod.asyncio, "sleep", _fast_sleep)
    svc, store, container = _make_svc(tmp_path, fail=True)
    h = link_hash(_LINK_OK)

    async def run():
        from app.monitor.service import _Pending

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
