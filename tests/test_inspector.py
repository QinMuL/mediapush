"""ShareInspector 巡检测试：失效撤卡 / 待定 / 异常 / 通知 / 限速熔断。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.providers.pan115 import ShareStatus
from app.telegram.inspector import ShareInspector


@pytest.fixture(autouse=True)
def _fast_pacing(monkeypatch):
    """巡检间隔清零（生产 1s/条），现有用例秒过；限速语义单独测。"""
    monkeypatch.setattr("app.telegram.inspector._CHECK_PACING", 0.0)


class _FakeCache:
    def __init__(self, rows):
        self.rows = rows
        self.touched = []
        self.dead = []

    async def list_pushed_shares(self, *, provider="115", limit=100):
        assert provider == "115"
        return self.rows[:limit]

    async def touch_checked(self, code):
        self.touched.append(code)

    async def mark_dead(self, code):
        self.dead.append(code)


class _FakePan115:
    def __init__(self, statuses):
        self.statuses = dict(statuses)  # code -> ShareStatus 或 Exception
        self.calls = []

    async def check_share_status(self, code, password):
        self.calls.append(code)
        v = self.statuses[code]
        if isinstance(v, Exception):
            raise v
        return v


class _FakeTelegram:
    def __init__(self):
        self.bot = MagicMock()
        self.bot.delete_message = AsyncMock()
        self.bot.send_message = AsyncMock()


class _FakeSettings:
    def __init__(self):
        self.inspect_interval_hours = 6.0
        self.inspect_notify = True
        self.tg_admin_ids = [123]


class _FakeContainer:
    def __init__(self, pan115, cache, telegram, settings):
        self.pan115 = pan115
        self.cache = cache
        self.telegram = telegram
        self.settings = settings


def _row(code, *, title="", password="", chat_id="@chan", message_id=555):
    return {
        "share_code": code, "title": title, "password": password,
        "chat_id": chat_id, "message_id": message_id,
        "last_checked_at": None, "pushed_at": 1.0,
    }


def _run(coro):
    return asyncio.run(coro)


def test_inspect_revokes_dead_and_keeps_alive():
    """state=7 → 撤卡 + mark_dead；正常 → touch；快照中 → 待定。"""
    rows = [
        _row("ALIVE", title="活着的"),
        _row("DEAD1", title="死了的"),
        _row("SNAP1", title="快照中的"),
    ]
    cache = _FakeCache(rows)
    pan115 = _FakePan115({
        "ALIVE": ShareStatus(state=1, title="t"),
        "DEAD1": ShareStatus(state=7, message="分享已失效"),
        "SNAP1": ShareStatus(state=0, snapshotting=True, message="分享正在生成文件快照"),
    })
    tg = _FakeTelegram()
    container = _FakeContainer(pan115, cache, tg, _FakeSettings())
    insp = ShareInspector(container, container.settings)

    report = _run(insp.run_once())

    assert report.total == 3
    assert report.ok == 1
    assert report.dead == 1
    assert report.pending == 1
    # 撤卡：删除频道消息 + 标记 dead
    tg.bot.delete_message.assert_awaited_once_with(chat_id="@chan", message_id=555)
    assert cache.dead == ["DEAD1"]
    # 存活与待定均记录检查时间，未标记死亡
    assert set(cache.touched) == {"ALIVE", "SNAP1"}
    assert report.dead_items[0]["title"] == "死了的"


def test_inspect_delete_failure_still_marks_dead():
    """撤卡失败（消息已删/无权限）不崩溃，仍 mark_dead 防重复巡检。"""
    rows = [_row("DEAD2", title="X")]
    cache = _FakeCache(rows)
    pan115 = _FakePan115({"DEAD2": ShareStatus(state=7, message="分享已失效")})
    tg = _FakeTelegram()
    tg.bot.delete_message = AsyncMock(side_effect=RuntimeError("message to delete not found"))
    container = _FakeContainer(pan115, cache, tg, _FakeSettings())
    insp = ShareInspector(container, container.settings)

    report = _run(insp.run_once())

    assert report.dead == 1
    assert cache.dead == ["DEAD2"]


def test_inspect_no_message_reference_still_marks_dead():
    """旧数据无消息引用（message_id=None）无法撤卡，仅告警 + mark_dead。"""
    rows = [_row("OLD1", title="旧卡", message_id=None)]
    cache = _FakeCache(rows)
    pan115 = _FakePan115({"OLD1": ShareStatus(state=7, message="分享已失效")})
    tg = _FakeTelegram()
    container = _FakeContainer(pan115, cache, tg, _FakeSettings())
    insp = ShareInspector(container, container.settings)

    report = _run(insp.run_once())

    assert report.dead == 1
    assert cache.dead == ["OLD1"]
    tg.bot.delete_message.assert_not_awaited()


def test_inspect_network_error_counts_and_defers():
    """查询抛异常（网络/限速）→ errors + touch，不判死。"""
    rows = [_row("ERR1")]
    cache = _FakeCache(rows)
    pan115 = _FakePan115({"ERR1": RuntimeError("connect timeout")})
    tg = _FakeTelegram()
    container = _FakeContainer(pan115, cache, tg, _FakeSettings())
    insp = ShareInspector(container, container.settings)

    report = _run(insp.run_once())

    assert report.errors == 1
    assert report.dead == 0
    assert cache.touched == ["ERR1"]
    assert cache.dead == []


def test_inspect_notify_admin_sends_summary():
    """撤卡明细 → admin 收到汇总。"""
    rows = [_row("DEAD3", title="失效片")]
    cache = _FakeCache(rows)
    pan115 = _FakePan115({"DEAD3": ShareStatus(state=7, message="分享已失效")})
    tg = _FakeTelegram()
    container = _FakeContainer(pan115, cache, tg, _FakeSettings())
    insp = ShareInspector(container, container.settings)

    report = _run(insp.run_once())
    _run(insp.notify_admin(report))

    tg.bot.send_message.assert_awaited_once()
    text = tg.bot.send_message.await_args.kwargs["text"]
    assert "失效片" in text and "巡检" in text
    assert tg.bot.send_message.await_args.kwargs["chat_id"] == 123


def test_inspect_empty_rows_noop():
    cache = _FakeCache([])
    pan115 = _FakePan115({})
    tg = _FakeTelegram()
    container = _FakeContainer(pan115, cache, tg, _FakeSettings())
    insp = ShareInspector(container, container.settings)

    report = _run(insp.run_once())

    assert report.total == 0
    tg.bot.delete_message.assert_not_awaited()


# -------------------- A5 cookie 文件热更新 + 失效告警 -------------------- #
class _CookiePan115:
    """带 cookie 状态的 pan115 桩。"""

    def __init__(self, cookie="UID=1;CID=2;", health=None):
        self.cookie = cookie
        self.updated = []
        self._health = health

    def update_cookie(self, cookie):
        self.updated.append(cookie)
        self.cookie = cookie

    async def check_share_status(self, code, password):
        return ShareStatus(state=1)

    async def check_health(self):
        return self._health


class _CookieSettings(_FakeSettings):
    def __init__(self, cookie_file=""):
        super().__init__()
        self.pan115_cookie_file = cookie_file


def test_cookie_file_refresh_hot_reload(tmp_path):
    """cookie 文件内容变化 → update_cookie 热生效；无变化/无文件不动。"""
    f = tmp_path / "cookie.txt"
    f.write_text("UID=9;CID=8;", encoding="utf-8")
    pan115 = _CookiePan115(cookie="UID=1;CID=2;")
    container = _FakeContainer(pan115, _FakeCache([]), _FakeTelegram(), _CookieSettings(str(f)))
    insp = ShareInspector(container, container.settings)

    insp._refresh_cookie_file()
    assert pan115.updated == ["UID=9;CID=8;"]
    assert pan115.cookie == "UID=9;CID=8;"

    # 再刷：内容未变 → 不重复更新
    insp._refresh_cookie_file()
    assert len(pan115.updated) == 1


def test_cookie_health_alert_once_with_throttle():
    """cookie 失效 → admin 告警一次；24h 内重复失败被节流。"""
    pan115 = _CookiePan115(health=False)
    tg = _FakeTelegram()
    container = _FakeContainer(pan115, _FakeCache([]), tg, _CookieSettings())
    insp = ShareInspector(container, container.settings)

    _run(insp._check_cookie_health())
    _run(insp._check_cookie_health())  # 节流：不重复告警

    assert tg.bot.send_message.await_count == 1
    assert "Cookie 已失效" in tg.bot.send_message.await_args.kwargs["text"]


def test_cookie_health_ok_no_alert():
    """cookie 健康（True）或匿名（None）不告警。"""
    pan115 = _CookiePan115(health=True)
    tg = _FakeTelegram()
    container = _FakeContainer(pan115, _FakeCache([]), tg, _CookieSettings())
    insp = ShareInspector(container, container.settings)

    _run(insp._check_cookie_health())
    tg.bot.send_message.assert_not_awaited()


# -------------------- 访问码语义：need_code 计存活 + 明细 -------------------- #
def test_inspect_need_code_counts_alive():
    """缺访问码（errno 4100012/4100008）→ 存活计数 + code_items 明细，不待定不撤卡。"""
    rows = [_row("NEED1", title="缺码的"), _row("CHG1", title="改码的")]
    cache = _FakeCache(rows)
    pan115 = _FakePan115({
        "NEED1": ShareStatus(need_code=True),
        "CHG1": ShareStatus(need_code=True, code_changed=True),
    })
    tg = _FakeTelegram()
    container = _FakeContainer(pan115, cache, tg, _FakeSettings())
    insp = ShareInspector(container, container.settings)

    report = _run(insp.run_once())

    assert report.ok == 2  # 活着
    assert report.need_code == 2
    assert report.pending == 0
    assert report.dead == 0
    reasons = {it["share_code"]: it["reason"] for it in report.code_items}
    assert "未存档" in reasons["NEED1"]
    assert "已变更" in reasons["CHG1"]
    tg.bot.delete_message.assert_not_awaited()
    assert set(cache.touched) == {"NEED1", "CHG1"}
    assert "缺访问码" in report.summary()


# -------------------- 限速与熔断：防 115 IP 限流（405） -------------------- #
def test_inspect_pacing_between_checks(monkeypatch):
    """每条检查之间留 1s 间隔（首条免等）——防匿名连发触发 IP 限流。"""
    sleeps: list = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr("app.telegram.inspector.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("app.telegram.inspector._CHECK_PACING", 1.0)
    rows = [_row(f"P{i}") for i in range(4)]
    cache = _FakeCache(rows)
    pan115 = _FakePan115({f"P{i}": ShareStatus() for i in range(4)})
    container = _FakeContainer(pan115, cache, _FakeTelegram(), _FakeSettings())
    insp = ShareInspector(container, container.settings)

    _run(insp.run_once())

    assert sleeps == [1.0, 1.0, 1.0]  # 4 条 = 3 个间隔


def test_inspect_aborts_after_consecutive_errors():
    """连续 5 次查询异常 → 中止本轮，剩余不查（IP 疑似被限，别硬打）。"""
    rows = [_row(f"E{i}") for i in range(10)]
    cache = _FakeCache(rows)
    pan115 = _FakePan115({f"E{i}": RuntimeError("HTTP 405") for i in range(10)})
    container = _FakeContainer(pan115, cache, _FakeTelegram(), _FakeSettings())
    insp = ShareInspector(container, container.settings)

    report = _run(insp.run_once())

    assert len(pan115.calls) == 5  # 第 5 次异常即熔断，后 5 条不再请求
    assert report.errors == 5
    assert report.total == 10  # 总数照记（summary 体现异常中断）


def test_inspect_error_counter_resets_on_success():
    """异常计数被成功重置：4 异常 + 1 成功 + 4 异常 → 不触发熔断。"""
    statuses = {}
    for i in range(4):
        statuses[f"X{i}"] = RuntimeError("405")
    statuses["OK1"] = ShareStatus()
    for i in range(4, 8):
        statuses[f"X{i}"] = RuntimeError("405")
    rows = ([_row(f"X{i}") for i in range(4)] + [_row("OK1")]
            + [_row(f"X{i}") for i in range(4, 8)])
    cache = _FakeCache(rows)
    pan115 = _FakePan115(statuses)
    container = _FakeContainer(pan115, cache, _FakeTelegram(), _FakeSettings())
    insp = ShareInspector(container, container.settings)

    report = _run(insp.run_once())

    assert len(pan115.calls) == 9  # 全部查完，未熔断
    assert report.errors == 8
    assert report.ok == 1
