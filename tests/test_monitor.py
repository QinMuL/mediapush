"""RateLimitMonitor 测试：扫描命中、冷却告警、告警失败不设冷却。

monkeypatch app.core.monitor.memory_handler 注入伪造缓冲，避免污染全局日志。
"""
from app.core import monitor
from app.core.monitor import RateLimitMonitor


class FakeHandler:
    """伪造内存日志缓冲。"""

    def __init__(self, lines: list[str]):
        self._lines = lines

    def recent(self, n: int = 200) -> list[str]:
        return self._lines[-n:] if n > 0 else list(self._lines)


class FakeContainer:
    """伪造容器，记录 send_alert 调用。"""

    def __init__(self, alert_ok: bool = True):
        self._alert_ok = alert_ok
        self.alerts: list[str] = []

    async def send_alert(self, text: str) -> bool:
        self.alerts.append(text)
        return self._alert_ok


def _make(alert_ok: bool = True):
    return RateLimitMonitor(FakeContainer(alert_ok=alert_ok))


# ---- scan（纯函数，不依赖全局缓冲）----
def test_scan_matches_keywords():
    m = _make()
    lines = [
        "2026-07-23 INFO 正常日志",
        "2026-07-23 WARNING 115 rate limit 触发",
        "2026-07-23 ERROR 请求过于频繁",
        "2026-07-23 INFO 另一条正常",
    ]
    matched = m.scan(lines)
    assert len(matched) == 2
    assert "rate limit" in matched[0]
    assert "请求过于频繁" in matched[1]


def test_scan_case_insensitive():
    m = _make()
    lines = ["WARNING Too Many Requests", "INFO ok"]
    assert len(m.scan(lines)) == 1


def test_scan_empty_lines():
    assert _make().scan([]) == []


# ---- check：无命中 ----
async def test_check_no_match_no_alert(monkeypatch):
    monkeypatch.setattr(monitor, "memory_handler", FakeHandler(["INFO 正常", "DEBUG ok"]))
    m = _make()
    count = await m.check()
    assert count == 0
    assert m._container.alerts == []  # noqa: SLF001


# ---- check：命中且告警成功 ----
async def test_check_match_sends_alert(monkeypatch):
    monkeypatch.setattr(
        monitor, "memory_handler",
        FakeHandler(["WARNING 115 风控", "ERROR frequent 请求"]),
    )
    m = _make(alert_ok=True)
    count = await m.check()
    assert count == 2
    assert len(m._container.alerts) == 1  # noqa: SLF001
    assert "115 速率限制告警" in m._container.alerts[0]  # noqa: SLF001
    assert m._last_alert_at is not None  # noqa: SLF001  冷却已记录


# ---- check：冷却期内不重复告警 ----
async def test_check_cooldown_no_repeat(monkeypatch):
    monkeypatch.setattr(
        monitor, "memory_handler", FakeHandler(["WARNING rate limit"]),
    )
    m = _make(alert_ok=True)
    first = await m.check()
    assert first == 1
    assert len(m._container.alerts) == 1  # noqa: SLF001
    # 同一缓冲再次 check，仍在冷却期
    second = await m.check()
    assert second == 1  # 仍返回命中数
    assert len(m._container.alerts) == 1  # noqa: SLF001  未重复告警


# ---- check：告警失败不设冷却（下次仍尝试）----
async def test_check_alert_fail_no_cooldown_set(monkeypatch):
    monkeypatch.setattr(
        monitor, "memory_handler", FakeHandler(["WARNING rate limit"]),
    )
    m = _make(alert_ok=False)  # send_alert 返回 False（bot 未运行等）
    first = await m.check()
    assert first == 1
    assert m._last_alert_at is None  # noqa: SLF001  失败不记录冷却
    # 下次仍会尝试告警
    second = await m.check()
    assert second == 1
    assert len(m._container.alerts) == 2  # noqa: SLF001  再次尝试


# ---- reset_cooldown ----
async def test_reset_cooldown_allows_repeat(monkeypatch):
    monkeypatch.setattr(
        monitor, "memory_handler", FakeHandler(["WARNING rate limit"]),
    )
    m = _make(alert_ok=True)
    await m.check()
    assert m._last_alert_at is not None  # noqa: SLF001
    m.reset_cooldown()
    assert m._last_alert_at is None  # noqa: SLF001
    await m.check()
    assert len(m._container.alerts) == 2  # noqa: SLF001  冷却重置后再次告警
