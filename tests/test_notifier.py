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
