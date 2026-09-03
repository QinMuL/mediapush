"""_error_handler 异常分类与降噪（网络稳定性方案的一部分）。"""

from __future__ import annotations

import asyncio
import logging

import pytest
from telegram.error import Conflict, NetworkError

from app.telegram.handlers import basic

_LOG = "app.telegram.handlers.basic"


class _Ctx:
    """最小 context stub：只带 error。"""

    def __init__(self, err: Exception) -> None:
        self.error = err


def _reset_state():
    basic._net_warn.clear()


@pytest.fixture(autouse=True)
def _clean():
    _reset_state()
    yield
    _reset_state()


def test_conflict_downgraded_to_warning(caplog):
    """Conflict(409) 降级 WARN，并给出自愈指引（不按 ERROR 打堆栈）。"""
    with caplog.at_level(logging.WARNING, logger=_LOG):
        asyncio.run(basic._error_handler(None, _Ctx(Conflict("terminated by other getUpdates"))))
    assert any("getUpdates 会话冲突" in r.message for r in caplog.records)
    assert not any(r.levelno == logging.ERROR for r in caplog.records)


def test_network_error_first_logs_detail(caplog):
    """网络异常首条打详情。"""
    with caplog.at_level(logging.WARNING, logger=_LOG):
        asyncio.run(basic._error_handler(None, _Ctx(NetworkError("httpx.ConnectError"))))
    assert any("TG 网络异常" in r.message for r in caplog.records)


def test_network_error_storm_suppressed_then_summary(caplog, monkeypatch):
    """风暴降噪：60s 窗口内第 2..N 条静默计数；下个窗口首条打风暴汇总。"""
    import time as _time

    t0 = _time.monotonic()
    monkeypatch.setattr(_time, "monotonic", lambda: t0)  # 冻结时间 → 同一窗口
    with caplog.at_level(logging.WARNING, logger=_LOG):
        for _ in range(10):
            asyncio.run(basic._error_handler(None, _Ctx(NetworkError("httpx.ReadTimeout"))))
    warns = [r for r in caplog.records if "TG 网络异常" in r.message]
    assert len(warns) == 1  # 窗口内只打 1 次详情
    assert basic._net_warn["NetworkError"][1] == 10  # 计数累计

    # 时间前进 61s → 新窗口，先打上一窗口风暴汇总
    monkeypatch.setattr(_time, "monotonic", lambda: t0 + 61)
    with caplog.at_level(logging.WARNING, logger=_LOG):
        asyncio.run(basic._error_handler(None, _Ctx(NetworkError("httpx.ReadTimeout"))))
    assert any("风暴已恢复" in r.message and "×10" in r.message for r in caplog.records)


def test_proxy_failure_hint(caplog):
    """代理不可达（refused）时日志给出代理排查提示。"""
    with caplog.at_level(logging.WARNING, logger=_LOG):
        asyncio.run(
            basic._error_handler(None, _Ctx(NetworkError("[Errno 111] Connection refused")))
        )
    assert any("疑似代理不可达" in r.getMessage() for r in caplog.records)


def test_other_error_keeps_error_level(caplog):
    """非网络异常仍按 ERROR + 堆栈记录。"""
    with caplog.at_level(logging.ERROR, logger=_LOG):
        asyncio.run(basic._error_handler(None, _Ctx(RuntimeError("boom"))))
    assert any(r.levelno == logging.ERROR for r in caplog.records)
