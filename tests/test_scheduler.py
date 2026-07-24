"""scheduler 测试：联动自适应、full_scan、_last_pipeline_execution、任务注册。

用 FakeScheduler 避免 AsyncIOScheduler 真实调度；FakeContainer 提供真实 session_factory
（文件 sqlite）以验证配置持久化，其余方法 mock。
"""
import tempfile

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import repository
from app.db.base import Base
from app.scheduler.scheduler import (
    HEALTH_JOB_ID,
    MONITOR_JOB_ID,
    PIPELINE_JOB_ID,
    WATCHDOG_JOB_ID,
    SchedulerService,
)


class FakeScheduler:
    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.rescheduled: list[tuple[str, dict]] = []
        self._running = False

    def add_job(self, func, trigger, **kw):
        self.jobs[kw["id"]] = {"trigger": trigger, **kw}

    def reschedule_job(self, job_id, **kw):
        self.rescheduled.append((job_id, kw))

    def start(self):
        self._running = True

    def shutdown(self, **kw):
        self._running = False

    @property
    def running(self):
        return self._running


class FakeTelegram:
    def __init__(self, running=False):
        self._running = running

    async def is_running(self):
        return self._running


class FakeContainer:
    def __init__(self, factory):
        self.session_factory = factory
        self._pipeline_running = False
        self._monitored = [("c1", "p1"), ("c2", "p2")]
        self.run_calls: list[tuple] = []
        self.run_result = {"new": 0, "pushed": 0, "existing": 2}
        self._pan115_ready = True
        self._health_ok = True
        self.telegram = None

    def is_pipeline_running(self):
        return self._pipeline_running

    async def get_monitored_shares(self):
        return self._monitored

    async def run_pipeline_once(self, codes, trigger="manual"):
        self.run_calls.append((list(codes), trigger))
        return self.run_result

    def pan115_ready(self):
        return self._pan115_ready

    async def check_pan115_health(self):
        return self._health_ok

    async def rebuild_telegram(self, raise_on_error=False):
        return False


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


def _make(factory, **kw):
    cont = FakeContainer(factory)
    for k, v in kw.items():
        setattr(cont, k, v)
    sched = FakeScheduler()
    svc = SchedulerService(cont, scheduler=sched)
    return svc, cont, sched


# ---- _last_pipeline_execution 即使跳过也更新（旧项目核心约束）----
async def test_pipeline_tick_updates_last_execution_on_skip(factory):
    svc, cont, _ = _make(factory)
    cont._pipeline_running = True
    await svc._pipeline_tick()
    assert svc.last_pipeline_execution is not None  # 跳过也更新
    assert cont.run_calls == []  # 未实际运行


async def test_pipeline_tick_runs_when_idle(factory):
    svc, cont, _ = _make(factory)
    await svc._pipeline_tick()
    assert svc.last_pipeline_execution is not None
    assert len(cont.run_calls) == 1
    codes, trigger = cont.run_calls[0]
    assert codes == [("c1", "p1"), ("c2", "p2")]
    assert trigger == "scheduler"


async def test_pipeline_tick_no_monitored_skips_run(factory):
    svc, cont, _ = _make(factory)
    cont._monitored = []
    await svc._pipeline_tick()
    assert svc.last_pipeline_execution is not None  # 仍更新
    assert cont.run_calls == []


# ---- full_scan 触发周期 ----
async def test_full_scan_trigger_every_n(factory):
    async with factory() as s:
        await repository.set_config(s, "full_scan_interval_runs", "3")
    svc, cont, _ = _make(factory)
    triggers = []
    for _ in range(4):
        cont.run_calls.clear()
        await svc._pipeline_tick()
        triggers.append(cont.run_calls[0][1])
    # counter%3: 1→scheduler, 2→scheduler, 3→full_scan, 4→scheduler
    assert triggers == ["scheduler", "scheduler", "full_scan", "scheduler"]


async def test_pipeline_tick_exception_does_not_crash(factory):
    svc, cont, _ = _make(factory)

    async def boom(codes, trigger="manual"):
        raise RuntimeError("boom")

    cont.run_pipeline_once = boom
    await svc._pipeline_tick()  # 不应抛
    assert svc.last_pipeline_execution is not None


# ---- 间隔联动自适应 ----
async def test_update_interval_recomputes_and_persists(factory):
    svc, _, sched = _make(factory)
    await svc.start()  # 置 _started=True，注册任务
    full_scan, health = await svc.update_interval(10)
    assert full_scan == 12  # round(120/10)
    assert health == 600  # 10*60, within [180,900]
    async with factory() as s:
        assert await repository.get_config(s, "schedule_interval") == "10"
        assert await repository.get_config(s, "full_scan_interval_runs") == "12"
        assert await repository.get_config(s, "pan115_health_interval") == "600"
    # 两个任务都重排
    rescheduled_ids = [jid for jid, _ in sched.rescheduled]
    assert PIPELINE_JOB_ID in rescheduled_ids
    assert HEALTH_JOB_ID in rescheduled_ids


async def test_update_interval_clamps_health_to_max(factory):
    svc, _, _ = _make(factory)
    _, health = await svc.update_interval(20)
    assert health == 900  # 20*60=1200 clamp 到 900


async def test_update_interval_clamps_health_to_min(factory):
    svc, _, _ = _make(factory)
    _, health = await svc.update_interval(2)
    assert health == 180  # 2*60=120 clamp 到 180


async def test_update_interval_full_scan_floor(factory):
    svc, _, _ = _make(factory)
    full_scan, _ = await svc.update_interval(200)
    assert full_scan == 1  # max(1, round(120/200)=1)


# ---- 启动校验 ----
async def test_validate_interval_warns_below_3(factory, caplog):
    svc, _, _ = _make(factory)
    import logging

    with caplog.at_level(logging.WARNING):
        cfg = {"full_scan_interval_runs": "60", "pan115_health_interval": "120"}
        await svc._validate_interval_on_start(2, cfg)
    assert any("< 3" in r.message for r in caplog.records)


async def test_validate_interval_no_warn_at_5(factory, caplog):
    svc, _, _ = _make(factory)
    import logging

    cfg = await svc._read_config()  # 默认 full_scan=24, health=300
    with caplog.at_level(logging.WARNING):
        await svc._validate_interval_on_start(5, cfg)
    # interval=5 不告警；联动期望 full_scan=24(120/5=24), health=300 → 匹配，无告警
    assert not any("< 3" in r.message for r in caplog.records)


# ---- start 注册任务 ----
async def test_start_registers_all_jobs(factory):
    svc, _, sched = _make(factory)
    await svc.start()
    assert PIPELINE_JOB_ID in sched.jobs
    assert HEALTH_JOB_ID in sched.jobs
    assert WATCHDOG_JOB_ID in sched.jobs
    assert MONITOR_JOB_ID in sched.jobs  # 阶段6：rate-limit 监控任务
    pj = sched.jobs[PIPELINE_JOB_ID]
    assert pj["max_instances"] == 1
    assert pj["coalesce"] is True
    assert pj["misfire_grace_time"] == max(60, 5 * 60)
    # 监控任务每 5 分钟
    mj = sched.jobs[MONITOR_JOB_ID]
    assert mj["trigger"] == "interval"
    assert mj["minutes"] == 5
    assert mj["coalesce"] is True
    assert sched.running is True


async def test_stop_shuts_down(factory):
    svc, _, sched = _make(factory)
    await svc.start()
    assert sched.running is True
    await svc.stop()
    assert sched.running is False


# ---- health tick ----
async def test_health_tick_checks_when_ready(factory):
    svc, cont, _ = _make(factory)
    cont._health_ok = True
    await svc._health_tick()  # 不应抛


async def test_health_tick_skips_when_not_ready(factory):
    svc, cont, _ = _make(factory)
    cont._pan115_ready = False
    called = {"check": False}
    orig = cont.check_pan115_health

    async def fake():
        called["check"] = True
        return True

    cont.check_pan115_health = fake
    await svc._health_tick()
    assert called["check"] is False
    cont.check_pan115_health = orig
