"""bot.py 心跳自愈探活 + 入站看门狗测试（模块级函数）。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.telegram.bot import _heartbeat_loop, _inbound_watchdog_tick, _recover_updater


def _make_app(*, polling_task=None, polling_running=False, get_me_exc=None):
    """构造 mock app：updater 暴露 name-mangled 私有 polling task 属性。"""
    app = MagicMock()
    app.updater = MagicMock()
    app.updater.running = polling_running
    # 看门狗恢复动作（旧测试默认可恢复成功，不误杀）
    app.updater.stop = AsyncMock()
    app.updater.start_polling = AsyncMock()
    if polling_task is None:
        # 未启动场景：属性不存在（探活应跳过，不误杀）
        del app.updater._Updater__polling_task
    else:
        app.updater._Updater__polling_task = polling_task
    app.bot.get_me = AsyncMock(side_effect=get_me_exc)
    return app


def _run_for(app, seconds: float = 0.1) -> None:
    """在真实事件循环中跑心跳循环一段时间后取消（monkeypatch 间隔加速）。"""

    async def run() -> None:
        task = asyncio.create_task(_heartbeat_loop(app))
        await asyncio.sleep(seconds)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())


def _patch_fast(monkeypatch):
    """加速心跳：间隔 1ms、每周期探活、拦截 os._exit。"""
    monkeypatch.setattr("app.telegram.bot._HEARTBEAT_INTERVAL", 0.001)
    monkeypatch.setattr("app.telegram.bot._PROBE_EVERY", 1)
    exits: list[int] = []
    monkeypatch.setattr("app.telegram.bot.os._exit", lambda code: exits.append(code))
    return exits


def test_probe_skipped_when_updater_not_started(monkeypatch):
    """updater 未启动（无 polling task 属性）：不误杀。"""
    exits = _patch_fast(monkeypatch)
    app = _make_app(polling_task=None, polling_running=False)
    _run_for(app, 0.05)
    assert not exits


def test_polling_task_dead_triggers_exit(monkeypatch):
    """polling 任务 done 且 running 标志仍 True（静默死亡）→ os._exit(1)。"""

    async def make_dead_task() -> asyncio.Task:
        t = asyncio.get_running_loop().create_future()
        t.set_exception(RuntimeError("unexpected boom"))
        return t

    async def run():
        dead = await make_dead_task()
        app = _make_app(polling_task=dead, polling_running=True)
        task = asyncio.create_task(_heartbeat_loop(app))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    exits = _patch_fast(monkeypatch)
    asyncio.run(run())
    assert 1 in exits  # 已触发强制退出（mock 不终止循环，可能多次）


def test_polling_task_cancelled_triggers_exit(monkeypatch):
    """polling 任务被取消（done + CancelledError）→ os._exit(1)。"""

    async def run():
        t = asyncio.ensure_future(asyncio.sleep(999))
        await asyncio.sleep(0)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        app = _make_app(polling_task=t, polling_running=True)
        task = asyncio.create_task(_heartbeat_loop(app))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    exits = _patch_fast(monkeypatch)
    asyncio.run(run())
    assert 1 in exits  # 已触发强制退出（mock 不终止循环，可能多次）


def test_network_probe_failures_triggers_exit(monkeypatch):
    """get_me 连续失败达上限 → os._exit(1)。"""
    exits = _patch_fast(monkeypatch)
    app = _make_app(
        polling_task=None, polling_running=False,
        get_me_exc=RuntimeError("proxy down"),
    )
    _run_for(app, 0.05)
    assert 1 in exits  # 已触发强制退出（mock 不终止循环，可能多次）


def test_healthy_app_no_exit(monkeypatch):
    """健康 app（polling 活着 + get_me 成功）：不退出。"""
    exits = _patch_fast(monkeypatch)

    async def run():
        live = asyncio.ensure_future(asyncio.sleep(999))
        await asyncio.sleep(0)
        app = _make_app(polling_task=live, polling_running=True)
        task = asyncio.create_task(_heartbeat_loop(app))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        live.cancel()

    asyncio.run(run())
    assert not exits


# ---------------------------------------------------------------------- #
# 入站看门狗（"能发不能收"卡死检测与恢复）
# ---------------------------------------------------------------------- #
class _WebhookInfo:
    def __init__(self, pending: int) -> None:
        self.pending_update_count = pending


def _wd_app(*, running=True, pending=0, start_raises=None):
    """看门狗专用 fake：running 状态 + pending 计数 + 可恢复/可失败的 updater。"""
    app = MagicMock()
    app.updater.running = running
    app.updater.stop = AsyncMock()
    app.updater.start_polling = AsyncMock(side_effect=start_raises)
    app.bot.get_webhook_info = AsyncMock(return_value=_WebhookInfo(pending))
    return app


def test_watchdog_strikes_accumulate_then_recover():
    """网络正常但 pending 持续堆积：3 轮可疑 → 重启 Updater 并复位计数。"""
    app = _wd_app(running=True, pending=2)
    s = asyncio.run(_inbound_watchdog_tick(app, 0))
    assert s == 1
    s = asyncio.run(_inbound_watchdog_tick(app, s))
    assert s == 2
    s = asyncio.run(_inbound_watchdog_tick(app, s))
    assert s == 0  # 触发恢复并复位
    app.updater.stop.assert_awaited_once()
    app.updater.start_polling.assert_awaited_once()
    assert app.updater.start_polling.await_args.kwargs.get("timeout") == 10


def test_watchdog_pending_cleared_resets_strikes():
    """堆积清零（下轮长轮询取走）：计数复位，不触发恢复。"""
    app = _wd_app(running=True, pending=1)
    s = asyncio.run(_inbound_watchdog_tick(app, 0))
    assert s == 1
    app.bot.get_webhook_info = AsyncMock(return_value=_WebhookInfo(0))
    s = asyncio.run(_inbound_watchdog_tick(app, s))
    assert s == 0
    app.updater.stop.assert_not_awaited()


def test_watchdog_probe_failure_keeps_strikes():
    """探针失败（网络抖动）：计数保持不变（既不误清零也不误累积）。"""
    app = _wd_app(running=True, pending=1)
    s = asyncio.run(_inbound_watchdog_tick(app, 0))
    assert s == 1
    app.bot.get_webhook_info = AsyncMock(side_effect=RuntimeError("net"))
    s = asyncio.run(_inbound_watchdog_tick(app, s))
    assert s == 1


def test_watchdog_updater_not_running_triggers_recover():
    """updater.running=False（轮询已停但应用还活着）：同样可疑 → 恢复。"""
    app = _wd_app(running=False)
    asyncio.run(_inbound_watchdog_tick(app, 0))
    asyncio.run(_inbound_watchdog_tick(app, 1))
    s = asyncio.run(_inbound_watchdog_tick(app, 2))
    assert s == 0
    app.updater.start_polling.assert_awaited_once()


def test_watchdog_recover_failure_exits(monkeypatch):
    """恢复失败（start_polling 抛错）→ os._exit(1) 交 Docker 重启。"""
    exits: list[int] = []
    monkeypatch.setattr("app.telegram.bot.os._exit", lambda code: exits.append(code))
    app = _wd_app(running=True, pending=5, start_raises=RuntimeError("boom"))
    asyncio.run(_inbound_watchdog_tick(app, 0))
    asyncio.run(_inbound_watchdog_tick(app, 1))
    asyncio.run(_inbound_watchdog_tick(app, 2))
    assert 1 in exits


def test_recover_updater_stop_failure_still_restarts():
    """stop() 失败也要尝试 start_polling（恢复优先于清理）。"""
    app = _wd_app(running=True)
    app.updater.stop = AsyncMock(side_effect=RuntimeError("stop boom"))
    asyncio.run(_recover_updater(app))
    app.updater.start_polling.assert_awaited_once()


def test_watchdog_via_heartbeat_loop_end_to_end(monkeypatch):
    """端到端：心跳循环内 pending 持续堆积 → 自动重启 Updater，不退出进程。"""
    exits = _patch_fast(monkeypatch)
    app = _make_app(polling_task=None, polling_running=True)
    app.bot.get_webhook_info = AsyncMock(return_value=_WebhookInfo(3))
    _run_for(app, 0.05)
    assert app.updater.start_polling.await_count >= 1
    assert not exits  # 恢复成功，无需进程级重启
