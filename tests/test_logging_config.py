"""日志配置测试。"""

import logging

from app.logging_config import _ConsoleFormatter, setup_logging


def _make_record(name: str, level: int, msg: str) -> logging.LogRecord:
    return logging.LogRecord(
        name=name, level=level, pathname="", lineno=0,
        msg=msg, args=(), exc_info=None,
    )


def test_formatter_shortens_module_name():
    """模块名取末段：app.telegram.handlers → handlers。"""
    fmt = _ConsoleFormatter(use_color=False)
    rec = _make_record("app.telegram.handlers", logging.INFO, "已注册菜单")
    out = fmt.format(rec)
    assert "[handlers]" in out
    assert "已注册菜单" in out
    assert "app.telegram" not in out  # 不出现完整模块路径


def test_formatter_no_color_when_disabled():
    """use_color=False 时不含 ANSI 转义码。"""
    fmt = _ConsoleFormatter(use_color=False)
    out = fmt.format(_make_record("app", logging.ERROR, "boom"))
    assert "\x1b[" not in out
    assert "ERROR" in out


def test_formatter_has_color_when_enabled():
    """use_color=True 时级别带 ANSI 颜色码。"""
    fmt = _ConsoleFormatter(use_color=True)
    out = fmt.format(_make_record("app", logging.INFO, "hi"))
    assert "\x1b[32m" in out  # green
    assert "\x1b[0m" in out  # reset


def test_setup_logging_sets_root_level():
    """setup_logging 设置根级别 + 噪声库降级。"""
    setup_logging("DEBUG", use_color=False)
    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger("telegram").level == logging.WARNING
    assert logging.getLogger("httpx").level == logging.WARNING
    # 恢复
    setup_logging("INFO", use_color=False)


def test_setup_logging_replaces_handlers():
    """重复调用不叠加 handler。"""
    setup_logging("INFO", use_color=False)
    n1 = len(logging.getLogger().handlers)
    setup_logging("INFO", use_color=False)
    n2 = len(logging.getLogger().handlers)
    assert n1 == n2 == 1
