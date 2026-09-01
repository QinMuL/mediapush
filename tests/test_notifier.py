"""通知器测试：群发、截断、节流告警、恢复通知。"""

import asyncio

import pytest

from app.telegram.notifier import AdminNotifier, notify_admins


class _FakeBot:
    def __init__(self, fail_uids: set[int] | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail_uids = fail_uids or set()

    async def send_message(self, chat_id: int, text: str) -> None:
        if chat_id in self.fail_uids:
            raise RuntimeError("blocked")
        self.sent.append((chat_id, text))


def test_notify_admins_sends_to_all():
    bot = _FakeBot()
    asyncio.run(notify_admins(bot, [1, 2], "hello"))
    assert bot.sent == [(1, "hello"), (2, "hello")]


def test_notify_admins_single_failure_does_not_block():
    """一个 admin 失败不影响其他 admin 收到。"""
    bot = _FakeBot(fail_uids={1})
    asyncio.run(notify_admins(bot, [1, 2], "hello"))
    assert bot.sent == [(2, "hello")]


def test_notify_admins_empty_inputs():
    bot = _FakeBot()
    asyncio.run(notify_admins(bot, [], "x"))  # 无 admin
    asyncio.run(notify_admins(bot, [1], ""))  # 空文本
    assert bot.sent == []


def test_notify_admins_truncates_long_text():
    bot = _FakeBot()
    asyncio.run(notify_admins(bot, [1], "x" * 5000))
    text = bot.sent[0][1]
    assert len(text) < 5000
    assert "超长已截断" in text


def test_notifier_alert_throttles_same_key():
    """同 key 节流窗口内只发一次。"""
    bot = _FakeBot()
    n = AdminNotifier(bot, [1], throttle_seconds=3600)
    assert asyncio.run(n.alert("k", "first")) is True
    assert asyncio.run(n.alert("k", "second")) is False  # 节流
    assert bot.sent == [(1, "first")]


def test_notifier_different_keys_independent():
    bot = _FakeBot()
    n = AdminNotifier(bot, [1], throttle_seconds=3600)
    asyncio.run(n.alert("a", "A"))
    asyncio.run(n.alert("b", "B"))  # 不同 key 不互相节流
    assert len(bot.sent) == 2


def test_notifier_resolve_sends_recovery_only_after_alert():
    """未告警过 → resolve 静默；告警过 → 发恢复通知并可重新告警。"""
    bot = _FakeBot()
    n = AdminNotifier(bot, [1], throttle_seconds=3600)

    asyncio.run(n.resolve("k", "recovered?"))  # 从未告警 → 不发
    assert bot.sent == []

    asyncio.run(n.alert("k", "down"))
    asyncio.run(n.resolve("k", "recovered"))  # 告警过 → 发恢复
    assert bot.sent == [(1, "down"), (1, "recovered")]

    # resolve 后重新 alert 不受旧节流限制（新一轮故障应及时告警）
    assert asyncio.run(n.alert("k", "down again")) is True


def test_notifier_throttle_expiry(monkeypatch):
    """节流窗口过后可再次告警（模拟时间流逝）。"""
    bot = _FakeBot()
    n = AdminNotifier(bot, [1], throttle_seconds=100)
    t = [0.0]
    monkeypatch.setattr("app.telegram.notifier.time.monotonic", lambda: t[0])
    asyncio.run(n.alert("k", "first"))
    t[0] = 101  # 时间前进超过节流窗口
    assert asyncio.run(n.alert("k", "second")) is True


@pytest.mark.parametrize("text", ["正常消息", "password=abc123"])
def test_notify_admins_text_passthrough(text):
    """通知器本身不做脱敏（脱敏在日志层）；原样送达。"""
    bot = _FakeBot()
    asyncio.run(notify_admins(bot, [1], text))
    assert bot.sent[0][1] == text


# -------------------- 轮次汇总统一模板 -------------------- #
def test_format_round_report_basic():
    """icon 标题 + 时间戳 + 汇总行 + 明细行，顺序稳定。"""
    from app.telegram.notifier import format_round_report

    text = format_round_report("📤", "CD2 上传汇总", "扫描 3：完成 1", ["✅ a.mkv"])
    lines = text.splitlines()
    assert lines[0].startswith("📤 CD2 上传汇总 · ")  # 头行含时间戳
    assert len(lines[0].split("· ")[-1]) == 11  # MM-DD HH:MM
    assert lines[1] == "扫描 3：完成 1"
    assert lines[2] == "✅ a.mkv"


def test_format_round_report_dry_run_tag():
    """dry-run 标注紧跟 icon，全大写可辨识。"""
    from app.telegram.notifier import format_round_report

    text = format_round_report("📤", "ed2k 推送汇总", "读取 1 条", dry_run=True)
    assert text.splitlines()[0].startswith("📤 [DRY-RUN] ed2k 推送汇总 · ")
    # 纯文本：不允许再走 HTML 粗体（统一渲染路径）
    assert "<b>" not in text and "<i>" not in text


def test_format_round_report_no_details():
    """无明细：只有头行 + 汇总行，不产生多余空行。"""
    from app.telegram.notifier import format_round_report

    text = format_round_report("📂", "目录监控扫描", "无新内容")
    assert len(text.splitlines()) == 2


# -------------------- 进度条渲染 -------------------- #
def test_render_progress_bar_basic():
    """0/50/100 三档填充正确。"""
    from app.telegram.notifier import render_progress_bar

    assert render_progress_bar(0, 10) == "░" * 10
    assert render_progress_bar(50, 10) == "█████░░░░░"
    assert render_progress_bar(100, 10) == "█" * 10


def test_render_progress_bar_clamps_out_of_range():
    """负数/超 100 自动截断到边界。"""
    from app.telegram.notifier import render_progress_bar

    assert render_progress_bar(-20, 8) == "░" * 8
    assert render_progress_bar(250, 8) == "█" * 8


def test_render_progress_bar_rounding():
    """四舍五入取整（46.5% 宽 20 → 9 格）。"""
    from app.telegram.notifier import render_progress_bar

    assert render_progress_bar(46.5, 20) == "█" * 9 + "░" * 11
