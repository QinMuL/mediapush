"""service_base 单测：循环骨架 / 退避状态机 / admin 进度消息。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.core.service_base import (
    AdminProgressMessages,
    FailureTracker,
    PollingService,
    retry_backoff_seconds,
)


# ---------------------------------------------------------------------- #
# retry_backoff_seconds（从 media.service 收敛而来，行为不变）
# ---------------------------------------------------------------------- #
def test_retry_backoff_values():
    assert retry_backoff_seconds(0) == 0
    assert retry_backoff_seconds(1) == 3600
    assert retry_backoff_seconds(2) == 7200
    assert retry_backoff_seconds(8) == 24 * 3600
    assert retry_backoff_seconds(50) == 24 * 3600  # 封顶


# ---------------------------------------------------------------------- #
# FailureTracker
# ---------------------------------------------------------------------- #
def test_tracker_due_first_try():
    t = FailureTracker(stuck_days=7.0)
    assert t.due("k", now=100.0) is True  # 无状态 = 首轮即试


def test_tracker_record_and_backoff():
    t = FailureTracker(stuck_days=7.0)
    now = 1000.0
    is_stuck = t.record("k", now)
    assert is_stuck is False
    st = t.get("k")
    assert st["failures"] == 1
    assert st["next_retry"] == now + 3600
    assert st["first_seen"] == now
    # 未到期：不可重试
    assert t.due("k", now + 1800) is False
    # 到期：可重试
    assert t.due("k", now + 3601) is True


def test_tracker_stuck_after_threshold():
    t = FailureTracker(stuck_days=1.0)
    old = 1000.0
    t.record("k", old)
    # 2 天后再失败：超 stuck_days → stuck
    assert t.record("k", old + 2 * 86400) is True


def test_tracker_extra_fields_preserved():
    t = FailureTracker(stuck_days=7.0)
    now = 100.0
    t.register("k", now, name="f1.mkv", size_bytes=123)
    st = t.get("k")
    assert st["failures"] == 0 and st["name"] == "f1.mkv"
    # 失败计数后附加字段保留
    t.record("k", now + 1)
    st = t.get("k")
    assert st["failures"] == 1
    assert st["name"] == "f1.mkv" and st["size_bytes"] == 123


def test_tracker_register_existing_updates_extra():
    t = FailureTracker()
    t.register("k", 1.0, name="a")
    t.register("k", 2.0, name="b")
    assert t.get("k")["name"] == "b"
    assert t.get("k")["first_seen"] == 1.0  # 首见时间不被覆盖


def test_tracker_clear_and_len():
    t = FailureTracker()
    t.register("a", 1.0)
    t.register("b", 1.0)
    assert len(t) == 2 and "a" in t
    t.clear("a")
    assert "a" not in t and len(t) == 1


def test_tracker_load_dump_format_compatible():
    """持久化格式与线上 state.db 条目一致（failures/next_retry/first_seen + 附加键）。"""
    t = FailureTracker()
    t.load({
        "_offset": 42,  # 元键过滤
        "url1": {"failures": 1, "next_retry": 99.0, "first_seen": 1.0, "name": "x"},
        "bad": "not-a-dict",  # 非 dict 条目过滤
    })
    assert len(t) == 1
    out = t.dump()
    assert out == {"url1": {"failures": 1, "next_retry": 99.0,
                            "first_seen": 1.0, "name": "x"}}


def test_tracker_oldest_first_seen():
    t = FailureTracker()
    assert t.oldest_first_seen() == 0.0
    t.register("a", 100.0)
    t.register("b", 50.0)
    assert t.oldest_first_seen() == 50.0


# ---------------------------------------------------------------------- #
# AdminProgressMessages
# ---------------------------------------------------------------------- #
class _FakeBot:
    def __init__(self, fail_send=False):
        self.sent: list = []
        self.edits: list = []
        self._fail_send = fail_send
        self._id = 0

    async def send_message(self, chat_id, text):
        if self._fail_send:
            raise RuntimeError("send failed")
        self._id += 1
        self.sent.append((chat_id, text))
        from types import SimpleNamespace
        return SimpleNamespace(message_id=self._id)

    async def edit_message_text(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))


def _mk_progress(bot, admins=(7,), throttle=0.0):
    return AdminProgressMessages(
        lambda: bot, lambda: admins, throttle=throttle
    )


def test_progress_send_and_edit():
    bot = _FakeBot()
    p = _mk_progress(bot)
    assert p.active is False
    assert asyncio.run(p.send("t0")) is True
    assert p.active and len(bot.sent) == 1
    asyncio.run(p.edit("t1"))
    assert len(bot.edits) == 1
    assert bot.edits[0] == (7, 1, "t1")


def test_progress_same_text_skipped():
    bot = _FakeBot()
    p = _mk_progress(bot)
    asyncio.run(p.send("t0"))
    asyncio.run(p.edit("t0"))  # 同文 → 不编辑
    assert bot.edits == []


def test_progress_send_all_failed_no_retry():
    """首条全失败 → 本周期不再重试 send。"""
    bot = _FakeBot(fail_send=True)
    p = _mk_progress(bot)
    assert asyncio.run(p.send("t0")) is False
    assert asyncio.run(p.send("t0")) is False  # 不再重试
    # reset 后新周期可重试
    p.reset()
    assert asyncio.run(p.send("t0")) is False  # bot 仍失败，但确实又试了一次
    assert p.active is False


def test_progress_edit_throttled():
    bot = _FakeBot()
    p = _mk_progress(bot, throttle=60.0)
    asyncio.run(p.send("t0"))
    asyncio.run(p.edit("t1"))
    assert bot.edits == []  # 60s 节流窗口内
    p.throttle = 0.0
    asyncio.run(p.edit("t1"))
    assert len(bot.edits) == 1


def test_progress_finalize_bypasses_throttle_and_resets():
    """finalize 绕过节流直达终态；复位后下轮可重新 send。"""
    bot = _FakeBot()
    p = _mk_progress(bot, throttle=60.0)
    asyncio.run(p.send("t0"))
    assert asyncio.run(p.finalize("final")) is True
    assert bot.edits[-1][2] == "final"
    assert p.active is False
    # 无活跃消息时 finalize 返回 False（不编辑）
    assert asyncio.run(p.finalize("x")) is False
    assert bot.edits[-1][2] == "final"  # 未新增编辑
    # 复位后可重新 send
    asyncio.run(p.send("t2"))
    assert len(bot.sent) == 2


# ---------------------------------------------------------------------- #
# PollingService 循环骨架
# ---------------------------------------------------------------------- #
@dataclass
class _Report:
    events: bool = True

    def summary(self) -> str:
        return "汇总"

    def has_events(self) -> bool:
        return self.events


class _Counting(PollingService):
    name = "counting"
    log_prefix = "计数服务"

    def __init__(self, interval=0.01, delay=0.0, fail=False, events=True):
        self.interval = interval
        self.startup_delay = delay
        self._fail = fail
        self._events = events
        self.rounds = 0
        self.before_calls = 0
        self.after_calls: list = []
        self.stopped = False

    async def run_once(self):
        self.rounds += 1
        if self._fail and self.rounds == 1:
            raise RuntimeError("boom")
        return _Report(events=self._events)

    async def before_round(self) -> None:
        self.before_calls += 1

    async def after_round(self, report) -> None:
        self.after_calls.append(report)

    def on_stopped(self) -> None:
        self.stopped = True


def test_loop_runs_rounds_with_hooks():
    svc = _Counting()
    asyncio.run(_run_for(svc, 0.08))
    assert svc.rounds >= 2
    assert svc.before_calls == svc.rounds
    assert len(svc.after_calls) == svc.rounds
    assert svc._last_report == "汇总"


def test_loop_swallows_round_exception():
    svc = _Counting(fail=True)
    asyncio.run(_run_for(svc, 0.05))
    assert svc.rounds >= 2  # 首轮异常后继续跑


def test_loop_startup_delay():
    svc = _Counting(delay=5.0)
    asyncio.run(_run_for(svc, 0.03))
    assert svc.rounds == 0  # 启动延迟未到，一轮都没跑


def test_stop_is_idempotent_and_persists():
    svc = _Counting()
    asyncio.run(svc.start())
    asyncio.run(svc.stop())
    assert svc.stopped is True
    asyncio.run(svc.stop())  # 幂等
    assert svc.running is False


async def _run_for(svc: PollingService, seconds: float) -> None:
    """观察窗：启动 svc 跑若干轮后停止。"""
    await svc.start()
    await asyncio.sleep(seconds)
    await svc.stop()
