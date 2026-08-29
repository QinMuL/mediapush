"""日志配置测试。"""

import logging
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.logging_config import (
    _ConsoleFormatter,
    _SanitizeFilter,
    make_trace_id,
    set_console_level,
    setup_logging,
    trace_id,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")


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
    """root 恒 DEBUG（由 handler 过滤）+ 控制台级别生效 + 噪声库降级。"""
    setup_logging("INFO", use_color=False)
    assert logging.getLogger().level == logging.DEBUG  # root 放开，handler 各自过滤
    console = logging.getLogger().handlers[0]
    assert console.level == logging.INFO
    assert logging.getLogger("telegram").level == logging.WARNING
    assert logging.getLogger("httpx").level == logging.WARNING
    # 恢复
    setup_logging("INFO", use_color=False)


def test_file_debug_console_info_split(tmp_path, capsys):
    """分级：控制台 INFO（DEBUG 不进），文件 DEBUG（全量）。"""
    log_file = tmp_path / "logs" / "app.log"
    setup_logging("INFO", use_color=False, log_file=str(log_file))
    logging.getLogger("test.split").debug("debug-only-detail")
    logging.getLogger("test.split").info("info-story")
    for h in logging.getLogger().handlers:
        h.flush()
    # 控制台：只有 INFO
    captured = capsys.readouterr()
    assert "debug-only-detail" not in captured.out
    assert "info-story" in captured.out
    # 文件：DEBUG + INFO 全量
    content = log_file.read_text(encoding="utf-8")
    assert "debug-only-detail" in content
    assert "info-story" in content
    # 恢复
    setup_logging("INFO", use_color=False)


def test_setup_logging_replaces_handlers():
    """重复调用不叠加 handler。"""
    setup_logging("INFO", use_color=False)
    n1 = len(logging.getLogger().handlers)
    setup_logging("INFO", use_color=False)
    n2 = len(logging.getLogger().handlers)
    assert n1 == n2 == 1


def test_formatter_uses_china_timezone():
    """时间戳为中国时区（Asia/Shanghai），与容器 TZ 无关。"""
    fmt = _ConsoleFormatter(use_color=False)
    rec = _make_record("app", logging.INFO, "x")
    out = fmt.format(rec)
    ts_str = " ".join(out.split(" ")[:2])  # "2026-07-28 13:06:15"
    expected = datetime.fromtimestamp(rec.created, tz=_SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    assert ts_str == expected


def test_setup_logging_writes_local_file(tmp_path):
    """log_file 启用后，日志写入本地文件，纯文本无 ANSI 颜色。"""
    log_file = tmp_path / "logs" / "app.log"
    setup_logging("INFO", use_color=False, log_file=str(log_file))
    logging.getLogger("test.file").info("hello-local-file")
    # flush 所有 handler
    for h in logging.getLogger().handlers:
        h.flush()
    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    assert "hello-local-file" in content
    assert "\x1b[" not in content  # 文件不含 ANSI 颜色码
    # 恢复
    setup_logging("INFO", use_color=False)


def test_setup_logging_no_file_when_disabled(tmp_path):
    """log_file=None 时只 stdout，不创建文件。"""
    setup_logging("INFO", use_color=False, log_file=None)
    assert len(logging.getLogger().handlers) == 1  # 仅 stdout


# ---------------------------------------------------------------------- #
# trace_id：处理链路上下文注入
# ---------------------------------------------------------------------- #
def test_trace_id_injected_and_restored():
    """with trace_id 内日志带 [tid=xxx]，退出后恢复无 tid。"""
    fmt = _ConsoleFormatter(use_color=False)
    out = fmt.format(_make_record("app", logging.INFO, "in"))
    assert "[tid=" not in out  # 外层无 tid
    with trace_id("abc12345"):
        out = fmt.format(_make_record("app", logging.INFO, "in"))
        assert "[tid=abc12345]" in out
    out = fmt.format(_make_record("app", logging.INFO, "out"))
    assert "[tid=" not in out  # 退出恢复


def test_trace_id_nested_restores_outer():
    """嵌套时退出内层恢复外层 tid（防串扰）。"""
    fmt = _ConsoleFormatter(use_color=False)
    with trace_id("outer"):
        with trace_id("inner"):
            assert "[tid=inner]" in fmt.format(_make_record("app", logging.INFO, "x"))
        assert "[tid=outer]" in fmt.format(_make_record("app", logging.INFO, "x"))


def test_make_trace_id_uses_share_code_prefix():
    """115 链接：tid = 分享码前 8 位（可从链接直接对上日志）。"""
    parsed = SimpleNamespace(provider="115", code="abc12345678")
    assert make_trace_id(parsed) == "abc12345"


def test_make_trace_id_uses_ed2k_hash_prefix():
    """ed2k 链接：tid = 文件 hash 前 8 位（e 前缀区分）。"""
    link = "ed2k://|file|片名.mkv|123|AABBCCDDEEFF00112233|/"
    parsed = SimpleNamespace(provider="ed2k", code=link)
    assert make_trace_id(parsed) == "eAABBCCDD"
    # 兜底：畸形 ed2k
    assert make_trace_id(SimpleNamespace(provider="ed2k", code="ed2k://|file")) == "ed2k"
    # 兜底：空码
    assert make_trace_id(SimpleNamespace(provider="115", code="")) == "share"


def test_trace_id_flows_to_file_and_console(tmp_path, capsys):
    """tid 经 setup_logging 的两个通道均可见。"""
    log_file = tmp_path / "logs" / "app.log"
    setup_logging("INFO", use_color=False, log_file=str(log_file))
    with trace_id("feed1234"):
        logging.getLogger("test.tid").info("story-with-tid")
    for h in logging.getLogger().handlers:
        h.flush()
    captured = capsys.readouterr()
    assert "[tid=feed1234]" in captured.out
    content = log_file.read_text(encoding="utf-8")
    assert "[tid=feed1234]" in content
    # 恢复
    setup_logging("INFO", use_color=False)


# ---------------------------------------------------------------------- #
# 脱敏：敏感值不落日志
# ---------------------------------------------------------------------- #
def test_sanitize_filter_masks_password():
    """password=xxx / 访问码：xxx / cookie 打码后再进日志。"""
    f = _SanitizeFilter()
    rec = _make_record("app", logging.INFO, "链接 https://115.com/s/abc?password=Secret1")
    f.filter(rec)
    assert "Secret1" not in rec.getMessage()
    assert "password=***" in rec.getMessage()

    rec = _make_record("app", logging.INFO, "访问码：abcd1234")
    f.filter(rec)
    assert "abcd1234" not in rec.getMessage()
    assert "访问码：***" in rec.getMessage()

    rec = _make_record("app", logging.INFO, "cookie: UID=1;CID=2;")
    f.filter(rec)
    assert "UID=1" not in rec.getMessage()
    assert "cookie: ***" in rec.getMessage()


def test_sanitize_filter_keeps_normal_messages():
    """普通消息（含"缺访问码"等无值表述）不受影响。"""
    f = _SanitizeFilter()
    for text in ("巡检存活但缺访问码", "已注册菜单", "收到链接：115（user=42）"):
        rec = _make_record("app", logging.INFO, text)
        assert f.filter(rec) is True
        assert rec.getMessage() == text
        assert rec.args == ()


def test_sanitize_filter_supports_lazy_args():
    """% 风格 args 延迟格式化也能脱敏。"""
    f = _SanitizeFilter()
    rec = logging.LogRecord(
        name="app", level=logging.INFO, pathname="", lineno=0,
        msg="读取失败 password=%s", args=("TopSecret",), exc_info=None,
    )
    f.filter(rec)
    assert "TopSecret" not in rec.getMessage()
    assert "password=***" in rec.getMessage()


# ---------------------------------------------------------------------- #
# 运行时调级（/loglevel）
# ---------------------------------------------------------------------- #
def test_set_console_level_adjusts_stdout_only(tmp_path):
    """只调 stdout handler，文件 handler 恒 DEBUG。"""
    log_file = tmp_path / "logs" / "app.log"
    setup_logging("INFO", use_color=False, log_file=str(log_file))
    assert set_console_level("DEBUG") is True
    console, file_h = logging.getLogger().handlers
    assert console.level == logging.DEBUG
    assert file_h.level == logging.DEBUG  # 文件不受影响（本来恒 DEBUG）
    set_console_level("WARNING")
    assert logging.getLogger().handlers[0].level == logging.WARNING
    # 恢复
    setup_logging("INFO", use_color=False)


def test_set_console_level_rejects_invalid():
    """无效级别返回 False 且不抛异常。"""
    setup_logging("INFO", use_color=False)
    assert set_console_level("NOT_A_LEVEL") is False
    assert logging.getLogger().handlers[0].level == logging.INFO  # 未被改动
    setup_logging("INFO", use_color=False)

