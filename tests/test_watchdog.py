"""watchdog 测试：指数退避（不跳过）、永久故障冷却、stall 告警、bot 存活判定。"""
import tempfile
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import repository
from app.db.base import Base
from app.scheduler.watchdog import Watchdog


class FakeTelegram:
    def __init__(self, running=False):
        self._running = running

    async def is_running(self):
        return self._running


class FakeContainer:
    """session_factory 用真实文件 sqlite（_should_bot_be_running 读 tg_bot_token）。"""

    def __init__(self, factory, token="1:fake"):
        self.session_factory = factory
        self.telegram = None  # bot 未运行
        self._rebuild_exc = None  # 重建抛出的异常
        self._rebuild_ok = True
        self.rebuild_calls = 0

    async def rebuild_telegram(self, raise_on_error=False):
        self.rebuild_calls += 1
        if self._rebuild_exc is not None:
            raise self._rebuild_exc
        return self._rebuild_ok


@pytest.fixture
async def factory():
    db_path = tempfile.mktemp(suffix=".db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with f() as s:
        await repository.ensure_default_config(s)
    yield f
    await engine.dispose()


async def _set_token(factory, token):
    async with factory() as s:
        await repository.set_config(s, "tg_bot_token", token)


def _make(factory, **kw):
    cont = FakeContainer(factory)
    for k, v in kw.items():
        setattr(cont, k, v)
    return Watchdog(cont), cont


# ---- 瞬时故障：指数退避，绝不跳过 ----
async def test_transient_failure_sets_backoff(factory):
    wd, cont = _make(factory)
    await _set_token(factory, "1:fake")
    cont._rebuild_exc = RuntimeError("network")
    await wd.check(None, interval_minutes=5)
    assert wd.rebuild_failures == 1
    assert wd.cooldown_until is not None
    # 第一次退避 30s
    delta = (wd.cooldown_until - datetime.now()).total_seconds()
    assert 25 <= delta <= 31


async def test_backoff_grows_exponentially(factory):
    wd, cont = _make(factory)
    await _set_token(factory, "1:fake")
    cont._rebuild_exc = RuntimeError("network")
    backoffs = []
    for _ in range(4):
        # 重置 cooldown 以便下一次能重试
        wd._cooldown_until = None
        await wd.check(None, interval_minutes=5)
        backoffs.append((wd.cooldown_until - datetime.now()).total_seconds())
    # 30, 60, 120, 240
    assert backoffs[0] < backoffs[1] < backoffs[2] < backoffs[3]
    assert 25 <= backoffs[0] <= 31
    assert 115 <= backoffs[2] <= 121


async def test_backoff_clamped_to_max(factory):
    wd, cont = _make(factory)
    await _set_token(factory, "1:fake")
    cont._rebuild_exc = RuntimeError("network")
    wd._rebuild_failures = 10  # 已失败很多次
    wd._cooldown_until = None
    await wd.check(None, interval_minutes=5)
    delta = (wd.cooldown_until - datetime.now()).total_seconds()
    assert delta <= 1800  # 不超过 30 分钟


# ---- 永久性故障：10 分钟冷却，不重试 ----
async def test_permanent_failure_cooldown_no_retry(factory):
    wd, cont = _make(factory)
    await _set_token(factory, "1:fake")
    cont._rebuild_exc = ImportError("ptb broken")
    await wd.check(None, interval_minutes=5)
    assert wd.cooldown_until is not None
    delta = (wd.cooldown_until - datetime.now()).total_seconds()
    assert 590 <= delta <= 601  # ~10 分钟
    # 永久故障不累计 _rebuild_failures（语义不同）
    assert wd.rebuild_failures == 0


async def test_permanent_failure_attribute_error(factory):
    wd, cont = _make(factory)
    await _set_token(factory, "1:fake")
    cont._rebuild_exc = AttributeError("missing attr")
    await wd.check(None, interval_minutes=5)
    assert wd.cooldown_until is not None


# ---- 成功重建重置 ----
async def test_successful_rebuild_resets(factory):
    wd, cont = _make(factory)
    await _set_token(factory, "1:fake")
    cont._rebuild_ok = True
    cont._rebuild_exc = None
    wd._rebuild_failures = 3
    wd._cooldown_until = None
    await wd.check(None, interval_minutes=5)
    assert wd.rebuild_failures == 0
    assert wd.cooldown_until is None


# ---- 冷却期不重试（无"网络正常即重建"分支）----
async def test_cooldown_prevents_retry(factory):
    wd, cont = _make(factory)
    await _set_token(factory, "1:fake")
    wd._cooldown_until = datetime.now() + timedelta(minutes=5)  # 冷却中
    cont._rebuild_ok = True
    await wd.check(None, interval_minutes=5)
    assert cont.rebuild_calls == 0  # 冷却期不调用 rebuild


# ---- 未配置 token 不重建 ----
async def test_no_rebuild_when_no_token(factory):
    wd, cont = _make(factory)
    await _set_token(factory, "")  # 无 token
    await wd.check(None, interval_minutes=5)
    assert cont.rebuild_calls == 0


# ---- bot 正常运行不重建 ----
async def test_no_rebuild_when_bot_running(factory):
    wd, cont = _make(factory)
    await _set_token(factory, "1:fake")
    cont.telegram = FakeTelegram(running=True)
    await wd.check(None, interval_minutes=5)
    assert cont.rebuild_calls == 0


# ---- pipeline 停滞告警 ----
async def test_stall_warning_when_stale(factory, caplog):
    wd, _ = _make(factory)
    stale = datetime.now() - timedelta(minutes=100)
    import logging

    with caplog.at_level(logging.WARNING):
        wd._check_pipeline_stall(stale, interval_minutes=5)
    assert any("未执行" in r.message for r in caplog.records)


async def test_no_stall_warning_when_fresh(factory, caplog):
    wd, _ = _make(factory)
    fresh = datetime.now() - timedelta(minutes=2)
    import logging

    with caplog.at_level(logging.WARNING):
        wd._check_pipeline_stall(fresh, interval_minutes=5)
    assert not any("未执行" in r.message for r in caplog.records)


async def test_stall_threshold_uses_max_interval3_30(factory, caplog):
    """阈值 = max(interval*3, 30)。interval=5 → 30；31 分钟前应告警。"""
    import logging

    wd, _ = _make(factory)
    stale = datetime.now() - timedelta(minutes=31)
    with caplog.at_level(logging.WARNING):
        wd._check_pipeline_stall(stale, interval_minutes=5)
    assert any("未执行" in r.message for r in caplog.records)


async def test_stall_threshold_small_interval_clamps_30(factory, caplog):
    """interval=1 → max(3,30)=30；20 分钟前不应告警（<30）。"""
    import logging

    wd, _ = _make(factory)
    recent = datetime.now() - timedelta(minutes=20)
    with caplog.at_level(logging.WARNING):
        wd._check_pipeline_stall(recent, interval_minutes=1)
    assert not any("未执行" in r.message for r in caplog.records)


async def test_no_stall_when_never_executed(factory, caplog):
    """last_pipeline_execution=None 时不告警（尚未执行过）。"""
    import logging

    wd, _ = _make(factory)
    with caplog.at_level(logging.WARNING):
        wd._check_pipeline_stall(None, interval_minutes=5)
    assert not any("未执行" in r.message for r in caplog.records)
