"""bot.py 心跳自愈探活测试（模块级 _heartbeat_loop）。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.telegram.bot import _heartbeat_loop


def _make_app(*, polling_task=None, polling_running=False, get_me_exc=None):
    """构造 mock app：updater 暴露 name-mangled 私有 polling task 属性。"""
    app = MagicMock()
    app.updater = MagicMock()
    app.updater.running = polling_running
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
